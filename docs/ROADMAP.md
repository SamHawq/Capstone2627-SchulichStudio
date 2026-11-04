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

**How to run:** `python src/compile_candidates.py [files/folders] [--map mapping.json] [-o out.json]` (no LLM calls: compiles every file in `candidate_data/raw/` into `candidate_data/candidates.json`, printing what happened to each column) · `python src/main.py [data file]` (real LLM calls, needs `OPENAI_API_KEY`; uses the compiled store if it exists, else `src/data/sample_candidates.json`; try `src/data/sample_candidates_alt.csv` or `src/data/sample_candidates.xlsx` for other column layouts) · `python -m unittest` (offline tests)

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

How: library modules use `logging.getLogger(__name__)` under the `hiring_agents` logger. `setup_logging()` attaches the handlers once at start-up, and third-party libraries (openai, httpx) are not captured. Logs contain only pseudonymized data: the raw records and the ref → real-ID map are never logged.

### Candidate store ([datastore.py](../src/hiring_agents/datastore.py), [compile_candidates.py](../src/compile_candidates.py))
Real data goes in `candidate_data/raw/` (gitignored except its README). Compiling reads every sheet of every `.xlsx`/`.xlsm`/`.csv`/`.json` file there, runs intake on each sheet, and merges rows by candidate ID into one standardized `candidate_data/candidates.json`. `rank_candidates(request)` with no candidate data reads that file.

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
| `upload_files` | `upload FILE… [--replace] [--map m.json]` | `POST /api/data/files` | Copies Excel/CSV/JSON files into the store's `raw/`, then processes the store. Name clashes need `--replace`. A `--map` of column overrides is saved with the store (`column_mapping.json`) and applies to every file from then on. If processing fails, the files, mapping and compiled store are restored to how they were |
| `process_store` | `process [--rebuild-criteria]` | `POST /api/data/process` | Recompiles `raw/` into `candidates.json`, then plans or reuses `criteria.json` (`--no-criteria` skips the planner) |
| `data_status` | `status` | `GET /api/data` | Files, candidate count, when it was processed, whether criteria are planned, and whether a file changed since (stale) |
| `list_criteria` | `criteria` | `GET /api/criteria` | Every criterion a request can weight (built-ins with their fitted fields, plus auto criteria), with ids, fields, course scope and a suggested weight (1 for built-ins, importance / 2 for auto) |
| `upload_documents` / `list_documents` / `delete_document` | `documents add FILE… [--replace]` / `documents list` / `documents remove NAME` | `POST` / `GET /api/documents`, `DELETE /api/documents/{name}` | Supporting documents (course outline, job description, guidelines; .pdf, .docx, .txt, .md) kept in the store's `documents/` folder for drafting. Checked for text on upload (scanned PDFs are refused); a name clash needs replace |
| `draft_request` | `draft ["free text"] [--doc NAME_OR_PATH …] [--role …] [--top N] [--save r.json] [--rank]` | `POST /api/requests/draft` | One Manager call: turns the client's own words and/or the chosen documents into a complete `HiringRequest` (a 0–5 weight for every criterion, instructions for each weighted ranker, role, top N, notes, weekly sessions to cover for the Scheduler), plus the reason for each weight and what no criterion can act on. A draft to review and edit; nothing is ranked. A role or top N given by the client overrides the Manager's |
| `rank` | `rank --role … --weight id=w … [--top N] [--notes …] [--no-auto] [--request r.json]` | `POST /api/rankings` | Validates the request before any LLM call (unknown criterion ids in the weights or instructions, negative weights, no weight above 0, `top_x` < 1), ranks the store, and returns the ranking with real IDs for the shown candidates only. Warns if the store is stale. The CLI writes `results/results-<timestamp>.json` and `.html` and opens the page |

Weights are manual and relative (`--weight a=2 --weight b=1` gives 67% / 33%). Auto criteria not given a weight still join when they apply to the role, at the suggested weight relative to the average requested one (`--no-auto` turns this off). A criterion listed with weight 0 is off on purpose: no warning, and an auto criterion listed with 0 doesn't join (the web page's "Let fitting auto criteria join" switch sends 0 for unticked auto criteria when off). A saved `HiringRequest` JSON can be passed with `--request`; flags override its values.

