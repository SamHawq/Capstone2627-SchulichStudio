"""Compile every candidate data file into the one standardized JSON the orchestrator reads.

Usage (from the repo root):
    python src/compile_candidates.py                       # everything in candidate_data/raw/
    python src/compile_candidates.py a.xlsx b.xlsx more/   # specific files and/or folders
    python src/compile_candidates.py --map mapping.json -o other.json
    python src/compile_candidates.py --purpose "Lab TA hiring" --rebuild-criteria
    python src/compile_candidates.py --no-criteria          # no LLM call

Reads every sheet of every .xlsx/.xlsm/.csv/.json file, maps the columns to the standard fields,
merges rows that share a candidate ID, and writes candidate_data/candidates.json. See
hiring_agents/datastore.py for the merge rules.

Then plans the criteria the data can be ranked on (one Criteria Planner call, see
hiring_agents/planning.py) and writes candidate_data/criteria.json. If the fields and purpose
are unchanged, the existing criteria.json is kept as is, including any hand edits; otherwise
the old one is kept as criteria.prev.json. Needs OPENAI_API_KEY in environment_vars.env.
Afterwards, `rank_candidates(request)` (and `python src/main.py`) use both files without
being given any data.

--map takes a JSON object of source column -> standard field (or null to drop), applied to
every file, for columns the built-in aliases don't recognize, e.g. {"Stu #": "candidate_id"}.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from hiring_agents.datastore import CATALOG_PATH, COMPILED_PATH, RAW_DIR, compile_sources, write_store
from hiring_agents.logging_config import setup_logging
from hiring_agents.planning import field_labels, refresh_catalog
from hiring_agents.registry import default_registry
from hiring_agents.service import DEFAULT_PURPOSE

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / "environment_vars.env")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile candidate data files into one JSON store.")
    parser.add_argument("inputs", nargs="*", type=Path, default=[RAW_DIR],
                        help=f"files or folders (default: {RAW_DIR})")
    parser.add_argument("-o", "--output", type=Path, default=COMPILED_PATH,
                        help=f"output path (default: {COMPILED_PATH})")
    parser.add_argument("--map", type=Path, help="JSON file with column overrides")
    parser.add_argument("--purpose", default=DEFAULT_PURPOSE,
                        help=f"what the hiring is for, given to the planner (default: {DEFAULT_PURPOSE!r})")
    parser.add_argument("--criteria", type=Path, default=CATALOG_PATH,
                        help=f"criteria output path (default: {CATALOG_PATH})")
    parser.add_argument("--rebuild-criteria", action="store_true",
                        help="plan the criteria again even if the fields haven't changed")
    parser.add_argument("--no-criteria", action="store_true", help="skip criteria planning")
    args = parser.parse_args()
    # Same LOG_FILE_LEVEL / LOG_CONSOLE_LEVEL switches as main.py (see hiring_agents/logging_config.py).
    log_path = setup_logging(REPO / "logs")
    if log_path:
        print(f"Log: {log_path}")

    mapping = json.loads(args.map.read_text(encoding="utf-8")) if args.map else None
    try:
        records, reports, warnings = compile_sources(args.inputs, column_mapping=mapping)
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Column names only, so this is safe to print.
    for label, report in reports.items():
        print(f"{label}")
        for col, fid in report.column_mapping.items():
            print(f"    {col!r:32} -> {fid}")
        if report.dropped_sensitive:
            print(f"    dropped (sensitive): {report.dropped_sensitive}")
        if report.dropped_unused:
            print(f"    dropped (mapped to null): {report.dropped_unused}")
    for w in warnings:
        print(f"Warning: {w}")

    out = write_store(records, args.output)
    print(f"\n{len(records)} candidates from {len(reports)} source(s) written to {out}")
    if not args.no_criteria:
        plan_criteria(records, list(reports.values()), args)
    return 0


def plan_criteria(records, reports, args) -> None:
    """Plan (or reuse) the store's criteria and print a summary. Field names only."""
    registry = default_registry()
    base = [registry.get(cid) for cid in registry.ids()]
    catalog, planned, warnings = asyncio.run(refresh_catalog(
        records, base, args.purpose, field_labels(reports), args.criteria, args.rebuild_criteria))
    for w in warnings:
        print(f"Warning: {w}")
    if catalog is None:
        print("No criteria planned; ranking will use the built-in criteria's own fields.")
        return
    print(f"\nCriteria {'planned' if planned else 'unchanged (same fields and purpose)'}: {args.criteria}")
    for e in catalog.extensions:
        print(f"  + {e.criterion_id}: {[n.field for n in e.fields]}")
    for c in catalog.criteria:
        scope = f" [{', '.join(c.courses)}]" if c.courses else ""
        print(f"  {c.id} (importance {c.importance}){scope}: {c.relevant_fields}")
    print(f"  {len(catalog.skipped)} field(s) skipped (reasons in the file)")


if __name__ == "__main__":
    sys.exit(main())
