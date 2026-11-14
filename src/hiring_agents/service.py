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
    create_job      POST /jobs            free text + documents -> a spreadsheet designed for that job
    list_jobs       GET  /jobs            the store's job spreadsheets
    get_job         GET  /jobs/{id}       one job: design, mapping, columns
    reapply_job     POST /jobs/{id}/apply rebuild a job's table from its (edited) files, no LLM call
    export_job      GET  /jobs/{id}/export the job as an Excel workbook (table + design)
    import_job      POST /jobs/import     a job workbook back in, without redesigning or re-mapping
    list_traces     GET  /traces          recorded runs (AGENT_TRACE=ON), with their operations
    get_trace       GET  /traces/{name}   every event of one recorded run, for the /runs page
    rank            POST /rankings        run a ranking

The command line (src/cli.py) is the first client. `save_result` is for local clients: it
writes the result as JSON plus the HTML results page.

Processing the store means: screen the files' column headers (screening.py; an LLM sees the
headers only, and the columns it drops never reach any LLM), compile every file in raw/ into
candidates.json (datastore.py), then plan or reuse criteria.json (planning.py). Files are only added if processing succeeds,
so a bad upload leaves the store as it was.
"""

import contextlib
import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from .datastore import SUPPORTED, Store, compile_sources, find_files, load_store, source_headers, write_store
from .documents import DocumentError, add_documents, describe, find_documents, load_context, remove_document
from . import agent_trace
from .agent_trace import trace_step, traced_operation
from .drafting import draft_request as manager_draft
from . import jobs
from .jobs import load_labels
from .orchestrator import rank_candidates
from .planning import field_labels, load_catalog, plan_catalog, profile_fields, refresh_catalog, with_catalog
from .registry import default_registry
from .screening import screen_headers
from .report import render_html
from .schemas import (
    CriterionInfo, DataStatus, DocumentInfo, DocumentsResult, HiringRequest, IntakeReport, JobInfo, JobResult,
    RankingResult, RequestBrief, RequestDraft, UploadResult,
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


@traced_operation("process")
async def process_store(
    store: Store = Store(),
    purpose: str = DEFAULT_PURPOSE,
    plan_criteria: bool = True,
    rebuild_criteria: bool = False,
    screen: bool = True,
) -> tuple[dict[str, IntakeReport], bool, list[str]]:
    """Screen the headers of every file in the store's raw/ folder, compile the files, then
    plan (or reuse) the store's criteria.

    Uses the column overrides saved by earlier uploads (column_mapping.json). The header
    screen makes an LLM call only for headers not already in header_screen.json.

    Args:
        screen: False skips the header screen; only intake's name blocklist then removes
            sensitive columns.

    Returns:
        (sources, criteria_planned, warnings)

    Raises:
        ServiceError: If there are no files, a file can't be read or mapped, or
            header_screen.json was edited into an invalid file.
        ScreeningError: If the header screen call fails (a server-side error); nothing is written.
    """
    if not store.raw.exists() or not find_files([store.raw]):
        raise ServiceError("No data files in the store yet; upload some first.")
    try:
        header_screen, warnings = None, []
        if screen:
            header_screen, _, warnings = await screen_headers(source_headers([store.raw]), store.header_screen)
        else:
            warnings.append("Header screen off: only the built-in name list removed sensitive columns.")
        records, reports, compile_warnings = compile_sources(
            [store.raw], column_mapping=_saved_mapping(store) or None, screen=header_screen)
        warnings += compile_warnings
    except ValueError as e:
        raise ServiceError(str(e)) from e
    trace_step("store", "Store compiled", candidates=len(records),
               sources={label: {"kept": len(r.column_mapping), "dropped_sensitive": r.dropped_sensitive}
                        for label, r in reports.items()})
    write_store(records, store.candidates)
    store.field_labels.write_text(json.dumps(field_labels(reports.values()), indent=2, ensure_ascii=False),
                                  encoding="utf-8")

    planned = False
    if plan_criteria:
        registry = default_registry()
        base = [registry.get(cid) for cid in registry.ids()]
        catalog, planned, plan_warnings = await refresh_catalog(
            records, base, purpose, field_labels(reports.values()), store.criteria, rebuild_criteria)
        warnings += plan_warnings
        trace_step("criteria", "Criteria catalog " + ("planned" if planned else "reused"),
                   extensions={e.criterion_id: [n.field for n in e.fields] for e in catalog.extensions}
                   if catalog else {},
                   auto_criteria={c.id: c.relevant_fields for c in catalog.criteria} if catalog else {},
                   skipped=len(catalog.skipped) if catalog else 0)
    return reports, planned, warnings


def _copy(src: Path, dst: Path) -> None:
    """shutil.copy2, with a clear error when Windows refuses because a program (usually Excel)
    has one of the files open."""
    try:
        shutil.copy2(src, dst)
    except PermissionError as e:
        raise ServiceError(f"Can't copy {src.name}: it's open in another program (e.g. Excel) or "
                           "read-only. Close it and upload again; the store was left as it was.") from e


@traced_operation("upload", lambda paths, *a, **k: ", ".join(Path(p).name for p in paths))
async def upload_files(
    paths: list[str | Path],
    store: Store = Store(),
    replace: bool = False,
    column_mapping: dict[str, str | None] | None = None,
    purpose: str = DEFAULT_PURPOSE,
    plan_criteria: bool = True,
    screen: bool = True,
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
        screen: False skips the header screen (see `process_store`).

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

    # Copy in, keeping what we overwrite, so a failed copy or processing run can be undone.
    old_mapping = _saved_mapping(store)
    with tempfile.TemporaryDirectory() as backup:
        copied: list[str] = []
        try:
            for name in existing:
                _copy(store.raw / name, Path(backup) / name)
            for f in files:
                _copy(f, store.raw / f.name)
                copied.append(f.name)
            if column_mapping:
                store.column_mapping.write_text(json.dumps({**old_mapping, **column_mapping}, indent=2),
                                                encoding="utf-8")
            sources, planned, warnings = await process_store(store, purpose, plan_criteria, screen=screen)
        except Exception:
            # Undo only what this upload changed. Each step is best-effort, so one locked file
            # can't stop the rest from being restored.
            for name in copied:
                with contextlib.suppress(OSError):
                    if name in existing:
                        shutil.copy2(Path(backup) / name, store.raw / name)
                    else:
                        (store.raw / name).unlink(missing_ok=True)
            if column_mapping:
                with contextlib.suppress(OSError):
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


def _infos(registry) -> list[CriterionInfo]:
    return [
        CriterionInfo(
            id=c.id, name=c.name, description=c.description, type=c.type,
            fields=c.relevant_fields, courses=c.courses,
            suggested_weight=c.importance / 2 if c.type == "auto" else 1.0,
        )
        for c in (registry.get(cid) for cid in registry.ids())
    ]


def list_criteria(store: Store = Store()) -> list[CriterionInfo]:
    """The criteria a request can weight before drafting: the built-ins (with the fields
    criteria.json fitted to them) and the auto criteria planned from the data in general.
    Drafting plans criteria for the specific instructions instead (see `draft_request`)."""
    return _infos(_registry(store))


def _request_registry(request: HiringRequest, store: Store):
    """The registry a request ranks with: its own catalog if it has one, else the store's."""
    if request.catalog is not None:
        return with_catalog(default_registry(), request.catalog)[0]
    return _registry(store, use_catalog=request.auto_criteria)


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

