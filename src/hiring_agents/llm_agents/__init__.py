"""LLM agent definitions, one module per agent role.

Each module builds an OpenAI Agents SDK `Agent` (instructions, output schema, model). Running
the agents, validating their output, and combining results all happen in the orchestrator, not
here. This keeps agent prompts in one place, where they're easy to review and compare across models.

Current:
    ranker    Scores candidates on a single criterion (one agent per active criterion).

Planned (see docs/ARCHITECTURE.md):
    scheduler           Hard/soft availability constraints against the course schedule.
    manager             Turns free-text hiring instructions into weights and constraints.
    criteria_formatter  Converts a client's .zip of hiring criteria into our criteria format.
"""

from .ranker import build_ranker

__all__ = ["build_ranker"]
