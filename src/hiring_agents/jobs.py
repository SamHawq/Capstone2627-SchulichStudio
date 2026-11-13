"""Job spreadsheets: turn the candidate store into a table designed for one job.

Flow (agents never see each other's output; this code passes everything along):

    brief + documents + data field names/headers (no values)
      -> Manager, schema task (llm_agents/manager.py)   JobSchema: column groups for this job
      -> validate_schema                                ids cleaned, caps, no sensitive columns
    JobSchema + field summaries + a few pseudonymized sample rows
      -> Data Agent (llm_agents/data_agent.py)          DataMapping: sources + method per column
      -> validate_mapping                               unknown fields/columns dropped, gaps listed
    gaps (unfilled columns or relevant data with no column)?
      -> one revision: Manager again with its design + the report, then the Data Agent again
    -> build_table                                      code applies the mapping to every candidate
    -> candidate_data/jobs/<id>/                        job.json, schema.json, mapping.json
                                                        (both editable), table.json, table.csv

What each agent sees: the Manager gets field names and headers only. The Data Agent gets field
summaries (as the Criteria Planner does: no individual answers) plus SAMPLE_ROWS sample rows.
The sample rows have already been screened (screening.py), have their IDs replaced with codes
and go through privacy.pseudonymize's sensitive-field filter, so no real ID reaches either agent.

Mapping methods (applied by `fill_value` to each candidate):
    copy       one source, value as is
    value_map  one source, values translated by the pairs (case-insensitive); others kept
    combine    several sources: text -> "Header: answer; ...", list -> all values,
               yes_no -> yes if any source is yes, number -> first number found
    count_yes  how many sources have a yes-like answer (not empty/no/none/false/0/n/a)
    list_yes   the headers (last part) of the sources with a yes-like answer
    max, sum   of the numbers in the sources
Free text: the Data Agent lists the data fields written in someone's own words (it sees each
field's average answer length and share of prose answers, measured by code over every answer),
and the code check adds any field with enough prose answers. Text and list columns fed by those
fields, and free-text fields in `other`, are the job's free_text_columns; after the table is
filled they alone go to text_extraction before anything ranks them, as a cut-down sheet: a
pseudonymous ref per candidate plus those columns with their headers (saved as free_text.csv).

Then the value is coerced to the column's kind. A candidate with no source value gets no value
for that column (rankers treat it as missing). Fields the Data Agent marks as `other` are
copied into an "other" column as is; availability is carried along for the Scheduler.

`reapply_job` rebuilds the table from the saved (possibly hand-edited) schema and mapping with
no LLM call, e.g. after editing mapping.json or processing new data.
"""

import csv
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .agent_trace import run_with_retries, trace_step, traced_operation
from .availability import AVAILABILITY_FIELD
from .datastore import Store, load_field_labels, load_store
from .intake import DEFAULT_FIELDS, normalize
from .llm_agents import build_data_agent, build_schema_manager
from .planning import load_catalog, profile_fields
from .privacy import pseudonymize
from .job_workbook import WorkbookError, read_workbook, write_workbook
from .screening import screen_headers
from .text_extraction import OTHER_PREFIX, extract_free_text
from .schemas import (
    EXCLUDED_USES, ID_FIELD, CriteriaCatalog, DataMapping, FieldMapping, FieldProfile, FreeTextSheet, Gap,
    JobColumn, JobInfo, JobResult, JobSchema, RequestBrief, SchemaField,
)

logger = logging.getLogger(__name__)

MAX_GROUPS = 8
MAX_COLUMNS = 30
SAMPLE_ROWS = 3       # sample rows shown to the Data Agent
SAMPLE_CHARS = 200    # each sample value is cut to this length
OTHER_FIELD = "other"
FREE_TEXT_SENTENCE_SHARE = 0.3   # a text field with this share of prose answers is free text (code check)
RESERVED = {ID_FIELD, OTHER_FIELD, AVAILABILITY_FIELD}
NO_WORDS = {"", "no", "n", "false", "0", "none", "never", "n/a", "na", "not applicable", "-"}
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_JOB_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,90}$")

# Column ids/labels that name sensitive data: intake's "drop" aliases, plus protected words.
_SENSITIVE_NAMES = {normalize(n) for f in DEFAULT_FIELDS if f.handling == "drop" for n in [f.id, *f.aliases]}
_PROTECTED_WORDS = {"gender", "sex", "ethnicity", "race", "religion", "disability", "citizenship",
                    "nationality", "pronouns", "age", "dob", "marital"}


class JobError(ValueError):
    """A job can't be found, or its saved files can't be used."""


# --- Validation (plain code) ---

