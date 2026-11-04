"""Agentic candidate-ranking system.

Pipeline (see docs/ARCHITECTURE.md for the full design):

    HiringRequest + raw candidate records (any column layout)
        -> orchestrator.resolve_criteria   decide which criteria are active (others "sleep")
        -> planning                  fit unused data fields into criteria / new auto criteria (1 LLM call, cached)
        -> intake.standardize        map columns to standard fields; keep only the ID and needed fields
        -> privacy.pseudonymize      replace real IDs with refs, second sensitive-field check
        -> orchestrator.criteria_with_data skip criteria no candidate has data for
        -> orchestrator.run_ranker   one LLM ranker per active criterion, run in parallel
        -> orchestrator.validate_output    fix or flag mistakes in each ranker's output
        -> aggregation.aggregate     weighted combination into the final top-X ranking

Modules:
    schemas       Pydantic data models shared by every stage.
    registry      Criterion Registry: the criteria the system knows how to rank on.
    planning      Criteria planner: profiles fields, calls the planner agent, validates its plan.
    drafting      Manager agent: free-text instructions -> a HiringRequest draft (weights and
                  instructions per ranker) for the client to review before ranking.
    intake        Standard field catalog and column mapping for any input layout.
    datastore     Compiles many data files into one candidate store the orchestrator reads by default.
    privacy       Data minimization and pseudonymization before anything reaches an LLM.
    llm_agents/   LLM agent definitions, one module per role (ranker, criteria_planner).
    aggregation   Deterministic weighted scoring and sorting.
    orchestrator  The Manager: ties all the stages together. Entry point: `rank_candidates`.
    report        HTML results page.
"""
