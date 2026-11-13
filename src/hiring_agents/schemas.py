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


class ColumnDecision(BaseModel):
    """The header screen's decision for one column header (see screening.py)."""

    column: str = Field(description="The column header, spelled exactly as given.")
    action: Literal["id", "drop", "keep"] = Field(
        description="id = the candidate's unique ID column; drop = identifies a person or holds "
                    "sensitive personal data; keep = anything else.")
    reason: str = Field(description="A short reason; start it with 'Unsure:' if in doubt.")


class HeaderScreenOutput(BaseModel):
    """What the Header Screen agent returns: one decision per header it was given."""

    columns: list[ColumnDecision]


SCREEN_NOTE = ("Header screen: one decision per column header, made from the header text only. "
               "drop = removed before any value reaches an AI; id = the candidate ID (replaced by "
               "a code); keep = passed on. Edit an action to override it, then process the store "
               "again. Headers not listed here are screened when they first appear. Delete this "
               "file to screen every header again.")


class HeaderScreen(BaseModel):
    """The saved header screen (header_screen.json in the store), hand-editable.

    Attributes:
        note: How to read and edit the file.
        columns: Every header screened so far, across all files.
    """

    note: str = SCREEN_NOTE
    columns: list[ColumnDecision]

    def decisions(self) -> dict[str, ColumnDecision]:
        return {d.column: d for d in self.columns}


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
        notes: Guidance for the Manager's final review (e.g. a summary of the position and
            preferences that cut across criteria). Not sent to rankers: each ranker gets only
            the role and its own `instructions`, so it judges its criterion's fields alone.
        client_instructions: The client's own words (the free text the request was drafted
            from), for the final review. Not sent to rankers.
        final_review: Let the Manager review the shortlist against the client's instructions
            and notes and nudge each weighted score by at most review.MAX_ADJUSTMENT, with a
            reason (see review.py). Skipped when there are no instructions or notes.
        instructions: Optional extra instructions for one ranker, keyed by criterion_id
            (e.g. {"technical_skills": "Python matters most; ignore CAD"}). The Manager agent
            writes these from free text and documents (see `drafting`), or the client sets them.
        sessions: Weekly sessions the hire must cover (from a course outline), for the
            Scheduler. Not used for ranking yet.
        documents: Names of the store's documents the request was drafted from (a record only).
        custom_criteria: Client-defined bonus criteria, registered for this request only. They
            must still be listed in `criteria` with a weight to be used.
        catalog: The criteria planned for this request from the client's instructions (see
            service.draft_request). When set, ranking uses it instead of the store's
            criteria.json, so a saved request re-ranks with exactly the same criteria.
        auto_criteria: Use the data's criteria catalog (see `planning`): requested built-in
            criteria also see the data fields fitted to them, and auto criteria that apply to
            the role join with their own rankers. Auto criteria can also be named in
            `criteria` with a weight. False = only the fields criteria list explicitly.
    """

    role: str
    criteria: dict[str, float]
    top_x: int = Field(default=5, ge=1)
    notes: str = ""
    client_instructions: str = Field(default="", max_length=4000)
    final_review: bool = True
    instructions: dict[str, str] = Field(default_factory=dict)
    custom_criteria: list[Criterion] = Field(default_factory=list)
    auto_criteria: bool = True
    sessions: list[CourseSession] = Field(default_factory=list)
    documents: list[str] = Field(default_factory=list)
    catalog: "CriteriaCatalog | None" = None



# --- Manager's final review (LLM structured output; see review.py) ---

class CandidateReview(BaseModel):
    """The Manager's review of one shortlisted candidate."""

    candidate_ref: str
    adjustment: float = Field(description="Added to the weighted score, from -0.10 to +0.10. 0 unless "
                                          "the client's instructions call for a change.")
    reason: str = Field(description="For a non-zero adjustment: the client's instruction it applies "
                                    "and the ranker evidence it rests on. Empty for 0.")
    justification: str = Field(description="One or two sentences: why this candidate ranks where they "
                                           "do for this job, citing the rankers' evidence.")
    tiebreak: float = Field(description="-1 to 1, from other_info only: how well the candidate's other "
                                        "information fits this job. Only orders candidates whose final "
                                        "scores are tied; 0 if there's no other information.")
    tiebreak_reason: str = Field(description="One short sentence on what in other_info decided the "
                                             "tiebreak value; empty for 0.")


class ReviewOutput(BaseModel):
    """What the final review returns."""

    candidates: list[CandidateReview]
    summary: str = Field(description="Two or three sentences on how the instructions shaped the result.")


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


