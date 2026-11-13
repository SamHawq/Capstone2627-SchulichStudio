"""Manager / Orchestrator: runs the whole ranking pipeline for one HiringRequest.

Design note: the deterministic steps (which rankers run, privacy filtering, validation,
aggregation) are plain code, so they behave the same every time and can be unit-tested
without API calls. LLMs are used only for the judgment calls: planning which data fields
feed which criteria (planning.py, once per dataset), turning free-text instructions into a
HiringRequest (the Manager agent, drafting.py, before this runs), and scoring candidates.

Entry point: `rank_candidates(request, raw_candidates)`.
"""

import asyncio
import json
import logging
import time

from .agent_trace import run_agent, trace_step, traced_operation
from .aggregation import aggregate
from .availability import extract_availability
from .datastore import Store, load_field_labels, load_store
from .intake import DEFAULT_FIELDS, report_warnings, standardize
from .planning import (
    add_auto_criteria, base_fields, field_labels, load_catalog, new_fields, plan_catalog, tiebreak_fields,
    with_catalog,
)
from .privacy import pseudonymize, view_for
from .llm_agents import build_ranker
from .registry import CriterionRegistry, default_registry
from .review import has_ties, other_info_for, review_ranking, shortlist_size, wants_review
from .screening import apply_screen, headers_of, screen_headers
from .schemas import (
    Candidate, CriteriaCatalog, Criterion, CriterionUsed, FieldSpec, FinalRanking, HiringRequest,
    RankerOutput,
)

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
        unknown or negative-weight criteria that were skipped.
    """
    registry = registry.copy()
    for custom in request.custom_criteria:
        registry.register(custom.model_copy(update={"type": "bonus"}))

    active, warnings = [], []
    for cid, weight in request.criteria.items():
        criterion = registry.get(cid)
        if criterion is None:
            warnings.append(f"Unknown criterion '{cid}' ignored.")
        elif weight < 0:
            warnings.append(f"Criterion '{cid}' has weight {weight} and was skipped.")
        elif weight == 0:
            pass  # switched off on purpose (e.g. by the Manager), so not worth a warning
        else:
            active.append(criterion)

    sleeping = sorted(set(registry.ids()) - {c.id for c in active})
    logger.info("Active criteria: %s", [c.id for c in active])
    logger.info("Sleeping criteria (no LLM call): %s", sleeping or "none")
    return active, warnings


def criteria_with_data(
    active: list[Criterion], candidates: list[Candidate]
) -> tuple[list[Criterion], list[str]]:
    """Put to sleep any active criterion whose fields no candidate has.

    Such a ranker could only score everyone 0, which costs an LLM call and drags every total
    down equally. Skipping it leaves its weight out, like a failed ranker.

    Returns:
        (runnable, warnings): criteria with data for at least one candidate, plus one warning
        per skipped criterion.
    """
    present = {f for c in candidates for f in c.fields}
    runnable, warnings = [], []
    for criterion in active:
        has = [f for f in criterion.relevant_fields if f in present]
        missing = [f for f in criterion.relevant_fields if f not in present]
        if not criterion.relevant_fields:
            warnings.append(f"Criterion '{criterion.id}' skipped: no data field was assigned to it "
                            "(the Criteria Planner found no evidence for it in this data).")
            continue
        if not has:
            warnings.append(f"Criterion '{criterion.id}' skipped: no candidate has any of its "
                            f"fields {criterion.relevant_fields}.")
            continue
        runnable.append(criterion)
        # A built-in criterion extended by the catalog is expected to miss its own standard
        # fields (the data uses other columns), so that isn't worth a warning.
        if missing and present & {n.field for n in criterion.field_notes}:
            logger.info("[%s] ranked on its planned fields; not in the data: %s", criterion.id, missing)
        elif missing:
            warnings.append(f"Criterion '{criterion.id}' has no data for {missing}; "
                            f"it is ranked on {has} only.")
    return runnable, warnings


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
    sent in one call so the LLM can compare them against each other. The request's
    instructions for this criterion, if any, go into the agent's instructions.

    Args:
        criterion: The criterion to score on.
        candidates: Sanitized candidates from `pseudonymize`.
        request: Provides `role`, `notes` and this criterion's `instructions`.

    Returns:
        The ranker's parsed `RankerOutput` (not yet validated).

    Raises:
        RuntimeError: If every attempt fails (API error, timeout, or output that doesn't
            match the schema).
    """
    # Only the role and this criterion's fields. The request's notes and the client's own
    # words go to the final review instead (review.py), so every ranker judges its own
    # evidence; what it needs from the instructions is in its agent prompt.
    payload = {
        "role": request.role,
        "candidates": [view_for(c, criterion) for c in candidates],
    }
    agent = build_ranker(criterion, request.instructions.get(criterion.id, ""))
    payload_json = json.dumps(payload, indent=2)
    tag = f"[{criterion.id}]"
    logger.info("%s Created agent '%s' (model: %s)%s", tag, agent.name, agent.model or "SDK default",
                " with client instructions" if request.instructions.get(criterion.id) else "")
    # Field names only; the values are in the DEBUG payload below.
    logger.info("%s Manager -> ranker: %d candidates, fields %s", tag, len(candidates),
                sorted({f for c in candidates for f in c.fields if f in criterion.relevant_fields}))
    logger.debug("%s Agent instructions:\n%s", tag, agent.instructions)
    logger.debug("%s Manager -> ranker payload:\n%s", tag, payload_json)

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        start = time.perf_counter()
        try:
            output = await run_agent(agent, payload_json, RankerOutput)
            logger.info("%s Ranker finished in %.1fs (attempt %d): %d scores, %d exclusions",
                        tag, time.perf_counter() - start, attempt, len(output.scores), len(output.excluded))
            logger.debug("%s Ranker -> manager raw reply:\n%s", tag, output.model_dump_json(indent=2))
            return output
        except Exception as e:  # retry on API/parse failures
            last_error = e
            logger.warning("%s Attempt %d/%d failed after %.1fs: %s",
                           tag, attempt, MAX_ATTEMPTS, time.perf_counter() - start, e)
    raise RuntimeError(f"Ranker '{criterion.id}' failed after {MAX_ATTEMPTS} attempts: {last_error}")


