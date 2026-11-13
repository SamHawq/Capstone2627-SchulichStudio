"""Builds the Data Agent: decides how to fill a job spreadsheet's columns from the candidate data.

It receives the Manager's design for one job (`JobSchema`), a summary of each data field
(name, original header, fill count, kind, widely shared values) and a few sample rows whose
IDs are replaced with codes. It returns a `DataMapping`: for each column, the source fields
and one of a fixed set of methods (copy, value_map, combine, count_yes, list_yes, max, sum),
plus the columns it can't fill, relevant data with no column, and fields to carry along.

The agent never writes values itself. Plain code (jobs.py) validates the mapping and applies
it to every candidate, so the result is the same on every run and a person can review or edit
the mapping (mapping.json) before using the table.
"""

import os

from agents import Agent

from ..prompts import render
from ..schemas import DataMapping


def build_data_agent() -> Agent:
    """Create the Data Agent.

    Model choice: DATA_MODEL if set, else PLANNER_MODEL, else RANKER_MODEL, else the SDK default.
    """
    return Agent(
        name="Data Agent",
        instructions=render("data_agent"),  # prompts/data_agent.md
        output_type=DataMapping,
        model=os.getenv("DATA_MODEL") or os.getenv("PLANNER_MODEL") or os.getenv("RANKER_MODEL") or None,
    )