def field_kinds(store: Store) -> dict[str, str]:
    """Each store field's kind for the Manager: "free text" (varied written answers),
    "category" (a few repeated values) or "number" (planning.profile_fields). Measured from
    the data; no values leave this function."""
    records = load_store(store.candidates)
    return {p.field: "free text" if p.kind == "text" else p.kind
            for p in profile_fields(records, exclude=set(), labels=load_labels(store))}


PURPOSE_DOC_CHARS = 3000   # of each document, in the purpose the Criteria Planner gets


def request_purpose(brief: RequestBrief, documents: list[dict[str, str]]) -> str:
    """What the Criteria Planner plans this request's criteria for: the client's own words
    and the start of each document (already scrubbed)."""
    parts = [f"Role: {brief.role}"] if brief.role.strip() else []
    if brief.instructions.strip():
        parts.append(f"Client's instructions: {brief.instructions.strip()}")
    parts += [f"From {d['name']}: {d['text'][:PURPOSE_DOC_CHARS]}" for d in documents]
    return "\n\n".join(parts)


@traced_operation("draft", lambda brief, *a, **k: brief.role or brief.instructions[:120])
async def draft_request(brief: RequestBrief, store: Store = Store()) -> RequestDraft:
    """Plan criteria for these instructions, then draft a request with the Manager.

    1. The Criteria Planner designs the criteria for THIS request: the client's instructions
       and documents are its purpose, so "Python matters most, Arduino is a plus" can split
       Python into its own criterion. Cached per instructions and fields, so redrafting the
       same text makes no new planner call. If planning fails, the store's general criteria
       (criteria.json) are used, with a warning.
    2. The Manager weights those criteria (only ones with fields) and writes instructions for
       each ranker.

    The draft's request carries its criteria (`request.catalog`), so ranking it, now or from a
    saved file, uses exactly these criteria. Review and edit it before ranking.

    Raises:
        ServiceError: If there is no candidate data yet, a document isn't in the store or has
            no text, or the text points to no criterion.
        RuntimeError: If the Manager call fails (a server-side error).
    """
    if not store.candidates.exists():
        raise ServiceError("No candidate data yet; upload some first.")
    try:
        documents, warnings = load_context(brief.documents, store.documents)
        registry = default_registry()
        base = [registry.get(cid) for cid in registry.ids()]
        catalog, plan_warnings = await plan_catalog(load_store(store.candidates), base, request_purpose(brief, documents),
                                                    load_labels(store), store.plan_cache)
        if catalog is None:
            catalog = load_catalog(store.criteria)
            plan_warnings.append("Couldn't plan criteria for these instructions, so the data's general "
                                 "criteria are used.")
        if catalog:
            registry = with_catalog(registry, catalog)[0]
        # Criteria with no fields in this data can't rank anyone, so they aren't offered.
        offered = [c for c in (registry.get(cid) for cid in registry.ids()) if c.relevant_fields]
        draft = await manager_draft(brief, offered, documents, warnings + plan_warnings, field_kinds(store))
    except ValueError as e:     # DocumentError is a ValueError too
        raise ServiceError(str(e)) from e
    request = draft.request.model_copy(update={"catalog": catalog, "auto_criteria": False})
    return draft.model_copy(update={"request": request,
                                    "criteria": [i for i in _infos(registry) if i.fields]})


