"""Local candidate store: compiles many data files into one standardized JSON for the orchestrator.

Layout (at the repo root, gitignored because it holds real candidate data):

    candidate_data/
        raw/              drop the sponsor's Excel / CSV / JSON files here
        candidates.json   the compiled store; rank_candidates reads it by default
        header_screen.json  which column headers were dropped before any value reached an LLM
                          (see screening.py); editable
        criteria.json     the criteria planned for the store (see planning.py); editable
        field_labels.json each field's original column header (for the job agents)
        jobs/<id>/        job spreadsheets designed for one job each (see jobs.py)
        documents/        course outlines, job descriptions, ... for the Manager (see documents.py)

`compile_sources` reads every sheet of every file, removes the columns the header screen
dropped (screening.apply_screen), maps each sheet's remaining columns to the standard fields
(intake.standardize), and merges rows that share a candidate ID. So the files can hold
different candidates (e.g. one per term), different information about the same candidates
(applications in one file, grades or reviews in another), or both.

Merging:
  - IDs are compared as text, ignoring leading zeros on all-digit IDs, so 123 (an Excel number
    cell) and "00000123" (a text cell) are the same candidate. The longer form is stored.
  - If two sources give a candidate different values for the same field, all distinct values
    are kept as a list and a warning names the field. Nothing is silently overwritten.
  - Sheets with no ID column (instructions, summaries, ...) are skipped with a warning.
  - Availability (time-slot columns, slot lists, the in-person question) is turned into one
    `availability` field per source, filed under the term in the file/sheet name, and merged
    term by term (see availability.py).

The store keeps every non-sensitive column, including unrecognized ones (under their normalized
name), so a later bonus criterion can still use them. Only the fields a request's active
criteria need are passed on to rankers, at ranking time.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .availability import AVAILABILITY_FIELD, extract_availability, merge_availability, term_of
from .intake import DEFAULT_FIELDS, NoIdColumnError, load_records, load_sources, standardize
from .schemas import ID_FIELD, FieldSpec, HeaderScreen, IntakeReport
from .screening import apply_screen, headers_of

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[2] / "candidate_data"
RAW_DIR = DATA_DIR / "raw"
COMPILED_PATH = DATA_DIR / "candidates.json"
CATALOG_PATH = DATA_DIR / "criteria.json"

SUPPORTED = {".xlsx", ".xlsm", ".csv", ".json"}


@dataclass(frozen=True)
class Store:
    """The files that make up one candidate store. The default is candidate_data/ at the repo
    root; tests (and later, one store per API client) use another folder."""

    root: Path = DATA_DIR

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def candidates(self) -> Path:
        return self.root / "candidates.json"

    @property
    def criteria(self) -> Path:
        return self.root / "criteria.json"

    @property
    def plan_cache(self) -> Path:
        return self.root / "criteria_plans.json"

    @property
    def column_mapping(self) -> Path:
        return self.root / "column_mapping.json"

    @property
    def field_labels(self) -> Path:
        return self.root / "field_labels.json"

    @property
    def jobs(self) -> Path:
        return self.root / "jobs"

    @property
    def header_screen(self) -> Path:
        return self.root / "header_screen.json"

    @property
    def documents(self) -> Path:
        return self.root / "documents"


def find_files(inputs: list[str | Path]) -> list[Path]:
    """Expand folders into their supported files (sorted, non-recursive). Skips Excel lock
    files ("~$...") and hidden files."""
    files = []
    for item in map(Path, inputs):
        if item.is_dir():
            files += sorted(p for p in item.iterdir() if p.suffix.lower() in SUPPORTED
                            and not p.name.startswith(("~$", ".")))
        elif item.exists():
            files.append(item)
        else:
            raise FileNotFoundError(f"No such file or folder: {item}")
    return files


def source_headers(inputs: list[str | Path]) -> list[str]:
    """Every column header in every sheet of the inputs, in first-seen order: what the header
    screen is given. Headers only; no values leave this function.

    Raises:
        ValueError: If a file can't be read (the message names it).
    """
    headers: dict[str, None] = {}
    for path in find_files(inputs):
        try:
            sources = load_sources(path)
        except ValueError as e:
            raise ValueError(f"{path.name}: {e}") from e
        for _, rows in sources:
            headers.update(dict.fromkeys(headers_of(rows)))
    return list(headers)


def _id_text(value: Any) -> str:
    """An ID as text; whole-number floats from Excel lose the ".0"."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _id_key(text: str) -> str:
    """Key used to match the same candidate across sources (see module docstring)."""
    return (text.lstrip("0") or "0") if text.isdigit() else text


