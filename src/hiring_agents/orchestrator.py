"""Manager / Orchestrator: runs the whole ranking pipeline for one HiringRequest.

Design note: the deterministic steps (which rankers run, privacy filtering, validation,
aggregation) are plain code, so they behave the same every time and can be unit-tested
without API calls. LLMs are used only for the judgment calls: scoring candidates. A Manager
*LLM* can be added later to turn free-text instructions into a HiringRequest.

Entry point: `rank_candidates(request, raw_candidates)`.
"""

import asyncio
import json
import logging
import time

from agents import Runner

from .aggregation import aggregate
from .privacy import pseudonymize, view_for
from .llm_agents import build_ranker
from .registry import CriterionRegistry, default_registry
from .schemas import Candidate, Criterion, FinalRanking, HiringRequest, RankerOutput

logger = logging.getLogger(__name__)

# Total tries per ranker (1 = no retry). Covers transient API errors and malformed output.
MAX_ATTEMPTS = 2


def resolve_criteria(
    request: HiringRequest, registry: CriterionRegistry
) -> tuple[list[Criterion], list[str]]:
    """Decide which criteria run for this request. Every other criterion sleeps.

    The request's custom (bonus) criteria are added to a *copy* of the registry, so they
    don't carry over to later requests. A criterion is active only if it's registered and
    has a weight above 0 in `request.criteria`.

    Args:
        request: The hiring request.
        registry: Criteria the system knows about (usually `default_registry()`).

    Returns:
        (active, warnings): criteria to run, in request order, plus warnings for any
        unknown or zero-weight criteria that were skipped.
    """
    registry = registry.copy()
    for custom in request.custom_criteria:
        registry.register(custom.model_copy(update={"type": "bonus"}))

    active, warnings = [], []
    for cid, weight in request.criteria.items():
        criterion = registry.get(cid)
        if criterion is None:
            warnings.append(f"Unknown criterion '{cid}' ignored.")
        elif weight <= 0:
            warnings.append(f"Criterion '{cid}' has weight {weight} and was skipped.")
        else:
            active.append(criterion)

    sleeping = sorted(set(registry.ids()) - {c.id for c in active})
    logger.info("Active criteria: %s", [c.id for c in active])
    logger.info("Sleeping criteria (no LLM call): %s", sleeping or "none")
    return active, warnings


def validate_output(
    output: RankerOutput, criterion: Criterion, expected_refs: set[str]
) -> tuple[RankerOutput, list[str]]:
    """Fix or flag anything the LLM got wrong instead of trusting it blindly.

    Checks and fixes:
      - criterion_id is overwritten with the correct one.
      - Scores for refs that weren't in the input (made-up candidates) are dropped.
      - Duplicate scores for the same candidate: the first is kept.
      - Scores outside 0-10 are clamped into range.
      - Exclusions are ignored unless the criterion is a hard constraint.
      - Candidates with no score and no exclusion are reported (aggregation counts them as 0).

    Args:
        output: Raw parsed output from the ranker agent.
        criterion: The criterion this ranker was evaluating.
        expected_refs: Every candidate ref that was sent to the ranker.

    Returns:
        (cleaned_output, warnings): a corrected `RankerOutput` and one warning per issue found.
    """
    warnings, seen, scores = [], set(), []
    for s in output.scores:
        if s.candidate_ref not in expected_refs:
            warnings.append(f"[{criterion.id}] dropped unknown candidate_ref '{s.candidate_ref}'.")
        elif s.candidate_ref in seen:
            warnings.append(f"[{criterion.id}] dropped duplicate score for '{s.candidate_ref}'.")
        else:
            seen.add(s.candidate_ref)
            if not 0 <= s.score <= 10:
                warnings.append(f"[{criterion.id}] clamped out-of-range score {s.score} for '{s.candidate_ref}'.")
                s = s.model_copy(update={"score": max(0, min(10, s.score))})
            scores.append(s)

    excluded = [e for e in output.excluded if e.candidate_ref in expected_refs]
    if excluded and criterion.constraint != "hard":
        warnings.append(f"[{criterion.id}] ignored exclusions from a non-hard criterion.")
        excluded = []

    accounted = seen | {e.candidate_ref for e in excluded}
    for ref in sorted(expected_refs - accounted):
        warnings.append(f"[{criterion.id}] no score returned for '{ref}' (counted as 0).")

    return RankerOutput(criterion_id=criterion.id, scores=scores, excluded=excluded), warnings


