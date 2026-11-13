# Production keyword extraction

`jobs.py` now uses `textagent.py` in place of the old placeholder extraction tool.
Job creation, reapplication, and workbook import all follow the same path:

1. `build_sheet` selects free-text fields and replaces candidate IDs with fresh refs.
2. The sheet is saved in the job folder as `free_text.csv`.
3. `extract_csv` reads that file and calls the keyword extractor once per nonblank
   candidate, in concurrent groups of up to 15 by default.
4. `free_text_keywords.json` stores `{candidate_ref: {original_header: [keywords]}}`.
5. Code maps refs/headers back to the job's IDs/field keys. Free-text answers become
   keyword lists in `table.json`; `table.csv` displays lists separated by semicolons.

Original headers are kept. CSV list cells are joined with `; ` before extraction.
Only free-text fields change; numbers, categories, availability, and unrelated
fields in `other` are retained. Empty sheets skip model calls. Errors stop the
operation rather than silently sending unchanged answers to the next stage.
The raw CSV is retained. Keyword extraction is not a guaranteed redaction step.

Configure the production extractor in the environment (or the server's loaded env):

```env
TEXT_MODEL=gpt-5.6-luna
TEXT_BATCH_SIZE=15
TEXT_REASONING_EFFORT=none
```

These are the defaults; `OPENAI_MODEL` is also supported if `TEXT_MODEL` is absent.
The selected model must support the requested reasoning effort and structured
output. `OPENAI_API_KEY` is required for live calls.

To extract from an existing job CSV directly, from the project root:

```bash
.venv/bin/python src/hiring_agents/textagent.py \
  candidate_data/jobs/JOB_ID/free_text.csv \
  --output keywords-preview.json
```

The input must have `Candidate` as its identifier column. Output IDs remain those
pseudonymous refs. Standalone output files are not overwritten. The CLI loads
the repository's `.env` and `environment_vars.env`; the job pipeline uses the
environment already loaded by its entry point.

`text_extraction.py` only re-exports the handoff helpers for existing imports.
Benchmarking is separate in `textagent_benchmark.py`, which uses the same
`extract_keywords` implementation. See [benchmark usage](TEXTAGENT_BENCHMARK.md).

Offline verification:

```bash
.venv/bin/python -m unittest tests.test_textagent tests.test_jobs tests.test_trace
```
