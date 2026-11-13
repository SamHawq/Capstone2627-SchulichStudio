"""Drafting a ranking request from free text, with the Manager agent (llm_agents/manager.py).

The client describes what they want in their own words and/or picks documents from the
store (a course outline, a job description; see documents.py); `draft_request` makes one
Manager call and returns a `RequestDraft`: a complete HiringRequest (a weight for every criterion,
instructions for each ranker, role, top N, notes, course sessions for the Scheduler) plus the
reason for each weight. The client
reviews and edits the draft, then ranks with it as with any request. Nothing is ranked here.

Steps:
  1. The Manager gets the text, the documents' scrubbed text and the criteria (ids, names,
     descriptions, field names, courses). No candidate data.
  2. `validate_choices` checks its output in plain code: unknown or repeated criteria are
     dropped, weights are clamped to 0-MAX_WEIGHT, criteria it left out get weight 0, and
     over-long text is cut. A course-specific criterion weighted for a role that doesn't name
     its course is flagged, since it wouldn't join automatically. Sessions with a bad day or
     time are dropped.

The draft lists every criterion, with weight 0 for those not to use, so auto criteria join
only if the Manager weighted them.
"""

import json
import logging
import math
import re
import time

from .agent_trace import run_agent, trace_step, traced_operation
from .availability import DAYS
from .llm_agents import build_manager
from .planning import applies_to
from .schemas import CourseSession, Criterion, HiringRequest, ManagerOutput, RequestBrief, RequestDraft

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2           # total tries for the Manager call
MAX_WEIGHT = 5             # weights are 0-5, like the UI's sliders
MAX_INSTRUCTIONS = 600     # characters per ranker's instructions
MAX_NOTES = 1000           # characters of notes for every ranker
DEFAULT_TOP_X = 5
MAX_SESSIONS = 30
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _fields_of(criterion: Criterion, kinds: dict[str, str]) -> list[dict[str, str]]:
    """Each field the criterion's ranker sees, with what it holds (the planner's note) and its
    kind ("free text" = answers written in someone's own words), so the Manager can route an
    instruction to the ranker whose fields carry the evidence. Names, notes and kinds only,
    never values."""
    notes = {n.field: n.meaning for n in criterion.field_notes}
    return [{"field": f, "holds": notes.get(f, ""), "kind": kinds.get(f, "")} for f in criterion.relevant_fields]


async def run_manager(
    brief: RequestBrief, criteria: list[Criterion], documents: list[dict[str, str]] | None = None,
    field_kinds: dict[str, str] | None = None,
) -> ManagerOutput:
    """Ask the Manager to read the brief and any documents ({"name", "text"}, already scrubbed).

    Raises:
        RuntimeError: If every attempt fails (API error or output that doesn't fit the schema).
    """
    payload = json.dumps({
        "client_instructions": brief.instructions,
        "role_set_by_client": brief.role,
        "documents": documents or [],
        "criteria": [
            {"id": c.id, "name": c.name, "description": c.description, "fields": _fields_of(c, field_kinds or {}),
             "type": "auto" if c.type == "auto" else "built-in", "courses": c.courses}
            for c in criteria
        ],
    }, indent=2)
    agent = build_manager()
    logger.info("[manager] Drafting a request over %d criteria (model: %s)", len(criteria),
                agent.model or "SDK default")
    logger.debug("[manager] Payload:\n%s", payload)

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        start = time.perf_counter()
        try:
            output = await run_agent(agent, payload, ManagerOutput)
            logger.info("[manager] Done in %.1fs (attempt %d)", time.perf_counter() - start, attempt)
            logger.debug("[manager] Output:\n%s", output.model_dump_json(indent=2))
            return output
        except Exception as e:  # retry on API/parse failures
            last_error = e
            logger.warning("[manager] Attempt %d/%d failed after %.1fs: %s",
                           attempt, MAX_ATTEMPTS, time.perf_counter() - start, e)
    raise RuntimeError(f"The Manager failed after {MAX_ATTEMPTS} attempts: {last_error}")


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _session(s: CourseSession) -> CourseSession | None:
    """The session with its day and times normalized ("Monday", "9:00" -> "mon", "09:00"), or None."""
    day = s.day.strip().lower()[:3]
    times = [_TIME.match(t.strip()) for t in (s.start, s.end)]
    if day not in DAYS or not all(times):
        return None
    start, end = (f"{int(m[1]):02d}:{m[2]}" for m in times)
    if start >= end:
        return None
    return CourseSession(label=_clip(s.label, 80), day=day, start=start, end=end)


