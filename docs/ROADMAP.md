# Roadmap

<!--
  The "when" of the project. Update regularly (e.g. weekly) as tasks move.
  Tie milestones to course deadlines/deliverables where possible.
  Dates are TBD. Fill them in from the course schedule.
-->

## Milestones
| Milestone | Target Date | Status |
|---|---|---|
| Project goal and architecture drafted | TBD | In progress |
| Access to sponsor MySQL data + schema reviewed | TBD | Not started |
| Proof of concept: Manager + 1 Ranker returning a ranking from real data | TBD | Not started |
| **Fall Term Evaluation** (Academic Advisor, Sponsor Rep, TA) | TBD | Not started |
| Criterion Registry + multiple rankers + Scheduler (hard/soft constraints) | TBD | Not started |
| Criteria Formatter Agent | TBD | Not started |
| REST API exposed end to end | TBD | Not started |
| LLM comparison (GPT vs Claude per agent) completed | TBD | Not started |
| RAG / vector DB evaluated and integrated (if adopted) | TBD | Not started |
| **Winter Term Evaluation** | TBD | Not started |
| Generality demo (non-TA hiring scenario) | TBD | Not started |
| Testing, evaluation against success criteria, documentation | TBD | Not started |
| **Final Evaluation** | TBD | Not started |

## Current Focus
- [ ] Finalize PROJECT_GOAL and ARCHITECTURE with the team and sponsor (owner: TBD)
- [ ] Settle the open questions below, especially demographic data use and data privacy (owner: TBD)
- [ ] Get access to the sponsor's MySQL database and document the schema (owner: TBD)
- [ ] Choose the API framework and set up the project folder structure (owner: TBD)

## Backlog
1. Finalize the criteria template (client input format); the Criterion Registry interface exists (see Current functionality)
2. Build DB access tools (read-only) for agents
3. Build the Scheduler agent (course schedule and availability, hard/soft constraints)
4. Manager LLM: map free-text hard constraints (e.g. availability) to the Scheduler once it exists (weights and per-ranker instructions are done)
5. Add more registered criteria as the sponsor's data fields become known
6. Build the Criteria Formatter Agent (zip intake → formatted criteria for client review)
7. Expose the REST API
8. Model evaluation harness (quality, latency, tokens, cost per agent)
9. Detect personal details inside free-text fields (the current privacy filter only matches field names)
10. Investigate RAG for guidelines, history and course requirements
11. Demo UI / mobile-friendly client (if in scope)

## Done
- Repo initialized; prototype `init.py` runs a single Agents SDK agent
- Initial docs created (CLAUDE.md, PROJECT_GOAL, ARCHITECTURE, ROADMAP)
- `.gitignore` protects secrets
- Code moved under `src/`; LLM agent definitions in `src/hiring_agents/llm_agents/`
- First end-to-end pipeline: Criterion Registry, privacy filter, parallel ranker agents, output validation, weighted aggregation (see Current functionality)
- Client-defined bonus criteria (up to 10 per request)
- Offline unit tests for the deterministic parts
- Logging to a log file and the terminal, each with its own level setting
- Agent prompts moved into editable files (`src/hiring_agents/prompts/`); optional per-run agent trace file; Agents SDK upload of traces to OpenAI turned off
- Header Screen agent: decides from column headers alone which columns to drop before any value reaches an LLM
- Job spreadsheets: the Manager designs a spreadsheet for one job and the Data Agent maps the store's data onto it, with one revision round
- Run viewer (`/runs`): every recorded run as a workflow diagram, timeline, agents table and per-call prompt / input / output
- Built-in criteria get their fields from the Criteria Planner's reasoning, not from default column names
- Rankers get only the role, their criterion's fields and instructions; a final Manager review applies the client's instructions to the shortlist within ±0.10, with reasons and justifications
- Job workbooks: export a job as Excel (table grouped by criterion + design) and import it back without redesigning or re-mapping
- Criteria are planned from the client's instructions at draft time (the request carries them), not only once per dataset
- Fields no criterion uses become "other information" (unless availability, administrative, protected, identifying or unusable) and break ties in the final review, ordering tied candidates without changing scores

## Risks & Open Questions
| Item | Notes | Owner |
|---|---|---|
| **Demographic data in ranking** | PROJECT_GOAL suggests factoring ethnicity and sex into ranking. Scoring candidates on protected characteristics likely conflicts with human-rights law and university hiring policy. Confirm with the sponsor and the university what is allowed. Safer options include bias auditing, anonymized review, or reporting diversity without scoring on it. | TBD |
| **Sending applicant data to third-party LLMs** | Applicant data is personal information. Confirm sponsor/university approval and provider data-retention settings. | TBD |
| LLM ranking consistency | LLM outputs can vary between runs. Mitigate with structured outputs, low temperature, numeric scoring rubrics, and consistency tests. | TBD |
| LLM cost and latency | Many agents per request. Measure during model evaluation and use smaller models where quality allows. | TBD |
| Data access timing and quality | The project depends on sponsor DB access and how clean the data is. | TBD |
| Aggregation ownership | Does the Manager aggregate, or a separate component? | TBD |
| Ranker output format | Finalize the "top X candidates" schema (draft in ARCHITECTURE.md). | TBD |
| Vector DB / RAG | Adopt or not; which product. | TBD |
| API framework and hosting | Not yet chosen. | TBD |
| Wording in PROJECT_GOAL | The Problem Statement mentions "a machine learning model", while the architecture deliberately uses agents instead of ML. Make them consistent. | TBD |

## Current functionality
<!-- Keep this in sync with the code: update it in the same change that adds or alters functionality. -->

**Summary:** given a hiring request and candidate records (by default the compiled candidate store), the system returns a weighted top-X ranking with an explanation per criterion. The input is currently fictional sample data in an Excel, CSV or JSON file, in any column layout that intake can map; no database or API yet. Results are printed and also shown as a local HTML page.

**How to run:** `python src/compile_candidates.py [files/folders] [--map mapping.json] [-o out.json]` (one Header Screen call for new column headers, then compiles every file in `candidate_data/raw/` into `candidate_data/candidates.json`, printing what happened to each column) · `python src/main.py [data file]` (real LLM calls, needs `OPENAI_API_KEY`; uses the compiled store if it exists, else `src/data/sample_candidates.json`; try `src/data/sample_candidates_alt.csv` or `src/data/sample_candidates.xlsx` for other column layouts) · `python -m unittest` (offline tests)