def _unique(base: str, taken: set[str]) -> str:
    cid, n = base, 2
    while cid in taken:
        cid, n = f"{base}_{n}", n + 1
    taken.add(cid)
    return cid


def _is_sensitive(*names: str) -> bool:
    for name in names:
        n = normalize(name)
        if n in _SENSITIVE_NAMES or set(n.split("_")) & _PROTECTED_WORDS:
            return True
    return False


def validate_schema(design: JobSchema, client_role: str = "") -> tuple[JobSchema, list[str]]:
    """Clean the Manager's design: snake_case unique ids, at most MAX_GROUPS groups and
    MAX_COLUMNS columns, no sensitive or reserved column ids, category values only for
    categories, empty groups dropped. A role the client set wins.

    Raises:
        ValueError: If no usable column is left.
    """
    warnings: list[str] = []
    group_ids: set[str] = set()
    field_ids: set[str] = set()
    groups, total = [], 0
    for g in design.groups:
        if len(groups) >= MAX_GROUPS or total >= MAX_COLUMNS:
            cap = f"{MAX_GROUPS} groups" if len(groups) >= MAX_GROUPS else f"{MAX_COLUMNS} columns"
            warnings.append(f"The design had more than {cap}; group '{g.name}' and later ones were dropped.")
            break
        fields = []
        for f in g.fields:
            if total >= MAX_COLUMNS:
                warnings.append(f"The design had more than {MAX_COLUMNS} columns; '{f.label}' and later ones were dropped.")
                break
            if _is_sensitive(f.id, f.label):
                warnings.append(f"Column '{f.label}' looks like personal or protected data, so it was removed.")
                continue
            base = normalize(f.id) or normalize(f.label) or "column"
            fid = _unique(f"{base}_value" if base in RESERVED else base, field_ids)
            values = [v.strip() for v in f.values if v.strip()] if f.kind == "category" else []
            fields.append(f.model_copy(update={"id": fid, "label": f.label.strip() or fid, "values": values}))
            total += 1
        if fields:
            gid = _unique(normalize(g.id) or normalize(g.name) or "group", group_ids)
            groups.append(g.model_copy(update={"id": gid, "fields": fields}))
        elif g.fields:
            warnings.append(f"Group '{g.name}' had no usable columns and was dropped.")
    if not groups:
        raise ValueError("The Manager designed no usable columns for this job.")
    schema = JobSchema(role=client_role.strip() or design.role.strip(), summary=design.summary.strip(),
                       groups=groups)
    return schema, warnings


YES_NO_WORDS = {"yes", "no", "y", "n", "true", "false"}


def _form_mismatch(column: SchemaField, method: str, sources: list[FieldProfile]) -> str | None:
    """Why the sources can't honestly fill a category column with levels, or None.

    Turning yes/no answers or free-text sentences into levels would invent information (and
    judging free text is the rankers' job), so such a mapping counts as unfilled instead,
    with a reason the Manager can act on in the revision round.
    """
    if column.kind != "category" or method in ("count_yes", "list_yes", "max", "sum"):
        return None
    if any(p.kind == "text" for p in sources):
        return "The data is free text, not one of the column's values; make it a text column."
    levels = {v.lower() for v in column.values}
    if levels and not levels <= YES_NO_WORDS and all(
            p.kind == "category" and p.common_values and {v.lower() for v in p.common_values} <= YES_NO_WORDS
            for p in sources):
        return "The data is yes/no only, not a level; make it a yes_no column."
    return None