def validate_sessions(sessions: list[CourseSession]) -> tuple[list[CourseSession], list[str]]:
    """Normalize the Manager's sessions; drop bad and repeated ones, keep at most MAX_SESSIONS."""
    kept, seen, warnings = [], set(), []
    for s in sessions:
        clean = _session(s)
        if clean is None:
            warnings.append(f"Dropped session {s.label!r} ({s.day} {s.start}-{s.end}): bad day or time.")
        elif (clean.day, clean.start, clean.end, clean.label) not in seen:
            seen.add((clean.day, clean.start, clean.end, clean.label))
            kept.append(clean)
    if len(kept) > MAX_SESSIONS:
        warnings.append(f"Kept the first {MAX_SESSIONS} of {len(kept)} sessions.")
        kept = kept[:MAX_SESSIONS]
    return kept, warnings


def validate_choices(
    output: ManagerOutput, criteria: list[Criterion], brief: RequestBrief
) -> RequestDraft:
    """Check the Manager's output and turn it into a request draft.

    Raises:
        ValueError: If no criterion ends up with a weight above 0 (the text gave nothing to
            rank on), so the client should rephrase or set weights by hand.
    """
    known = {c.id: c for c in criteria}
    warnings: list[str] = []
    weights: dict[str, float] = {}
    instructions: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for choice in output.criteria:
        cid = choice.criterion_id
        if cid not in known:
            warnings.append(f"Manager weighted unknown criterion '{cid}'; ignored.")
            continue
        if cid in weights:
            warnings.append(f"Manager weighted '{cid}' twice; the first is kept.")
            continue
        weight = choice.weight if math.isfinite(choice.weight) else 0.0
        if not 0 <= weight <= MAX_WEIGHT:
            warnings.append(f"Manager's weight {choice.weight} for '{cid}' was clamped to 0-{MAX_WEIGHT}.")
            weight = max(0.0, min(MAX_WEIGHT, weight))
        weights[cid] = round(weight, 2)
        reasons[cid] = _clip(choice.reason, 300)
        if weight > 0 and choice.instructions.strip():
            instructions[cid] = _clip(choice.instructions, MAX_INSTRUCTIONS)
    for cid in known:
        if cid not in weights:
            weights[cid] = 0.0
            reasons[cid] = "Not weighted by the Manager."

    if not any(w > 0 for w in weights.values()):
        raise ValueError("The instructions didn't point to any criterion. Say what matters for the role "
                         "(skills, experience, ...) or set the weights by hand.")

    role = (brief.role.strip() or output.role.strip() or _clip(brief.instructions, 120)
            or ", ".join(brief.documents))
    top_x = brief.top_x or (output.top_x if output.top_x >= 1 else DEFAULT_TOP_X)
    for cid, w in weights.items():
        if w > 0 and not applies_to(known[cid], role):
            warnings.append(f"'{cid}' is specific to {', '.join(known[cid].courses)}, which the role "
                            "doesn't name; check the role or the weight.")

    sessions, session_warnings = validate_sessions(output.sessions)
    warnings += session_warnings
    request = HiringRequest(role=role, criteria=weights, top_x=top_x, notes=_clip(output.notes, MAX_NOTES),
                            client_instructions=brief.instructions,
                            instructions=instructions, sessions=sessions, documents=list(brief.documents))
    logger.info("[manager] Draft: role=%r, top_x=%d, weights=%s, instructions for %s, %d sessions",
                role, top_x, {c: w for c, w in weights.items() if w > 0}, sorted(instructions), len(sessions))
    return RequestDraft(request=request, reasons=reasons,
                        not_covered=[_clip(n, 300) for n in output.not_covered if n.strip()],
                        warnings=warnings)


@traced_operation("draft", lambda brief, *a, **k: brief.role or brief.instructions[:120])
async def draft_request(
    brief: RequestBrief, criteria: list[Criterion], documents: list[dict[str, str]] | None = None,
    document_warnings: list[str] | None = None, field_kinds: dict[str, str] | None = None,
) -> RequestDraft:
    """Turn free-text instructions (and documents) into a request draft (one Manager call).

    Args:
        brief: The client's text, plus an optional role and top N that override the Manager's.
        criteria: Every criterion a request can weight (built-ins with the catalog applied,
            plus auto criteria; see service.list_criteria).
        documents: The brief's documents as {"name", "text"}, scrubbed and size-capped
            (documents.load_context).
        document_warnings: Warnings from loading them, passed through to the draft.

    Raises:
        ValueError: See `validate_choices`.
        RuntimeError: If the Manager call fails.
    """
    output = await run_manager(brief, criteria, documents, field_kinds)
    draft = validate_choices(output, criteria, brief)
    draft.warnings[:0] = document_warnings or []
    req = draft.request
    trace_step("manager", "Draft validated", role=req.role, top_x=req.top_x,
               weights={cid: w for cid, w in req.criteria.items() if w > 0},
               instructions_for=sorted(req.instructions), sessions=len(req.sessions),
               not_covered=len(draft.not_covered), documents=[d["name"] for d in documents or []])
    return draft