async def _get_catalog(
    request: HiringRequest,
    raw_candidates: list[dict],
    from_store: bool,
    registry: CriterionRegistry,
    fields: list[FieldSpec] | None,
    column_mapping: dict[str, str | None] | None,
    store: Store,
) -> tuple[CriteriaCatalog | None, list[str]]:
    """The data's criteria catalog: the store's criteria.json, else planned now (cached)."""
    base = [registry.get(cid) for cid in registry.ids()]
    if from_store and (catalog := load_catalog(store.criteria)):
        logger.info("Criteria catalog: criteria.json (%d extensions, %d auto criteria)",
                    len(catalog.extensions), len(catalog.criteria))
        added = new_fields(catalog, raw_candidates, base)
        warnings = [f"{len(added)} field(s) in the store aren't in criteria.json, so no criterion uses "
                    "them; run compile_candidates.py to plan them."] if added else []
        return catalog, warnings
    # Keep every non-sensitive column, so the planner can see what data exists.
    keep = {f.id for f in fields or DEFAULT_FIELDS if f.handling == "keep"}
    everything, report, _ = standardize(raw_candidates, base_fields(base) | keep, fields, column_mapping,
                                        keep_unmatched=True)
    if not from_store:  # the store's availability was extracted at compile time
        everything, _, _ = extract_availability(everything, "unknown term")
    # Data passed in directly is planned for this role; the store is planned at compile time.
    logger.info("Criteria catalog: planning for this request (data passed in directly, or no criteria.json)")
    catalog, warnings = await plan_catalog(everything, base, request.role, field_labels([report]),
                                           store.plan_cache)
    if from_store and catalog:
        warnings.append("The store has no criteria.json; planned criteria for this request only. "
                        "Run compile_candidates.py to plan and save them.")
    return catalog, warnings


