"""Builds the Header Screen agent: decides from column headers alone which columns to drop.

The first step any data goes through. The agent receives the column headers of every source
file (no values, no rows) and returns a `HeaderScreenOutput` with one decision per header:
  - id:   the candidate's unique ID column (code replaces it with a pseudonymous code),
  - drop: names, contact details, other identifiers, protected characteristics and other
          sensitive personal data; code removes these before any value reaches an LLM,
  - keep: everything else (whether it is useful for ranking is the Criteria Planner's call).

Plain code (screening.py) validates the decisions, saves them to the store's editable
header_screen.json and applies them. The intake blocklist (intake.DEFAULT_FIELDS "drop"
fields) still applies on top, so a header the agent keeps by mistake can still be dropped.
"""

import os

from agents import Agent

from ..prompts import render
from ..schemas import HeaderScreenOutput


def build_header_screen() -> Agent:
    """Create the Header Screen agent.

    Model choice: SCREEN_MODEL if set, else PLANNER_MODEL, else RANKER_MODEL, else the SDK default.
    """
    return Agent(
        name="Header Screen",
        instructions=render("header_screen"),  # prompts/header_screen.md
        output_type=HeaderScreenOutput,
        model=os.getenv("SCREEN_MODEL") or os.getenv("PLANNER_MODEL") or os.getenv("RANKER_MODEL") or None,
    )