### Logging ([logging_config.py](../src/hiring_agents/logging_config.py))
Two outputs, each switched by an env var (in `environment_vars.env` or the shell). Values: `OFF`, `ERROR`, `WARNING`, `INFO`, `DEBUG`.

| Variable | Output | Default |
|---|---|---|
| `LOG_FILE_LEVEL` | `logs/run-<timestamp>.log` (gitignored) | `OFF` |
| `LOG_CONSOLE_LEVEL` | Terminal (stderr) | `WARNING` |

| Level | What gets logged |
|---|---|
| WARNING | Ranker retries and failures, validation fixes, unknown or zero-weight criteria |
| INFO | The workflow: request summary, sensitive field names removed, active vs sleeping criteria, each agent created (name + model), each ranker's finish time and output counts, total time, final ranking (as refs) |
| DEBUG | Full detail: each agent's instructions, the exact JSON the Manager sends each ranker, each ranker's raw reply, output after validation fixes, and the per-candidate aggregation math |

**Agent trace** ([agent_trace.py](../src/hiring_agents/agent_trace.py)): `AGENT_TRACE=ON` (default `OFF`) writes two files per process (a running server appends to one pair):
- `logs/trace-<timestamp>.md`: one section per agent call, readable in an editor: agent, model, time taken, the filled-in instructions, the exact input and the structured reply (or the error).
- `logs/trace-<timestamp>.jsonl`: one event per line, read by the run viewer. Events are:
  - `operation_start` / `operation_end`: one user action (upload, process, compile, draft, rank, job create, job apply), marked with `@traced_operation`. A nested action counts as the outer one.
  - `agent`: an agent call, as above, with input and output stored as data.
  - `step`: a plain-code step between agent calls (`trace_step`). It holds a summary only: names, counts and pseudonymous refs, never candidate values or real IDs.

Code steps recorded:
- Processing: header screen applied/reused, store compiled, criteria catalog planned/reused.
- Ranking: criteria selected, intake + pseudonymize, data check, ranker outputs validated, weighted aggregation.
- Drafting: draft validated.
- Jobs: data profiled, design validated, mapping validated, revision unchanged, table built, text extraction.

Every agent call goes through `run_agent`, so the trace is everything any agent saw or said. The Agents SDK's own tracing, which uploads runs to the OpenAI dashboard by default, is turned off in `llm_agents/__init__.py`.

How: library modules use `logging.getLogger(__name__)` under the `hiring_agents` logger. `setup_logging()` attaches the handlers once at start-up, and third-party libraries (openai, httpx) are not captured. Logs contain only pseudonymized data: the raw records and the ref → real-ID map are never logged.

### Final review ([review.py](../src/hiring_agents/review.py) + [llm_agents/manager.py](../src/hiring_agents/llm_agents/manager.py) `build_review_manager`)
Rankers judge one criterion each and see only the role, that criterion's fields and the Manager's instructions for it. The request's `notes` and the client's own words (`client_instructions`, kept from the draft) are not sent to them.

The final ranking is no longer a plain average when there is something to apply. After aggregation:
1. Code takes a shortlist: twice the top N, at least top N + 5, at most 30.
2. The Manager (review task) sees the role, the client's instructions, the notes, each criterion's weight share and ranker instructions, and per shortlisted candidate the weighted score and every ranker's score and reason. It sees pseudonymous refs only, never candidate data.
3. It returns per candidate an adjustment of at most ±`MAX_ADJUSTMENT` (0.10), a reason naming the instruction and the evidence, and a one-or-two-sentence justification, plus a summary.
4. Code checks the reply: unknown or repeated refs are dropped, adjustments are clamped, and an adjustment without a reason is not applied.
5. Code computes final = weighted + adjustment (kept within 0–1), re-sorts (ties go to the higher weighted score, then the ref) and cuts to top N.

**Tie-breaks from other information.** Fields no criterion uses are kept as other information when the Criteria Planner marks them `tiebreak` (motivation, general comments, interests, ...). Each skipped field now carries a `use`: `tiebreak`, `availability`, `administrative`, `protected`, `identifying` or `unusable`. Fields the planner doesn't mention count as `tiebreak`, because they already passed the header screen. Only `tiebreak` fields are ever shown to an agent.

These fields come through intake and pseudonymization, but rankers never see them: each ranker gets only its own criterion's fields. The review gets them per shortlisted candidate, under their original headers and cut to 300 characters, as `other_info`. It returns a `tiebreak` value (−1 to 1, judged from that information only) with a reason. Code uses the value **only to order candidates whose final scores are within 0.01 of each other** (`TIE_MARGIN`, chained). It never changes a score, so it can't lift anyone past a better-scored candidate. Adjustments may not be based on other information.