# --- Job spreadsheets ---

async def create_job(brief: RequestBrief, store: Store = Store(), revise: bool = True) -> JobResult:
    """Design a spreadsheet for one job and fill it from the store (see jobs.py).

    Two LLM calls (Manager designs the columns, Data Agent maps the data onto them), four if
    the Data Agent reports gaps and `revise` is on. Saved under the store's jobs/<id>/.

    Raises:
        ServiceError: If there is no candidate data yet, a document isn't in the store or has
            no text, or the design has no usable column.
        RuntimeError: If an agent call fails (a server-side error).
    """
    if not store.candidates.exists():
        raise ServiceError("No candidate data yet; upload some first.")
    try:
        documents, warnings = load_context(brief.documents, store.documents)
        return await jobs.create_job(brief, store, documents, warnings, revise)
    except ValueError as e:     # DocumentError and an empty design are ValueErrors too
        raise ServiceError(str(e)) from e


def list_jobs(store: Store = Store()) -> list[JobInfo]:
    """The store's job spreadsheets, newest first."""
    return jobs.list_jobs(store)


def get_job(job_id: str, store: Store = Store()) -> JobResult:
    """One job's design, mapping and column summary.

    Raises:
        ServiceError: For an unknown job id or a file that can't be read.
    """
    try:
        return jobs.load_job(job_id, store)
    except jobs.JobError as e:
        raise ServiceError(str(e)) from e


async def reapply_job(job_id: str, store: Store = Store()) -> JobResult:
    """Rebuild a job's table from its schema.json and mapping.json (hand edits included) and
    its data (the store's current data, or an imported job's own rows). No LLM call.

    Raises:
        ServiceError: For an unknown job, an invalid edited file, or no candidate data.
    """
    try:
        return await jobs.reapply_job(job_id, store)
    except jobs.JobError as e:
        raise ServiceError(str(e)) from e


def export_job(job_id: str, store: Store = Store()) -> Path:
    """The job as a job workbook (.xlsx: the table with headers grouped by criterion, plus its
    design), written to the job's folder. Holds real IDs: keep it local.

    Raises:
        ServiceError: For an unknown job.
    """
    try:
        return jobs.export_job(job_id, store)
    except jobs.JobError as e:
        raise ServiceError(str(e)) from e


async def import_job(path: str | Path, store: Store = Store()) -> JobResult:
    """Make a job from a job workbook (an exported one, possibly edited), without redesigning
    or re-mapping it. The column labels still go through the header screen.

    Raises:
        ServiceError: If the file isn't a usable job workbook (the message says why).
        ScreeningError: If the header screen call fails.
    """
    try:
        return await jobs.import_job(Path(path), store)
    except jobs.JobError as e:
        raise ServiceError(str(e)) from e


# --- Recorded runs (agent traces) ---

def list_traces(log_dir: Path | None = None) -> list[dict]:
    """Trace files written with AGENT_TRACE=ON, newest first, each with its operations
    (process, draft, rank, job create, ...). See agent_trace.py."""
    return agent_trace.list_traces(log_dir)


def get_trace(name: str, log_dir: Path | None = None) -> list[dict]:
    """Every event of one trace file: operations, code steps and agent calls (with prompts,
    inputs and outputs; pseudonymized, never real IDs).

    Raises:
        ServiceError: For a name that isn't a trace file.
    """
    try:
        return agent_trace.read_trace(name, log_dir)
    except FileNotFoundError as e:
        raise ServiceError(str(e)) from e


# --- Ranking ---

def validate_request(request: HiringRequest, store: Store = Store()) -> None:
    """Check a request's weights before any LLM call.

    Raises:
        ServiceError: For an unknown criterion id (usually a typo) in the weights or the
            instructions, a negative weight, or no weight above 0. Auto criteria count as known only with `auto_criteria` on.
    """
    known = set(_request_registry(request, store).ids())
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
