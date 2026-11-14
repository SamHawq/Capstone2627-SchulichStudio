"""Intake: maps candidate data from any column layout into the standard field format.

Data sources name and arrange their columns differently ("UCID" vs "Student Number").
Intake matches every source column against the field catalog (`FieldSpec`s) and handles it
by that field's rule:

    key   the real candidate ID, renamed to `ID_FIELD` so privacy.pseudonymize can replace it
    drop  sensitive (name, contact info, protected characteristics); removed here
    keep  rankable; kept only if an active criterion lists it in `relevant_fields`

The default catalog has only the ID and the sensitive names. Every other column keeps its own
(normalized) name, and the Criteria Planner decides by reasoning which criterion it is evidence
for; no column is renamed to a guessed "standard" field. When preparing data for rankers, only
the columns an active criterion lists are kept, so an unexpected or misspelled sensitive
column is still never sent.

Matching uses column NAMES only. Values pass through unchanged, so personal details written
inside free text are not removed (see Known limitations in docs/ROADMAP.md).
"""

import csv
import io
import json
import logging
import re
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from .schemas import ID_FIELD, FieldSpec, IntakeReport

logger = logging.getLogger(__name__)

# The ID and the sensitive names. Other columns are not renamed to guessed "standard" fields:
# they keep their own normalized names and the Criteria Planner assigns them to criteria.
# A client can still pass its own catalog (with "keep" fields) to `standardize`.
DEFAULT_FIELDS = [
    FieldSpec(id=ID_FIELD, handling="key", description="The candidate's real unique ID",
              aliases=["ucid", "student_id", "student_number", "student_no", "applicant_id", "id"]),

    FieldSpec(id="name", handling="drop",
              aliases=["full_name", "first_name", "last_name", "given_name", "surname",
                       "family_name", "preferred_name", "legal_name"]),
    FieldSpec(id="contact", handling="drop",
              aliases=["email", "email_address", "personal_email", "phone", "phone_number",
                       "mobile", "address", "mailing_address", "postal_code"]),
    FieldSpec(id="date_of_birth", handling="drop", aliases=["dob", "birthdate", "birth_date", "age"]),
    FieldSpec(id="protected_characteristic", handling="drop",
              aliases=["ethnicity", "race", "sex", "gender", "pronouns", "religion", "disability",
                       "citizenship", "nationality", "marital_status", "sin",
                       "social_insurance_number"]),
]


class NoIdColumnError(ValueError):
    """No column in the data could be matched to the candidate ID."""


def normalize(name: str) -> str:
    """Normalize a column name for matching: "Courses & Grades" -> "courses_grades"."""
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")


def _cell_value(value: Any) -> Any:
    """Dates and times -> ISO strings. Excel stores every date as a datetime, so midnight
    datetimes become plain dates ("2026-09-02")."""
    if isinstance(value, datetime) and value.time() == time(0):
        return value.date().isoformat()
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return value


def _filled(row: list[Any]) -> int:
    return sum(not _is_empty(v) for v in row)


def _header_names(group: list[Any] | None, header: list[Any]) -> list[str | None]:
    """Column names from the header row, prefixed by the group row above it if there is one.

    Survey exports often put group labels above the real headers, with each label spanning
    the columns to its right until the next label:

        Student details |            |           | Wish to TA | TA experience
        Student ID      | First Name | Last Name | ENGG 225   | ENGG 225

    gives "Student details / Student ID", ..., "Wish to TA / ENGG 225",
    "TA experience / ENGG 225". A column with a group label but no header (a long survey
    question) is named by the label. Columns with neither get None and are skipped.
    """
    names, current = [], None
    for i, h in enumerate(header):
        g = group[i] if group and i < len(group) and not _is_empty(group[i]) else None
        current = str(g).strip() if g is not None else current
        h = str(h).strip() if not _is_empty(h) else None
        if h and current:
            names.append(f"{current} / {h}")
        else:
            names.append(h or (str(g).strip() if g is not None else None))
    return names


