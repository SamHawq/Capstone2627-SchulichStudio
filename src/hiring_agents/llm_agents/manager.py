"""Builds the Manager agent: turns a client's free-text instructions into a ranking request.

The client writes what they want ("TA for ENGG 233; Python matters most, some teaching
experience is nice, give me 8") and/or attaches documents (a course outline, a job
description). The Manager receives those plus the criteria available for the data (built-ins
and the auto criteria the Criteria Planner made) and returns a `ManagerOutput`:
  - a weight (0-5) for every criterion,
  - instructions for each weighted criterion's ranker, taken from the text,
  - the role, how many candidates to return, and notes for every ranker,
  - weekly sessions the hire must cover (labs, tutorials), for the Scheduler,
  - the parts of the text no criterion can act on.

The documents are where the Manager turns a course's needs into ranking guidance: an outline
that lists Python labs and Arduino projects raises technical skills and tells that ranker
what to look for. Rankers get only what the Manager writes, never the documents.

The Manager never sees candidate data, only the text, the documents (scrubbed, see
documents.py) and the criteria (names, descriptions and field names). Its output is
validated by plain code (drafting.validate_choices) and shown to the client as a draft to
review before any ranker runs.
"""

import os

from agents import Agent

from ..prompts import render
from ..schemas import JobSchema, ManagerOutput, ReviewOutput


def build_schema_manager() -> Agent:
    """Create the Manager in its other task: designing a job's spreadsheet (`JobSchema`).

    It gets the client's instructions, the documents and the data's field names and headers
    (no values), and on a revision its previous design plus the Data Agent's report. See
    jobs.py. Same model choice as `build_manager`.
    """
    return Agent(
        name="Manager: job spreadsheet",
        instructions=render("manager_schema"),  # prompts/manager_schema.md
        output_type=JobSchema,
        model=os.getenv("MANAGER_MODEL") or os.getenv("RANKER_MODEL") or None,
    )


def build_review_manager() -> Agent:
    """Create the Manager in its third task: the final review of a ranking (`ReviewOutput`).

    It gets the shortlist (pseudonymous refs, weighted scores, each ranker's score and reason),
    the client's instructions and the notes, and may nudge each weighted score within
    review.MAX_ADJUSTMENT. Never candidate data itself. See review.py. Same model as the Manager.
    """
    return Agent(
        name="Manager: final review",
        instructions=render("manager_review"),  # prompts/manager_review.md
        output_type=ReviewOutput,
        model=os.getenv("MANAGER_MODEL") or os.getenv("RANKER_MODEL") or None,
    )


def build_manager() -> Agent:
    """Create the Manager agent.

    Model choice: MANAGER_MODEL if set, else RANKER_MODEL, else the SDK default model.
    """
    return Agent(
        name="Manager",
        instructions=render("manager"),  # prompts/manager.md
        output_type=ManagerOutput,
        model=os.getenv("MANAGER_MODEL") or os.getenv("RANKER_MODEL") or None,
    )