def validate_mapping(mapping: DataMapping, schema: JobSchema,
                     profiles: list[FieldProfile]) -> tuple[DataMapping, list[str]]:
    """Check the Data Agent's mapping against the schema and the data.

    Mappings for unknown or repeated columns are dropped; unknown sources are removed (a
    mapping left with none becomes unfilled); copy/value_map with several sources becomes
    combine; a mapping that would turn yes/no answers or free text into category levels
    becomes unfilled (see `_form_mismatch`); every column ends up either mapped or unfilled.
    `unplaced` and `other` keep only real data fields that no mapping uses.

    Args:
        profiles: The data fields (planning.profile_fields), as given to the Data Agent.

    Returns:
        (mapping, warnings): mappings in schema order.
    """
    by_field = {p.field: p for p in profiles}
    available = set(by_field)
    columns = {f.id: f for _, f in schema.columns()}
    warnings: list[str] = []
    mapped: dict[str, FieldMapping] = {}
    reasons: dict[str, str] = {}
    for m in mapping.fields:
        if m.field_id not in columns:
            warnings.append(f"The mapping names unknown column '{m.field_id}'; ignored.")
            continue
        if m.field_id in mapped:
            warnings.append(f"Column '{m.field_id}' was mapped twice; the first mapping is kept.")
            continue
        sources = list(dict.fromkeys(m.sources))
        unknown = [s for s in sources if s not in available]
        if unknown:
            warnings.append(f"Column '{m.field_id}': unknown data fields {unknown} were removed from its sources.")
            sources = [s for s in sources if s in available]
        if not sources:
            reasons.setdefault(m.field_id, "No usable data field.")
            continue
        method = m.method
        if method in ("copy", "value_map") and len(sources) > 1:
            warnings.append(f"Column '{m.field_id}': {method} takes one source, so its {len(sources)} are combined.")
            method = "combine"
        mismatch = _form_mismatch(columns[m.field_id], method, [by_field[s] for s in sources])
        if mismatch:
            warnings.append(f"Column '{m.field_id}' wasn't filled: {mismatch}")
            reasons.setdefault(m.field_id, mismatch)
            continue
        mapped[m.field_id] = m.model_copy(update={
            "sources": sources, "method": method,
            "value_map": m.value_map if method == "value_map" else []})
    for gap in mapping.unfilled:
        if gap.name in columns and gap.name not in mapped:
            reasons.setdefault(gap.name, gap.reason)
    for fid in columns:
        if fid not in mapped:
            reasons.setdefault(fid, "The data agent didn't map it.")

    used = {s for m in mapped.values() for s in m.sources}
    unplaced, seen = [], set()
    for gap in mapping.unplaced:
        if gap.name in available and gap.name not in used and gap.name not in seen:
            seen.add(gap.name)
            unplaced.append(gap)
    other = [o for o in dict.fromkeys(mapping.other)
             if o in available and o not in used and o != AVAILABILITY_FIELD]
    free_text = [f for f in dict.fromkeys(mapping.free_text) if f in available]
    result = DataMapping(
        fields=[mapped[fid] for fid in columns if fid in mapped],
        unfilled=[Gap(name=fid, reason=reasons[fid]) for fid in columns if fid in reasons],
        unplaced=unplaced, other=other, free_text=free_text)
    return result, warnings


# --- Applying a mapping (plain code) ---

def _empty(x: Any) -> bool:
    return x is None or (isinstance(x, str) and not x.strip())


def _flat(value: Any) -> list[Any]:
    return [x for x in (value if isinstance(value, list) else [value]) if not _empty(x)]


def is_yes(x: Any) -> bool:
    """A yes-like answer: anything except empty, no, none, false, 0, n/a, ..."""
    if isinstance(x, bool):
        return x
    return not _empty(x) and str(x).strip().lower() not in NO_WORDS


def as_number(x: Any) -> float | int | None:
    """A number from a number or text ("3", "3.5", "3 years"); None if there isn't one."""
    if isinstance(x, bool) or _empty(x):
        return None
    if isinstance(x, (int, float)):
        n = x
    else:
        m = _NUMBER.search(str(x))
        if not m:
            return None
        n = float(m.group())
    return int(n) if float(n).is_integer() else n


def short_label(field: str, labels: dict[str, str]) -> str:
    """The last part of a field's original header ("Skill checklist / Python" -> "Python")."""
    return (labels.get(field) or field).rsplit(" / ", 1)[-1].strip()


