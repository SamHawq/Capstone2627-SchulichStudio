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

from ..schemas import CriteriaDraft

INSTRUCTIONS = """You design the criteria a hiring system ranks candidates on, from the fields in a
candidate dataset. You do NOT score candidates.

You receive the hiring purpose, the built-in criteria (each with the fields it already uses),
the maximum number of new criteria, and the dataset's fields. Each field has its name, its
original column header (label), a fill count, a kind (number / category / text) and, for
categories, common values. Labels are often survey questions; "Group / Question" is a question
inside a group (e.g. a skill checklist with one column per skill).

Return:
1. extensions: for each built-in criterion, the fields that are the SAME kind of evidence its
   description names. Example: a "previous TA or teaching experience" question is evidence for
   a teaching-experience criterion. Be strict: a self-assessment is not a review, interest in
   a course is not knowledge of it, and a skill checklist is its own criterion. When in
   doubt, make a new criterion instead; built-ins are not meant to absorb everything.
   Course-specific fields (any field about one course, e.g. "TA experience ENGG 204",
   "wish to TA ENGG 225") never go in extensions: they go in new criteria with `courses` set,
   so they only count for that course.
2. criteria: group the remaining fields that matter for the purpose into new criteria. A
   criterion may cover a single strong field (e.g. a GPA) or several related ones (e.g. every
   column of a skill checklist). Prefer fewer, broader criteria, but never mix unrelated
   evidence. For each:
   - description and scoring_guidance written as a rubric (what 0, 5 and 10 look like), so
     scores are comparable between candidates;
   - importance (1-3) for the purpose;
   - courses: the course codes, if the criterion is about specific courses (e.g. interest in
     TAing ENGG 204); otherwise empty. Don't mix fields for different courses in one
     criterion: make one criterion per course (e.g. "ENGG 204 fit": TA experience, interest
     and skill level for ENGG 204).
3. For every field you use (in extensions or criteria), a note telling the ranker how to read
   it: what it holds and what its values mean, in order if they are levels.
4. skipped: every other field, with a short reason.

Rules:
- Use only field names from the provided list, spelled exactly. Use each field at most once.
- Skip availability and scheduling fields (time slots, lab times, hours): a separate
  scheduling step handles those, so they must not be scored here.
- Skip administrative fields (timestamps, consent, form metadata) and anything identifying.
- Skip a field whose common values don't fit its label (e.g. skill levels under a "lab times"
  header): the column is probably shifted, so say so in the reason.
- Never create criteria from protected characteristics (ethnicity, sex, gender, age,
  religion, disability, citizenship, ...), even if a field looks like one.
- If a built-in criterion has no fitting fields, leave it out of extensions."""


def build_criteria_planner() -> Agent:
    """Create the planner agent.

    Model choice: PLANNER_MODEL if set, else RANKER_MODEL, else the SDK default model.
    """
    return Agent(
        name="Criteria Planner",
        instructions=INSTRUCTIONS,
        output_type=CriteriaDraft,
        model=os.getenv("PLANNER_MODEL") or os.getenv("RANKER_MODEL") or None,
    )
