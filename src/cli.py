"""Command line for the hiring system: the first client of the service layer (hiring_agents/service.py).

Usage (from the repo root):
    python src/cli.py upload survey1.csv more_files/      # add files to the store and process it
    python src/cli.py upload new.xlsx --replace --map mapping.json
    python src/cli.py process [--rebuild-criteria]        # reprocess the files already in the store
    python src/cli.py status                              # files, candidates, needs processing?
    python src/cli.py criteria                            # criteria you can weight, with ids
    python src/cli.py rank --role "TA for ENGG 233" --top 5 \\
        --weight teaching_experience=0.4 --weight technical_skills=0.6
    python src/cli.py rank --request request.json        # a saved HiringRequest (flags override it)
    python src/cli.py draft "TA for ENGG 233. Python matters most; teaching experience helps. Top 8."
    python src/cli.py draft "..." --save request.json    # review/edit the file, then rank --request it
    python src/cli.py draft "..." --rank                 # draft and rank straight away
    python src/cli.py documents add outline.pdf          # course outline / job description for the Manager
    python src/cli.py draft "TA for ENGG 233" --doc outline.pdf   # (--doc also takes a file path)
    python src/cli.py documents list | remove outline.pdf

`upload` and `process` make one Criteria Planner call when the data's fields change
(--no-criteria skips it); `draft` makes one Manager call (free text -> weights and
instructions per ranker); `rank` makes one LLM call per criterion that runs. Results are
written to results/ as JSON plus an HTML page, which opens in the browser (--no-open to skip).
Needs OPENAI_API_KEY in environment_vars.env. Logging: LOG_FILE_LEVEL / LOG_CONSOLE_LEVEL.
"""

import argparse
import asyncio
import json
import sys
import webbrowser
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from hiring_agents.logging_config import setup_logging
from hiring_agents.schemas import DocumentInfo, HiringRequest, IntakeReport, RequestBrief
from hiring_agents.service import (
    DEFAULT_PURPOSE, ServiceError, data_status, delete_document, draft_request, list_criteria,
    list_documents, process_store, rank, save_result, upload_documents, upload_files,
)

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / "environment_vars.env")


def _print_sources(sources: dict[str, IntakeReport]) -> None:
    # Column names only, so this is safe to print.
    for label, report in sources.items():
        print(f"{label}: {len(report.column_mapping)} columns kept, "
              f"{len(report.dropped_sensitive)} sensitive dropped")


def _print_warnings(warnings: list[str]) -> None:
    for w in warnings:
        print(f"Warning: {w}")


def _print_status() -> None:
    s = data_status()
    print(f"Files ({len(s.files)}): {', '.join(s.files) or 'none'}")
    print(f"Candidates: {s.candidates}" + (f" (processed {s.compiled_at})" if s.compiled_at else ""))
    print(f"Criteria planned: {'yes' if s.criteria_planned else 'no'}")
    if s.stale:
        print("Files changed since the last processing: run `python src/cli.py process`.")


async def cmd_upload(args) -> None:
    mapping = json.loads(args.map.read_text(encoding="utf-8")) if args.map else None
    result = await upload_files(args.files, replace=args.replace, column_mapping=mapping,
                                purpose=args.purpose, plan_criteria=not args.no_criteria)
    print(f"Added: {result.added or 'none'}; replaced: {result.replaced or 'none'}")
    _print_sources(result.sources)
    _print_warnings(result.warnings)
    print(f"Criteria {'planned' if result.criteria_planned else 'unchanged'}.\n")
    _print_status()


async def cmd_process(args) -> None:
    sources, planned, warnings = await process_store(purpose=args.purpose, plan_criteria=not args.no_criteria,
                                                     rebuild_criteria=args.rebuild_criteria)
    _print_sources(sources)
    _print_warnings(warnings)
    print(f"Criteria {'planned' if planned else 'unchanged'}.\n")
    _print_status()


async def cmd_status(args) -> None:
    _print_status()


async def cmd_criteria(args) -> None:
    criteria = list_criteria()
    print(f"{'id':40} {'type':10} {'suggested':>9}  fields")
    for c in criteria:
        scope = f" [{', '.join(c.courses)}]" if c.courses else ""
        print(f"{c.id:40} {c.type:10} {c.suggested_weight:>9.2f}  {len(c.fields)}{scope}  {c.name}")
    print("\nUse the ids with --weight id=value. Auto criteria not given a weight join "
          "automatically when they apply to the role (unless --no-auto).")


