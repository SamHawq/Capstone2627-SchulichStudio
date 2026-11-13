"""Builds a Ranker Agent for a single criterion.

A ranker is an OpenAI Agents SDK `Agent` with:
  - instructions generated from its `Criterion` (what to look for, how to score, which fields
    it covers and how to read them, the rules), so the same builder serves built-in criteria
    and auto criteria planned from any dataset. The text is in prompts/ranker/main.md, and
  - `output_type=RankerOutput`, which makes the SDK request structured JSON output and parse
    it into a `RankerOutput` object.

Rankers have no tools and no handoffs. They receive a JSON list of candidates (built in
orchestrator.run_ranker) and return scores. All coordination happens in the orchestrator.
"""

import os

from agents import Agent

from ..prompts import render
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
    # One criterion can cover several fields; the planner's notes say how to read each one.
    notes = {n.field: n.meaning for n in criterion.field_notes}
    field_lines = "\n".join(f"- {f}: {notes[f]}" if f in notes else f"- {f}"
                            for f in criterion.relevant_fields)

    client = ("\n" + render("ranker/client_instructions", instructions=client_instructions.strip()) + "\n"
              if client_instructions.strip() else "")

    # The prompt text is in prompts/ranker/. Each rule there targets a failure mode that
    # validate_output also checks for.
    instructions = render(
        "ranker/main",
        criterion_id=criterion.id,
        criterion_name=criterion.name,
        description=criterion.description,
        scoring_guidance=criterion.scoring_guidance,
        field_lines=field_lines,
        client_instructions=client,
        exclusion_rule=render("ranker/hard_constraint" if criterion.constraint == "hard"
                              else "ranker/soft_constraint"),
    )

    return Agent(
        name=f"Ranker: {criterion.name}",
        instructions=instructions,
        output_type=RankerOutput,
        model=os.getenv("RANKER_MODEL") or None,  # None -> SDK default model
    )
