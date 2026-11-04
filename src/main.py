"""Demo: rank the sample candidates for an ENGG 233 TA role.

Usage (from the repo root):
    python src/main.py                                   # compiled store, else the JSON sample
    python src/main.py src/data/sample_candidates_alt.csv  # a specific file, any column layout

With no argument it uses candidate_data/candidates.json (build it with
`python src/compile_candidates.py`), falling back to src/data/sample_candidates.json.

Makes real LLM calls (one per active criterion), so OPENAI_API_KEY must be set in
environment_vars.env at the repo root. Both sample files are fictional; intake maps either
column layout to the standard fields.
Also writes an HTML results page to results/ and opens it in the browser.
"""

import asyncio
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from hiring_agents.datastore import COMPILED_PATH
from hiring_agents.intake import load_records
from hiring_agents.logging_config import setup_logging
from hiring_agents.orchestrator import rank_candidates
from hiring_agents.report import render_html
from hiring_agents.schemas import HiringRequest

SRC = Path(__file__).parent
load_dotenv(SRC.parent / "environment_vars.env")  # env file lives at the repo root


async def main():
    """Build a sample HiringRequest, run the pipeline, and print a readable report."""
    # Controlled by LOG_FILE_LEVEL / LOG_CONSOLE_LEVEL (see hiring_agents/logging_config.py).
    log_path = setup_logging(SRC.parent / "logs")
    if len(sys.argv) > 1:
        data_path = Path(sys.argv[1])
    elif COMPILED_PATH.exists():
        data_path = None  # rank_candidates reads the compiled store itself
    else:
        data_path = SRC / "data" / "sample_candidates.json"
    print(f"Data: {data_path or COMPILED_PATH}")
    raw_candidates = load_records(data_path) if data_path else None
    request = HiringRequest(
        role="TA for ENGG 233 (Computing for Engineers), Fall 2026",
        criteria={"teaching_experience": 0.4, "subject_knowledge": 0.4, "past_performance": 0.2},
        top_x=10,
        notes="Prefer TAs who can explain programming concepts to first-year students.",
    )

    final, ref_map = await rank_candidates(request, raw_candidates)

    print(f"\n{final.role}\n")
    for i, c in enumerate(final.ranking, 1):
        # Re-identification happens only here, on our side, after the LLMs are done.
        print(f"{i}. UCID {ref_map[c.candidate_ref]}  score={c.total_score:.2f}  {c.per_criterion}")
        for cid, why in c.rationales.items():
            print(f"     - {cid}: {why}")
    for ex in final.excluded:
        print(f"Excluded UCID {ref_map[ex.candidate_ref]}: {ex.reason}")
    print("\nCriteria that ran:")
    for c in final.criteria:
        print(f"  [{c.source}] {c.id} ({c.weight:.0%}): {c.fields}")
    if final.skipped_fields:
        print(f"Fields not ranked on: {len(final.skipped_fields)} (reasons on the results page)")
    if final.warnings:
        print("\nWarnings:\n  " + "\n  ".join(final.warnings))
    if log_path:
        print(f"\nLog written to {log_path}")

    # HTML view of the same results; contains real IDs, so it goes in the gitignored results/.
    out_dir = SRC.parent / "results"
    out_dir.mkdir(exist_ok=True)
    report_path = out_dir / f"results-{datetime.now():%Y%m%d-%H%M%S}.html"
    report_path.write_text(render_html(final, ref_map, request), encoding="utf-8")
    print(f"Results page written to {report_path}")
    webbrowser.open(report_path.as_uri())


if __name__ == "__main__":
    asyncio.run(main())
