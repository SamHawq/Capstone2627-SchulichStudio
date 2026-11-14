"""Final review: the Manager applies the client's instructions and breaks ties, within bounds.

The weighted score (aggregation.py) is plain arithmetic, so it's reproducible but can't express
preferences that cut across criteria ("hands-on lab work matters more than grades for this
course"), and many candidates end up with the same score. The final review adds that judgement
without giving up consistency:

    weighted ranking (code) -> shortlist (the top SHORTLIST_FACTOR x top_x, at least top_x + 5)
      -> Manager, review task (llm_agents/manager.py)   per candidate: an adjustment with its
                                                        reason, a justification, and a tiebreak
                                                        value from their other information
      -> validate_review                                unknown/repeated refs dropped, adjustments
                                                        clamped to +-MAX_ADJUSTMENT (one without a
                                                        reason is set to 0), tiebreaks to -1..1
      -> apply_review                                   final = weighted + adjustment (0..1);
                                                        sorted; candidates within TIE_MARGIN of
                                                        each other are ordered by tiebreak; cut

Other information = the fields no criterion uses that the Criteria Planner marked "tiebreak"
(motivation, general comments, ...; see planning.tiebreak_fields). Availability,
administrative, protected, identifying and unusable fields are never included. The tiebreak
value only orders candidates whose final scores are tied: it never changes a score, so it
can't lift anyone past a candidate with a better score.

So every final score is within MAX_ADJUSTMENT of a score anyone can recompute, and every change
or tie-break says why. The Manager sees pseudonymous refs, the rankers' scores and reasons, the
criteria, the instructions and the other information: never IDs.

The review runs when the request has client instructions or notes to apply, or when the
shortlist has tied candidates with other information (and `final_review` is on). If the call
fails, the weighted ranking is returned with a warning.
"""

import json
import logging
from typing import Any

from .agent_trace import run_with_retries, trace_step
from .llm_agents import build_review_manager
from .schemas import CandidateReview, CriterionUsed, FinalRanking, HiringRequest, RankedCandidate, ReviewOutput

logger = logging.getLogger(__name__)

MAX_ADJUSTMENT = 0.10
TIE_MARGIN = 0.01        # final scores this close count as tied
SHORTLIST_FACTOR = 2
MAX_SHORTLIST = 30
OTHER_CHARS = 300        # each other-information value is cut to this length


def wants_review(request: HiringRequest, has_ties: bool = False) -> bool:
    """Review when there are instructions or notes to apply, or ties to break."""
    return request.final_review and bool(request.client_instructions.strip() or request.notes.strip() or has_ties)


def shortlist_size(top_x: int) -> int:
    """How many candidates the review sees: enough that a nudge can bring in someone just
    below the cut, few enough to keep the call small."""
    return min(max(SHORTLIST_FACTOR * top_x, top_x + 5), max(MAX_SHORTLIST, top_x))


def tie_groups(scores: list[float]) -> list[list[int]]:
    """Indexes of `scores` (sorted best first) grouped into runs where each is within
    TIE_MARGIN of the next. Groups of one are left out."""
    groups, current = [], [0] if scores else []
    for i in range(1, len(scores)):
        if scores[i - 1] - scores[i] <= TIE_MARGIN + 1e-9:
            current.append(i)
        else:
            groups.append(current)
            current = [i]
    groups.append(current)
    return [g for g in groups if len(g) > 1]


def has_ties(ranking: FinalRanking, other_info: dict[str, dict[str, Any]]) -> bool:
    """Whether the shortlist has tied candidates with other information to break the tie."""
    scores = [r.total_score for r in ranking.ranking]
    return any(any(ranking.ranking[i].candidate_ref in other_info for i in g) for g in tie_groups(scores))


def _clip(value: Any) -> Any:
    if isinstance(value, list):
        return [_clip(v) for v in value]
    if isinstance(value, str) and len(value) > OTHER_CHARS:
        return value[:OTHER_CHARS] + "…"
    return value


