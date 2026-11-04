"""LLM agent definitions, one module per agent role.

Each module builds an OpenAI Agents SDK `Agent` (instructions, output schema, model). Running
the agents, validating their output, and combining results all happen in the orchestrator, not
here. This keeps agent prompts in one place, where they're easy to review and compare across models.

Current:
    ranker            Scores candidates on a single criterion (one agent per active criterion).
    criteria_planner  Designs the criteria a dataset can be ranked on: fits fields into the
                      built-in criteria and groups the rest into new criteria covering one or
                      more fields (once per dataset, at compile time or cached).
    manager           Turns a client's free-text instructions into weights and instructions
                      for each ranker (a draft the client reviews before ranking).

Planned (see docs/ARCHITECTURE.md):
    scheduler           Hard/soft availability constraints against the course schedule.
    criteria_formatter  Converts a client's .zip of hiring criteria into our criteria format.
"""

from .criteria_planner import build_criteria_planner
from .manager import build_manager
from .ranker import build_ranker

__all__ = ["build_criteria_planner", "build_ranker"]