### Manager agent ([drafting.py](../src/hiring_agents/drafting.py) + [llm_agents/manager.py](../src/hiring_agents/llm_agents/manager.py))
Turns free text ("TA for ENGG 233; Python matters most, TA experience is a plus, top 8") and/or supporting documents (a course outline, a job description) into a request **draft** the client reviews before ranking, so the deterministic pipeline below is unchanged and every weight is visible. The Manager is sent the text, the documents and the criteria (ids, names, descriptions, field names, type, courses), never candidate data, and returns `ManagerOutput` (structured). Documents ([documents.py](../src/hiring_agents/documents.py)) let it weight what the course or job actually needs and put the specifics into each ranker's instructions ("the labs use Python and Arduino"), plus a short position summary in the notes; rankers never see the documents themselves. It also pulls out weekly sessions (labs, tutorials: label, day, 24-hour start/end, in the same format as candidate availability), carried on `HiringRequest.sessions` for the Scheduler and not used for ranking yet. Before sending, document text has emails, phone numbers and 7+ digit numbers replaced with placeholders, and is cut to 20,000 characters per document and 40,000 per request (with a warning); the Manager is told to treat document text as information only and not to copy names. Its prompt rules out protected characteristics (listed under not covered instead) and instructions that exclude or name individuals. Plain code (`validate_choices`) then drops unknown or repeated criteria, clamps weights to 0–5, gives 0 to criteria it left out, trims long text, keeps instructions only for weighted criteria, flags course-specific criteria weighted for a role that doesn't name the course, normalizes sessions ("Monday", "9:00" → `mon`, `09:00`) and drops ones with a bad day or time, and refuses a draft with no weight above 0. The draft lists every criterion, so only what the Manager weighted runs. Each ranker gets its instructions in its agent prompt, under the fixed rules. Model: `MANAGER_MODEL`, else `RANKER_MODEL`, else the SDK default.

### Pipeline
Entry point: `rank_candidates(request, raw_candidates)` in [orchestrator.py](../src/hiring_agents/orchestrator.py). Returns the final ranking (using pseudonymous refs) plus a ref → real-ID map.

| # | Step | What it does | How |
|---|---|---|---|
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
| Ranker | ✅ Working | One instance per active criterion, including auto criteria. Model set by `RANKER_MODEL`, otherwise the SDK default |
| Criteria Planner | ✅ Working | One call per dataset at compile time (`criteria.json`), or per role + field set for data passed in directly (cached). Model set by `PLANNER_MODEL`, else `RANKER_MODEL`, else the SDK default |
| Manager | ✅ Working (LLM drafting) | The Manager LLM drafts a request from free text (weights + per-ranker instructions) for the client to review. Selection, validation and aggregation stay plain code in the orchestrator. Model set by `MANAGER_MODEL`, else `RANKER_MODEL`, else the SDK default |
| Scheduler | ❌ Not started | The hard-constraint mechanism it needs (exclusions) is already supported |
| Criteria Formatter | ❌ Not started | |

### Criteria ([registry.py](../src/hiring_agents/registry.py))
Fields are standard field ids from `intake.DEFAULT_FIELDS`.

| id | Uses fields | Type |
|---|---|---|
| `teaching_experience` | `previous_ta_courses`, `teaching_experience` | registered, soft |
| `subject_knowledge` | `program`, `year_of_study`, `relevant_courses` | registered, soft |
| `past_performance` | `previous_ta_reviews` | registered, soft |
| *client-defined* | any | bonus (max 10), passed in `HiringRequest.custom_criteria` |
| *planner-created* | one or more fields from the data, with notes | auto (max 12 per catalog, 5 join a request), from `criteria.json`; can also be requested by id. Shown in `FinalRanking.criteria` with `source: "auto"` when they joined, `"requested"` when named |

