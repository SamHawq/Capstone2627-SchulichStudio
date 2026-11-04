"""Shared data structures passed between the orchestrator, rankers, and aggregation.

All models are Pydantic, so data is validated whenever one is created. `RankerOutput` (and the
models nested in it) also serves as the JSON schema the LLM must follow. The Agents SDK passes it
to the model as a structured-output format, so field names and `description`s here act as
instructions to the LLM.

Data moves through the models in this order:
    HiringRequest + Criterion  ->  (FieldSpec catalog)  ->  Candidate  ->  RankerOutput  ->  FinalRanking
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


# Standard name of the real candidate ID after intake. privacy.pseudonymize replaces it with a ref.
ID_FIELD = "candidate_id"


class FieldSpec(BaseModel):
    """One field of the standard candidate format. Every data source is mapped into these.

    Data sources name their columns differently ("UCID" vs "Student Number"). Intake matches
    each source column to a FieldSpec by its id or aliases, then applies the field's handling.

    Attributes:
        id: Standard name. Criteria list these in `relevant_fields`.
        handling: "key" = the real candidate ID (replaced by a ref, never sent to an LLM; the
            catalog must have exactly one, with id `ID_FIELD`). "drop" = sensitive, always
            removed. "keep" = may be sent to the rankers whose criteria need it.
        aliases: Other column names that mean the same thing. Compared after normalizing
            (lowercase, runs of spaces/punctuation -> "_"), so "Student Number" matches
            "student_number".
        description: What the field holds, for people filling in the template.
    """

    id: str
    handling: Literal["key", "drop", "keep"]
    aliases: list[str] = Field(default_factory=list)
    description: str = ""


class IntakeReport(BaseModel):
    """What intake did with each source column. Column names only, never values.

    Attributes:
        column_mapping: Source column -> standard field, for every column that was kept.
        dropped_sensitive: Columns matching a "drop" field (names, contact info, protected
            characteristics, ...).
        dropped_unused: Recognized columns that no active criterion needs, or that the client
            mapped to None.
        dropped_unmatched: Columns that match no standard field. Dropped by default, so an
            unexpected sensitive column is never sent.
        missing_fields: Fields an active criterion needs that no column provides.
    """

    column_mapping: dict[str, str]
    dropped_sensitive: list[str]
    dropped_unused: list[str]
    dropped_unmatched: list[str]
    missing_fields: list[str]


class FieldNote(BaseModel):
    """How a ranker should read one data field (written by the criteria planner)."""

    field: str = Field(description="Field name from the provided list, spelled exactly")
    meaning: str = Field(description="What the field holds and how to read its values, e.g. "
                                     "'self-rated Python level; Beginner < Intermediate < Expert'")


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
        relevant_fields: Fields this ranker is allowed to see (one or more). Everything else
            is withheld (data minimization; see `privacy.view_for`). Intake keeps only fields
            some active criterion lists here.
        field_notes: How to read the fields, shown to the ranker. Fields without a note are
            shown by name only.
        type: "registered" for built-in criteria, "bonus" for client-defined ones, "auto" for
            ones the criteria planner created from the data (see `planning`).
        constraint: "hard" lets the ranker exclude candidates outright (e.g. must be available
            on lab days). "soft"/"none" means the ranker can only score.
        importance: Auto criteria only: 1 = minor, 2 = normal, 3 = major. Sets the weight an
            auto criterion gets when it joins a request it wasn't named in.
        courses: Auto criteria only: course codes the criterion is specific to (e.g. interest
            in TAing "ENGG 204"). It joins a request automatically only if the role names one.
    """

    id: str
    name: str
    description: str
    scoring_guidance: str
    relevant_fields: list[str]
    field_notes: list[FieldNote] = Field(default_factory=list)
    type: Literal["registered", "bonus", "auto"] = "registered"
    constraint: Literal["hard", "soft", "none"] = "none"
    importance: int = 2
    courses: list[str] = Field(default_factory=list)


