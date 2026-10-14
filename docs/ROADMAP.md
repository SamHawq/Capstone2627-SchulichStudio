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
4. Build the Manager LLM agent (free-text instructions → weights and constraints)
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

**Summary:** given a hiring request and a list of candidate records, the system returns a weighted top-X ranking with an explanation per criterion. The input is currently fictional sample data in a JSON file; no database, API, or UI yet.

**How to run:** `python src/main.py` (real LLM calls, needs `OPENAI_API_KEY`) · `python -m unittest` (offline tests)

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

### Pipeline
Entry point: `rank_candidates(request, raw_candidates)` in [orchestrator.py](../src/hiring_agents/orchestrator.py). Returns the final ranking (using pseudonymous refs) plus a ref → real-ID map.

| # | Step | What it does | How |
|---|---|---|---|
| 1 | **Privacy filter** ([privacy.py](../src/hiring_agents/privacy.py)) | Removes identifying and protected fields (UCID, name, email, ethnicity, sex, …) and replaces each real ID with a code like `C-3f9a12bc` | Field-name blocklist; the code is a keyed SHA-256 hash with a random per-run key, so it can't be reversed without the map, which never leaves our process |
| 2 | **Criteria selection** (`resolve_criteria`) | Activates only the criteria named in the request with a weight above 0; all other criteria sleep (no LLM call) | Looks up each id in the Criterion Registry; adds the request's bonus criteria to a copy of the registry; warns about unknown or zero-weight ids |
| 3 | **Ranking** (`run_ranker` + [llm_agents/ranker.py](../src/hiring_agents/llm_agents/ranker.py)) | One LLM agent per active criterion scores every candidate from 0 to 10 with a 1–2 sentence rationale | OpenAI Agents SDK `Agent` with instructions built from the criterion and `RankerOutput` as structured output. Each ranker sees only that criterion's `relevant_fields`. All rankers run in parallel (`asyncio.gather`), with one retry on failure |
| 4 | **Validation** (`validate_output`) | Fixes or flags LLM mistakes | Drops made-up or duplicate candidates, clamps scores into 0–10, ignores exclusions from criteria that aren't hard constraints, reports candidates with no score. A ranker that fails is left out with a warning; the request still completes |
| 5 | **Aggregation** ([aggregation.py](../src/hiring_agents/aggregation.py)) | Combines scores into one ranking | `total = Σ(weight × score/10) / Σ weight` (0–1). Missing scores count as 0. Candidates excluded by a hard constraint are removed. Sorted by score, ties broken by ref, cut to `top_x` |
| 6 | **Re-identification** ([main.py](../src/main.py)) | Shows real IDs in the output | `ref_map` lookup, done only after all LLM calls finish |

### Agents ([llm_agents/](../src/hiring_agents/llm_agents/))
| Agent | Status | Notes |
|---|---|---|
| Ranker | ✅ Working | One instance per active criterion. Model set by `RANKER_MODEL`, otherwise the SDK default |
| Manager (Orchestrator) | ⚠️ Plain code, no LLM yet | Covers selection, validation and aggregation. An LLM for interpreting free-text instructions is still to come |
| Scheduler | ❌ Not started | The hard-constraint mechanism it needs (exclusions) is already supported |
| Criteria Formatter | ❌ Not started | |

### Criteria ([registry.py](../src/hiring_agents/registry.py))
| id | Uses fields | Type |
|---|---|---|
| `teaching_experience` | `previous_ta_courses`, `teaching_experience` | registered, soft |
| `subject_knowledge` | `program`, `year_of_study`, `relevant_courses` | registered, soft |
| `past_performance` | `previous_ta_reviews` | registered, soft |
| *client-defined* | any | bonus (max 10), passed in `HiringRequest.custom_criteria` |

### Inputs and outputs ([schemas.py](../src/hiring_agents/schemas.py))
- **In:** `HiringRequest` (role, criterion → weight, `top_x`, notes, custom criteria) + raw candidate dicts (must include `ucid`)
- **Out:** `FinalRanking` (ranked candidates with total score, per-criterion scores and rationales; excluded candidates with reasons; warnings)

### Tests ([tests/](../tests/))
Offline (no API calls): privacy filtering, per-ranker field selection, criteria selection and bonus criteria, validation fixes, weighted aggregation with exclusions (`test_pipeline.py`); log file on/off, level filtering, and invalid level values (`test_logging.py`).

### Known limitations
- Sample JSON data only; no MySQL connection
- No REST API or UI; run as a script
- The privacy filter matches field names only; personal details written inside free text are not removed
- No hard-constraint criteria are registered yet (waiting on the Scheduler)
- Rankers score every candidate in a single call, which may need batching for large applicant pools