def _parse_weights(items: list[str]) -> dict[str, float]:
    weights = {}
    for item in items:
        cid, sep, value = item.partition("=")
        try:
            if not sep:
                raise ValueError
            weights[cid.strip()] = float(value)
        except ValueError:
            raise ServiceError(f"Bad --weight {item!r}; use id=number, e.g. teaching_experience=0.4") from None
    return weights


def _print_documents(docs: list[DocumentInfo]) -> None:
    if not docs:
        print("No documents in the store.")
    for d in docs:
        note = d.problem or f"{d.chars:,} characters" + (" (only the start is read)" if d.truncated else "")
        print(f"  {d.name}: {note}")


async def cmd_documents(args) -> None:
    if args.action == "add":
        if not args.files:
            raise ServiceError("Give the documents to add.")
        result = upload_documents(args.files, replace=args.replace)
        print(f"Added: {result.added or 'none'}; replaced: {result.replaced or 'none'}")
        _print_documents(result.documents)
    elif args.action == "remove":
        if len(args.files) != 1:
            raise ServiceError("Give one document name to remove.")
        _print_documents(delete_document(args.files[0].name))
    else:
        _print_documents(list_documents())


def _document_names(items: list[str]) -> list[str]:
    """Store names for --doc values; a value that is a file path is added to the store first."""
    names = []
    for item in items:
        path = Path(item)
        if path.is_file():
            upload_documents([path], replace=True)
        names.append(path.name)
    return names


async def cmd_draft(args) -> None:
    if not args.instructions.strip() and not args.doc:
        raise ServiceError('Give instructions, --doc, or both, e.g. draft "TA for ENGG 233" --doc outline.pdf')
    brief = RequestBrief(instructions=args.instructions, documents=_document_names(args.doc),
                         role=args.role or "", top_x=args.top)
    draft = await draft_request(brief)
    req = draft.request
    print(f"Role: {req.role}\nTop: {req.top_x}")
    if req.documents:
        print(f"Documents: {', '.join(req.documents)}")
    if req.notes:
        print(f"Notes for every ranker: {req.notes}")
    print("\nWeights:")
    for cid, w in sorted(req.criteria.items(), key=lambda kv: -kv[1]):
        print(f"  {w:>4.1f}  {cid:40} {draft.reasons.get(cid, '')}")
        if cid in req.instructions:
            print(f"        instructions: {req.instructions[cid]}")
    if req.sessions:
        print("\nSessions to cover (for the Scheduler; not used for ranking yet):")
        for s in req.sessions:
            print(f"  {s.label}: {s.day} {s.start}-{s.end}")
    for item in draft.not_covered:
        print(f"Not covered: {item}")
    _print_warnings(draft.warnings)
    if args.save:
        args.save.write_text(req.model_dump_json(indent=2), encoding="utf-8")
        print(f"\nSaved to {args.save}. Edit it if needed, then: python src/cli.py rank --request {args.save}")
    if args.rank:
        print()
        await _run_ranking(req, args)


async def cmd_rank(args) -> None:
    base = json.loads(args.request.read_text(encoding="utf-8")) if args.request else {}
    if args.role:
        base["role"] = args.role
    if args.weight:
        base["criteria"] = _parse_weights(args.weight)
    if args.top is not None:
        base["top_x"] = args.top
    if args.notes is not None:
        base["notes"] = args.notes
    if args.no_auto:
        base["auto_criteria"] = False
    if "role" not in base or "criteria" not in base:
        raise ServiceError("Give --role and at least one --weight (or --request). "
                           "See the criterion ids with `python src/cli.py criteria`.")
    try:
        request = HiringRequest.model_validate(base)
    except ValidationError as e:
        raise ServiceError(f"Invalid request: {e}") from None
    await _run_ranking(request, args)


