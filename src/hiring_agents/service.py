"""Service layer: the operations a client can perform, as plain async functions.

This is the API's core. Each function takes and returns Pydantic models (schemas.py), prints
nothing, and raises `ServiceError` for bad input, so a REST layer can wrap each one directly:

    upload_files    POST /data/files      add data files to the store, then process it
    process_store   POST /data/process    recompile the store (and plan criteria) from its files
    data_status     GET  /data            what's in the store
    list_criteria   GET  /criteria        the criteria a request can weight
    list_documents  GET  /documents       supporting documents (course outlines, job descriptions)
    upload_documents POST /documents      add documents to the store
    delete_document DELETE /documents/{name}
    draft_request   POST /requests/draft  free text + documents -> a request to review (Manager agent)
    rank            POST /rankings        run a ranking

The command line (src/cli.py) is the first client. `save_result` is for local clients: it
writes the result as JSON plus the HTML results page.

Processing the store means: compile every file in raw/ into candidates.json (datastore.py),
then plan or reuse criteria.json (planning.py). Files are only added if processing succeeds,
so a bad upload leaves the store as it was.
"""

import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from .datastore import SUPPORTED, Store, compile_sources, find_files, load_store, write_store
from .documents import DocumentError, add_documents, describe, find_documents, load_context, remove_document
from .drafting import draft_request as manager_draft
from .orchestrator import rank_candidates
from .planning import field_labels, load_catalog, refresh_catalog, with_catalog
from .registry import default_registry
from .report import render_html
from .schemas import (
    CriterionInfo, DataStatus, DocumentInfo, DocumentsResult, HiringRequest, IntakeReport, RankingResult,
    RequestBrief, RequestDraft, UploadResult,
)

DEFAULT_PURPOSE = "Hiring teaching assistants (TAs) for university engineering courses"


class ServiceError(ValueError):
    """The client's input can't be used (a 4xx error in the API). The message says why."""


# --- Data ---

def data_status(store: Store = Store()) -> DataStatus:
    """Summarize the store: its files, candidate count, and whether it needs processing."""
    files = find_files([store.raw]) if store.raw.exists() else []
    compiled = store.candidates.exists()
    compiled_mtime = store.candidates.stat().st_mtime if compiled else 0
    return DataStatus(
        files=[f.name for f in files],
        candidates=len(load_store(store.candidates)) if compiled else 0,
        compiled_at=datetime.fromtimestamp(compiled_mtime).isoformat(timespec="seconds") if compiled else None,
        criteria_planned=store.criteria.exists(),
        stale=any(f.stat().st_mtime > compiled_mtime for f in files),
    )


def _saved_mapping(store: Store) -> dict[str, str | None]:
    if not store.column_mapping.exists():
        return {}
    return json.loads(store.column_mapping.read_text(encoding="utf-8"))


async def process_store(
    store: Store = Store(),
    purpose: str = DEFAULT_PURPOSE,
    plan_criteria: bool = True,
    rebuild_criteria: bool = False,
) -> tuple[dict[str, IntakeReport], bool, list[str]]:
    """Compile every file in the store's raw/ folder, then plan (or reuse) its criteria.

    Uses the column overrides saved by earlier uploads (column_mapping.json).

    Returns:
        (sources, criteria_planned, warnings)

    Raises:
        ServiceError: If there are no files, or a file can't be read or mapped.
    """
    if not store.raw.exists() or not find_files([store.raw]):
        raise ServiceError("No data files in the store yet; upload some first.")
    try:
        records, reports, warnings = compile_sources([store.raw], column_mapping=_saved_mapping(store) or None)
    except ValueError as e:
        raise ServiceError(str(e)) from e
    write_store(records, store.candidates)

    planned = False
    if plan_criteria:
        registry = default_registry()
        base = [registry.get(cid) for cid in registry.ids()]
        _, planned, plan_warnings = await refresh_catalog(
            records, base, purpose, field_labels(reports.values()), store.criteria, rebuild_criteria)
        warnings += plan_warnings
    return reports, planned, warnings