# --- Job spreadsheets (jobs.py): Manager designs the columns, Data Agent maps data onto them ---

FieldKind = Literal["number", "category", "yes_no", "text", "list"]
MapMethod = Literal["copy", "value_map", "combine", "count_yes", "list_yes", "max", "sum"]


class SchemaField(BaseModel):
    """One column of a job spreadsheet."""

    id: str = Field(description="snake_case column id.")
    label: str = Field(description="Column header a person would read.")
    description: str = Field(description="What the value holds, e.g. 'Self-rated Python skill'.")
    kind: FieldKind = Field(description="number, category (one of `values`), yes_no, text, or list.")
    values: list[str] = Field(description="For category only: the allowed values, lowest to highest "
                                          "if they are levels. Empty otherwise.")


class SchemaGroup(BaseModel):
    """Columns that are evidence for the same quality; a draft ranking criterion."""

    id: str = Field(description="snake_case group id.")
    name: str
    purpose: str = Field(description="One sentence: why this matters for the job.")
    fields: list[SchemaField]


class JobSchema(BaseModel):
    """The spreadsheet designed for one job (the Manager's output, saved as schema.json)."""

    role: str
    summary: str = Field(description="One or two sentences on what the job needs.")
    groups: list[SchemaGroup]

    def columns(self) -> list[tuple[SchemaGroup, SchemaField]]:
        return [(g, f) for g in self.groups for f in g.fields]


class ValuePair(BaseModel):
    source_value: str
    value: str


class FieldMapping(BaseModel):
    """How code fills one column from the candidate data (see jobs.py for each method)."""

    field_id: str = Field(description="The column's id from the job schema.")
    sources: list[str] = Field(description="Data field names that feed the column, spelled exactly.")
    method: MapMethod
    value_map: list[ValuePair] = Field(description="For value_map only: source value -> column value.")
    note: str = Field(description="One short sentence on why these sources fit.")


class Gap(BaseModel):
    name: str = Field(description="A column id (unfilled) or data field name (unplaced).")
    reason: str


class DataMapping(BaseModel):
    """The Data Agent's output, saved as mapping.json (editable)."""

    fields: list[FieldMapping]
    unfilled: list[Gap] = Field(description="Columns no data field can fill.")
    unplaced: list[Gap] = Field(description="Data fields relevant to the job that fit no column.")
    other: list[str] = Field(description="Data fields to carry along as is, e.g. for tie-breaks.")
    free_text: list[str] = Field(description="Data fields whose answers are written in the candidate's "
                                             "or a reviewer's own words (reviews, comments, experience "
                                             "descriptions), however short.")


class FreeTextColumn(BaseModel):
    """A column of the free-text sheet: its key in the job table and the header a reader sees."""

    key: str = Field(description="Job table column id, or 'other.<field>'.")
    header: str


class FreeTextSheet(BaseModel):
    """What the text extraction tool gets (and returns): a cut-down job table with only the
    candidate's pseudonymous ref and the free-text columns (see textagent.py).

    Attributes:
        columns: The free-text columns, in order, with their headers.
        rows: One per candidate with any text: {"candidate_ref": "C-...", <key>: text, ...}.
            A cell is a string, or a list of strings for list columns; empty cells are left out.
    """

    columns: list[FreeTextColumn]
    rows: list[dict[str, str | list[str]]]

    def to_csv(self) -> str:
        """The sheet as CSV: "Candidate" then each column's header; lists joined with "; "."""
        import csv
        import io
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")   # the file write adds the OS line ending
        writer.writerow(["Candidate"] + [c.header for c in self.columns])
        for row in self.rows:
            cells = [row.get(c.key, "") for c in self.columns]
            writer.writerow([row["candidate_ref"]] + ["; ".join(v) if isinstance(v, list) else v for v in cells])
        return out.getvalue()


class JobInfo(BaseModel):
    """A job spreadsheet's metadata (job.json).

    Attributes:
        id: Folder name under the store's jobs/.
        brief: What the client asked for.
        candidates: Rows in the table.
        revised: Whether the Manager revised the design after the Data Agent's report.
        warnings: Problems found while designing, mapping or filling.
        source: "store" (designed and mapped from the candidate store) or "import" (read from
            a job workbook; its rows are kept in the job's source.json).
    """

    id: str
    role: str
    created_at: str
    brief: RequestBrief
    candidates: int
    revised: bool
    warnings: list[str]
    source: Literal["store", "import"] = "store"