async def _run_ranking(request: HiringRequest, args) -> None:
    result = await rank(request)
    final = result.ranking
    print(f"\n{final.role}  (top {request.top_x})\n")
    for i, c in enumerate(final.ranking, 1):
        # Real IDs are shown only here, on our side, after all LLM calls.
        print(f"{i}. {result.candidate_ids[c.candidate_ref]}  score={c.total_score:.2f}  {c.per_criterion}")
    for ex in final.excluded:
        print(f"Excluded {result.candidate_ids[ex.candidate_ref]}: {ex.reason}")
    print("\nCriteria that ran:")
    for c in final.criteria:
        print(f"  [{c.source}] {c.id} ({c.weight:.0%})")
    _print_warnings(final.warnings)

    json_path, html_path = save_result(result, args.out)
    print(f"\nResults: {json_path}\n         {html_path}")
    if not args.no_open:
        webbrowser.open(html_path.as_uri())


def main() -> int:
    parser = argparse.ArgumentParser(description="Rank candidates from the local candidate store.")
    sub = parser.add_subparsers(dest="command", required=True)

    def planning_flags(p):
        p.add_argument("--purpose", default=DEFAULT_PURPOSE, help="what the hiring is for, given to the planner")
        p.add_argument("--no-criteria", action="store_true", help="skip criteria planning (no LLM call)")

    p = sub.add_parser("upload", help="add data files to the store and process it")
    p.add_argument("files", nargs="+", type=Path, help="Excel / CSV / JSON files or folders")
    p.add_argument("--replace", action="store_true", help="overwrite files with the same name")
    p.add_argument("--map", type=Path, help="JSON of source column -> standard field (or null), saved with the store")
    planning_flags(p)
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("process", help="reprocess the files already in the store")
    p.add_argument("--rebuild-criteria", action="store_true", help="plan the criteria again")
    planning_flags(p)
    p.set_defaults(func=cmd_process)

    sub.add_parser("status", help="show what's in the store").set_defaults(func=cmd_status)
    sub.add_parser("criteria", help="list the criteria you can weight").set_defaults(func=cmd_criteria)

    p = sub.add_parser("rank", help="rank the store's candidates")
    p.add_argument("--role", help='e.g. "TA for ENGG 233, Fall 2026"')
    p.add_argument("--weight", action="append", default=[], metavar="ID=W",
                   help="criterion weight; repeat for each criterion (relative: 2/1 = 0.66/0.33)")
    p.add_argument("--top", type=int, help="number of candidates to return (default 5)")
    p.add_argument("--notes", help="extra instructions for every ranker")
    p.add_argument("--no-auto", action="store_true", help="don't let auto criteria join")
    p.add_argument("--request", type=Path, help="JSON HiringRequest; flags override its values")
    p.add_argument("--out", type=Path, default=REPO / "results", help="results folder")
    p.add_argument("--no-open", action="store_true", help="don't open the results page")
    p.set_defaults(func=cmd_rank)

    p = sub.add_parser("draft", help="turn free-text instructions into a request (Manager agent)")
    p.add_argument("instructions", nargs="?", default="", help="what you're looking for, in your own words")
    p.add_argument("--doc", action="append", default=[], metavar="NAME_OR_PATH",
                   help="a document for the Manager (course outline, job description); repeatable")
    p.add_argument("--role", help="set the role yourself instead of letting the Manager write it")
    p.add_argument("--top", type=int, help="number of candidates (overrides the text)")
    p.add_argument("--save", type=Path, help="write the drafted HiringRequest to this JSON file")
    p.add_argument("--rank", action="store_true", help="rank with the draft straight away")
    p.add_argument("--out", type=Path, default=REPO / "results", help="results folder (with --rank)")
    p.add_argument("--no-open", action="store_true", help="don't open the results page (with --rank)")
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("documents", help="course outlines / job descriptions for the Manager")
    p.add_argument("action", choices=["list", "add", "remove"])
    p.add_argument("files", nargs="*", type=Path, help="files to add (.pdf, .docx, .txt, .md), or the name to remove")
    p.add_argument("--replace", action="store_true", help="overwrite documents with the same name")
    p.set_defaults(func=cmd_documents)

    args = parser.parse_args()
    log_path = setup_logging(REPO / "logs")
    try:
        asyncio.run(args.func(args))
    except (ServiceError, ValidationError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    if log_path:
        print(f"Log: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