def _merge_value(old: Any, new: Any) -> Any:
    """Combine two values for the same field; distinct values become one flat list."""
    values = []
    for v in (old, new):
        for item in (v if isinstance(v, list) else [v]):
            if item not in values:
                values.append(item)
    return values[0] if len(values) == 1 else values


def compile_sources(
    inputs: list[str | Path],
    fields: list[FieldSpec] | None = None,
    column_mapping: dict[str, str | None] | None = None,
    screen: HeaderScreen | None = None,
) -> tuple[list[dict[str, Any]], dict[str, IntakeReport], list[str]]:
    """Read, screen, standardize and merge every sheet of every input file.

    Args:
        inputs: Files and/or folders (folders are expanded with `find_files`).
        fields: Standard field catalog. Defaults to `intake.DEFAULT_FIELDS`.
        column_mapping: Overrides applied to every source (source column -> field, or None).
        screen: The header screen for these files (see `source_headers` and
            screening.screen_headers). Its dropped columns are removed from each source and
            listed with the sensitive ones in the report. None: only intake's blocklist applies.

    Returns:
        (records, reports, warnings): one merged record per candidate in first-seen order,
        the intake report for each source label, and warnings for the person compiling.

    Raises:
        ValueError: If no source has an ID column, or a source can't be mapped (e.g. two
            columns map to the same field). The message names the source.
    """
    keep = {f.id for f in fields or DEFAULT_FIELDS if f.handling == "keep"}
    merged: dict[str, dict[str, Any]] = {}
    conflicts: dict[str, set[str]] = {}  # field -> candidate keys with differing values
    reports, warnings = {}, []

    for path in find_files(inputs):
        try:
            sources = load_sources(path)
        except ValueError as e:  # includes decoding and malformed-file errors
            raise ValueError(f"{path.name}: {e}") from e
        for label, rows in sources:
            if not rows:
                warnings.append(f"{label}: no data rows, skipped.")
                continue
            mapping, screened_out = column_mapping, []
            if screen is not None:
                rows, mapping, screened_out, screen_warnings = apply_screen(rows, screen, fields, column_mapping)
                warnings += [f"{label}: {w}" for w in screen_warnings]
            try:
                records, report, source_warnings = standardize(
                    rows, keep, fields, mapping, keep_unmatched=True)
            except NoIdColumnError:
                warnings.append(f"{label}: no candidate ID column, skipped.")
                continue
            except ValueError as e:
                raise ValueError(f"{label}: {e}") from e
            report.dropped_sensitive = screened_out + report.dropped_sensitive
            records, consumed, avail_warnings = extract_availability(records, term_of(label))
            for col, fid in report.column_mapping.items():
                if fid in consumed:
                    report.column_mapping[col] = f"{AVAILABILITY_FIELD} ({term_of(label)})"
            reports[label] = report
            # Missing fields aren't reported: expected when files split the data.
            warnings += [f"{label}: {w}" for w in source_warnings + avail_warnings]

            for rec in records:
                text = _id_text(rec[ID_FIELD])
                key = _id_key(text)
                target = merged.setdefault(key, {ID_FIELD: text})
                if len(text) > len(target[ID_FIELD]):
                    target[ID_FIELD] = text  # keep leading zeros if any source had them
                for fid, value in rec.items():
                    if fid == ID_FIELD:
                        continue
                    if fid == AVAILABILITY_FIELD:
                        target[fid] = merge_availability(target.get(fid), value)
                    elif fid in target and target[fid] != value:
                        conflicts.setdefault(fid, set()).add(key)
                        target[fid] = _merge_value(target[fid], value)
                    else:
                        target.setdefault(fid, value)

    if not reports:
        raise ValueError("No source had a candidate ID column:\n" + "\n".join(warnings))
    for fid, keys in sorted(conflicts.items()):
        warnings.append(f"'{fid}' differed between sources for {len(keys)} candidate(s); "
                        "all values were kept as a list.")
    logger.info("Compiled %d candidates from %d sources", len(merged), len(reports))
    return list(merged.values()), reports, warnings


def load_field_labels(store: Store) -> dict[str, str]:
    """Field -> original column header, saved when the store was processed ({} if not)."""
    try:
        return json.loads(store.field_labels.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_store(records: list[dict[str, Any]], path: Path = COMPILED_PATH) -> Path:
    """Write compiled records as the candidate store (creating the folder if needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def load_store(path: Path = COMPILED_PATH) -> list[dict[str, Any]]:
    """Read the compiled candidate store.

    Raises:
        FileNotFoundError: If it hasn't been compiled yet.
    """
    if not path.exists():
        raise FileNotFoundError(f"No candidate store at {path}. Put the data files in {RAW_DIR} "
                                "and run: python src/compile_candidates.py")
    return load_records(path)
