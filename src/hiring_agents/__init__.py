"""Agentic candidate-ranking system.

Pipeline (see docs/ARCHITECTURE.md for the full design):

    Data files (at upload/process time)
        -> screening                 Header Screen agent sees headers only; dropped columns are removed
                                     before any value reaches an LLM (decisions in header_screen.json)
        -> datastore                 compile the remaining columns into candidates.json

    Job brief (job create)
        -> jobs                      Manager designs a spreadsheet for the job, Data Agent maps the
                                     store onto it, one revision round, code fills jobs/<id>/table

    HiringRequest + raw candidate records (any column layout)
        -> orchestrator.resolve_criteria   decide which criteria are active (others "sleep")
        -> planning                  fit unused data fields into criteria / new auto criteria (1 LLM call, cached)
        -> intake.standardize        map columns to standard fields; keep only the ID and needed fields
        -> privacy.pseudonymize      replace real IDs with refs, second sensitive-field check
        -> orchestrator.criteria_with_data skip criteria no candidate has data for
        -> orchestrator.run_ranker   one LLM ranker per active criterion, run in parallel
        -> orchestrator.validate_output    fix or flag mistakes in each ranker's output
        -> aggregation.aggregate     weighted combination (the shortlist, if a review follows)
        -> review.review_ranking     Manager applies the client's instructions: +-0.10 per candidate,
                                     with reasons and justifications; code re-sorts to the top-X

Modules:
    schemas       Pydantic data models shared by every stage.
    registry      Criterion Registry: the criteria the system knows how to rank on.
    planning      Criteria planner: profiles fields, calls the planner agent, validates its plan.
    drafting      Manager agent: free-text instructions -> a HiringRequest draft (weights and
                  instructions per ranker) for the client to review before ranking.
    intake        Standard field catalog and column mapping for any input layout.
    datastore     Compiles many data files into one candidate store the orchestrator reads by default.
    jobs          Job spreadsheets: a table designed for one job, from the store.
    screening     Header screen: which columns to drop, decided from headers alone, applied before intake.
    privacy       Data minimization and pseudonymization before anything reaches an LLM.
    llm_agents/   LLM agent definitions, one module per role (header_screen, criteria_planner,
                  manager, ranker).
    prompts/      Every agent's instructions as editable .md files (see prompts/README.md).
    agent_trace   run_agent: every agent call goes through it; optional trace file (AGENT_TRACE=ON).
    aggregation   Deterministic weighted scoring and sorting.
    orchestrator  The Manager: ties all the stages together. Entry point: `rank_candidates`.
    report        HTML results page.
"""