The review also runs, even with no instructions or notes, when the shortlist has tied candidates who have other information. Results show "Tie broken by other information: …" and list the fields used (`FinalRanking.tiebreak_fields`). In job spreadsheets, every field no column uses goes into `other`, except the excluded uses (judged from the store's criteria catalog).

Every final score is therefore within 0.10 of a score anyone can recompute, and every change says why. The prompt forbids adjusting for what the weights already reward and stating the order (code decides it).

The review runs only when the request has client instructions or notes and `final_review` is on. A failed review returns the weighted ranking with a warning. Results show the final score, the weighted score and the adjustment with its reason, the justification and the review summary (web page, HTML report, `RankedCandidate.weighted_score` / `adjustment` / `adjustment_reason` / `justification`, `FinalRanking.reviewed` / `review_summary`).

### Run viewer ([static/runs.html](../src/hiring_agents/static/runs.html), `/runs`)
A page in the local web UI (linked from the main page's header) for reviewing recorded runs. It needs `AGENT_TRACE=ON`.
- **Sidebar:** every trace file and its operations, with status, duration and agent-call count. The selection is kept in the URL hash, so a view can be bookmarked.
- **Summary:** wall time, agent calls, failures, code steps, total agent time and models.
- **Workflow diagram:** code steps and agent calls in order, coloured by stage (header screen, store, criteria, manager, job, data agent, privacy, rankers, validation, aggregation, text extraction). Agent calls that overlapped in time are grouped as "N in parallel".
- **Detail panel:** for an agent call, its stage, prompt file, model, timing and sizes, with tabs for the filled-in prompt, the input sent and the output received (or the error). For a code step, its summary. JSON is shown as a collapsible tree, built lazily and 100 entries at a time, so large ranker payloads stay fast.
- **Timeline:** agent calls as bars and code steps as ticks, so parallel rankers and the slow calls stand out.
- **Agents table:** each agent created in the operation with its prompt file, model, calls, time and data sent and received.

Auto-refresh (every 5 s) follows a run in progress. Every value is inserted as text, never as HTML. Endpoints: `GET /api/traces` and `GET /api/traces/{name}`; names must match `trace-YYYYMMDD-HHMMSS.jsonl`, so no other file can be read.

### Prompts ([prompts/](../src/hiring_agents/prompts/))
Each agent's instructions are a `.md` file (`header_screen.md`, `criteria_planner.md`, `manager.md`, `ranker/main.md` plus its parts), with `{placeholders}` filled by `prompts.render`. Files are read on every call, so an edit takes effect on the next call. A placeholder with no value, or a value with no placeholder, raises an error naming it. [prompts/README.md](../src/hiring_agents/prompts/README.md) lists, for each agent, which code builds its input, what that input holds and what it returns.

### Header screen ([screening.py](../src/hiring_agents/screening.py) + [llm_agents/header_screen.py](../src/hiring_agents/llm_agents/header_screen.py))
The first step for any data. The Header Screen agent gets every column header of every file, with no values, and returns `id`, `drop` or `keep` for each one:
- `drop`: names, contact details, other identifiers, protected characteristics (and close proxies), and other sensitive personal data such as health or emergency contacts.
- `id`: the candidate ID.
- When it is unsure, it drops the column and starts the reason with "Unsure:", which raises a warning.

Plain code validates the reply: headers it invented are ignored, a repeated header keeps its first decision, and headers it left out are dropped with a warning. The decisions are saved in the store's `header_screen.json` (editable: change an action and process again). Only headers not already in that file are sent, so reprocessing the same files makes no call.

`apply_screen` then removes the dropped columns from every row of each sheet before intake runs, so their values never reach the store, the planner or any ranker. They are listed with the sensitive columns in the intake report. The ID column is the one intake recognizes by alias or `column_mapping`, else the first column the screen marked `id`; other `id` columns are dropped as identifiers. The recognized ID column is never dropped, since it is pseudonymized before any ranker anyway.

Intake's name blocklist still applies on top, so both have to miss a column for it to get through. If the screen call fails, processing fails and an upload is rolled back: data is never stored unscreened. `--no-screen` (CLI and `compile_candidates.py`) or `screen=False` turns it off, with a warning. Data passed straight to `rank_candidates` (e.g. `main.py` with a file) is screened the same way, using the store's `header_screen.json`. Model: `SCREEN_MODEL`, else `PLANNER_MODEL`, else `RANKER_MODEL`, else the SDK default.

### Job spreadsheets ([jobs.py](../src/hiring_agents/jobs.py) + [llm_agents/manager.py](../src/hiring_agents/llm_agents/manager.py) `build_schema_manager` + [llm_agents/data_agent.py](../src/hiring_agents/llm_agents/data_agent.py))
Turns the store into a table designed for one job. Not connected to ranking yet: that is the per-job Criteria Agent step.

1. **Manager, schema task.** It gets the client's text, scrubbed documents, and the data's field names, original headers, kinds and fill counts, with no values. It returns a `JobSchema`: up to 8 groups (each a draft ranking criterion), each with columns of kind number, category (with ordered `values`), yes_no, text or list, 30 columns at most. Groups are split when the job treats parts differently (e.g. "Python essential, other languages a plus").
2. **Code validates the design.** Ids are made snake_case and unique and the caps are applied. Columns whose id or label names personal or protected data are removed, reserved ids (`candidate_id`, `other`, `availability`) are renamed, and a role the client set wins.
3. **Data Agent.** It gets the design, the field summaries (as the Criteria Planner does: values shared by 3+ candidates only) and 3 sample rows. The samples are the most complete candidates, already screened, run through `privacy.pseudonymize` (codes instead of IDs, sensitive field names removed), and cut to 200 characters per value. It returns a `DataMapping`: per column, the source fields and one method (`copy`, `value_map`, `combine`, `count_yes`, `list_yes`, `max`, `sum`). It also returns columns it can't fill, relevant data with no column, and fields to carry along as `other` for tie-breaks.
4. **Code validates the mapping.** Unknown or repeated columns and unknown sources are dropped, `copy`/`value_map` with several sources becomes `combine`, and every column ends up mapped or unfilled. A mapping that would turn yes/no-only data or free text into category levels is rejected as unfilled, with the reason ("data is yes/no only", "data is free text"). Without this, the model invented levels ("Yes" → "Intermediate") despite its prompt.
5. **One revision.** If columns are unfilled or relevant data has no column, the Manager gets its design plus the report and revises it (e.g. a level column becomes yes_no, a category becomes text), then the Data Agent maps again. If the revised design is unchanged, the second Data Agent call is skipped. No further rounds.
6. **Free text is identified.** Code measures every text field over all answers, not just the first rows (which are often blank). It records `avg_words` and `sentence_share`: the share of answers that read like prose, meaning 6+ words, mostly ordinary lowercase words, so a course list like "ENGG 233: A, ENSF 338: B+" doesn't count. The Data Agent sees these numbers plus the labels and samples, and lists every free-text field in `free_text`, including short ones a length check can't catch ("Great TA"). Code adds any field with at least 30% prose answers that the agent missed. The job's `free_text_columns` are the text and list columns fed by those fields, plus free-text fields carried in `other` (as `other.<field>`); the CLI marks them `[free text]`.
7. **Code fills the table** for every candidate. Values are coerced to each column's kind: numbers parsed from text ("3 years" → 3), yes-like answers, category values snapped to the allowed spelling. Problem values are counted per column in warnings (never shown). A candidate with no source value gets no value. Availability is carried along for the Scheduler.

8. **Text extraction** ([text_extraction.py](../src/hiring_agents/text_extraction.py)). The tool gets a cut-down version of the job spreadsheet (`FreeTextSheet`): one row per candidate who has any free text, identified only by a fresh pseudonymous ref (never the real ID), and only the free-text columns, each with its header (the column's label, or the original header for fields in `other`). It returns the same sheet with cleaned text, which code merges back by ref and column key; anything for an unknown ref or column, or of a different type, is ignored. The sheet that was sent is saved as `free_text.csv` in the job folder (`Candidate,<header>,…`), so the team can see what the tool gets and test against it. This is where the team's gendered-language tool plugs in. **Not connected yet:** `extract` returns the text unchanged and the job carries a warning saying so. `job apply` runs it again, using the saved `free_text` list as edited.

Everything is saved in `candidate_data/jobs/<role-slug>-<time>/`:
- `job.json`: brief, counts, warnings.
- `schema.json` and `mapping.json`: both editable.
- `table.json` and `table.csv`: real IDs, gitignored with the store. The CSV has a UTF-8 BOM so Excel opens it.
- `free_text.csv`: the sheet sent to text extraction (refs, not IDs); only when the job has free text.

`job apply` rebuilds the table from edited files and the current store with no LLM call. Calls: 2, or up to 4 with the revision (`--no-revise` / `revise=False`). Models: `MANAGER_MODEL` for the Manager. The Data Agent uses `DATA_MODEL`, else `PLANNER_MODEL`, else `RANKER_MODEL`, else the SDK default. Processing the store now also saves `field_labels.json` (field → original header) so both agents see the real question text.

### Job workbooks ([job_workbook.py](../src/hiring_agents/job_workbook.py))
`job export ID` (`GET /api/jobs/{id}/export`, "Download .xlsx" on the web page) writes the job as an Excel workbook, `candidate_data/jobs/<id>/<id>.xlsx`. It holds real IDs, so keep it local. Sheets:
- **Candidates:** row 1 has each criterion group's name over its columns, row 2 the column labels, then one row per candidate. Lists are written as `a; b`.
- **Design:** one row per column: group, column id and label, kind, allowed values, description, free text yes/no, and where the column came from.
- **Job:** the format marker `hiring-agents-job/1`, role, summary and the original instructions.

`job import FILE` (`POST /api/jobs/import`, "Import workbook" on the web page) turns a workbook, possibly edited, back into a job **without the Manager's design or the Data Agent's mapping**: the Design sheet is the design and each column fills from itself. Privacy checks still run:
- the column labels go through the header screen (an LLM call only for labels not seen before);
- columns whose id or label names personal or protected data are removed;
- Candidates columns not in the Design sheet are ignored with a warning;
- values are coerced to each column's kind, and free-text columns go to text extraction.

An imported job keeps its rows in `source.json`, so `job apply` rebuilds it without the store (`JobInfo.source = "import"`). A client can also prepare a workbook in this format by hand. Next step: per-job criteria, so a job (designed or imported) can go straight to weights and ranking.

### Candidate store ([datastore.py](../src/hiring_agents/datastore.py), [compile_candidates.py](../src/compile_candidates.py))
Real data goes in `candidate_data/raw/` (gitignored except its README). Compiling first runs the header screen over every file's headers. It then reads every sheet of every `.xlsx`/`.xlsm`/`.csv`/`.json` file there, removes the screened-out columns, runs intake on each sheet, and merges rows by candidate ID into one standardized `candidate_data/candidates.json`. `rank_candidates(request)` with no candidate data reads that file.

| Rule | Behaviour |
|---|---|
| Files split the data | Files can hold different candidates, different fields for the same candidates, or both; rows with the same ID are merged into one record |
| ID matching | Compared as text, ignoring leading zeros on all-digit IDs (`123` from a number cell = `"00000123"` from a text cell); the longer form is stored |
| Conflicts | Different values for the same field from different sources are all kept, as a list, with a warning naming the field |
| Survey-style layouts | The header row is the row that names the candidate ID ("Student ID", "UCID", …), so title rows above it are skipped. If the row above it holds group labels, column names become "Group / Column" (e.g. "Wish to TA / ENGG 225"), and ID/sensitive aliases still match on the last part. Rows with no ID are skipped with a warning |
| Availability ([availability.py](../src/hiring_agents/availability.py)) | Per-slot yes/no columns, slot-list columns ("Monday 8.00 am - 1.00 pm,…") and the in-person question are detected from field names and values and replaced by one `availability` field: `{term: {available, unavailable, conflicting, in_person_portion}}`, slots as `{day, start, end}` on a 24-hour clock. The term comes from the file/sheet name. Sources for the same term are combined; a slot marked both ways goes to `conflicting`; the most restrictive in-person answer wins. Unanswered slots appear in neither list. Values that aren't slots or yes/no (e.g. a shifted column) are ignored with a warning |
| Encodings | Text files are read as UTF-8, then Windows-1252, then Latin-1. Mac-Excel mojibake (`‚Äô` for `’`) is repaired |
| Sheets without an ID column | Skipped with a warning (e.g. instructions or summary sheets); Excel lock files (`~$…`) are ignored |
| Criteria catalog | After merging, the Criteria Planner designs the criteria the store can be ranked on and `criteria.json` is written next to `candidates.json` (see pipeline step 1). Recompiling with the same fields and `--purpose` keeps the file as is, including hand edits; a changed field set re-plans and keeps the old file as `criteria.prev.json`. `--rebuild-criteria` forces a new plan, `--no-criteria` skips the LLM call |
| Columns | Sensitive columns dropped; recognized columns renamed to standard fields; unrecognized columns kept under their normalized name, so a later bonus criterion can use them. Only the fields the active criteria need are passed on at ranking time |

### Service layer, REST API, web UI and CLI ([service.py](../src/hiring_agents/service.py), [api.py](../src/hiring_agents/api.py), [static/index.html](../src/hiring_agents/static/index.html), [server.py](../src/server.py), [cli.py](../src/cli.py))
The local version is the API's core: each service function takes and returns Pydantic models, prints nothing, and raises `ServiceError` for bad input. `api.py` (FastAPI) wraps each one as an endpoint under `/api/...` with the same models as request/response bodies (OpenAPI docs at `/docs`); a `ServiceError` becomes HTTP 400. `python src/server.py` serves the API and a local web page at http://127.0.0.1:8000 (localhost only: no login, and responses hold real IDs). The page covers everything below: store status, upload (with replace and plan options), reprocess, a free-text box plus a documents list (add, tick for the draft, remove) that drafts the request with the Manager agent (session times found are shown and sent with the ranking), criteria with a checkbox, weight slider and ranker-instructions field each (plus the Manager's reason for each weight after a draft) and a live share-of-total column (including auto criteria expected to join), role, top N, notes, and results (ranked cards with per-criterion scores and reasons, criteria that ran, warnings, link to the full HTML report). It inserts every value as text, never HTML. The CLI is a second client of the same functions.

| Operation | CLI | Endpoint | What it does |
|---|---|---|---|
| `upload_files` | `upload FILE… [--replace] [--map m.json] [--no-screen]` | `POST /api/data/files` | Copies Excel/CSV/JSON files into the store's `raw/`, then processes the store. Name clashes need `--replace`. A `--map` of column overrides is saved with the store (`column_mapping.json`) and applies to every file from then on. If processing fails, the files, mapping and compiled store are restored to how they were |
| `process_store` | `process [--rebuild-criteria] [--no-screen]` | `POST /api/data/process` | Screens new column headers (`header_screen.json`), recompiles `raw/` into `candidates.json` without the dropped columns, then plans or reuses `criteria.json` (`--no-criteria` skips the planner). The API always screens |
| `data_status` | `status` | `GET /api/data` | Files, candidate count, when it was processed, whether criteria are planned, and whether a file changed since (stale) |
| `list_criteria` | `criteria` | `GET /api/criteria` | Every criterion a request can weight (built-ins with their fitted fields, plus auto criteria), with ids, fields, course scope and a suggested weight (1 for built-ins, importance / 2 for auto) |
| `upload_documents` / `list_documents` / `delete_document` | `documents add FILE… [--replace]` / `documents list` / `documents remove NAME` | `POST` / `GET /api/documents`, `DELETE /api/documents/{name}` | Supporting documents (course outline, job description, guidelines; .pdf, .docx, .txt, .md) kept in the store's `documents/` folder for drafting. Checked for text on upload (scanned PDFs are refused); a name clash needs replace |
| `draft_request` | `draft ["free text"] [--doc NAME_OR_PATH …] [--role …] [--top N] [--save r.json] [--rank]` | `POST /api/requests/draft` | One Manager call: turns the client's own words and/or the chosen documents into a complete `HiringRequest` (a 0–5 weight for every criterion, instructions for each weighted ranker, role, top N, notes, weekly sessions to cover for the Scheduler), plus the reason for each weight and what no criterion can act on. A draft to review and edit; nothing is ranked. A role or top N given by the client overrides the Manager's |
| `create_job` | `job create "free text" [--doc NAME_OR_PATH …] [--role …] [--no-revise]` | `POST /api/jobs` (`?revise=false`) | Manager designs a spreadsheet for the job, Data Agent maps the store onto it, one revision round, table saved under `jobs/<id>/` (see Job spreadsheets). The CLI prints each group's columns with their sources, method and fill count |
| `list_jobs` / `get_job` / `reapply_job` | `job list` / `job show ID` / `job apply ID` | `GET /api/jobs`, `GET /api/jobs/{id}`, `POST /api/jobs/{id}/apply` | List jobs, show one, or rebuild its table after editing `schema.json` / `mapping.json` (no LLM call). Job ids are checked against a strict pattern, so a path can't be passed |
| `rank` | `rank --role … --weight id=w … [--top N] [--notes …] [--no-auto] [--request r.json]` | `POST /api/rankings` | Validates the request before any LLM call (unknown criterion ids in the weights or instructions, negative weights, no weight above 0, `top_x` < 1), ranks the store, and returns the ranking with real IDs for the shown candidates only. Warns if the store is stale. The CLI writes `results/results-<timestamp>.json` and `.html` and opens the page |

Weights are manual and relative (`--weight a=2 --weight b=1` gives 67% / 33%). Auto criteria not given a weight still join when they apply to the role, at the suggested weight relative to the average requested one (`--no-auto` turns this off). A criterion listed with weight 0 is off on purpose: no warning, and an auto criterion listed with 0 doesn't join (the web page's "Let fitting auto criteria join" switch sends 0 for unticked auto criteria when off). A saved `HiringRequest` JSON can be passed with `--request`; flags override its values.

### Criteria planned from the instructions ([service.py](../src/hiring_agents/service.py) `draft_request`)
Drafting first runs the Criteria Planner with the request as its purpose: the client's words plus the start of each document (3,000 characters each, already scrubbed). So the criteria fit what this client stresses. For example, "Python matters most; Arduino is only a plus" gives separate Python Proficiency and Arduino criteria, where the data alone gave one "Technical Skills" criterion.

The plan is cached per purpose and fields (`criteria_plans.json`), so redrafting the same text makes no new planner call. If planning fails, the store's general `criteria.json` is used, with a warning.

The Manager is offered only criteria that have fields in this data, and weights them. The drafted `HiringRequest` carries the plan in `catalog` (with `auto_criteria` off, so nothing joins on its own). Ranking uses that catalog instead of `criteria.json`, so a saved request re-ranks with exactly the same criteria even after the store is re-planned. `RequestDraft.criteria` lists them for the client.

The store's `criteria.json` (planned once per dataset for a general purpose) is still what `criteria` / `GET /api/criteria` list before drafting, and what manual `rank --weight` requests use. Calls per draft: one planner call (unless cached) and one Manager call.

### Manager agent ([drafting.py](../src/hiring_agents/drafting.py) + [llm_agents/manager.py](../src/hiring_agents/llm_agents/manager.py))
Turns free text ("TA for ENGG 233; Python matters most, TA experience is a plus, top 8") and/or supporting documents (a course outline, a job description) into a request **draft** the client reviews before ranking, so the deterministic pipeline below is unchanged and every weight is visible. For each criterion it sees each field with what it holds (the planner's note) and its kind, "free text" for varied written answers. That lets it route a preference about what candidates wrote ("prioritize candidates who mention materials") to every ranker with a free-text field, as evidence to look for rather than a keyword match, instead of reporting it as not covered. Its `notes` are for the final review, not the rankers. The Manager is sent the text, the documents and the criteria (ids, names, descriptions, field names, type, courses), never candidate data, and returns `ManagerOutput` (structured). Documents ([documents.py](../src/hiring_agents/documents.py)) let it weight what the course or job actually needs and put the specifics into each ranker's instructions ("the labs use Python and Arduino"), plus a short position summary in the notes; rankers never see the documents themselves. It also pulls out weekly sessions (labs, tutorials: label, day, 24-hour start/end, in the same format as candidate availability), carried on `HiringRequest.sessions` for the Scheduler and not used for ranking yet. Before sending, document text has emails, phone numbers and 7+ digit numbers replaced with placeholders, and is cut to 20,000 characters per document and 40,000 per request (with a warning); the Manager is told to treat document text as information only and not to copy names. Its prompt rules out protected characteristics (listed under not covered instead) and instructions that exclude or name individuals. Plain code (`validate_choices`) then drops unknown or repeated criteria, clamps weights to 0–5, gives 0 to criteria it left out, trims long text, keeps instructions only for weighted criteria, flags course-specific criteria weighted for a role that doesn't name the course, normalizes sessions ("Monday", "9:00" → `mon`, `09:00`) and drops ones with a bad day or time, and refuses a draft with no weight above 0. The draft lists every criterion, so only what the Manager weighted runs. Each ranker gets its instructions in its agent prompt, under the fixed rules. Model: `MANAGER_MODEL`, else `RANKER_MODEL`, else the SDK default.

### Pipeline
Entry point: `rank_candidates(request, raw_candidates)` in [orchestrator.py](../src/hiring_agents/orchestrator.py). Returns the final ranking (using pseudonymous refs) plus a ref → real-ID map.

| # | Step | What it does | How |
|---|---|---|---|
| 0 | **Header screen** (data passed in directly only) | Drops identifying and sensitive columns before anything else sees the data. The store's data was screened when it was compiled | See Header screen above. Uses and extends the store's `header_screen.json`; `rank_candidates(..., screen=False)` turns it off |
| 1 | **Criteria catalog** ([planning.py](../src/hiring_agents/planning.py) + [llm_agents/criteria_planner.py](../src/hiring_agents/llm_agents/criteria_planner.py)) | Makes data with any column names usable. Planned **once per dataset**, at compile time, into `candidate_data/criteria.json` (editable): data fields that are evidence for a built-in criterion are added to it (e.g. `verbal_and_written_skills_to_communicate` → teaching experience); the rest are grouped into **auto** criteria, each covering one or more fields (e.g. every column of a skill checklist → technical skills), each with a 0/5/10 rubric, an importance (1–3) and, if course-specific, its course codes. Every used field gets a note telling the ranker how to read it. Availability, administrative, identifying, shifted and protected fields are skipped with a reason | One LLM call (`CriteriaDraft` structured output) that sees only field names, original column headers, fill counts, kind (number / category / text) and category values shared by 3+ candidates, never individual answers or IDs; availability is removed in code before it. Plain code validates the draft: unknown or reused fields dropped, ids made unique, at most 12 auto criteria. Data passed in directly (not the store) is planned at request time for the role, cached in `candidate_data/criteria_plans.json` per role + field names. If the store has fields `criteria.json` doesn't know, a warning says to recompile. If the planner fails, only the criteria's own fields are used |
| 1b | **Criteria selection** (`resolve_criteria` + `add_auto_criteria`) | Activates the criteria named in the request with a weight above 0 (built-in, bonus, or auto criteria by id). Then auto criteria that apply to the role join: not course-specific, or the role names one of their courses (an ENGG 204 interest criterion doesn't join an ENGG 233 request). All other criteria sleep (no LLM call) | Built-ins are extended with the catalog's fields on a copy of the registry. Up to 5 auto criteria join, most important first; joined weight = average requested weight × importance / 2. Fields of criteria that didn't run are reported in `skipped_fields` with the reason. `HiringRequest.auto_criteria=False` turns the catalog off |
| 2 | **Intake** ([intake.py](../src/hiring_agents/intake.py)) | Loads the compiled store (or the records passed in) and maps any column layout onto the standard field catalog (`DEFAULT_FIELDS`), e.g. `Student Number` → `candidate_id`, `Courses & Grades` → `relevant_courses`. Keeps only the ID and the fields active criteria need; drops sensitive, unneeded and unrecognized columns | Column names are normalized and matched against each field's id and aliases; the client can override with `column_mapping`. Columns with sensitive names are always dropped, even if mapped. Unrecognized columns are dropped by default (allowlist). Fields a bonus criterion needs that aren't in the catalog are matched by exact name unless sensitive. Blank values are left out. Reads `.xlsx`/`.xlsm` (openpyxl; first sheet or `sheet=`; header is the first row with at least two filled cells, so title rows are skipped; empty rows and blank-header columns ignored; dates become ISO strings), `.csv` and `.json` (`load_records`). Raises an error if there is no ID column or two columns map to one field. What happened to each column is returned as `FinalRanking.intake` |
| 2b | **Privacy filter** ([privacy.py](../src/hiring_agents/privacy.py)) | Replaces each real ID with a code like `C-3f9a12bc`, and removes any sensitive field name intake missed | The code is a keyed SHA-256 hash with a random per-run key, so it can't be reversed without the map, which never leaves our process. Field-name blocklist as a second layer |
| 2c | **Data check** (`criteria_with_data`) | Skips any active criterion whose fields no candidate has, with a warning. A criterion with only some of its fields gets a warning naming what it is ranked on, unless the catalog extended it (a built-in running on planned fields is expected to miss its own standard fields) | Saves an LLM call that could only score everyone 0; its weight is left out, as for a failed ranker |
| 3 | **Ranking** (`run_ranker` + [llm_agents/ranker.py](../src/hiring_agents/llm_agents/ranker.py)) | One LLM agent per active criterion scores every candidate from 0 to 10 with a 1–2 sentence rationale | OpenAI Agents SDK `Agent` with instructions built from the criterion (rubric, plus each field it covers with the planner's note on how to read it, plus the request's instructions for that criterion, if any) and `RankerOutput` as structured output, so one generic builder serves built-in and auto criteria. Each ranker sees only that criterion's `relevant_fields`. All rankers run in parallel (`asyncio.gather`), with one retry on failure |
| 4 | **Validation** (`validate_output`) | Fixes or flags LLM mistakes | Drops made-up or duplicate candidates, clamps scores into 0–10, ignores exclusions from criteria that aren't hard constraints, reports candidates with no score. A ranker that fails is left out with a warning; the request still completes |
| 5 | **Aggregation** ([aggregation.py](../src/hiring_agents/aggregation.py)) | Combines scores into one ranking | `total = Σ(weight × score/10) / Σ weight` (0–1). Missing scores count as 0. Candidates excluded by a hard constraint are removed. Sorted by score, ties broken by ref, cut to `top_x` |
| 6 | **Re-identification** ([main.py](../src/main.py)) | Shows real IDs in the output | `ref_map` lookup, done only after all LLM calls finish |
| 7 | **Results page** ([report.py](../src/hiring_agents/report.py)) | `main.py` writes `results/results-<timestamp>.html` and opens it in the browser: ranked cards with total score, per-criterion score bars and rationales, excluded candidates, warnings, and the column mapping from intake | Self-contained HTML (standard library only), all values escaped. Shows real IDs, so `results/` is gitignored |

### Agents ([llm_agents/](../src/hiring_agents/llm_agents/))
| Agent | Status | Notes |
|---|---|---|
| Header Screen | ✅ Working | One call per batch of new column headers, headers only, at upload/process time (or for data passed in directly). Model set by `SCREEN_MODEL`, else `PLANNER_MODEL`, else `RANKER_MODEL`, else the SDK default |
| Manager (job spreadsheet) | ✅ Working | `build_schema_manager`: designs a job's columns from the brief and the data's headers (no values); 1–2 calls per job |
| Data Agent | ✅ Working | Maps the data onto a job's columns from field summaries + 3 pseudonymized sample rows; 1–2 calls per job. Model: `DATA_MODEL`, else `PLANNER_MODEL`, else `RANKER_MODEL` |
| Ranker | ✅ Working | One instance per active criterion, including auto criteria. Model set by `RANKER_MODEL`, otherwise the SDK default |
| Criteria Planner | ✅ Working | One call per dataset at compile time (`criteria.json`), or per role + field set for data passed in directly (cached). Model set by `PLANNER_MODEL`, else `RANKER_MODEL`, else the SDK default |
| Manager | ✅ Working (LLM drafting) | The Manager LLM drafts a request from free text (weights + per-ranker instructions) for the client to review. Selection, validation and aggregation stay plain code in the orchestrator. Model set by `MANAGER_MODEL`, else `RANKER_MODEL`, else the SDK default |
| Scheduler | ❌ Not started | The hard-constraint mechanism it needs (exclusions) is already supported |
| Criteria Formatter | ❌ Not started | |

### Criteria ([registry.py](../src/hiring_agents/registry.py))
Built-in criteria describe what to judge and start with no fields. The Criteria Planner assigns each one the data fields that are evidence for it (`criteria.json` extensions), by reasoning about each field's name, header, kind and values, never by matching default column names. A built-in criterion that the planner gave no fields sleeps, with a warning. Intake's default catalog holds only the ID and the sensitive names; every other column keeps its own normalized name. A client can still pass its own catalog with renaming rules.

| id | Uses fields | Type |
|---|---|---|
| `teaching_experience` | assigned by the Criteria Planner | registered, soft |
| `subject_knowledge` | assigned by the Criteria Planner | registered, soft |
| `past_performance` | assigned by the Criteria Planner | registered, soft |
| *client-defined* | any | bonus (max 10), passed in `HiringRequest.custom_criteria` |
| *planner-created* | one or more fields from the data, with notes | auto (max 12 per catalog, 5 join a request), from `criteria.json`; can also be requested by id. Shown in `FinalRanking.criteria` with `source: "auto"` when they joined, `"requested"` when named |

### Inputs and outputs ([schemas.py](../src/hiring_agents/schemas.py))
- **In:** `HiringRequest` (role, criterion → weight, `top_x`, notes, custom criteria) + raw candidate dicts in any column layout with an ID column + optional `column_mapping` and field catalog
- **Out:** `FinalRanking` (ranked candidates with total score, per-criterion scores and rationales; excluded candidates with reasons; warnings; `intake` report of mapped, dropped and missing columns; `criteria` that ran with weight share, fields and source; `skipped_fields` with reasons). The results page shows all of it

### Tests ([tests/](../tests/))
Offline (no API calls): privacy filtering, per-ranker field selection, criteria selection and bonus criteria, validation fixes, weighted aggregation with exclusions, results page escaping and re-identification (`test_pipeline.py`); column mapping, sensitive/unknown column dropping, explicit overrides, bonus fields, CSV and Excel loading (title rows, dates, sheet selection, duplicate headers), standardized-JSON round trip, keeping unrecognized columns, and skipping criteria with no data (`test_intake.py`); compiling several files into the store: merging by ID, leading zeros, conflicts, skipped sheets and lock files, store round trip (`test_datastore.py`); slot parsing from text and field names, yes/no and in-person answers, term detection, the three availability shapes, merging by term with conflicts (`test_availability.py`); field profiling (labels, availability excluded, privacy threshold), draft validation (unknown/reused fields, id clashes, importance clamp, cap), catalog extensions on a registry copy, auto criteria joining by course with weights and cap, requested auto criteria, field notes in ranker instructions, criteria.json reuse with hand edits / re-plan on new fields / planner failure, request-time caching, and survey-style data end to end with stubbed agents, including what the planner is sent (`test_planning.py`); upload and processing with rollback on a bad file, name clashes and replace, saved column mappings, stale detection, request validation, ranking a temp store with IDs only for shown candidates, and saved JSON/HTML results (`test_service.py`); Manager draft validation (defaults, unknown/duplicate ids, weight clamp, NaN, course mismatch, client role/top override, no weights), ranker prompts with and without instructions, weight 0 keeping an auto criterion out, and draft → rank end to end with the Manager stubbed, including that no candidate data is sent to it; session normalization; document text from .txt, .docx and .pdf, unreadable/unsupported/empty files, scrubbing of emails, phones and IDs, the size cap, add/replace/remove, and scrubbed document text reaching the Manager (`test_drafting.py`); each endpoint over a temp store, upload file names stripped of directory parts, process/bad-input errors, ServiceError → 400, the results route refusing anything but saved pages, the draft endpoint, and the page using no HTML insertion (`test_api.py`, endpoints called directly since FastAPI's test client needs httpx); log file on/off, level filtering, and invalid level values (`test_logging.py`); the header screen's validation (repeats, invented and missing headers), reuse of `header_screen.json` with only new headers sent and hand edits kept, "Unsure" warnings, invalid edits, applying it (dropped columns, the screen's ID column, extra ID columns, the recognized ID never dropped, unscreened columns, `column_mapping` conflicts), screened columns never reaching the store or the planner, a failed screen leaving the store unchanged, screening off, direct data screened before rankers, every agent building from its prompt file, placeholder checks, and the trace file on and off (`test_screening.py`). Job spreadsheets: prose detection over every answer (course lists aren't prose), the agent's and the length check's free-text fields combined and turned into free-text columns (text/list columns and `other` only), the extraction handoff (a cut-down sheet with refs not IDs, only free-text columns with their headers and only candidates with text, its CSV form, results merged back without changing the input, bad replies ignored, the not-connected warning, `free_text.csv` saved with the job), a hand-removed free-text field staying removed on `job apply`, design validation (ids, sensitive and reserved columns, caps, client role, empty design), mapping validation (unknown/repeated columns and sources, several sources for copy, every column mapped or unfilled, yes/no or free text never turned into levels), every fill method and kind coercion, value-map and category checks, missing data and non-numbers, `other` and availability carried, problem counts without values, sample rows pseudonymized and clipped, the revision round (and skipping the second Data Agent call when the design is unchanged), what each agent's payload holds (no values for the Manager, no real IDs for either), saved files and the CSV, `job apply` with hand edits and broken edits, bad job ids, and the job endpoints (`test_jobs.py`). Drafting plans criteria from the instructions (the planner's purpose holds the client's words; the Manager is offered the request's own criteria and none without fields), the request carries its catalog and a saved request ranks with it after the store's catalog is gone, a redraft reuses the cached plan, and an old catalog's unlabelled skipped fields are excluded from tie-breaks (`test_drafting.py`, `test_planning.py`). Tie-breaks: tie groups chain close scores, the tiebreak orders tied candidates only and never changes scores or moves an untied candidate, other information holds only `tiebreak` fields (clipped), ties alone trigger a review, a full run where ties are broken from a motivation field while an administrative field is never shown, the planner's skip uses with unnamed fields as other information, and every unused job field landing in `other` except administrative ones. Final review: when it runs, shortlist size, adjustments clamped and needing a reason, skipped candidates, weighted + adjustment re-sorted and cut, a reviewed candidate moving into the top N (and a maximum nudge alone only tying), no call without instructions, a failed review keeping the weighted order, the review seeing refs only, and rankers getting only the role and their own fields and instructions (`test_review.py`). Job workbooks: export layout, import with no design or mapping call and identical values, rebuilding an imported job without the store, edited workbooks (unknown columns, protected columns, header-screen drops), and non-workbook files (`test_jobs.py`). The test package blocks real LLM calls, so a forgotten stub fails loudly (`tests/__init__.py`). Run traces: operation/step/agent events with input and output stored as data, nested operations, failed operations, tracing off, a full ranking run and a job run traced with no real ID anywhere in the file, and the viewer endpoints (listing, reading, rejected names, no traces yet, the page using no HTML insertion) (`test_trace.py`). Test stubs shared between files are in `tests/stubs.py`.

### Known limitations
- Data comes from local files compiled into a JSON store; no MySQL connection
- The API runs locally only: no authentication, no hosting, and rankings run synchronously (tens of seconds per request). A hosted version needs auth and a start-then-poll job endpoint
- The Manager drafts weights and instructions only; hard constraints in the text (availability, must-haves) are reported as not covered until the Scheduler exists
- Manager drafts haven't been evaluated against real hiring briefs or course outlines yet (weight quality, session extraction, model choice)
- Document scrubbing removes emails, phone numbers and long numbers only; names in a document (e.g. the instructor's) reach the Manager, which is told not to repeat them. Scanned PDFs have no text and are refused
- Sessions extracted from documents are carried on the request but not used until the Scheduler exists
- The text extraction tool isn't connected: free-text columns are identified and handed over, but the text comes back unchanged
- Short free-text answers with only a few distinct values (e.g. "None", "TAed twice") profile as categories, so they aren't marked free text unless the Data Agent flags them; with real, varied answers they are
- Job spreadsheets can't be ranked yet: the per-job Criteria Agent (turning a job's groups into criteria) is the next step. The web page has no job screen yet (CLI and API only)
- Grouped survey headers: a group label applies to every column to its right until the next label, so a free-text question after a group without its own label is named after that group (e.g. "Time available / Instructor comments")
- One store per machine (`candidate_data/`); the `Store` type allows more (e.g. one per API client) but nothing creates them yet
- The header screen, intake and the privacy filter all judge columns by their names only; personal details written inside free text (e.g. a name in a reviewer's comment) are not removed
- Intake renames columns but doesn't reformat values (e.g. courses as a dict vs a comma-separated string); rankers get the values as written
- Column matching is alias-based; layouts the aliases don't cover need a `column_mapping` from the client (a Formatter agent that suggests one from the column names is planned)
- No hard-constraint criteria are registered yet (waiting on the Scheduler). Availability is already structured for it (`availability` field), but nothing matches it against course lab times yet
- Rankers score every candidate in a single call, which may need batching for large applicant pools
- Per-course survey answers merged from several files reach rankers as unlabelled lists, so a ranker can't tell which course an answer was for
- The catalog is planned for the store's purpose, not each role; course-specific criteria are filtered by course code in the role, but other relevance to a role comes only from the requested weights and importance
