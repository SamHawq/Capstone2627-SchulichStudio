"""Builds the Criteria Planner agent: designs the criteria a dataset can be ranked on.

Real data rarely uses our field names. A TA survey has columns like
"verbal_and_written_skills_to_communicate" or "schulich_studio_skill_checklist_python"
instead of `teaching_experience`. The planner runs once per dataset (when the candidate
store is compiled), reads every field's name and original column header (plus
non-identifying summaries, see `schemas.FieldProfile`), and returns a `CriteriaDraft`:
  - extensions: fields that are evidence for a built-in criterion, added to its ranker;
  - criteria: the remaining relevant fields grouped into new criteria, each covering one or
    more fields and each with its own ranker;
  - a note per used field telling the ranker how to read it;
  - skipped: fields not worth ranking on, with reasons.

The planner never sees candidate answers or IDs. Its draft is validated by plain code
(planning.validate_draft), so it can't invent fields or reuse one in two criteria.
"""

import os

from agents import Agent

from ..prompts import render
from ..schemas import CriteriaDraft


def build_criteria_planner() -> Agent:
    """Create the planner agent.

    Model choice: PLANNER_MODEL if set, else RANKER_MODEL, else the SDK default model.
    """
    return Agent(
        name="Criteria Planner",
        instructions=render("criteria_planner"),  # prompts/criteria_planner.md
        output_type=CriteriaDraft,
        model=os.getenv("PLANNER_MODEL") or os.getenv("RANKER_MODEL") or None,
    )
