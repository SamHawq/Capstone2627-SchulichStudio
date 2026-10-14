"""Agentic candidate-ranking system.

Pipeline (see docs/ARCHITECTURE.md for the full design):

    HiringRequest + raw candidate records
        -> privacy.pseudonymize      strip sensitive fields, replace real IDs with refs
        -> orchestrator.resolve_criteria   decide which criteria are active (others "sleep")
        -> orchestrator.run_ranker   one LLM ranker per active criterion, run in parallel
        -> orchestrator.validate_output    fix or flag mistakes in each ranker's output
        -> aggregation.aggregate     weighted combination into the final top-X ranking

Modules:
    schemas       Pydantic data models shared by every stage.
    registry      Criterion Registry: the criteria the system knows how to rank on.
    privacy       Data minimization and pseudonymization before anything reaches an LLM.
    llm_agents/   LLM agent definitions, one module per role (currently: ranker).
    aggregation   Deterministic weighted scoring and sorting.
    orchestrator  The Manager: ties all the stages together. Entry point: `rank_candidates`.
"""
