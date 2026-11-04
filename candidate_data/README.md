# Candidate data

Real applicant data lives here. Everything in this folder except this README is gitignored. Never commit it.

1. Put the data files in `raw/`: Excel (`.xlsx`/`.xlsm`), `.csv` or `.json`. Files can hold different candidates, different information about the same candidates (e.g. applications in one file, TA reviews in another), or both. Every sheet is read.
2. From the repo root, run `python src/compile_candidates.py`.
3. That writes `candidates.json` here: one record per candidate, merged by ID, using the standard field names. Sensitive columns (names, contact info, protected characteristics) are dropped. The real ID is kept, so this file is still identifying.
4. It then plans `criteria.json` here: the criteria the data can be ranked on (one LLM call that sees column names and shared category values only, not answers). Edit it freely; recompiling keeps it while the fields stay the same.
5. Optional: course outlines or job descriptions for the Manager go in `documents/` (`python src/cli.py documents add outline.pdf`, or the web page). They hold no candidate data but may name staff; emails, phone numbers and ID numbers are removed before the Manager reads them.
6. `python src/main.py` (or `rank_candidates(request)` with no data argument) now ranks from `candidates.json`.

The compile step prints which column went to which field. If a column isn't recognized, add a mapping with `--map mapping.json` (e.g. `{"Stu #": "candidate_id", "Internal notes": null}`) or add an alias in `src/hiring_agents/intake.py`. See `src/hiring_agents/datastore.py` for the merge rules.