@traced_operation("rank", lambda request, *a, **k: request.role)
async def rank_candidates(
    request: HiringRequest,
    raw_candidates: list[dict] | None = None,
    registry: CriterionRegistry | None = None,
    fields: list[FieldSpec] | None = None,
    column_mapping: dict[str, str | None] | None = None,
    store: Store | None = None,
    screen: bool = True,
) -> tuple[FinalRanking, dict[str, str]]:
    """Main entry point: rank candidates for `request`.

    Steps:
      0. Data passed in directly goes through the header screen first (the store's data was
         screened when it was compiled): see screening.py.
      1. Criteria catalog (if `request.auto_criteria`): the store's criteria.json, planned at
         compile time, or for data passed in directly, one Criteria Planner call (cached).
         Its fields are added to the built-in criteria and its auto criteria to the registry
         (see `planning`).
      2. Resolve the requested criteria; then the auto criteria that apply to the role join,
         each with its own ranker. The rest sleep.
      3. Intake: map the source columns to standard fields, keeping only the ID and the
         fields the criteria need.
      4. Pseudonymize the candidates (IDs replaced with refs, sensitive fields removed).
      5. Skip criteria that no candidate has data for.
      6. Run the remaining rankers in parallel (asyncio.gather).
      7. Validate each output. A ranker that fails is left out with a warning instead of
         failing the whole request.
      8. Aggregate into a weighted ranking.
      9. Final review (if the request has client instructions or notes): the Manager nudges
         each shortlisted candidate's weighted score by at most review.MAX_ADJUSTMENT, with a
         reason and a justification; code re-sorts and cuts to top-X (see review.py).

    Args:
        request: What to hire for and how to weight the criteria.
        raw_candidates: Candidate records in any column layout that intake can map (see
            `intake.standardize`). If omitted, the compiled candidate store
            (`candidate_data/candidates.json`, see `datastore`) is used.
        registry: Criteria to use. Defaults to the built-in TA criteria.
        fields: Standard field catalog. Defaults to `intake.DEFAULT_FIELDS`.
        column_mapping: Source column -> standard field (or None to drop), for columns the
            catalog's aliases don't cover.
        store: The candidate store to read (and whose criteria.json and plan cache to use).
            Defaults to candidate_data/.
        screen: Header-screen data passed in directly (one LLM call for headers the store's
            header_screen.json doesn't have yet; headers only). False: only intake's name
            blocklist removes sensitive columns.

    Returns:
        (final_ranking, ref_map): the ranking uses pseudonymous refs. Use `ref_map[ref]` to
        get each candidate's real ID back. Do this only on our side, after all LLM calls.

    Raises:
        ValueError: If no requested criterion is valid or has data, or intake can't map the
            data (see `intake.standardize`).
        FileNotFoundError: If `raw_candidates` is omitted and the store hasn't been compiled.
        RuntimeError: If every ranker fails.
        ScreeningError: If the header screen call fails.
    """
    store = store or Store()
    from_store = raw_candidates is None
    if from_store:
        raw_candidates = load_store(store.candidates)
    logger.info("Request: role=%r, %d candidates, weights=%s, top_x=%d",
                request.role, len(raw_candidates), request.criteria, request.top_x)
    registry = registry or default_registry()
    warnings: list[str] = []
    if not from_store and screen:
        header_screen, _, screen_warnings = await screen_headers(headers_of(raw_candidates), store.header_screen)
        raw_candidates, column_mapping, _, apply_warnings = apply_screen(
            raw_candidates, header_screen, fields, column_mapping)
        warnings += screen_warnings + apply_warnings
    catalog = None
    if request.catalog is not None:
        # Criteria planned from this request's instructions (service.draft_request).
        catalog = request.catalog
        logger.info("Criteria catalog: planned for this request (%d auto criteria)", len(catalog.criteria))
    elif request.auto_criteria:
        catalog, catalog_warnings = await _get_catalog(request, raw_candidates, from_store, registry,
                                                       fields, column_mapping, store)
        warnings += catalog_warnings
    if catalog:
        registry, catalog_warnings = with_catalog(registry, catalog)
        warnings += catalog_warnings

    active, resolve_warnings = resolve_criteria(request, registry)
    warnings += resolve_warnings
    if not active:
        raise ValueError("No valid criteria in request.")
    weights = {c.id: request.criteria[c.id] for c in active}
    skipped_fields = []
    if catalog and request.auto_criteria:
        active, weights, skipped_fields = add_auto_criteria(catalog, active, request)
    elif catalog:
        skipped_fields = list(catalog.skipped)
    needed = {f for c in active for f in c.relevant_fields}
    trace_step("criteria", "Criteria selected",
               catalog="criteria.json" if from_store and catalog else ("planned now" if catalog else "none"),
               active=[{"id": c.id, "weight": weights[c.id], "source": "requested" if c.id in request.criteria
                        else "auto", "fields": c.relevant_fields} for c in active],
               sleeping=[cid for cid in registry.ids() if cid not in weights])
    # Other information: fields no criterion uses that the planner kept for breaking ties.
    # They come through intake and pseudonymization like any field, but no ranker sees them
    # (view_for sends each ranker only its criterion's fields); only the final review does.
    tie_fields = [f for f in tiebreak_fields(catalog) if f not in needed] if request.final_review else []
    needed = needed | set(tie_fields)

    records, intake_report, intake_warnings = standardize(raw_candidates, needed, fields, column_mapping)
    # Unused columns are expected in the store, and the planner already reports what it skipped.
    # Missing fields are reported per criterion by criteria_with_data.
    intake_warnings += report_warnings(intake_report, unmatched=not (from_store or request.auto_criteria),
                                       missing=False)
    candidates, ref_map = pseudonymize(records)
    trace_step("privacy", "Intake + pseudonymize", candidates=len(candidates),
               fields_kept=sorted(needed & set(intake_report.column_mapping.values())),
               dropped_sensitive=intake_report.dropped_sensitive, ids="replaced with C-xxxxxxxx refs")
    active, data_warnings = criteria_with_data(active, candidates)
    if data_warnings:
        trace_step("criteria", "Data check", skipped_or_partial=data_warnings)
    warnings += intake_warnings + data_warnings
    for w in warnings:
        logger.warning(w)
    if not active:
        raise ValueError("None of the criteria have data:\n" + "\n".join(warnings))

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
    trace_step("validate", "Ranker outputs validated",
               succeeded=list(outputs), failed=[c.id for c in active if c.id not in outputs],
               scores={cid: len(o.scores) for cid, o in outputs.items()},
               fixes=[w for w in warnings if w.startswith("[")][:30])

    if not outputs:
        raise RuntimeError("All rankers failed:\n" + "\n".join(warnings))

    may_review = request.final_review and (wants_review(request) or bool(tie_fields))
    final = aggregate(
        role=request.role,
        outputs=outputs,
        # Only criteria that actually produced output, so failed rankers don't drag scores down.
        weights={cid: weights[cid] for cid in outputs},
        all_refs=[c.ref for c in candidates],
        # The final review sees a shortlist, so a nudge can bring in someone just below the cut.
        top_x=shortlist_size(request.top_x) if may_review else request.top_x,
        warnings=warnings,
    )
    present = {f for c in candidates for f in c.fields}
    total = sum(weights[cid] for cid in outputs)
    used = [
        CriterionUsed(id=c.id, name=c.name, source="requested" if c.id in request.criteria else "auto",
                      weight=round(weights[c.id] / total, 3),
                      fields=[f for f in c.relevant_fields if f in present])
        for c in active if c.id in outputs
    ]
    shortlisted = {r.candidate_ref for r in final.ranking}
    other = other_info_for([c for c in candidates if c.ref in shortlisted], tie_fields,
                           load_field_labels(store)) if tie_fields else {}
    if wants_review(request, has_ties(final, other)):
        final = await review_ranking(final, request, used, other)
    else:
        final = final.model_copy(update={"ranking": [r.model_copy(update={"weighted_score": r.total_score})
                                                     for r in final.ranking[:request.top_x]]})
    final = final.model_copy(update={"tiebreak_fields": [f for f in tie_fields if f in present]})
    final = final.model_copy(update={"intake": intake_report, "criteria": used,
                                     "skipped_fields": skipped_fields})
    trace_step("aggregate", "Weighted aggregation",
               weights={c.id: c.weight for c in used}, top_x=request.top_x, candidates=len(candidates),
               ranking=[{"ref": r.candidate_ref, "total": r.total_score, "per_criterion": r.per_criterion}
                        for r in final.ranking],
               excluded=len(final.excluded), warnings=len(final.warnings))
    logger.info("Final ranking (top %d of %d, %d excluded, %d warnings): %s",
                request.top_x, len(candidates), len(final.excluded), len(final.warnings),
                [(r.candidate_ref, r.total_score) for r in final.ranking])
    return final, ref_map
