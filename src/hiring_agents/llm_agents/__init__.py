"""LLM agent definitions, one module per agent role.

Each module builds an OpenAI Agents SDK `Agent` (instructions, output schema, model). The
instructions themselves are text files in prompts/ (see prompts/README.md). Running the agents
(agent_trace.run_agent), validating their output, and combining results all happen in plain
code outside this folder.

Current:
    header_screen     Reads column headers only (no values) and says which columns identify a
                      person or hold protected characteristics, so code can drop them before
                      any value reaches an LLM, and which column is the candidate ID.
    ranker            Scores candidates on a single criterion (one agent per active criterion).
    criteria_planner  Designs the criteria a dataset can be ranked on: fits fields into the
                      built-in criteria and groups the rest into new criteria covering one or
                      more fields (once per dataset, at compile time or cached).
    manager           Turns a client's free-text instructions into weights and instructions
                      for each ranker (a draft the client reviews before ranking), and
                      (build_schema_manager) designs a job's spreadsheet: column groups for
                      what the job needs, from the brief and the data's headers; and
                      (build_review_manager) reviews the final ranking against the client's
                      instructions, nudging scores within a fixed bound.
    data_agent        Maps the candidate data onto a job spreadsheet's columns (sources plus a
                      fixed method per column); code applies it.

Planned (see docs/ARCHITECTURE.md):
    scheduler           Hard/soft availability constraints against the course schedule.
    criteria_formatter  Converts a client's .zip of hiring criteria into our criteria format.
"""

from agents import set_tracing_disabled

from .criteria_planner import build_criteria_planner
from .data_agent import build_data_agent
from .header_screen import build_header_screen
from .manager import build_manager, build_review_manager, build_schema_manager
from .ranker import build_ranker

# The Agents SDK uploads a trace of every run (instructions, inputs and outputs) to the OpenAI
# dashboard by default. Candidate data must only go to the model calls themselves, so that is
# off. For a local record of every call, set AGENT_TRACE=ON (see agent_trace.py).
set_tracing_disabled(True)

__all__ = ["build_criteria_planner", "build_data_agent", "build_header_screen", "build_manager",
           "build_ranker", "build_review_manager", "build_schema_manager"]
