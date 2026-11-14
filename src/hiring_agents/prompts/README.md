# Agent prompts

Every LLM agent's instructions are a `.md` file in this folder. Edit a file and the next agent
call uses it (no restart needed). Dynamic parts are `{placeholders}` (lowercase letters and
underscores), filled in by code with `render(...)`. Other braces, such as a JSON example, are
left as written. Renaming or removing a placeholder makes `render` raise an error that names
it, so a typo can't slip through silently.

## How agents get their input

Agents never message each other. Plain Python code (the orchestrator) calls each agent once,
builds its input from data and earlier agents' outputs, validates its structured reply, and
decides what happens next. Each agent gets:

- **instructions**: the prompt file below, filled in;
- **input**: one JSON message, built by the code in the "Input built in" column.

To see both for a real run, set `AGENT_TRACE=ON` (in `environment_vars.env` or the shell).
Every call is then written to `logs/trace-<time>.md` (the filled-in instructions, the exact
input and the reply, in order) and `.jsonl`, which the run viewer at
http://127.0.0.1:8000/runs shows as a workflow diagram with each call's prompt, input and output.

| Prompt file | Agent | Input built in | Input holds | Returns (schemas.py) |
|---|---|---|---|---|
| `header_screen.md` | Header Screen | `screening.run_header_screen` | Column headers only, no values | `HeaderScreenOutput`: id / drop / keep per header |
| `criteria_planner.md` | Criteria Planner | `planning.run_planner` | Purpose (general at upload/process; the client's instructions and document starts at draft time), built-in criteria, field summaries (no answers or IDs) | `CriteriaDraft` |
| `manager.md` | Manager | `drafting.run_manager` | Client's text, scrubbed documents, criteria list (no candidate data) | `ManagerOutput` |
| `manager_schema.md` | Manager, job spreadsheet task | `jobs.run_schema_manager` | Client's text, scrubbed documents, field names/headers/kinds/fill counts (no values); on a revision also its previous design and the Data Agent's report | `JobSchema` |
| `data_agent.md` | Data Agent | `jobs.run_data_agent` | The job schema, field summaries (widely shared values only, plus average words and share of prose answers per field), 3 pseudonymized sample rows | `DataMapping`, including which fields are free text |
| `manager_review.md` | Manager, final review | `review.review_payload` | Role, client's instructions, notes, criteria with weight shares and ranker instructions, shortlist (refs, weighted scores, each ranker's score and reason, and other information: fields no criterion uses, for tie-breaks only) | `ReviewOutput` (adjustments, justifications, tiebreak values) |
| `ranker/main.md` | Ranker (one per criterion) | `orchestrator.run_ranker` | Role and pseudonymized candidates with only that criterion's fields (its instructions are in its prompt) | `RankerOutput` |

Ranker parts filled into `ranker/main.md`:

| File | Used for |
|---|---|
| `ranker/client_instructions.md` | The request's instructions for that criterion (usually written by the Manager); left out if there are none |
| `ranker/hard_constraint.md` / `ranker/soft_constraint.md` | Whether the ranker may exclude candidates (only hard constraints may) |

## Order in a run

```
upload / process:   headers ──► Header Screen ──► code drops columns ──► candidates.json
                    field summaries ──► Criteria Planner ──► criteria.json
job create:         brief + headers ──► Manager (schema) ──► Data Agent ──► code checks the mapping
                    gaps? ──► Manager revises once ──► Data Agent ──► code fills jobs/<id>/table.csv
draft:              client text + documents ──► Criteria Planner (criteria for this request, cached)
                    ──► Manager weights them ──► request draft carrying its criteria (client reviews)
rank:               request ──► code picks criteria, pseudonymizes ──► Rankers (in parallel) ──► code aggregates
                    instructions/notes? ──► Manager final review (±0.10 per candidate) ──► code re-sorts
```

The output schemas are in `schemas.py`. Their field descriptions are sent to the model too,
so they count as instructions as well.
