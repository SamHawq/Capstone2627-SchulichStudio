# Compare keyword extraction models

Benchmarking lives in `textagent_benchmark.py`; `textagent.py` is the production
extractor used by `jobs.py`. Both use the same `extract_keywords()` function.

Keep the Agents SDK for the current OpenAI extraction pipeline. Benchmark the
models with the same prompt, schema, and rows. LangChain is an option if a unified
interface to several providers becomes a requirement; changing frameworks does
not establish which model extracts better keywords.

Run from the repository root with `OPENAI_API_KEY` set in `.env` or the shell:

```bash
.venv/bin/python src/hiring_agents/textagent_benchmark.py \
  "src/hiring_agents/sample_datacsv/Sample keyword extractor data(Sheet1).csv" \
  --benchmark-models gpt-5.6-luna gpt-6-luna \
  --reasoning-effort none \
  --limit 5 --repeats 3 --output benchmark-luna.json
```

Use model IDs available to your account that support structured output. The
current CLI uses the SDK's OpenAI provider; other providers require an adapter
and their own credentials. This command makes up to 30 row extractions (5 rows ×
2 models × 3 repetitions), with normal API charges. It does not make live calls
until you run it. Existing output files are never overwritten.

As of November 11, 2026, GPT-5.6 Luna and GPT-6 Luna are lightweight options for
their respective families. Both support reasoning effort `none`; the command
uses that explicit setting to compare speed consistently instead of relying on
SDK/model defaults. Try `low` separately if extraction quality is insufficient.
The report records the chosen effort. GPT-4.1 nano was shut down on October 23,
2026 and should not be used in new benchmarks.

Sources: [GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna),
[GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna),
[deprecations](https://developers.openai.com/api/docs/deprecations).

The JSON report includes each model's `summary` and every run's `keywords`:

- `median_request_seconds`: median time for a nonblank candidate's SDK run.
- `p95_request_seconds`: nearest-rank 95th percentile of those durations.
- `median_dataset_seconds`: median duration to extract the entire selected set.
- `successful_runs` / `failed_runs`: completed datasets versus failed attempts.
- `mean_f1`: quality against a reviewed reference, or null without one.

Requests are sequential by default, and model order rotates between repetitions. Blank rows
do not call the model. Timing includes SDK work, network latency, provider retries,
and any cache/schema initialization effects; there is no excluded warm-up. Request
latency summaries use completed datasets only. Failed attempts retain their
elapsed time and error. Small samples give rough p95 estimates. Repeat a larger
test before deciding, and compare reliability alongside speed.

## Test concurrent groups of 15

`--batch-size 15` runs up to 15 separate candidate requests concurrently. It waits
for the current group before starting the next one. A final smaller group is
processed normally. It preserves the UCID mapping and original row order even
when responses finish out of order. If a request fails, the current group drains
before the error is reported, and subsequent groups are not started.

Compare both models on the first 45 candidates, once per model:

```bash
.venv/bin/python src/hiring_agents/textagent_benchmark.py \
  "src/hiring_agents/sample_datacsv/Sample keyword extractor data(Sheet1).csv" \
  --benchmark-models gpt-5.6-luna gpt-6-luna \
  --reasoning-effort none --batch-size 15 \
  --limit 45 --repeats 1 --output benchmark-batch15.json
```

For a sequential baseline, run the same command with `--batch-size 1` and a new
output filename. Compare `median_dataset_seconds` for total elapsed time; with
one repeat this is simply that run's duration. Per-request medians measure each
individual request, so they do not directly measure total concurrent throughput.
The report records `batch_size`. Use 3 repeats for a less noisy comparison.

For production-style extraction with one model, use `--model gpt-5.6-luna`
instead of `--benchmark-models ...`, and omit benchmark-only options
`--repeats` and `--expected`. This writes the usual keyword dictionary instead
of a timing report. Remove `--limit` to process all candidates in the CSV.

This option controls concurrent regular requests; every candidate still has a
separate request. It does not combine 15 answers into one prompt or use OpenAI's
offline Batch API. Larger groups can hit request/token rate limits, so measured
speedup will depend on your account's limits and provider retries. See
[rate limits](https://developers.openai.com/api/docs/guides/rate-limits).

## Assess extraction quality

Select representative answers, including detailed examples, blank answers,
proficiency levels, and negations such as "no MATLAB experience". Write the
desired keywords yourself in `expected.json`, using exactly the same output
format: every selected UCID, every exact column header, and lists of phrases.
You can use an extraction output as a starting point, but review every phrase
against the source answers before treating it as a reference.

Add `--expected expected.json` to the benchmark command. When using `--limit`,
the reference must contain exactly that subset of candidates.

Precision measures how many returned phrases match the reference; recall measures
how many reference phrases were found; F1 balances both. Scoring uses exact phrase
matching after case/whitespace normalization, with UCID and header boundaries
preserved. Synonyms receive no credit, and unlisted but valid phrases count as
extras. Review outputs manually for equivalent wording, missing context, invented
claims, and lost negation. More keywords alone do not indicate better quality.

Choose a minimum acceptable quality level, then compare speed among models that
meet it. A fast model with frequent extraction failures should not win based on
successful-run latency alone.

Offline checks:

```bash
.venv/bin/python -m unittest tests.test_textagent -v
```