def _rows_to_records(rows: list[list[Any]], label: str) -> list[dict[str, Any]]:
    """Turn a grid of cells (a sheet or a CSV) into one dict per data row.

    The header row is the first row with a cell that names the candidate ID (e.g. "Student
    ID", "UCID"); if there is none, the first row with at least two filled cells. If the row
    just above the header has at least two filled cells, it's a group row (see
    `_header_names`). Rows above that (titles) and fully empty rows are skipped, as are
    columns with no name.
    """
    id_names = {normalize(n) for f in DEFAULT_FIELDS if f.handling == "key" for n in [f.id, *f.aliases]}
    h = next((i for i, r in enumerate(rows) if any(normalize(c) in id_names for c in r if not _is_empty(c))),
             next((i for i, r in enumerate(rows) if _filled(r) >= 2), None))
    if h is None:
        return []
    group = rows[h - 1] if h > 0 and _filled(rows[h - 1]) >= 2 else None
    names = _header_names(group, rows[h])
    dupes = sorted({n for n in names if n and names.count(n) > 1})
    if dupes:
        raise ValueError(f"Duplicate column headers in {label}: {dupes}")
    return [{name: _cell_value(value) for name, value in zip(names, row) if name is not None}
            for row in rows[h + 1:] if _filled(row)]


def _read_sheet(ws, label: str) -> list[dict[str, Any]]:
    """Read one sheet (see `_rows_to_records`). Formulas give their last computed value, and
    dates become ISO strings so the records stay JSON-serializable."""
    return _rows_to_records([list(r) for r in ws.iter_rows(values_only=True)], label)