async def upload_files(
    paths: list[str | Path],
    store: Store = Store(),
    replace: bool = False,
    column_mapping: dict[str, str | None] | None = None,
    purpose: str = DEFAULT_PURPOSE,
    plan_criteria: bool = True,
) -> UploadResult:
    """Add data files to the store and process it.

    Args:
        paths: Excel / CSV / JSON files, or folders of them.
        store: The store to add them to.
        replace: Overwrite files with the same name. Otherwise a name clash is an error.
        column_mapping: Source column -> standard field (or None to drop), for columns the
            built-in aliases don't recognize. Saved with the store and applied to every file
            from now on.
        purpose: What the hiring is for, given to the Criteria Planner.
        plan_criteria: False skips the planner (no LLM call).

    Raises:
        ServiceError: For missing or unsupported files, name clashes, or a file that can't be
            processed. The store is left as it was.
    """
    try:
        files = find_files(paths)
    except FileNotFoundError as e:
        raise ServiceError(str(e)) from e
    if not files:
        raise ServiceError("No data files given.")
    bad = [f.name for f in files if f.suffix.lower() not in SUPPORTED]
    if bad:
        raise ServiceError(f"Unsupported file type: {bad}. Use {sorted(SUPPORTED)}.")
    names = [f.name for f in files]
    if len(set(names)) < len(names):
        raise ServiceError(f"Two files share a name: {sorted({n for n in names if names.count(n) > 1})}.")
    store.raw.mkdir(parents=True, exist_ok=True)
    existing = [n for n in names if (store.raw / n).exists()]
    if existing and not replace:
        raise ServiceError(f"Already in the store: {existing}. Upload with replace to overwrite them.")

    # Copy in, keeping what we overwrite, so a failed processing run can be undone.
    old_mapping = _saved_mapping(store)
    with tempfile.TemporaryDirectory() as backup:
        for name in existing:
            shutil.copy2(store.raw / name, Path(backup) / name)
        for f in files:
            shutil.copy2(f, store.raw / f.name)
        if column_mapping:
            store.column_mapping.write_text(json.dumps({**old_mapping, **column_mapping}, indent=2),
                                            encoding="utf-8")
        try:
            sources, planned, warnings = await process_store(store, purpose, plan_criteria)
        except Exception:
            for f in files:
                (store.raw / f.name).unlink(missing_ok=True)
            for name in existing:
                shutil.copy2(Path(backup) / name, store.raw / name)
            if column_mapping:
                store.column_mapping.write_text(json.dumps(old_mapping, indent=2), encoding="utf-8")
            raise

    return UploadResult(
        added=[n for n in names if n not in existing], replaced=existing, sources=sources,
        criteria_planned=planned, warnings=warnings, status=data_status(store),
    )


# --- Criteria ---

def _registry(store: Store, use_catalog: bool = True):
    registry = default_registry()
    catalog = load_catalog(store.criteria) if use_catalog else None
    return with_catalog(registry, catalog)[0] if catalog else registry


def list_criteria(store: Store = Store()) -> list[CriterionInfo]:
    """The criteria a request can weight: the built-ins (with the fields criteria.json fitted
    to them) and the auto criteria planned from the data."""
    registry = _registry(store)
    return [
        CriterionInfo(
            id=c.id, name=c.name, description=c.description, type=c.type,
            fields=c.relevant_fields, courses=c.courses,
            suggested_weight=c.importance / 2 if c.type == "auto" else 1.0,
        )
        for c in (registry.get(cid) for cid in registry.ids())
    ]


# --- Supporting documents ---

def list_documents(store: Store = Store()) -> list[DocumentInfo]:
    """The store's documents, with how much text each has (and any problem reading it)."""
    return [describe(p) for p in find_documents(store.documents)]


def upload_documents(paths: list[str | Path], store: Store = Store(), replace: bool = False) -> DocumentsResult:
    """Add course outlines, job descriptions, ... (.pdf, .docx, .txt, .md) to the store.

    Raises:
        ServiceError: For an unsupported or unreadable file, one with no text, or a name clash
            without `replace`. Nothing is added if any file fails.
    """
    paths = [Path(p) for p in paths]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing or not paths:
        raise ServiceError(f"Not found: {missing}." if missing else "No documents given.")
    try:
        added, replaced = add_documents(paths, store.documents, replace)
    except DocumentError as e:
        raise ServiceError(str(e)) from e
    return DocumentsResult(added=added, replaced=replaced, documents=list_documents(store))