async def run_ranker(
    criterion: Criterion, candidates: list[Candidate], request: HiringRequest
) -> RankerOutput:
    """Run one ranker agent over all candidates, retrying on failure.

    The agent receives a JSON payload containing the role, the client's notes, and each
    candidate's `view_for` record (only the fields this criterion needs). Every candidate is
    sent in one call so the LLM can compare them against each other.

    Args:
        criterion: The criterion to score on.
        candidates: Sanitized candidates from `pseudonymize`.
        request: Provides `role` and `notes` as context.

    Returns:
        The ranker's parsed `RankerOutput` (not yet validated).

    Raises:
        RuntimeError: If every attempt fails (API error, timeout, or output that doesn't
            match the schema).
    """
    payload = {
        "role": request.role,
        "additional_notes": request.notes,
        "candidates": [view_for(c, criterion) for c in candidates],
    }
    agent = build_ranker(criterion)
    payload_json = json.dumps(payload, indent=2)
    tag = f"[{criterion.id}]"
    logger.info("%s Created agent '%s' (model: %s)", tag, agent.name, agent.model or "SDK default")
    logger.debug("%s Agent instructions:\n%s", tag, agent.instructions)
    logger.debug("%s Manager -> ranker payload:\n%s", tag, payload_json)

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        start = time.perf_counter()
        try:
            result = await Runner.run(agent, payload_json)
            output = result.final_output_as(RankerOutput, raise_if_incorrect_type=True)
            logger.info("%s Ranker finished in %.1fs (attempt %d): %d scores, %d exclusions",
                        tag, time.perf_counter() - start, attempt, len(output.scores), len(output.excluded))
            logger.debug("%s Ranker -> manager raw reply:\n%s", tag, output.model_dump_json(indent=2))
            return output
        except Exception as e:  # retry on API/parse failures
            last_error = e
            logger.warning("%s Attempt %d/%d failed after %.1fs: %s",
                           tag, attempt, MAX_ATTEMPTS, time.perf_counter() - start, e)
    raise RuntimeError(f"Ranker '{criterion.id}' failed after {MAX_ATTEMPTS} attempts: {last_error}")


async def rank_candidates(
    request: HiringRequest,
    raw_candidates: list[dict],
    registry: CriterionRegistry | None = None,
) -> tuple[FinalRanking, dict[str, str]]:
    """Main entry point: rank `raw_candidates` for `request`.

    Steps:
      1. Pseudonymize the candidates (sensitive fields removed, IDs replaced with refs).
      2. Resolve the active criteria; the rest sleep.
      3. Run all active rankers in parallel (asyncio.gather).
      4. Validate each output. A ranker that fails is left out with a warning instead of
         failing the whole request.
      5. Aggregate into a weighted top-X ranking.

    Args:
        request: What to hire for and how to weight the criteria.
        raw_candidates: Candidate records from the data source. Each must have a "ucid" key.
        registry: Criteria to use. Defaults to the built-in TA criteria.

    Returns:
        (final_ranking, ref_map): the ranking uses pseudonymous refs. Use `ref_map[ref]` to
        get each candidate's real ID back. Do this only on our side, after all LLM calls.

    Raises:
        ValueError: If no requested criterion is valid.
        RuntimeError: If every ranker fails.
    """
    logger.info("Request: role=%r, %d candidates, weights=%s, top_x=%d",
                request.role, len(raw_candidates), request.criteria, request.top_x)
    candidates, ref_map = pseudonymize(raw_candidates)
    active, warnings = resolve_criteria(request, registry or default_registry())
    for w in warnings:
        logger.warning(w)
    if not active:
        raise ValueError("No valid criteria in request.")

    run_start = time.perf_counter()

    # return_exceptions=True: one failing ranker doesn't cancel the others.
    results = await asyncio.gather(
        *(run_ranker(c, candidates, request) for c in active), return_exceptions=True
    )

    expected = {c.ref for c in candidates}
    outputs: dict[str, RankerOutput] = {}
    for criterion, result in zip(active, results):
        if isinstance(result, BaseException):
            msg = f"[{criterion.id}] ranker failed and was left out: {result}"
            logger.error(msg)
            warnings.append(msg)
            continue
        validated, issues = validate_output(result, criterion, expected)
        for issue in issues:
            logger.warning(issue)
        if issues:
            logger.debug("[%s] Output after validation fixes:\n%s",
                         criterion.id, validated.model_dump_json(indent=2))
        outputs[criterion.id] = validated
        warnings.extend(issues)
    logger.info("All rankers done in %.1fs (%d/%d succeeded)",
                time.perf_counter() - run_start, len(outputs), len(active))

    if not outputs:
        raise RuntimeError("All rankers failed:\n" + "\n".join(warnings))

    final = aggregate(
        role=request.role,
        outputs=outputs,
        # Only criteria that actually produced output, so failed rankers don't drag scores down.
        weights={cid: request.criteria[cid] for cid in outputs},
        all_refs=[c.ref for c in candidates],
        top_x=request.top_x,
        warnings=warnings,
    )
    logger.info("Final ranking (top %d of %d, %d excluded, %d warnings): %s",
                request.top_x, len(candidates), len(final.excluded), len(final.warnings),
                [(r.candidate_ref, r.total_score) for r in final.ranking])
    return final, ref_map
