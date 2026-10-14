"""Demo: rank the sample candidates for an ENGG 233 TA role.

Usage (from the repo root):
    python src/main.py

Makes real LLM calls (one per active criterion), so OPENAI_API_KEY must be set in
environment_vars.env at the repo root. Uses fictional data from src/data/sample_candidates.json.
"""

import asyncio
import json
from pathlib import Path

from dotenv import load_dotenv

from hiring_agents.logging_config import setup_logging
from hiring_agents.orchestrator import rank_candidates
from hiring_agents.schemas import HiringRequest

SRC = Path(__file__).parent
load_dotenv(SRC.parent / "environment_vars.env")  # env file lives at the repo root


async def main():
    """Build a sample HiringRequest, run the pipeline, and print a readable report."""
    # Controlled by LOG_FILE_LEVEL / LOG_CONSOLE_LEVEL (see hiring_agents/logging_config.py).
    log_path = setup_logging(SRC.parent / "logs")
    raw_candidates = json.loads((SRC / "data" / "sample_candidates.json").read_text())
    request = HiringRequest(
        role="TA for ENGG 233 (Computing for Engineers), Fall 2026",
        criteria={"teaching_experience": 0.4, "subject_knowledge": 0.4, "past_performance": 0.2},
        top_x=3,
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
    if final.warnings:
        print("\nWarnings:\n  " + "\n  ".join(final.warnings))
    if log_path:
        print(f"\nLog written to {log_path}")


if __name__ == "__main__":
    asyncio.run(main())