def delete_document(name: str, store: Store = Store()) -> list[DocumentInfo]:
    """Remove a document from the store. Returns the documents left."""
    try:
        remove_document(name, store.documents)
    except DocumentError as e:
        raise ServiceError(str(e)) from e
    return list_documents(store)


# --- Drafting a request from free text ---

async def draft_request(brief: RequestBrief, store: Store = Store()) -> RequestDraft:
    """Turn free-text instructions and documents into a request draft with the Manager agent
    (one LLM call).

    The draft is for the client to review and edit; pass its `request` to `rank` to run it.

    Raises:
        ServiceError: If there is no candidate data yet, a document isn't in the store or has
            no text, or the text points to no criterion.
        RuntimeError: If the Manager call fails (a server-side error).
    """
    if not store.candidates.exists():
        raise ServiceError("No candidate data yet; upload some first.")
    registry = _registry(store)
    try:
        documents, warnings = load_context(brief.documents, store.documents)
        return await manager_draft(brief, [registry.get(cid) for cid in registry.ids()], documents, warnings)
    except ValueError as e:     # DocumentError is a ValueError too
        raise ServiceError(str(e)) from e


# --- Ranking ---

def validate_request(request: HiringRequest, store: Store = Store()) -> None:
    """Check a request's weights before any LLM call.

    Raises:
        ServiceError: For an unknown criterion id (usually a typo) in the weights or the
            instructions, a negative weight, or no weight above 0. Auto criteria count as known only with `auto_criteria` on.
    """
    known = set(_registry(store, use_catalog=request.auto_criteria).ids())
    known |= {c.id for c in request.custom_criteria}
    unknown = sorted(set(request.criteria) - known)
    if unknown:
        raise ServiceError(f"Unknown criteria {unknown}. Available: {sorted(known)}.")
    stray = sorted(set(request.instructions) - known)
    if stray:
        raise ServiceError(f"Instructions for unknown criteria {stray}. Available: {sorted(known)}.")
    negative = sorted(cid for cid, w in request.criteria.items() if w < 0)
    if negative:
        raise ServiceError(f"Weights can't be negative: {negative}.")
    if not any(w > 0 for w in request.criteria.values()):
        raise ServiceError("Give at least one criterion a weight above 0.")


async def rank(request: HiringRequest, store: Store = Store()) -> RankingResult:
    """Rank the store's candidates for `request`.

    Raises:
        ServiceError: If the store is empty or the request is invalid (see `validate_request`),
            or no requested criterion has data.
        RuntimeError: If every ranker fails (a server-side error, not the client's).
    """
    if not store.candidates.exists():
        raise ServiceError("No candidate data yet; upload some first.")
    validate_request(request, store)
    status = data_status(store)
    try:
        final, ref_map = await rank_candidates(request, store=store)
    except ValueError as e:
        raise ServiceError(str(e)) from e
    if status.stale:
        final.warnings.insert(0, "Data files changed since the store was processed; "
                                 "these results use the older data. Process the store to update.")
    shown = [c.candidate_ref for c in final.ranking] + [e.candidate_ref for e in final.excluded]
    return RankingResult(request=request, ranking=final, candidate_ids={r: ref_map[r] for r in shown})


def save_result(result: RankingResult, out_dir: Path) -> tuple[Path, Path]:
    """Write a result as JSON and as the HTML results page. Both hold real IDs, so `out_dir`
    must not be shared or committed (results/ is gitignored).

    Returns:
        (json_path, html_path)
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_dir / f"results-{datetime.now():%Y%m%d-%H%M%S}"
    json_path, html_path = stem.with_suffix(".json"), stem.with_suffix(".html")
    json_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    html_path.write_text(render_html(result.ranking, result.candidate_ids, result.request), encoding="utf-8")
    return json_path, html_path