def fill_value(record: dict[str, Any], mapping: FieldMapping, column: SchemaField,
               labels: dict[str, str]) -> tuple[Any, str | None]:
    """One candidate's value for one column (see the module docstring for the methods).

    Returns:
        (value, issue): value is None if the candidate has no data for it; issue names a
        problem to count ("unmapped", "not_a_number", "not_in_values") or is None.
    """
    present = [(s, record[s]) for s in mapping.sources if s in record and _flat(record[s])]
    if not present:
        return None, None
    method, kind, issue = mapping.method, column.kind, None

    if method == "copy":
        value = present[0][1]
    elif method == "value_map":
        table = {p.source_value.strip().lower(): p.value for p in mapping.value_map}
        allowed = {v.lower() for v in column.values}
        items = _flat(present[0][1])
        mapped = [table.get(str(x).strip().lower(), x) for x in items]
        # A value the map doesn't list is only a problem if it isn't already an allowed value.
        if any(str(x).strip().lower() not in table and str(x).strip().lower() not in allowed for x in items):
            issue = "unmapped"
        value = mapped[0] if len(mapped) == 1 else mapped
    elif method == "combine":
        items = [x for _, v in present for x in _flat(v)]
        if kind == "list":
            value = list(dict.fromkeys(items))
        elif kind == "yes_no":
            value = any(is_yes(x) for x in items)
        elif kind == "number":
            value = next((n for x in items if (n := as_number(x)) is not None), None)
        elif len(present) == 1:
            value = present[0][1]
        else:
            value = "; ".join(f"{short_label(s, labels)}: {', '.join(map(str, _flat(v)))}" for s, v in present)
    elif method == "count_yes":
        value = sum(1 for _, v in present if any(is_yes(x) for x in _flat(v)))
    elif method == "list_yes":
        value = [short_label(s, labels) for s, v in present if any(is_yes(x) for x in _flat(v))]
    else:  # max / sum
        numbers = [n for _, v in present for x in _flat(v) if (n := as_number(x)) is not None]
        value = (max(numbers) if method == "max" else sum(numbers)) if numbers else None

    # Coerce to the column's kind.
    if value is None:
        return None, issue or ("not_a_number" if kind == "number" else None)
    if kind == "number":
        n = as_number(value[0] if isinstance(value, list) and value else value)
        return (n, issue) if n is not None else (None, "not_a_number")
    if kind == "yes_no":
        yes = value if isinstance(value, bool) else any(is_yes(x) for x in _flat(value))
        return ("yes" if yes else "no"), issue
    if kind == "list":
        return _flat(value), issue
    if kind == "text":
        return ("; ".join(map(str, value)) if isinstance(value, list) else str(value)), issue
    # category: snap to the allowed spelling if it matches ignoring case
    allowed = {v.lower(): v for v in column.values}
    items = [allowed.get(str(x).strip().lower(), x) for x in _flat(value)]
    if allowed and any(str(x).lower() not in allowed for x in items):
        issue = issue or "not_in_values"
    return (items[0] if len(items) == 1 else items), issue


_ISSUE_TEXT = {
    "unmapped": "had values the value map doesn't cover (kept as they were)",
    "not_a_number": "had values that aren't numbers (left empty)",
    "not_in_values": "had values outside the column's list (kept as they were)",
}


