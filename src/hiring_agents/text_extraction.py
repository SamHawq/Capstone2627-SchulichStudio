"""Text extraction: where a job table's free-text answers go before anything ranks them.

The team's external tool (removes gendered language and leftover sensitive details) plugs in
here. It is not connected yet: `extract` returns the sheet unchanged.

The tool gets a cut-down version of the job spreadsheet the Manager designed (`FreeTextSheet`):
  - one row per candidate who has any free text, identified only by a pseudonymous ref
    ("C-3f9a12bc", from a fresh key on every call), never the real ID;
  - only the free-text columns (jobs.free_text_columns: text/list columns fed by a field
    marked as free text, plus free-text fields carried in `other`), each with its header.

    columns: [{"key": "instructor_comments", "header": "Instructor comments"}, ...]
    rows:    [{"candidate_ref": "C-3f9a12bc", "instructor_comments": "Explains debugging well"}, ...]

As CSV (`FreeTextSheet.to_csv`, also saved as free_text.csv in each job's folder):

    Candidate,Instructor comments,Prior TA or tutoring experience
    C-3f9a12bc,Explains debugging well,TAed ENGG 233 twice

It must return the same shape: the same refs and column keys, with the cleaned text. Code here
re-identifies the rows and writes the text back. Anything returned for an unknown ref or
column, or as a different type (a list cell must come back as a list), is ignored.

To connect the tool: replace the body of `extract` (keep it async), and set
`EXTRACTION_ENABLED = True` so jobs stop warning that the text wasn't cleaned.
"""

import copy
import logging
from typing import Any

from .agent_trace import trace_step
from .privacy import pseudonymize
from .schemas import ID_FIELD, FreeTextColumn, FreeTextSheet

logger = logging.getLogger(__name__)

EXTRACTION_ENABLED = False   # True once `extract` calls the real tool
OTHER_PREFIX = "other."
REF = "candidate_ref"


async def extract(sheet: FreeTextSheet) -> FreeTextSheet:
    """Send the free-text sheet to the extraction tool and return the cleaned sheet.

    Placeholder: returns the sheet unchanged until the external tool is connected.
    """
    return sheet


def _get(row: dict[str, Any], key: str) -> Any:
    if key.startswith(OTHER_PREFIX):
        return (row.get("other") or {}).get(key[len(OTHER_PREFIX):])
    return row.get(key)


def _set(row: dict[str, Any], key: str, value: Any) -> None:
    if key.startswith(OTHER_PREFIX):
        row.setdefault("other", {})[key[len(OTHER_PREFIX):]] = value
    else:
        row[key] = value


def _text(value: Any) -> str | list[str] | None:
    """The text to send for a cell, or None if it holds no text."""
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, list):
        items = [v for v in value if isinstance(v, str) and v.strip()]
        return items or None
    return None


def build_sheet(rows: list[dict[str, Any]], headers: dict[str, str]) -> tuple[FreeTextSheet, dict[str, str]]:
    """The cut-down sheet to send, and its ref -> real ID map (which stays here).

    Args:
        rows: The job table (one dict per candidate, with real IDs).
        headers: Free-text column key -> header, in column order.
    """
    slices = []
    for row in rows:
        cells = {k: t for k in headers if (t := _text(_get(row, k))) is not None}
        if cells:
            slices.append({ID_FIELD: row[ID_FIELD], "cells": cells})
    columns = [FreeTextColumn(key=k, header=h) for k, h in headers.items()]
    if not slices:
        return FreeTextSheet(columns=columns, rows=[]), {}
    # pseudonymize gives each candidate a fresh ref; the cell texts ride along untouched.
    candidates, ref_map = pseudonymize(slices)
    return FreeTextSheet(columns=columns, rows=[{REF: c.ref, **c.fields["cells"]} for c in candidates]), ref_map


def merge_back(rows: list[dict[str, Any]], sent: FreeTextSheet, result: FreeTextSheet,
               ref_map: dict[str, str]) -> tuple[list[dict[str, Any]], int]:
    """Write the tool's text into copies of `rows`. Returns (rows, cells changed)."""
    rows = copy.deepcopy(rows)
    by_id = {str(r[ID_FIELD]): r for r in rows}
    sent_by_ref = {r[REF]: r for r in sent.rows}
    changed = 0
    for cleaned in result.rows:
        ref = cleaned.get(REF)
        original = sent_by_ref.get(ref)
        row = by_id.get(str(ref_map.get(ref)))
        if original is None or row is None:
            continue
        for key, text in cleaned.items():
            if key == REF or key not in original or type(text) is not type(original[key]):
                continue
            if text != original[key]:
                changed += 1
            _set(row, key, text)
    return rows, changed


async def extract_free_text(rows: list[dict[str, Any]], headers: dict[str, str]) -> tuple[list[dict[str, Any]], FreeTextSheet, list[str]]:
    """Run a job table's free-text columns through `extract`.

    Args:
        rows: The job table.
        headers: Free-text column key -> header (jobs.free_text_headers).

    Returns:
        (rows, sheet, warnings): rows with the cleaned text written back, and the sheet
        that was sent (pseudonymized).
    """
    sheet, ref_map = build_sheet(rows, headers)
    if not sheet.rows:
        return rows, sheet, []
    # Column names and counts only; never the text.
    logger.info("[extract] %d candidates, columns %s (tool %s)", len(sheet.rows), list(headers),
                "connected" if EXTRACTION_ENABLED else "not connected: text unchanged")
    result = await extract(sheet)
    rows, changed = merge_back(rows, sheet, result, ref_map)
    trace_step("extract", "Text extraction", connected=EXTRACTION_ENABLED, candidates=len(sheet.rows),
               columns={c.key: c.header for c in sheet.columns}, cells_changed=changed)
    if EXTRACTION_ENABLED:
        logger.info("[extract] %d cells changed", changed)
        return rows, sheet, []
    return rows, sheet, [f"Free-text columns {list(headers)} were not cleaned: the text extraction "
                         "tool isn't connected yet."]