def other_info_for(candidates, fields: list[str], labels: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Ref -> {header: value} of each candidate's other-information fields (clipped)."""
    info = {}
    for c in candidates:
        values = {labels.get(f) or f: _clip(c.fields[f]) for f in fields if f in c.fields}
        if values:
            info[c.ref] = values
    return info


def review_payload(ranking: FinalRanking, request: HiringRequest, criteria: list[CriterionUsed],
                   other_info: dict[str, dict[str, Any]] | None = None) -> str:
    names = {c.id: c.name for c in criteria}
    other_info = other_info or {}
    return json.dumps({
        "role": request.role,
        "client_instructions": request.client_instructions,
        "notes": request.notes,
        "criteria": [{"id": c.id, "name": c.name, "weight_share": c.weight,
                      "ranker_instructions": request.instructions.get(c.id, "")} for c in criteria],
        "max_adjustment": MAX_ADJUSTMENT,
        "tie_margin": TIE_MARGIN,
        "shortlist": [{
            "candidate_ref": r.candidate_ref,
            "weighted_score": r.total_score,
            "criteria": {names.get(cid, cid): {"score": s, "reason": r.rationales.get(cid, "")}
                         for cid, s in r.per_criterion.items()},
            "other_info": other_info.get(r.candidate_ref, {}),
        } for r in ranking.ranking],
    }, indent=2, ensure_ascii=False, default=str)


def validate_review(output: ReviewOutput, refs: list[str]) -> tuple[dict[str, CandidateReview], list[str]]:
    """One review per shortlisted ref, adjustments clamped to +-MAX_ADJUSTMENT, tiebreaks to -1..1.

    Returns:
        (reviews by ref, warnings): a ref the Manager skipped gets no entry (adjustment 0).
    """
    allowed, reviews, warnings = set(refs), {}, []
    for r in output.candidates:
        if r.candidate_ref not in allowed or r.candidate_ref in reviews:
            continue
        adjustment = round(max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, r.adjustment)), 3)
        if adjustment != r.adjustment:
            warnings.append(f"Final review: the adjustment for {r.candidate_ref} was clamped to {adjustment:+.2f}.")
        if adjustment and not r.reason.strip():
            warnings.append(f"Final review: the adjustment for {r.candidate_ref} had no reason, so it was not applied.")
            adjustment = 0.0
        tiebreak = max(-1.0, min(1.0, r.tiebreak))
        reviews[r.candidate_ref] = r.model_copy(update={
            "adjustment": adjustment, "reason": r.reason.strip() if adjustment else "",
            "tiebreak": tiebreak, "tiebreak_reason": r.tiebreak_reason.strip() if tiebreak else ""})
    missing = [ref for ref in refs if ref not in reviews]
    if missing:
        warnings.append(f"Final review skipped {len(missing)} shortlisted candidate(s); their weighted score stands.")
    return reviews, warnings


def apply_review(ranking: FinalRanking, reviews: dict[str, CandidateReview],
                 top_x: int) -> tuple[list[RankedCandidate], list[list[str]]]:
    """Final scores, sorted, ties ordered by tiebreak, cut to top_x.

    Returns:
        (ranking, tie groups): the groups (refs, in their final order) whose order the tiebreak
        decided, for the trace.
    """
    rows = []
    for r in ranking.ranking:
        rev = reviews.get(r.candidate_ref)
        adj = rev.adjustment if rev else 0.0
        rows.append(r.model_copy(update={
            "weighted_score": r.total_score,
            "total_score": round(max(0.0, min(1.0, r.total_score + adj)), 3),
            "adjustment": adj, "adjustment_reason": rev.reason if rev else "",
            "justification": rev.justification.strip() if rev else ""}))
    rows.sort(key=lambda r: (-r.total_score, -r.weighted_score, r.candidate_ref))

    tiebreak = {ref: rev.tiebreak for ref, rev in reviews.items()}
    decided = []
    for group in tie_groups([r.total_score for r in rows]):
        members = [rows[i] for i in group]
        ordered = sorted(members, key=lambda r: (-tiebreak.get(r.candidate_ref, 0.0), -r.total_score,
                                                 -r.weighted_score, r.candidate_ref))
        if any(tiebreak.get(r.candidate_ref) for r in members):
            decided.append([r.candidate_ref for r in ordered])
            ordered = [r.model_copy(update={"tiebreak_reason": reviews[r.candidate_ref].tiebreak_reason})
                       if r.candidate_ref in reviews and reviews[r.candidate_ref].tiebreak_reason else r
                       for r in ordered]
        for i, r in zip(group, ordered):
            rows[i] = r
    return rows[:top_x], decided


async def review_ranking(ranking: FinalRanking, request: HiringRequest, criteria: list[CriterionUsed],
                         other_info: dict[str, dict[str, Any]] | None = None) -> FinalRanking:
    """Run the final review over `ranking` (already cut to the shortlist) and return the
    final top-`request.top_x`. A failed review keeps the weighted order, with a warning."""
    refs = [r.candidate_ref for r in ranking.ranking]
    plain = [r.model_copy(update={"weighted_score": r.total_score}) for r in ranking.ranking[:request.top_x]]
    if not refs:
        return ranking.model_copy(update={"ranking": plain})
    try:
        output = await run_with_retries(build_review_manager(), review_payload(ranking, request, criteria, other_info),
                                        ReviewOutput, "review")
    except RuntimeError as e:
        logger.warning("Final review failed: %s", e)
        return ranking.model_copy(update={"ranking": plain, "warnings": ranking.warnings + [
            f"The final review failed, so the ranking is the plain weighted score: {e}"]})
    reviews, warnings = validate_review(output, refs)
    final, decided = apply_review(ranking, reviews, request.top_x)
    before = refs[:request.top_x]
    trace_step("review", "Final review applied", shortlist=len(refs),
               adjustments={ref: r.adjustment for ref, r in reviews.items() if r.adjustment},
               ties_broken=decided, other_info_for=len(other_info or {}),
               weighted_top=before, final_top=[r.candidate_ref for r in final],
               moved_in=[r.candidate_ref for r in final if r.candidate_ref not in before])
    logger.info("Final review: %d adjustments, %d tie group(s) ordered; top changed: %s",
                sum(1 for r in reviews.values() if r.adjustment), len(decided),
                [r.candidate_ref for r in final] != before)
    return ranking.model_copy(update={"ranking": final, "reviewed": True, "review_summary": output.summary.strip(),
                                      "warnings": ranking.warnings + warnings})
