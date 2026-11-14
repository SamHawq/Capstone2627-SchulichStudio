"""REST API over the service layer, plus the local web UI (static/index.html).

Each endpoint wraps one service.py function; request and response bodies are the same
Pydantic models, so the OpenAPI docs at /docs describe the contract a client (the web UI now,
the Schulich Studio website later) codes against.

    GET  /api/data            data_status
    POST /api/data/files      upload_files     (multipart: files, replace, plan_criteria, purpose)
    POST /api/data/process    process_store
    GET  /api/criteria        list_criteria
    GET  /api/documents       list_documents
    POST /api/documents       upload_documents (multipart: files, replace)
    DELETE /api/documents/{name}  delete_document
    POST /api/requests/draft  draft_request    (body: RequestBrief; free text + documents -> RequestDraft)
    POST /api/jobs            create_job       (body: RequestBrief; ?revise=false for no revision round)
    GET  /api/jobs            list_jobs
    GET  /api/jobs/{id}       get_job
    POST /api/jobs/{id}/apply reapply_job      (rebuild the table from the edited files; no LLM call)
    GET  /api/jobs/{id}/export export_job      (the job as an .xlsx workbook: table + design)
    POST /api/jobs/import     import_job       (multipart: file; a job workbook, no redesign)
    POST /api/rankings        rank             (body: HiringRequest)
    GET  /api/rankings/latest                  latest completed ranking (no model calls)
    GET  /api/traces          list_traces      (recorded runs; AGENT_TRACE=ON)
    GET  /api/traces/{name}   get_trace        (one run's events, for the /runs page)
    GET  /runs                the run viewer page (static/runs.html)
    GET  /results/{name}      a saved HTML results page

Errors: a `ServiceError` (bad input) becomes 400 with {"detail": message}.

Local use only for now: there is no authentication, and responses hold real candidate IDs,
so the server binds to 127.0.0.1 (see src/server.py). Rankings run synchronously and take
tens of seconds; a job-style endpoint (start, then poll) is the plan for the hosted API.
"""

import asyncio
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from .datastore import Store
from .schemas import (
    CriterionInfo, DataStatus, DocumentInfo, DocumentsResult, HiringRequest, IntakeReport, JobInfo, JobResult,
    RankingResult, RequestBrief, RequestDraft, UploadResult,
)
from .service import (
    DEFAULT_PURPOSE, ServiceError, create_job, data_status, delete_document, draft_request, export_job,
    get_job, get_trace, import_job, list_criteria, list_documents, list_jobs, list_traces, process_store, rank, reapply_job,
    save_result, upload_documents, upload_files,
)

STATIC = Path(__file__).parent / "static"


class ProcessRequest(BaseModel):
    purpose: str = DEFAULT_PURPOSE
    plan_criteria: bool = True
    rebuild_criteria: bool = False


class ProcessResult(BaseModel):
    sources: dict[str, IntakeReport]
    criteria_planned: bool
    warnings: list[str]
    status: DataStatus


class RankingResponse(BaseModel):
    result: RankingResult
    report_url: str