class Candidate(BaseModel):
    """A candidate after sensitive fields are removed.

    Attributes:
        ref: Pseudonymous ID (e.g. "C-3f9a12bc"). This is the only identifier an LLM ever sees.
            Map it back to a real ID with the `ref_map` returned by `privacy.pseudonymize`.
        fields: Remaining non-sensitive data (program, courses, experience, ...).
    """

    ref: str
    fields: dict[str, Any]


class CourseSession(BaseModel):
    """A weekly session the hire must cover (lab, tutorial, ...), e.g. from a course outline.

    Same day/time format as a candidate's `availability` slots, so the Scheduler can match them.
    Carried on `HiringRequest.sessions`; not used for ranking until the Scheduler exists.
    """

    label: str = Field(description="What it is, e.g. 'Lab B01' or 'Tutorial T02'")
    day: str = Field(description="mon, tue, wed, thu, fri, sat or sun")
    start: str = Field(description="24-hour HH:MM, e.g. '14:00'")
    end: str = Field(description="24-hour HH:MM, after start")


class HiringRequest(BaseModel):
    """What the client is hiring for and how candidates should be judged.

    Attributes:
        role: Free-text description of the position, given to every ranker as context.
        criteria: Maps criterion_id to weight. Only criteria listed here run; all others "sleep".
            Weights are relative (0.4/0.4/0.2 and 2/2/1 give the same result). A weight of
            0 skips the criterion, and an auto criterion listed with 0 won't join on its own.
        top_x: How many candidates to return in the final ranking.
        notes: Optional extra instructions from the client, given to every ranker.
        instructions: Optional extra instructions for one ranker, keyed by criterion_id
            (e.g. {"technical_skills": "Python matters most; ignore CAD"}). The Manager agent
            writes these from free text and documents (see `drafting`), or the client sets them.
        sessions: Weekly sessions the hire must cover (from a course outline), for the
            Scheduler. Not used for ranking yet.
        documents: Names of the store's documents the request was drafted from (a record only).
        custom_criteria: Client-defined bonus criteria, registered for this request only. They
            must still be listed in `criteria` with a weight to be used.
        auto_criteria: Use the data's criteria catalog (see `planning`): requested built-in
            criteria also see the data fields fitted to them, and auto criteria that apply to
            the role join with their own rankers. Auto criteria can also be named in
            `criteria` with a weight. False = only the fields criteria list explicitly.
    """

    role: str
    criteria: dict[str, float]
    top_x: int = Field(default=5, ge=1)
    notes: str = ""
    instructions: dict[str, str] = Field(default_factory=dict)
    custom_criteria: list[Criterion] = Field(default_factory=list)
    auto_criteria: bool = True
    sessions: list[CourseSession] = Field(default_factory=list)
    documents: list[str] = Field(default_factory=list)



# --- Manager agent (LLM structured output; see llm_agents/manager.py and drafting.py) ---

class CriterionChoice(BaseModel):
    """The Manager's weight and instructions for one criterion."""

    criterion_id: str = Field(description="A criterion id from the provided list, spelled exactly")
    weight: float = Field(description="0 = don't use, 1 = minor, 2 = normal, 3 = important, "
                                      "4 = very important, 5 = the most important")
    instructions: str = Field(description="What this criterion's ranker should focus on, taken from the "
                                          "client's text; empty if the text says nothing specific about it")
    reason: str = Field(description="One short sentence on why this weight, referring to the client's text")


class ManagerOutput(BaseModel):
    """The Manager's reading of a client's free-text instructions, before validation."""

    role: str = Field(description="Short description of the position, including any course codes; "
                                  "empty if the text doesn't say")
    top_x: int = Field(description="How many candidates the client wants; 0 if not stated")
    notes: str = Field(description="Guidance that applies to every ranker; empty if none")
    criteria: list[CriterionChoice] = Field(description="Every provided criterion exactly once")
    sessions: list[CourseSession] = Field(description="Weekly sessions the hire must cover, from the "
                                                      "documents (labs, tutorials, ...); empty if none")
    not_covered: list[str] = Field(description="Parts of the instructions no criterion can act on "
                                               "(e.g. scheduling, or anything protected), each with why")


