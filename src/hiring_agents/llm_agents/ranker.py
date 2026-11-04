"""Builds a Ranker Agent for a single criterion.

A ranker is an OpenAI Agents SDK `Agent` with:
  - instructions generated from its `Criterion` (what to look for, how to score, which fields
    it covers and how to read them, the rules), so the same builder serves built-in criteria
    and auto criteria planned from any dataset, and
  - `output_type=RankerOutput`, which makes the SDK request structured JSON output and parse
    it into a `RankerOutput` object.

Rankers have no tools and no handoffs. They receive a JSON list of candidates (built in
orchestrator.run_ranker) and return scores. All coordination happens in the orchestrator.
"""

import os

from agents import Agent

from ..schemas import Criterion, RankerOutput


def build_ranker(criterion: Criterion, client_instructions: str = "") -> Agent:
    """Create the LLM agent that scores candidates on `criterion`.

    Hard-constraint criteria are allowed to exclude candidates. All others are told to score
    everyone and leave `excluded` empty. The orchestrator enforces this too (see
    `validate_output`), so a model that ignores the rule can't wrongly remove a candidate.

    Model choice: uses the RANKER_MODEL environment variable if set, otherwise the SDK's
    default model. Change RANKER_MODEL to compare GPT and Claude models during LLM evaluation.

    Args:
        criterion: The registry entry this ranker evaluates.
        client_instructions: The client's extra instructions for this criterion (from the request,
            usually written by the Manager agent). The rules below still take priority.

    Returns:
        A configured `Agent`, ready for `Runner.run(agent, input_json)`.
    """
    if criterion.constraint == "hard":
        exclusion_rule = (
            "This is a HARD constraint: put candidates who clearly fail it in `excluded` "
            "with a reason, and do not score them."
        )
    else:
        exclusion_rule = "This is NOT a hard constraint: score every candidate and leave `excluded` empty."

    # One criterion can cover several fields; the planner's notes say how to read each one.
    notes = {n.field: n.meaning for n in criterion.field_notes}
    field_lines = "\n".join(f"- {f}: {notes[f]}" if f in notes else f"- {f}"
                            for f in criterion.relevant_fields)

    client = (f"\nThe client's instructions for this criterion (follow them within the rules below):\n"
              f"{client_instructions.strip()}\n") if client_instructions.strip() else ""

    # Each rule targets a specific failure mode that validate_output also checks for.
    instructions = f"""You are a ranking agent in a hiring system. You evaluate candidates on ONE criterion only.

Criterion id: {criterion.id}
Criterion: {criterion.name}
What to look for: {criterion.description}
Scoring guidance: {criterion.scoring_guidance}

Fields a candidate may have (missing fields are left out):
{field_lines}
A value may be a list when sources gave different answers (e.g. one per course survey);
weigh all of them.
{client}
Rules:
- Score each candidate from 0 to 10 using only the data provided for them. Do not guess missing information.
- If a candidate has no relevant data, give them 0 and say "no data" in the rationale.
- Ignore anything unrelated to this criterion, and never infer or consider demographic characteristics.
- Return every candidate exactly once, using their exact candidate_ref.
- Keep each rationale to 1-2 sentences that cite specific evidence.
- Set criterion_id to "{criterion.id}".
- {exclusion_rule}"""

    return Agent(
        name=f"Ranker: {criterion.name}",
        instructions=instructions,
        output_type=RankerOutput,
        model=os.getenv("RANKER_MODEL") or None,  # None -> SDK default model
    )