def build_table(records: list[dict[str, Any]], schema: JobSchema, mapping: DataMapping,
                labels: dict[str, str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Apply the mapping to every candidate: one row each, keyed by column id, plus `other`
    (fields carried as is) and availability if the store has it.

    Returns:
        (rows, warnings): warnings count problem values per column (never the values).
    """
    columns = {f.id: f for _, f in schema.columns()}
    issues: dict[tuple[str, str], int] = {}
    rows = []
    for rec in records:
        row: dict[str, Any] = {ID_FIELD: rec[ID_FIELD]}
        for m in mapping.fields:
            value, issue = fill_value(rec, m, columns[m.field_id], labels)
            if issue:
                issues[(m.field_id, issue)] = issues.get((m.field_id, issue), 0) + 1
            if value is not None:   # [] from list_yes = answered, nothing ticked
                row[m.field_id] = value
        other = {f: rec[f] for f in mapping.other if f in rec}
        if other:
            row[OTHER_FIELD] = other
        if AVAILABILITY_FIELD in rec:
            row[AVAILABILITY_FIELD] = rec[AVAILABILITY_FIELD]
        rows.append(row)
    warnings = [f"Column '{fid}': {n} candidate(s) {_ISSUE_TEXT[issue]}."
                for (fid, issue), n in sorted(issues.items())]
    return rows, warnings


# --- Free text ---

def fill_other(mapping: DataMapping, profiles: list[FieldProfile],
               catalog: CriteriaCatalog | None) -> DataMapping:
    """Every data field no column uses lands in `other` (other information, for tie-breaks),
    except fields the Criteria Planner marked availability, administrative, protected,
    identifying or unusable. Without a criteria catalog to say which those are, the Data
    Agent's own `other` list is kept as it is."""
    if catalog is None:
        return mapping
    used = {s for m in mapping.fields for s in m.sources}
    excluded = {s.field for s in catalog.skipped if s.use in EXCLUDED_USES}
    other = [p.field for p in profiles if p.field not in used and p.field not in excluded]
    return mapping.model_copy(update={"other": other})


def looks_like_free_text(profile: FieldProfile) -> bool:
    """The code check: a text field where at least FREE_TEXT_SENTENCE_SHARE of the answers
    read like prose (planning.is_sentence), measured over every answer. Short free text
    ("Great TA") passes this check only via the Data Agent."""
    return profile.kind == "text" and profile.sentence_share >= FREE_TEXT_SENTENCE_SHARE


def add_detected_free_text(mapping: DataMapping, profiles: list[FieldProfile]) -> tuple[DataMapping, list[str]]:
    """The Data Agent's free-text fields plus those the code check finds (union: when in
    doubt, a field goes to text extraction)."""
    detected = [p.field for p in profiles if looks_like_free_text(p) and p.field not in mapping.free_text]
    if detected:
        logger.info("[job] Free text found by the length check only: %s", detected)
    return mapping.model_copy(update={"free_text": mapping.free_text + detected}), []


def free_text_columns(schema: JobSchema, mapping: DataMapping) -> list[str]:
    """The table's columns that hold free text, for text extraction: text and list columns
    fed by a free-text field, and free-text fields carried in `other` (as "other.<field>")."""
    free = set(mapping.free_text)
    kinds = {f.id: f.kind for _, f in schema.columns()}
    columns = [m.field_id for m in mapping.fields
               if set(m.sources) & free and kinds[m.field_id] in ("text", "list")]
    return columns + [f"{OTHER_PREFIX}{f}" for f in mapping.other if f in free]


def free_text_headers(schema: JobSchema, mapping: DataMapping, labels: dict[str, str]) -> dict[str, str]:
    """Free-text column key -> the header the extraction tool sees: the column's label, or
    for a field carried in `other`, its original column header."""
    names = {f.id: f.label for _, f in schema.columns()}
    return {k: names.get(k) or labels.get(k[len(OTHER_PREFIX):]) or k[len(OTHER_PREFIX):]
            for k in free_text_columns(schema, mapping)}


def summarize_columns(schema: JobSchema, mapping: DataMapping, rows: list[dict[str, Any]]) -> list[JobColumn]:
    by_field = {m.field_id: m for m in mapping.fields}
    free = set(free_text_columns(schema, mapping))
    return [
        JobColumn(id=f.id, label=f.label, group=g.name, kind=f.kind,
                  sources=by_field[f.id].sources if f.id in by_field else [],
                  method=by_field[f.id].method if f.id in by_field else "",
                  filled=sum(1 for r in rows if f.id in r), free_text=f.id in free)
        for g, f in schema.columns()
    ]


# --- Agent calls ---

def _clip(value: Any) -> Any:
    if isinstance(value, list):
        return [_clip(v) for v in value]
    if isinstance(value, str) and len(value) > SAMPLE_CHARS:
        return value[:SAMPLE_CHARS] + "…"
    return value


def sample_rows(records: list[dict[str, Any]], fields: set[str], n: int = SAMPLE_ROWS) -> list[dict[str, Any]]:
    """`n` of the most complete candidates, pseudonymized (codes, not IDs; sensitive field
    names removed), limited to `fields`, with long values cut to SAMPLE_CHARS."""
    candidates, _ = pseudonymize(records)   # the ref -> ID map is thrown away
    fullest = sorted(candidates, key=lambda c: -sum(1 for f in c.fields if f in fields))[:n]
    return [{"candidate_ref": c.ref, **{f: _clip(v) for f, v in c.fields.items() if f in fields}}
            for c in fullest]


async def run_schema_manager(
    brief: RequestBrief, documents: list[dict[str, str]], profiles: list[FieldProfile],
    previous: JobSchema | None = None, report: DataMapping | None = None,
) -> JobSchema:
    """One Manager call (schema task). Gets field names and headers only, never values."""
    payload: dict[str, Any] = {
        "client_instructions": brief.instructions,
        "role_set_by_client": brief.role,
        "documents": documents,
        "available_data": [{"field": p.field, "label": p.label, "kind": p.kind, "filled": p.filled}
                           for p in profiles],
    }
    if previous is not None and report is not None:
        payload["previous_design"] = previous.model_dump()
        payload["data_agent_report"] = {"unfilled": [g.model_dump() for g in report.unfilled],
                                        "unplaced": [g.model_dump() for g in report.unplaced]}
    return await run_with_retries(build_schema_manager(), json.dumps(payload, indent=2, ensure_ascii=False),
                                  JobSchema, "job schema")


async def run_data_agent(schema: JobSchema, profiles: list[FieldProfile],
                         samples: list[dict[str, Any]]) -> DataMapping:
    """One Data Agent call: the design, field summaries and pseudonymized sample rows."""
    payload = {
        "job_schema": schema.model_dump(),
        "data_fields": [p.model_dump() for p in profiles],
        "sample_rows": samples,
    }
    return await run_with_retries(build_data_agent(), json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                                  DataMapping, "data agent")


# --- Job files ---

def job_dir(store: Store, job_id: str) -> Path:
    if not _JOB_ID.match(job_id):
        raise JobError(f"Invalid job id {job_id!r}.")
    path = store.jobs / job_id
    if not path.is_dir():
        raise JobError(f"No job named {job_id!r}.")
    return path


def _new_job_id(store: Store, role: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-")[:40].strip("-") or "job"
    base = f"{slug}-{datetime.now():%Y%m%d-%H%M%S}"
    job_id, n = base, 2
    while (store.jobs / job_id).exists():
        job_id, n = f"{base}-{n}", n + 1
    return job_id


def load_labels(store: Store) -> dict[str, str]:
    """Field -> original column header, saved when the store was processed ({} if not)."""
    return load_field_labels(store)


def _csv_value(value: Any) -> str:
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, list):
        return "; ".join(map(str, value))
    return "" if value is None else str(value)


def _write_table(folder: Path, schema: JobSchema, rows: list[dict[str, Any]], with_other: bool) -> Path:
    (folder / "table.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    header = [ID_FIELD] + [f.id for _, f in schema.columns()] + ([OTHER_FIELD] if with_other else [])
    labels = ["Candidate ID"] + [f.label for _, f in schema.columns()] + (["Other"] if with_other else [])
    path = folder / "table.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:   # BOM so Excel reads UTF-8
        writer = csv.writer(f)
        writer.writerow(labels)
        for row in rows:
            writer.writerow([_csv_value(row.get(h)) for h in header])
    return path


def _save(folder: Path, info: JobInfo, schema: JobSchema, mapping: DataMapping,
          rows: list[dict[str, Any]], sheet: FreeTextSheet) -> JobResult:
    folder.mkdir(parents=True, exist_ok=True)
    # What the text extraction tool was sent: refs (not IDs) and free-text columns only.
    sheet_path = folder / "free_text.csv"
    if sheet.rows:
        sheet_path.write_text(sheet.to_csv(), encoding="utf-8-sig")
    else:
        sheet_path.unlink(missing_ok=True)
    (folder / "job.json").write_text(info.model_dump_json(indent=2), encoding="utf-8")
    (folder / "schema.json").write_text(schema.model_dump_json(indent=2), encoding="utf-8")
    (folder / "mapping.json").write_text(mapping.model_dump_json(indent=2), encoding="utf-8")
    csv_path = _write_table(folder, schema, rows, bool(mapping.other))
    return JobResult(info=info, design=schema, mapping=mapping,
                     columns=summarize_columns(schema, mapping, rows),
                     free_text_columns=free_text_columns(schema, mapping), table_csv=str(csv_path))


def _read(path: Path, model):
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise JobError(f"{path.parent.name}/{path.name} can't be read (check any edits): {e}") from e


# --- Trace summaries (names and counts only) ---

def _trace_design(schema: JobSchema, warnings: list[str]) -> None:
    trace_step("job", "Design validated", role=schema.role,
               groups={g.name: [f"{f.id} ({f.kind})" for f in g.fields] for g in schema.groups},
               fixes=warnings)


def _trace_mapping(mapping: DataMapping, warnings: list[str]) -> None:
    trace_step("job", "Mapping validated",
               mapped={m.field_id: f"{m.method}({', '.join(m.sources)})" for m in mapping.fields},
               unfilled={g.name: g.reason for g in mapping.unfilled},
               unplaced={g.name: g.reason for g in mapping.unplaced},
               free_text=mapping.free_text, other=mapping.other, fixes=warnings)


def _trace_table(schema: JobSchema, mapping: DataMapping, rows: list[dict[str, Any]], warnings: list[str]) -> None:
    trace_step("job", "Table built", rows=len(rows),
               filled={c.id: c.filled for c in summarize_columns(schema, mapping, rows)},
               problems=warnings)


# --- Entry points (wrapped by service.py) ---

@traced_operation("job create", lambda brief, *a, **k: brief.role or brief.instructions[:120])
async def create_job(
    brief: RequestBrief, store: Store, documents: list[dict[str, str]], warnings: list[str],
    revise: bool = True,
) -> JobResult:
    """Design, map and fill a spreadsheet for one job from the store (2 LLM calls, 4 with the
    revision). See the module docstring.

    Args:
        brief: The client's instructions, role and document names.
        store: The candidate store (must be compiled).
        documents: The documents' scrubbed text (documents.load_context).
        warnings: Warnings so far (e.g. from loading documents); returned with the job.
        revise: Allow one revision round when the Data Agent reports gaps.

    Raises:
        ValueError: If the Manager's design has no usable column.
        RuntimeError: If an agent call fails.
    """
    records = load_store(store.candidates)
    labels = load_labels(store)
    profiles = profile_fields(records, exclude=set(), labels=labels)   # no ID or availability
    available = {p.field for p in profiles}
    samples = sample_rows(records, available)
    warnings = list(warnings)

    trace_step("job", "Data profiled", fields=len(profiles), candidates=len(records),
               sample_rows=[s["candidate_ref"] for s in samples], documents=[d["name"] for d in documents])
    schema, w = validate_schema(await run_schema_manager(brief, documents, profiles), brief.role)
    _trace_design(schema, w)
    mapping, mw = validate_mapping(await run_data_agent(schema, profiles, samples), schema, profiles)
    _trace_mapping(mapping, mw)
    revised = False
    if revise and (mapping.unfilled or mapping.unplaced):
        logger.info("[job] Revising: %d unfilled columns, %d unplaced fields",
                    len(mapping.unfilled), len(mapping.unplaced))
        new_schema, w = validate_schema(
            await run_schema_manager(brief, documents, profiles, previous=schema, report=mapping), brief.role)
        if new_schema.model_dump(exclude={"summary"}) != schema.model_dump(exclude={"summary"}):
            schema, revised = new_schema, True
            _trace_design(schema, w)
            mapping, mw = validate_mapping(await run_data_agent(schema, profiles, samples), schema, profiles)
            _trace_mapping(mapping, mw)
        else:
            logger.info("[job] The revised design is unchanged; keeping the first mapping")
            trace_step("job", "Revision unchanged", note="Same design; the first mapping is kept.")
    mapping = fill_other(mapping, profiles, load_catalog(store.criteria))
    mapping, _ = add_detected_free_text(mapping, profiles)
    warnings += w + mw

    rows, table_warnings = build_table(records, schema, mapping, labels)
    _trace_table(schema, mapping, rows, table_warnings)
    rows, sheet, extract_warnings = await extract_free_text(rows, free_text_headers(schema, mapping, labels))
    warnings += table_warnings + extract_warnings
    if mapping.unfilled:
        warnings.append(f"{len(mapping.unfilled)} column(s) have no data: "
                        f"{[g.name for g in mapping.unfilled]} (reasons in mapping.json).")
    job_id = _new_job_id(store, schema.role)
    info = JobInfo(id=job_id, role=schema.role, created_at=datetime.now().isoformat(timespec="seconds"),
                   brief=brief, candidates=len(rows), revised=revised, warnings=warnings)
    logger.info("[job] %s: %d columns (%d filled), %d rows", job_id, len(schema.columns()),
                len(mapping.fields), len(rows))
    return _save(store.jobs / job_id, info, schema, mapping, rows, sheet)


@traced_operation("job apply", lambda job_id, *a, **k: job_id)
async def reapply_job(job_id: str, store: Store) -> JobResult:
    """Rebuild a job's table from its saved schema.json and mapping.json (hand edits
    included) and the store's current data, then run text extraction again. No LLM call.
    The mapping's free_text list is used as saved, so removing a field there by hand sticks.

    Raises:
        JobError: For an unknown job or a file that doesn't validate.
    """
    folder = job_dir(store, job_id)
    info = _read(folder / "job.json", JobInfo)
    saved = _read(folder / "schema.json", JobSchema)
    try:
        schema, warnings = validate_schema(saved)
    except ValueError as e:
        raise JobError(str(e)) from e
    saved_mapping = _read(folder / "mapping.json", DataMapping)
    if info.source == "import":
        # An imported job's rows are its own (source.json); each column fills from itself.
        try:
            records = json.loads((folder / "source.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise JobError(f"{job_id}/source.json can't be read: {e}") from e
        labels: dict[str, str] = {}
        mapping, mw = _import_mapping(schema, records, set(saved_mapping.free_text)), []
    else:
        try:
            records = load_store(store.candidates)
        except FileNotFoundError as e:
            raise JobError("No candidate data yet; upload some first.") from e
        labels = load_labels(store)
        profiles = profile_fields(records, exclude=set(), labels=labels)
        mapping, mw = validate_mapping(saved_mapping, schema, profiles)
    rows, table_warnings = build_table(records, schema, mapping, labels)
    _trace_table(schema, mapping, rows, table_warnings)
    rows, sheet, extract_warnings = await extract_free_text(rows, free_text_headers(schema, mapping, labels))
    info = info.model_copy(update={"role": schema.role, "candidates": len(rows),
                                   "warnings": warnings + mw + table_warnings + extract_warnings})
    return _save(folder, info, schema, mapping, rows, sheet)


# --- Job workbooks (export / import; see job_workbook.py) ---

IMPORT_NOTE = "Imported from a job workbook: the column fills from itself."


def _import_mapping(schema: JobSchema, records: list[dict[str, Any]], free: set[str]) -> DataMapping:
    """Each column copies its own values; columns with no values in any row are unfilled."""
    present = {k for r in records for k in r}
    ids = [f.id for _, f in schema.columns()]
    return DataMapping(
        fields=[FieldMapping(field_id=i, sources=[i], method="copy", value_map=[], note=IMPORT_NOTE)
                for i in ids if i in present],
        unfilled=[Gap(name=i, reason="No values in the imported workbook.") for i in ids if i not in present],
        unplaced=[], other=[], free_text=[i for i in ids if i in free and i in present])


def export_job(job_id: str, store: Store) -> Path:
    """Write the job as a job workbook (<job id>.xlsx in its folder) and return the path."""
    job = load_job(job_id, store)
    rows = json.loads((store.jobs / job_id / "table.json").read_text(encoding="utf-8"))
    return write_workbook(job, rows, store.jobs / job_id / f"{job_id}.xlsx")


@traced_operation("job import", lambda path, *a, **k: Path(path).name)
async def import_job(path: Path, store: Store) -> JobResult:
    """Make a job from a job workbook, skipping the Manager's design and the Data Agent's
    mapping: the workbook's Design sheet is the design and each column fills from itself.

    Privacy checks still run: the column labels go through the header screen (an LLM call
    only for labels not screened before), columns whose id or label names personal or
    protected data are removed (validate_schema), values are coerced to each column's kind,
    and free-text columns go to text extraction.

    Raises:
        JobError: If the workbook can't be used (the message says what to fix).
        ScreeningError: If the header screen call fails.
    """
    path = Path(path)
    try:
        design, free, records, warnings = read_workbook(path)
    except WorkbookError as e:
        raise JobError(str(e)) from e
    labels = [f.label for _, f in design.columns()]
    screen, _, screen_warnings = await screen_headers(labels, store.header_screen)
    decisions = screen.decisions()
    dropped = [lb for lb in labels if lb in decisions and decisions[lb].action in ("drop", "id")]
    if dropped:
        warnings.append(f"The header screen dropped columns {dropped}; their values were not imported.")
    kept = design.model_copy(update={"groups": [
        g.model_copy(update={"fields": [f for f in g.fields if f.label not in dropped]}) for g in design.groups]})
    try:
        schema, schema_warnings = validate_schema(kept)
    except ValueError as e:
        raise JobError(f"{path.name}: {e}") from e
    # validate_schema may clean ids; carry the values over by label.
    new_id = {f.label: f.id for _, f in schema.columns()}
    label_of = {f.id: f.label for _, f in kept.columns()}
    recs = [{ID_FIELD: r["candidate_id"], **{new_id[label_of[k]]: v for k, v in r.items()
                                              if k in label_of and label_of[k] in new_id}} for r in records]
    mapping = _import_mapping(schema, recs, {new_id[label_of[k]] for k in free if label_of.get(k) in new_id})

    rows, table_warnings = build_table(recs, schema, mapping, {})
    _trace_table(schema, mapping, rows, table_warnings)
    rows, sheet, extract_warnings = await extract_free_text(rows, free_text_headers(schema, mapping, {}))
    job_id = _new_job_id(store, schema.role or "imported job")
    info = JobInfo(id=job_id, role=schema.role, created_at=datetime.now().isoformat(timespec="seconds"),
                   brief=RequestBrief(instructions=f"Imported from {path.name}"), candidates=len(rows),
                   revised=False, source="import",
                   warnings=warnings + screen_warnings + schema_warnings + table_warnings + extract_warnings)
    folder = store.jobs / job_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "source.json").write_text(json.dumps(recs, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    logger.info("[job] %s imported from %s: %d columns, %d rows", job_id, path.name, len(schema.columns()), len(rows))
    return _save(folder, info, schema, mapping, rows, sheet)


def load_job(job_id: str, store: Store) -> JobResult:
    folder = job_dir(store, job_id)
    schema = _read(folder / "schema.json", JobSchema)
    mapping = _read(folder / "mapping.json", DataMapping)
    try:
        rows = json.loads((folder / "table.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise JobError(f"{job_id}/table.json can't be read: {e}") from e
    return JobResult(info=_read(folder / "job.json", JobInfo), design=schema, mapping=mapping,
                     columns=summarize_columns(schema, mapping, rows),
                     free_text_columns=free_text_columns(schema, mapping), table_csv=str(folder / "table.csv"))


def list_jobs(store: Store) -> list[JobInfo]:
    """Every job in the store, newest first. Jobs whose job.json can't be read are skipped."""
    if not store.jobs.exists():
        return []
    jobs = []
    for folder in store.jobs.iterdir():
        try:
            jobs.append(_read(folder / "job.json", JobInfo))
        except JobError:
            continue
    return sorted(jobs, key=lambda j: j.created_at, reverse=True)
