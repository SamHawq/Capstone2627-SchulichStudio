"""Combines validated ranker outputs into one weighted ranking.

This step is plain arithmetic, not an LLM: the same inputs always give the same ranking, and
the total score can be explained exactly from the per-criterion scores and weights.
"""

import logging

from .schemas import ExcludedCandidate, FinalRanking, RankedCandidate, RankerOutput

logger = logging.getLogger(__name__)


def aggregate(
    role: str,
    outputs: dict[str, RankerOutput],
    weights: dict[str, float],
    all_refs: list[str],
    top_x: int,
    warnings: list[str],
) -> FinalRanking:
    """Merge per-criterion scores into a final ranking.

    How the score is computed:
        total_score = sum(weight_c * score_c / 10) / sum(weight_c)
    over every criterion c that produced output. Each score is scaled from 0-10 down to 0-1,
    and the weights are normalized, so total_score is always between 0 and 1. A candidate a
    ranker didn't score counts as 0 for that criterion (validate_output has already warned).

    Any candidate excluded by any hard-constraint ranker is removed entirely. Ties are broken
    by ref so the order is stable from run to run.

    Args:
        role: Copied into the result.
        outputs: Validated ranker outputs, keyed by criterion_id.
        weights: Weight per criterion_id. Must contain every key in `outputs`.
        all_refs: Every candidate in the request, so nobody is silently dropped.
        top_x: Number of candidates to return.
        warnings: Warnings collected so far, passed through to the result.

    Returns:
        A `FinalRanking` with the top-X candidates, the excluded list, and the warnings.
    """
    total_weight = sum(weights[cid] for cid in outputs)

    # Gather exclusions from every ranker; if more than one excludes the same candidate,
    # the first reason is kept.
    excluded: dict[str, ExcludedCandidate] = {}
    for out in outputs.values():
        for ex in out.excluded:
            excluded.setdefault(ex.candidate_ref, ex)

    ranked = []
    for ref in all_refs:
        if ref in excluded:
            continue
        per_criterion, rationales, total = {}, {}, 0.0
        for cid, out in outputs.items():
            score = next((s for s in out.scores if s.candidate_ref == ref), None)
            value = score.score if score else 0
            per_criterion[cid] = value
            rationales[cid] = score.rationale if score else "No score returned."
            total += weights[cid] * value / 10
        ranked.append(RankedCandidate(
            candidate_ref=ref,
            total_score=round(total / total_weight, 3) if total_weight else 0.0,
            per_criterion=per_criterion,
            rationales=rationales,
        ))
        logger.debug("Aggregate %s: scores=%s weights=%s -> total=%.3f",
                     ref, per_criterion, {cid: weights[cid] for cid in outputs}, ranked[-1].total_score)

    ranked.sort(key=lambda r: (-r.total_score, r.candidate_ref))  # highest score first
    return FinalRanking(
        role=role, ranking=ranked[:top_x], excluded=list(excluded.values()), warnings=warnings
    )