class RequestBrief(BaseModel):
    """A free-text ranking request for the Manager agent to turn into a HiringRequest.

    Attributes:
        instructions: What the client is looking for, in their own words. May be empty if
            `documents` are given (e.g. just a course outline).
        documents: Names of documents in the store (course outline, job description, ...)
            for the Manager to read.
        role: The position, if the client wants to set it themselves (overrides the Manager's).
        top_x: How many candidates to return, if set (overrides the Manager's).
    """

    instructions: str = Field(default="", max_length=4000)
    documents: list[str] = Field(default_factory=list)
    role: str = ""
    top_x: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _needs_something(self):
        if not self.instructions.strip() and not self.documents:
            raise ValueError("Give instructions, documents, or both.")
        return self


class DocumentInfo(BaseModel):
    """A supporting document in the store.

    Attributes:
        name: File name; use it in `RequestBrief.documents`.
        kind: pdf, docx, txt or md.
        chars: Characters of text found (0 if unreadable).
        truncated: Longer than the Manager reads; only the start is used.
        problem: Why the text can't be read, if so.
    """

    name: str
    kind: str
    chars: int
    truncated: bool
    problem: str


class DocumentsResult(BaseModel):
    """The outcome of adding documents to the store."""

    added: list[str]
    replaced: list[str]
    documents: list[DocumentInfo]


class RequestDraft(BaseModel):
    """The Manager's proposed request, for the client to review (and edit) before ranking.

    Attributes:
        request: A complete HiringRequest. It lists every criterion, with weight 0 for those not
            to use, so auto criteria join only if the Manager weighted them.
        reasons: Why each criterion got its weight, keyed by criterion_id.
        not_covered: Parts of the instructions the request can't act on.
        warnings: Fixes made to the Manager's output, and other things to check.
    """

    request: HiringRequest
    reasons: dict[str, str]
    not_covered: list[str]
    warnings: list[str]


# --- Criteria planner (LLM structured output; see llm_agents/criteria_planner.py) ---
# Lists instead of dicts throughout: strict structured-output schemas don't allow free-form keys.

class FieldProfile(BaseModel):
    """What the planner is told about one data field. Never a candidate's own answer.

    Attributes:
        field: Field name in the candidate data.
        label: The original column header (e.g. a full survey question), if known.
        filled: How many candidates have a value.
        kind: "number", "category" (a few short repeated values), or "text" (free text).
        common_values: For categories only, values shared by several candidates
            (e.g. "Expert", "Yes"); rare values are never included.
    """

    field: str
    label: str = ""
    filled: int
    kind: Literal["number", "category", "text"]
    common_values: list[str]


class CriterionExtension(BaseModel):
    """Data fields that are evidence for an existing (built-in) criterion."""

    criterion_id: str
    fields: list[FieldNote]


class DraftCriterion(BaseModel):
    """A new criterion covering one or more related fields that no existing criterion covers."""

    id: str = Field(description="snake_case id, e.g. 'technical_skills'")
    name: str
    description: str = Field(description="What the ranker should look for")
    scoring_guidance: str = Field(description="What a 0, 5 and 10 look like")
    fields: list[FieldNote] = Field(description="The fields this criterion covers, each with how to read it")
    importance: int = Field(description="1 = minor, 2 = normal, 3 = major relevance to the hiring purpose")
    courses: list[str] = Field(description="Course codes this criterion is specific to (e.g. 'ENGG 204'); "
                                           "empty if it applies to any role")


class SkippedField(BaseModel):
    """A field the planner chose not to rank on, with the reason shown to the client."""

    field: str
    reason: str