class JobColumn(BaseModel):
    """A column of the job table, with how it was filled."""

    id: str
    label: str
    group: str
    kind: FieldKind
    sources: list[str]
    method: str = Field(description="The mapping method, or '' if the column is unfilled.")
    filled: int = Field(description="Candidates with a value.")
    free_text: bool = Field(default=False, description="Fed by a free-text field, so it goes to text extraction.")


class JobResult(BaseModel):
    """A job spreadsheet: its design, mapping, column summary and where the table is."""

    info: JobInfo
    design: JobSchema
    mapping: DataMapping
    columns: list[JobColumn]
    free_text_columns: list[str] = Field(
        default_factory=list,
        description="Table columns holding free text, sent to text extraction: column ids, and "
                    "'other.<field>' for free-text fields carried in `other`.")
    table_csv: str = Field(description="Path of table.csv (holds real IDs; local only).")


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
        criteria: The criteria this request can weight (planned from the instructions; see
            `request.catalog`), for a client to show.
    """

    request: HiringRequest
    reasons: dict[str, str]
    not_covered: list[str]
    warnings: list[str]
    criteria: list["CriterionInfo"] = Field(default_factory=list)


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
        avg_words: Average words per answer, over every answer (0 for numbers).
        sentence_share: Share of answers that read like prose (planning.is_sentence: 6+
            words, mostly ordinary lowercase words). Tells free-text answers from short codes
            or lists.
    """

    field: str
    label: str = ""
    filled: int
    kind: Literal["number", "category", "text"]
    common_values: list[str]
    avg_words: float = 0.0
    sentence_share: float = 0.0


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


SkipUse = Literal["tiebreak", "availability", "administrative", "protected", "identifying", "unusable"]
# Skipped fields with these uses are never shown to any agent as "other" information.
# "unreviewed" = a catalog planned before fields were labelled with a use: excluded to be safe.
EXCLUDED_USES = {"availability", "administrative", "protected", "identifying", "unusable", "unreviewed"}


class PlannedSkip(BaseModel):
    """A field the planner leaves out of every criterion (LLM structured output)."""

    field: str
    reason: str
    use: SkipUse = Field(description=(
        "tiebreak = says something about the candidate but isn't evidence for any criterion "
        "(e.g. motivation, general comments, unrelated skills): kept as other information for "
        "breaking ties; availability = time slots or scheduling; administrative = timestamps, "
        "consent, form metadata; protected = a protected characteristic or a proxy for one; "
        "identifying = identifies a person; unusable = empty, shifted or garbled."))


class SkippedField(BaseModel):
    """A field no criterion uses, with the reason shown to the client.

    Attributes:
        use: What it's good for (see PlannedSkip): "tiebreak" fields are other information the
            final review may use to order candidates whose scores are tied; every other use is
            never shown to an agent. "unused_criterion" = its criterion didn't run for this request;
            "unreviewed" = planned before uses existed (re-plan to label it).
    """

    field: str
    reason: str
    use: str = "unreviewed"


class CriteriaDraft(BaseModel):
    """The planner's output (LLM structured output), before validation."""

    extensions: list[CriterionExtension]
    criteria: list[DraftCriterion]
    skipped: list[PlannedSkip]


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
        total_score: The final score from 0 to 1: the weighted score plus the final review's
            adjustment (equal to weighted_score when there was no review).
        weighted_score: The plain weighted average of the ranker scores (aggregation.py).
        adjustment: What the Manager's final review added or subtracted (at most
            review.MAX_ADJUSTMENT either way), and adjustment_reason why.
        justification: The Manager's summary of why the candidate ranks here for this job.
        tiebreak_reason: Set when the candidate's final score tied with another's and the other
            information (fields no criterion uses) decided their order.
        per_criterion: Raw 0-10 score from each ranker, keyed by criterion_id.
        rationales: Each ranker's explanation, keyed by criterion_id.
    """

    candidate_ref: str
    total_score: float
    weighted_score: float | None = None
    adjustment: float = 0.0
    adjustment_reason: str = ""
    justification: str = ""
    tiebreak_reason: str = ""
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
        reviewed: Whether the Manager's final review ran, and review_summary its overview.
        tiebreak_fields: The other-information fields (no criterion uses them) the review could
            use to break ties.
    """

    role: str
    ranking: list[RankedCandidate]
    reviewed: bool = False
    review_summary: str = ""
    tiebreak_fields: list[str] = Field(default_factory=list)
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


# Forward references (models used before they are defined).
HiringRequest.model_rebuild()
RequestDraft.model_rebuild()
