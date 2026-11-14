"""Shared stand-ins for LLM agents and fixtures, so tests stay offline."""

from unittest.mock import patch

import hiring_agents.screening as screening
from hiring_agents.intake import DEFAULT_FIELDS
from hiring_agents.registry import CriterionRegistry, default_registry
from hiring_agents.schemas import ColumnDecision, FieldSpec, HeaderScreenOutput

# A client's own field catalog with "keep" fields (the default catalog has only the ID and
# sensitive names): for tests of intake's renaming mechanics.
TA_FIELDS = DEFAULT_FIELDS + [
    FieldSpec(id="program", handling="keep", aliases=["degree", "degree_program", "major"]),
    FieldSpec(id="year_of_study", handling="keep", aliases=["year", "study_year"]),
    FieldSpec(id="relevant_courses", handling="keep", aliases=["courses", "courses_grades", "grades"]),
    FieldSpec(id="previous_ta_courses", handling="keep", aliases=["previous_ta_positions", "ta_history"]),
    FieldSpec(id="teaching_experience", handling="keep", aliases=["teaching_background"]),
    FieldSpec(id="previous_ta_reviews", handling="keep", aliases=["reviews", "instructor_feedback"]),
]

# The fields the Criteria Planner would assign to each built-in criterion for TA-style data.
TA_ASSIGNMENT = {
    "teaching_experience": ["previous_ta_courses", "teaching_experience"],
    "subject_knowledge": ["program", "year_of_study", "relevant_courses"],
    "past_performance": ["previous_ta_reviews"],
}


def ta_registry() -> CriterionRegistry:
    """The built-in criteria with TA_ASSIGNMENT's fields, as after planning."""
    base = default_registry()
    return CriterionRegistry([base.get(cid).model_copy(update={"relevant_fields": TA_ASSIGNMENT[cid]})
                              for cid in base.ids()])


def screen_reply(drop=(), ids=()):
    """A fake Header Screen: `drop` columns -> drop, `ids` -> id, everything else -> keep.
    Records the headers it was sent in `.calls`."""
    async def fake(headers):
        fake.calls.append(list(headers))
        return HeaderScreenOutput(columns=[
            ColumnDecision(column=h, action="drop" if h in drop else "id" if h in ids else "keep",
                           reason="test")
            for h in headers])
    fake.calls = []
    return fake


def runner_by_agent(replies):
    """A fake Runner.run that answers by agent name ({"Criteria Planner": draft, "Manager": ...}).
    An agent with no reply fails the test. Use as patch.object(agent_trace.Runner, "run", ...)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    async def run(agent, payload, *args, **kwargs):
        if agent.name not in replies:
            raise AssertionError(f"No stub reply for agent {agent.name!r}")
        out = replies[agent.name]
        return SimpleNamespace(final_output_as=lambda *a, **k: out)
    return AsyncMock(side_effect=run)


def review_reply(adjustments=None, tiebreaks=None):
    """A fake final review: `adjustments` / `tiebreaks` map ref -> value (others 0). Records each
    payload it was sent (parsed) in `.payloads`."""
    import json

    from hiring_agents.schemas import CandidateReview, ReviewOutput

    async def fake(agent, payload, output_type, tag, attempts=2):
        data = json.loads(payload)
        fake.payloads.append(data)
        adj = adjustments or {}
        return ReviewOutput(summary="Reviewed.", candidates=[
            CandidateReview(candidate_ref=c["candidate_ref"], adjustment=adj.get(c["candidate_ref"], 0.0),
                            reason="Matches the client's emphasis." if adj.get(c["candidate_ref"]) else "",
                            justification=f"Why {c['candidate_ref']}.",
                            tiebreak=(tiebreaks or {}).get(c["candidate_ref"], 0.0),
                            tiebreak_reason="Motivation fits the course." if (tiebreaks or {}).get(c["candidate_ref"]) else "")
            for c in data["shortlist"]])
    fake.payloads = []
    return fake


def patch_review(adjustments=None, tiebreaks=None):
    """Patch the final review's agent call."""
    import hiring_agents.review as review
    return patch.object(review, "run_with_retries", review_reply(adjustments, tiebreaks))


def patch_screen(drop=(), ids=()):
    """Patch the Header Screen call (keep-all by default, so the intake blocklist alone decides,
    as before the screen existed)."""
    return patch.object(screening, "run_header_screen", screen_reply(drop, ids))