class CriteriaDraft(BaseModel):
    """The planner's output (LLM structured output), before validation."""

    extensions: list[CriterionExtension]
    criteria: list[DraftCriterion]
    skipped: list[SkippedField]


class CriteriaCatalog(BaseModel):
    """The validated criteria for one dataset, saved as candidate_data/criteria.json.

    Safe to edit by hand (e.g. reword a rubric, change an importance, move a field); edits
    are kept until the data's fields change (see `planning`).

    Attributes:
        purpose: What the hiring is for, as given to the planner.
        fields: Every field the planner was offered, to detect when the data has changed.
        extensions: Fields added to built-in criteria.
        criteria: The auto criteria (type "auto").
        skipped: Fields not ranked on, with reasons.
    """

    purpose: str
    fields: list[str]
    extensions: list[CriterionExtension]
    criteria: list[Criterion]
    skipped: list[SkippedField]


class CriterionUsed(BaseModel):
    """One criterion that ran, for the client to review.

    Attributes:
        id, name: The criterion.
        source: "requested" (named in the request) or "auto" (an auto criterion that joined
            because it applies to the role).
        weight: Share of the total weight, 0-1.
        fields: Fields its ranker saw.
    """

    id: str
    name: str
    source: Literal["requested", "auto"]
    weight: float
    fields: list[str]


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
        intake: How the source columns were mapped, dropped or found missing.
        criteria: The criteria that ran, with their weights and fields.
        skipped_fields: Data fields the planner chose not to rank on, with reasons.
    """

    role: str
    ranking: list[RankedCandidate]
    excluded: list[ExcludedCandidate]
    warnings: list[str]
    intake: IntakeReport | None = None
    criteria: list[CriterionUsed] = Field(default_factory=list)
    skipped_fields: list[SkippedField] = Field(default_factory=list)


# --- Service layer (the future API's request/response bodies; see service.py) ---

class DataStatus(BaseModel):
    """What is in the candidate store.

    Attributes:
        files: Data files in the store's raw/ folder.
        candidates: Candidates in the compiled store (0 if it isn't compiled).
        compiled_at: When candidates.json was last written (ISO time), or None.
        criteria_planned: Whether criteria.json exists.
        stale: A raw file is newer than candidates.json, so the store needs processing.
    """

    files: list[str]
    candidates: int
    compiled_at: str | None
    criteria_planned: bool
    stale: bool


class UploadResult(BaseModel):
    """The outcome of adding data files and processing the store.

    Attributes:
        added, replaced: File names added to the store, and existing ones overwritten.
        sources: What intake did with each file/sheet's columns (column names only).
        criteria_planned: True if the Criteria Planner ran (False if criteria.json was reused
            or planning was turned off).
        warnings: Problems found while processing (skipped sheets, conflicts, ...).
        status: The store afterwards.
    """

    added: list[str]
    replaced: list[str]
    sources: dict[str, IntakeReport]
    criteria_planned: bool
    warnings: list[str]
    status: DataStatus


class CriterionInfo(BaseModel):
    """A criterion a request can weight, for a client to show (e.g. as a weight slider).

    Attributes:
        id, name, description: The criterion; use `id` in `HiringRequest.criteria`.
        type: "registered" (built-in) or "auto" (planned from the data).
        fields: Data fields its ranker sees.
        courses: Auto criteria only: courses it is specific to.
        suggested_weight: A starting weight: 1 for built-ins, importance / 2 for auto ones
            (the weight an auto criterion gets when it joins automatically, relative to 1).
    """

    id: str
    name: str
    description: str
    type: str
    fields: list[str]
    courses: list[str]
    suggested_weight: float


class RankingResult(BaseModel):
    """The response to a ranking request.

    Attributes:
        request: The request as run.
        ranking: The final ranking (pseudonymous refs).
        candidate_ids: Real candidate ID for each ref in the ranking and the excluded list
            only, so the response doesn't carry every applicant's ID.
    """

    request: HiringRequest
    ranking: FinalRanking
    candidate_ids: dict[str, str]