### Inputs and outputs ([schemas.py](../src/hiring_agents/schemas.py))
- **In:** `HiringRequest` (role, criterion → weight, `top_x`, notes, custom criteria) + raw candidate dicts in any column layout with an ID column + optional `column_mapping` and field catalog
- **Out:** `FinalRanking` (ranked candidates with total score, per-criterion scores and rationales; excluded candidates with reasons; warnings; `intake` report of mapped, dropped and missing columns; `criteria` that ran with weight share, fields and source; `skipped_fields` with reasons). The results page shows all of it

### Tests ([tests/](../tests/))
Offline (no API calls): privacy filtering, per-ranker field selection, criteria selection and bonus criteria, validation fixes, weighted aggregation with exclusions, results page escaping and re-identification (`test_pipeline.py`); column mapping, sensitive/unknown column dropping, explicit overrides, bonus fields, CSV and Excel loading (title rows, dates, sheet selection, duplicate headers), standardized-JSON round trip, keeping unrecognized columns, and skipping criteria with no data (`test_intake.py`); compiling several files into the store: merging by ID, leading zeros, conflicts, skipped sheets and lock files, store round trip (`test_datastore.py`); slot parsing from text and field names, yes/no and in-person answers, term detection, the three availability shapes, merging by term with conflicts (`test_availability.py`); field profiling (labels, availability excluded, privacy threshold), draft validation (unknown/reused fields, id clashes, importance clamp, cap), catalog extensions on a registry copy, auto criteria joining by course with weights and cap, requested auto criteria, field notes in ranker instructions, criteria.json reuse with hand edits / re-plan on new fields / planner failure, request-time caching, and survey-style data end to end with stubbed agents, including what the planner is sent (`test_planning.py`); upload and processing with rollback on a bad file, name clashes and replace, saved column mappings, stale detection, request validation, ranking a temp store with IDs only for shown candidates, and saved JSON/HTML results (`test_service.py`); Manager draft validation (defaults, unknown/duplicate ids, weight clamp, NaN, course mismatch, client role/top override, no weights), ranker prompts with and without instructions, weight 0 keeping an auto criterion out, and draft → rank end to end with the Manager stubbed, including that no candidate data is sent to it; session normalization; document text from .txt, .docx and .pdf, unreadable/unsupported/empty files, scrubbing of emails, phones and IDs, the size cap, add/replace/remove, and scrubbed document text reaching the Manager (`test_drafting.py`); each endpoint over a temp store, upload file names stripped of directory parts, process/bad-input errors, ServiceError → 400, the results route refusing anything but saved pages, the draft endpoint, and the page using no HTML insertion (`test_api.py`, endpoints called directly since FastAPI's test client needs httpx); log file on/off, level filtering, and invalid level values (`test_logging.py`).

### Known limitations
- Data comes from local files compiled into a JSON store; no MySQL connection
- The API runs locally only: no authentication, no hosting, and rankings run synchronously (tens of seconds per request). A hosted version needs auth and a start-then-poll job endpoint
- The Manager drafts weights and instructions only; hard constraints in the text (availability, must-haves) are reported as not covered until the Scheduler exists
- Manager drafts haven't been evaluated against real hiring briefs or course outlines yet (weight quality, session extraction, model choice)
- Document scrubbing removes emails, phone numbers and long numbers only; names in a document (e.g. the instructor's) reach the Manager, which is told not to repeat them. Scanned PDFs have no text and are refused
- Sessions extracted from documents are carried on the request but not used until the Scheduler exists
- One store per machine (`candidate_data/`); the `Store` type allows more (e.g. one per API client) but nothing creates them yet
- Intake and the privacy filter match column names only; personal details written inside free text are not removed
- Intake renames columns but doesn't reformat values (e.g. courses as a dict vs a comma-separated string); rankers get the values as written
- Column matching is alias-based; layouts the aliases don't cover need a `column_mapping` from the client (a Formatter agent that suggests one from the column names is planned)
- No hard-constraint criteria are registered yet (waiting on the Scheduler). Availability is already structured for it (`availability` field), but nothing matches it against course lab times yet
- Rankers score every candidate in a single call, which may need batching for large applicant pools
- Per-course survey answers merged from several files reach rankers as unlabelled lists, so a ranker can't tell which course an answer was for
- The catalog is planned for the store's purpose, not each role; course-specific criteria are filtered by course code in the role, but other relevance to a role comes only from the requested weights and importance