def create_app(store: Store | None = None, results_dir: Path | None = None,
               trace_dir: Path | None = None) -> FastAPI:
    """Build the app for one store. Results pages are saved to `results_dir`; recorded runs
    are read from `trace_dir` (default logs/)."""
    store = store or Store()
    results_dir = results_dir or Path(__file__).resolve().parents[2] / "results"
    write_lock = asyncio.Lock()   # one upload/processing run at a time
    app = FastAPI(title="Hiring Agents", description=__doc__)

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, e: ServiceError):
        return JSONResponse(status_code=400, content={"detail": str(e)})

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/runs", response_class=HTMLResponse, include_in_schema=False)
    async def runs_page():
        return (STATIC / "runs.html").read_text(encoding="utf-8")

    @app.get("/api/traces")
    async def get_traces():
        return list_traces(trace_dir)

    @app.get("/api/traces/{name}")
    async def get_one_trace(name: str):
        return get_trace(name, trace_dir)

    @app.get("/api/data", response_model=DataStatus)
    async def get_data():
        return data_status(store)

    async def save_uploads(files: list[UploadFile], tmp: str) -> list[Path]:
        paths = []
        for f in files:
            path = Path(tmp) / Path(f.filename or "upload").name   # no directory parts
            path.write_bytes(await f.read())
            paths.append(path)
        return paths

    @app.post("/api/data/files", response_model=UploadResult)
    async def post_files(
        files: list[UploadFile] = File(...),
        replace: bool = Form(False),
        plan_criteria: bool = Form(True),
        purpose: str = Form(DEFAULT_PURPOSE),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            paths = await save_uploads(files, tmp)
            async with write_lock:
                return await upload_files(paths, store, replace=replace, purpose=purpose,
                                          plan_criteria=plan_criteria)

    @app.post("/api/data/process", response_model=ProcessResult)
    async def post_process(body: ProcessRequest):
        async with write_lock:
            sources, planned, warnings = await process_store(store, body.purpose, body.plan_criteria,
                                                             body.rebuild_criteria)
        return ProcessResult(sources=sources, criteria_planned=planned, warnings=warnings,
                             status=data_status(store))

    @app.get("/api/criteria", response_model=list[CriterionInfo])
    async def get_criteria():
        return list_criteria(store)

    @app.get("/api/documents", response_model=list[DocumentInfo])
    async def get_documents():
        return list_documents(store)

    @app.post("/api/documents", response_model=DocumentsResult)
    async def post_documents(files: list[UploadFile] = File(...), replace: bool = Form(False)):
        with tempfile.TemporaryDirectory() as tmp:
            paths = await save_uploads(files, tmp)
            async with write_lock:
                return upload_documents(paths, store, replace)

    @app.delete("/api/documents/{name}", response_model=list[DocumentInfo])
    async def remove_doc(name: str):
        async with write_lock:
            return delete_document(name, store)

    @app.post("/api/requests/draft", response_model=RequestDraft)
    async def post_draft(brief: RequestBrief):
        return await draft_request(brief, store)

    @app.post("/api/jobs", response_model=JobResult)
    async def post_job(brief: RequestBrief, revise: bool = True):
        async with write_lock:
            return await create_job(brief, store, revise)

    @app.get("/api/jobs", response_model=list[JobInfo])
    async def get_jobs():
        return list_jobs(store)

    @app.post("/api/jobs/import", response_model=JobResult)
    async def post_job_import(file: UploadFile = File(...)):
        with tempfile.TemporaryDirectory() as tmp:
            [path] = await save_uploads([file], tmp)
            async with write_lock:
                return await import_job(path, store)

    @app.get("/api/jobs/{job_id}/export", include_in_schema=True)
    async def get_job_export(job_id: str):
        path = export_job(job_id, store)
        return FileResponse(path, filename=path.name,
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/api/jobs/{job_id}", response_model=JobResult)
    async def get_one_job(job_id: str):
        return get_job(job_id, store)

    @app.post("/api/jobs/{job_id}/apply", response_model=JobResult)
    async def post_job_apply(job_id: str):
        async with write_lock:
            return await reapply_job(job_id, store)

    @app.post("/api/rankings", response_model=RankingResponse)
    async def post_ranking(request: HiringRequest):
        result = await rank(request, store)
        _, html_path = save_result(result, results_dir)
        return RankingResponse(result=result, report_url=f"/results/{html_path.name}")

    @app.get("/api/rankings/latest", response_model=RankingResponse | None)
    async def get_latest_ranking():
        """Reopen a completed ranking from either the GUI or CLI without model calls."""
        for path in sorted(results_dir.glob("results-*.json"), reverse=True):
            report = path.with_suffix(".html")
            if not report.is_file():
                continue  # A partially saved result is not a completed run.
            try:
                result = RankingResult.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue  # A malformed saved file must not hide earlier valid results.
            return RankingResponse(result=result, report_url=f"/results/{report.name}")
        return None

    @app.get("/results/{name}", include_in_schema=False)
    async def get_result_page(name: str):
        path = results_dir / Path(name).name
        if path.suffix != ".html" or not path.exists():
            raise HTTPException(status_code=404)
        return FileResponse(path)

    return app