def _load_excel(path: Path, sheet: str | None) -> list[dict[str, Any]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet is not None and sheet not in wb.sheetnames:
            raise ValueError(f"No sheet '{sheet}' in {path.name}; sheets: {wb.sheetnames}")
        ws = wb[sheet] if sheet is not None else wb.worksheets[0]
        return _read_sheet(ws, path.name)
    finally:
        wb.close()


def load_sources(path: str | Path) -> list[tuple[str, list[dict[str, Any]]]]:
    """Read every table in a file as (label, records): one per sheet for Excel, one otherwise.

    Labels look like "applicants.xlsx [Fall 2026]" and are used in reports and errors.
    """
    path = Path(path)
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        return [(path.name, load_records(path))]
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        return [(f"{path.name} [{ws.title}]", _read_sheet(ws, f"{path.name} [{ws.title}]"))
                for ws in wb.worksheets]
    finally:
        wb.close()


# Runs of non-ASCII characters; candidates for mojibake repair in `_fix_mojibake`.
_NON_ASCII_RUN = re.compile(r"[^\x00-\x7f]{2,}")


def _fix_mojibake(text: str) -> str:
    """Repair UTF-8 text that was once misread as Mac Roman ("‚Äô" -> "’", "‚Äú" -> "“").

    Mac Excel exports often contain this. Each run of non-ASCII characters is repaired only
    if it turns back into valid UTF-8, so correctly encoded accents ("é", "ñ") are untouched.
    """
    def repair(m: re.Match) -> str:
        try:
            return m.group().encode("mac_roman").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return m.group()
    return _NON_ASCII_RUN.sub(repair, text)


def _read_text(path: Path) -> str:
    """Read a text file whatever encoding Excel saved it in, then repair mojibake.

    Tries UTF-8 (utf-8-sig also strips Excel's byte-order mark), then Windows-1252 (Excel's
    plain "CSV" format on Windows), then Latin-1, which accepts any byte.
    """
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if encoding != "utf-8-sig":
        logger.info("%s is not UTF-8; read as %s", path.name, encoding)
    return _fix_mojibake(text)


def load_records(path: str | Path, sheet: str | None = None) -> list[dict[str, Any]]:
    """Read candidate records from a file.

    Supported: .json (list of objects), .csv, and .xlsx/.xlsm (one row per candidate; `sheet`
    picks the sheet by name, default the first).
    """
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        return _load_excel(path, sheet)
    if path.suffix.lower() == ".csv":
        return _rows_to_records(list(csv.reader(io.StringIO(_read_text(path), newline=""))), path.name)
    if path.suffix.lower() == ".json":
        records = json.loads(_read_text(path))
        if not isinstance(records, list):
            raise ValueError(f"{path} must contain a JSON list of candidate objects.")
        return records
    raise ValueError(f"Unsupported file type '{path.suffix}' (use .json, .csv or .xlsx).")


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def report_warnings(report: IntakeReport, unmatched: bool = True, missing: bool = True) -> list[str]:
    """Client-facing warnings for missing fields and unrecognized columns (each optional).

    Kept separate from `standardize` because whether these matter depends on the caller: when
    compiling the store from split files, missing fields are expected.
    """
    warnings = [f"No column provides '{fid}'; criteria using it get no data for it."
                for fid in report.missing_fields] if missing else []
    if unmatched and report.dropped_unmatched:
        warnings.append(f"Unrecognized columns dropped: {report.dropped_unmatched}. "
                        "Map them with column_mapping if a criterion needs them.")
    return warnings


def _name_lookup(catalog) -> dict[str, FieldSpec]:
    """Normalized field id or alias -> its field."""
    return {normalize(name): f for f in catalog for name in [f.id, *f.aliases]}


def _match_by_name(col: str, lookup: dict[str, FieldSpec]) -> FieldSpec | None:
    # Grouped headers ("Student details / Student ID") also match on their last part.
    return lookup.get(normalize(col)) or lookup.get(normalize(col.rsplit(" / ", 1)[-1]))


def id_columns(
    columns: list[str],
    fields: list[FieldSpec] | None = None,
    column_mapping: dict[str, str | None] | None = None,
) -> list[str]:
    """The columns `standardize` would take as the candidate ID: mapped to it in
    `column_mapping`, or (if not mapped at all) named by the ID field's aliases."""
    lookup = _name_lookup(fields or DEFAULT_FIELDS)
    explicit = {normalize(col): target for col, target in (column_mapping or {}).items()}
    found = []
    for col in columns:
        n = normalize(col)
        spec = _match_by_name(col, lookup)
        if spec is not None and spec.handling == "drop":
            continue
        if explicit.get(n) == ID_FIELD or (n not in explicit and spec is not None and spec.handling == "key"):
            found.append(col)
    return found


def standardize(
    raw_records: list[dict[str, Any]],
    needed: set[str],
    fields: list[FieldSpec] | None = None,
    column_mapping: dict[str, str | None] | None = None,
    keep_unmatched: bool = False,
) -> tuple[list[dict[str, Any]], IntakeReport, list[str]]:
    """Rename source columns to standard fields and drop everything that isn't needed.

    Each column is matched in this order:
      1. A column whose name matches a "drop" field is always dropped, even if
         `column_mapping` says otherwise.
      2. `column_mapping` (source column -> standard field id, or None to drop it).
      3. The standard fields' ids and aliases.
    Fields in `needed` that aren't in the catalog (e.g. from a client's bonus criterion) are
    matched by exact normalized name, as long as that name isn't a sensitive one.

    Empty values (None, blank strings) are left out, so rankers treat them as missing data.

    Args:
        raw_records: Records from the data source, one dict per candidate.
        needed: Standard fields the active criteria use (their `relevant_fields`).
        fields: The field catalog. Defaults to `DEFAULT_FIELDS`.
        column_mapping: Client overrides for columns the aliases don't cover.
        keep_unmatched: Keep unrecognized columns under their normalized name instead of
            dropping them (sensitive names are still dropped). Used when compiling the local
            candidate store, so no data is lost before a bonus criterion asks for it. Leave
            False when preparing data for rankers.

    Returns:
        (records, report, warnings): records keyed by standard field names (always including
        `ID_FIELD`), the per-column report, and warnings about criterion fields. Warnings
        about missing or unrecognized columns come from `report_warnings(report)`.

    Raises:
        NoIdColumnError: If no column holds the candidate ID.
        ValueError: If two columns map to the same field,
            or `column_mapping` names an unknown field. (Rows with no ID value are skipped
            with a warning.)
    """
    catalog = {f.id: f for f in fields or DEFAULT_FIELDS}
    if [f.id for f in catalog.values() if f.handling == "key"] != [ID_FIELD]:
        raise ValueError(f"The field catalog needs exactly one 'key' field, with id '{ID_FIELD}'.")
    lookup = _name_lookup(catalog.values())
    warnings: list[str] = []

    for fid in sorted(needed - catalog.keys()):
        match = lookup.get(normalize(fid))
        if match is None:
            catalog[fid] = lookup[normalize(fid)] = FieldSpec(id=fid, handling="keep")
        elif match.handling == "drop":
            warnings.append(f"Criterion field '{fid}' is sensitive and will not be sent to any ranker.")
        else:
            warnings.append(f"Criterion field '{fid}' is an alias; use the standard name '{match.id}'.")

    explicit = {normalize(col): target for col, target in (column_mapping or {}).items()}
    report = IntakeReport(column_mapping={}, dropped_sensitive=[], dropped_unused=[],
                          dropped_unmatched=[], missing_fields=[])
    taken: dict[str, str] = {}  # standard field -> source column

    # Union of columns across all records, in first-seen order (records may differ).
    for col in dict.fromkeys(k for r in raw_records for k in r):
        n = normalize(col)
        by_name = _match_by_name(col, lookup)
        if by_name is not None and by_name.handling == "drop":
            report.dropped_sensitive.append(col)
            continue
        if n in explicit:
            if explicit[n] is None:
                report.dropped_unused.append(col)
                continue
            spec = catalog.get(explicit[n])
            if spec is None:
                raise ValueError(f"column_mapping maps '{col}' to unknown field '{explicit[n]}'.")
        else:
            spec = by_name

        if spec is None and keep_unmatched and normalize(col) and normalize(col) not in taken:
            taken[n] = col
            report.column_mapping[col] = n
        elif spec is None:
            report.dropped_unmatched.append(col)
        elif spec.handling == "drop":
            report.dropped_sensitive.append(col)
        elif spec.handling == "keep" and spec.id not in needed:
            report.dropped_unused.append(col)
        elif spec.id in taken:
            raise ValueError(f"Columns '{taken[spec.id]}' and '{col}' both map to '{spec.id}'; "
                             "use column_mapping to pick one.")
        else:
            taken[spec.id] = col
            report.column_mapping[col] = spec.id

    if ID_FIELD not in taken:
        aliases = ", ".join(catalog[ID_FIELD].aliases)
        raise NoIdColumnError(f"No candidate ID column found (expected one of: {aliases}); "
                         f"use column_mapping to name it, e.g. {{'My ID': '{ID_FIELD}'}}.")

    report.missing_fields = sorted(needed - taken.keys())

    records, no_id = [], 0
    for raw in raw_records:
        rec = {report.column_mapping[c]: v for c, v in raw.items()
               if c in report.column_mapping and not _is_empty(v)}
        if ID_FIELD in rec:
            records.append(rec)
        else:
            no_id += 1
    if no_id:
        warnings.append(f"{no_id} row(s) had no value in the ID column '{taken[ID_FIELD]}' and were skipped.")

    # Column names only; values are never logged.
    logger.info("Intake mapping: %s", report.column_mapping)
    logger.info("Intake dropped: sensitive=%s unused=%s unmatched=%s",
                report.dropped_sensitive, report.dropped_unused, report.dropped_unmatched)
    return records, report, warnings
