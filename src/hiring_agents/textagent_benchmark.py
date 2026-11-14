"""Standalone model benchmark; production extraction lives in textagent.py."""

import argparse
import asyncio
import json
import math
import statistics
import sys
from pathlib import Path
from time import perf_counter

import pandas as pd
from dotenv import load_dotenv

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "hiring_agents"

from .textagent import _prepare_rows, extract_keywords


def score_keywords(actual: dict, expected: dict) -> dict[str, float | int]:
    """Micro precision/recall/F1 of (UCID, header, normalized phrase) matches.

    Matching ignores case and whitespace, but does NOT recognize synonyms.
    An empty actual and empty reference score 1; missed/extra terms score 0.
    """
    def phrases(data):
        return {(ucid, header, " ".join(term.casefold().split()))
                for ucid, answers in data.items() for header, terms in answers.items()
                for term in terms if term.strip()}

    predicted, reference = phrases(actual), phrases(expected)
    matched = len(predicted & reference)
    precision = matched / len(predicted) if predicted else float(not reference)
    recall = matched / len(reference) if reference else float(not predicted)
    return {
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "matched": matched,
        "extra": len(predicted - reference),
        "missed": len(reference - predicted),
    }


async def benchmark_models(
    df: pd.DataFrame, models: list[str], *, repeats: int = 3,
    id_column: str = "UCID", expected: dict | None = None,
    reasoning_effort: str | None = None,
    batch_size: int = 1,
) -> dict:
    """Compare models sequentially using identical inputs and rotated run order.

    Records failures without stopping other models. All run outputs are retained.
    No automatic quality claim is made unless a human-reviewed reference is supplied.
    """
    headers, rows = _prepare_rows(df, id_column)
    if not rows:
        raise ValueError("Benchmark input must contain at least one candidate.")
    if not models or any(not m.strip() for m in models) or len(set(models)) != len(models):
        raise ValueError("Provide distinct, nonblank model IDs.")
    if repeats < 1:
        raise ValueError("repeats must be at least 1.")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1.")
    if expected is not None:
        # Require labels for precisely the selected subset, including empty categories.
        if not isinstance(expected, dict) or set(expected) != set(rows):
            raise ValueError("Expected JSON must contain exactly the selected UCIDs.")
        for answers in expected.values():
            if not isinstance(answers, dict) or set(answers) != set(headers):
                raise ValueError("Expected JSON must contain every original answer header.")
            if any(not isinstance(terms, list) or any(not isinstance(t, str) for t in terms)
                   for terms in answers.values()):
                raise ValueError("Expected keywords must be lists of strings.")

    report = {
        "candidates": len(rows),
        "nonblank_candidates": sum(any(a.values()) for a in rows.values()),
        "repeats": repeats,
        "batch_size": batch_size,
        "reasoning_effort": reasoning_effort or "model default",
        "quality_metric": "case/whitespace-normalized exact phrase matching" if expected is not None else None,
        "models": {model: {"runs": []} for model in models},
    }
    for repeat in range(repeats):
        offset = repeat % len(models)
        for model in models[offset:] + models[:offset]:
            print(f"Benchmark: {model}, run {repeat + 1}/{repeats}", file=sys.stderr)
            timings = []
            started = perf_counter()
            try:
                keywords = await extract_keywords(
                    df, id_column=id_column, model=model, request_seconds=timings,
                    reasoning_effort=reasoning_effort,
                    batch_size=batch_size,
                )
            except Exception as error:
                run = {"repeat": repeat + 1, "status": "error",
                       "elapsed_seconds": perf_counter() - started,
                       "error": f"{type(error).__name__}: {error}"}
            else:
                run = {"repeat": repeat + 1, "status": "ok",
                       "elapsed_seconds": perf_counter() - started,
                       "request_seconds": timings, "keywords": keywords}
                if expected is not None:
                    run["quality"] = score_keywords(keywords, expected)
            report["models"][model]["runs"].append(run)

    for entry in report["models"].values():
        successful = [r for r in entry["runs"] if r["status"] == "ok"]
        latencies = sorted(t for run in successful for t in run["request_seconds"])
        entry["summary"] = {
            "successful_runs": len(successful),
            "failed_runs": repeats - len(successful),
            "median_dataset_seconds": statistics.median(r["elapsed_seconds"] for r in successful) if successful else None,
            "median_request_seconds": statistics.median(latencies) if latencies else None,
            "p95_request_seconds": latencies[math.ceil(0.95 * len(latencies)) - 1] if latencies else None,
            "mean_f1": statistics.mean(r["quality"]["f1"] for r in successful) if successful and expected is not None else None,
        }
    return report


def main() -> None:
    """Read a single-header CSV and print the extracted dictionary as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path, help="CSV containing UCID and answer columns")
    parser.add_argument("--id-column", default="UCID")
    parser.add_argument("--model", help="Override OPENAI_MODEL / the SDK default")
    parser.add_argument("--benchmark-models", nargs="+", metavar="MODEL", help="Compare these OpenAI model IDs")
    parser.add_argument("--repeats", type=int, default=3, help="Runs per benchmark model (default: 3)")
    parser.add_argument("--limit", type=int, help="Use only the first N candidates")
    parser.add_argument("--batch-size", type=int, default=1,
                        help="Candidates per concurrent group (default: 1, sequential)")
    parser.add_argument("--expected", type=Path, help="Human-reviewed reference JSON for benchmark quality scoring")
    parser.add_argument("--reasoning-effort", choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"],
                        help="Explicit effort level; must be supported by every selected model")
    parser.add_argument("--output", type=Path, help="Also save JSON to a new file")
    args = parser.parse_args()
    if args.model and args.benchmark_models:
        parser.error("Use either --model or --benchmark-models.")
    if args.repeats < 1 or (args.limit is not None and args.limit < 1):
        parser.error("--repeats and --limit must be positive.")
    if args.batch_size < 1:
        parser.error("--batch-size must be positive.")
    if args.expected and not args.benchmark_models:
        parser.error("--expected requires --benchmark-models.")
    if args.output and args.output.exists():
        parser.error(f"Output file already exists: {args.output}")
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env")
    load_dotenv(root / "environment_vars.env")
    df = pd.read_csv(args.csv_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    if args.limit is not None:
        df = df.head(args.limit)
    if args.benchmark_models:
        expected = json.loads(args.expected.read_text(encoding="utf-8")) if args.expected else None
        data = asyncio.run(benchmark_models(
            df, args.benchmark_models, repeats=args.repeats,
            id_column=args.id_column, expected=expected,
            reasoning_effort=args.reasoning_effort,
            batch_size=args.batch_size,
        ))
    else:
        data = asyncio.run(extract_keywords(
            df, id_column=args.id_column, model=args.model, reasoning_effort=args.reasoning_effort,
            batch_size=args.batch_size,
        ))
    output = json.dumps(data, ensure_ascii=False, indent=2)
    if args.output:
        with args.output.open("x", encoding="utf-8") as destination:
            destination.write(output + "\n")
    print(output)


if __name__ == "__main__":
    main()
