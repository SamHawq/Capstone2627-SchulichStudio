"""Shared data structures passed between the orchestrator, rankers, and aggregation.

All models are Pydantic, so data is validated whenever one is created. `RankerOutput` (and the
models nested in it) also serves as the JSON schema the LLM must follow. The Agents SDK passes it
to the model as a structured-output format, so field names and `description`s here act as
instructions to the LLM.

Data moves through the models in this order:
    HiringRequest + Criterion  ->  Candidate  ->  RankerOutput  ->  FinalRanking
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class Criterion(BaseModel):
    """A registry entry: the standard interface that tells a ranker what to look for.

    Each criterion becomes one ranker agent. The text fields are inserted directly into that
    agent's instructions, so write them as clear guidance for the LLM.

    Attributes:
        id: Unique key, used in `HiringRequest.criteria` and in outputs (e.g. "subject_knowledge").
        name: Human-readable name, also used as the agent's name.
        description: What the ranker should look for.
        scoring_guidance: What a 0, 5, and 10 look like. Anchors the LLM's scale so scores
            are comparable across runs.
        relevant_fields: Candidate fields this ranker is allowed to see. Everything else is
            withheld (data minimization; see `privacy.view_for`).
        type: "registered" for built-in criteria, "bonus" for client-defined ones.
        constraint: "hard" lets the ranker exclude candidates outright (e.g. must be available
            on lab days). "soft"/"none" means the ranker can only score.
    """

    id: str
    name: str
    description: str
    scoring_guidance: str
    relevant_fields: list[str]
    type: Literal["registered", "bonus"] = "registered"
    constraint: Literal["hard", "soft", "none"] = "none"


class Candidate(BaseModel):
    """A candidate after sensitive fields are removed.

    Attributes:
        ref: Pseudonymous ID (e.g. "C-3f9a12bc"). This is the only identifier an LLM ever sees.
            Map it back to a real ID with the `ref_map` returned by `privacy.pseudonymize`.
        fields: Remaining non-sensitive data (program, courses, experience, ...).
    """

    ref: str
    fields: dict[str, Any]


class HiringRequest(BaseModel):
    """What the client is hiring for and how candidates should be judged.

    Attributes:
        role: Free-text description of the position, given to every ranker as context.
        criteria: Maps criterion_id to weight. Only criteria listed here run; all others "sleep".
            Weights are relative (0.4/0.4/0.2 and 2/2/1 give the same result). A weight of
            0 or less skips the criterion.
        top_x: How many candidates to return in the final ranking.
        notes: Optional extra instructions from the client, given to every ranker.
        custom_criteria: Client-defined bonus criteria, registered for this request only. They
            must still be listed in `criteria` with a weight to be used.
    """

    role: str
    criteria: dict[str, float]
    top_x: int = 5
    notes: str = ""
    custom_criteria: list[Criterion] = Field(default_factory=list)


# --- Ranker agent output (this is the LLM's structured output schema) ---

class CandidateScore(BaseModel):
    """One ranker's score for one candidate."""

    candidate_ref: str
    # No ge/le bounds on purpose: strict structured-output schemas may reject them, so
    # out-of-range values are clamped in orchestrator.validate_output instead.
    score: int = Field(description="0 (no evidence / poor fit) to 10 (exceptional fit)")
    rationale: str = Field(description="1-2 sentences citing evidence from the candidate's data")


class ExcludedCandidate(BaseModel):
    """A candidate removed by a hard constraint, with the reason shown to the client."""

    candidate_ref: str
    reason: str


class RankerOutput(BaseModel):
    """Everything a single ranker agent returns for its criterion."""

    criterion_id: str
    scores: list[CandidateScore]
    excluded: list[ExcludedCandidate] = Field(
        description="Only used for hard-constraint criteria; otherwise leave empty"
    )


# --- Final output ---

class RankedCandidate(BaseModel):
    """One row of the final ranking.

    Attributes:
        candidate_ref: Pseudonymous ID (map back using `ref_map`).
        total_score: Weighted score from 0 to 1 across all criteria that ran.
        per_criterion: Raw 0-10 score from each ranker, keyed by criterion_id.
        rationales: Each ranker's explanation, keyed by criterion_id.
    """

    candidate_ref: str
    total_score: float
    per_criterion: dict[str, int]
    rationales: dict[str, str]


class FinalRanking(BaseModel):
    """The system's answer to a HiringRequest.

    Attributes:
        role: Copied from the request.
        ranking: Top-X candidates, best first.
        excluded: Candidates removed by hard constraints. They never appear in `ranking`.
        warnings: Problems the client should know about (failed rankers, missing scores,
            ignored criteria, ...). An empty list means everything ran cleanly.
    """

    role: str
    ranking: list[RankedCandidate]
    excluded: list[ExcludedCandidate]
    warnings: list[str]
