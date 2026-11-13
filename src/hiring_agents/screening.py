"""Header screen: drop identifying and sensitive columns before any value reaches an LLM.

The first step for any data. Flow (agents never see each other's output; this code passes
everything along):

    every file's column headers (no values)
      -> Header Screen agent (llm_agents/header_screen.py, one call)  id / drop / keep per header
      -> validate_screen          unknown headers ignored, missing ones dropped, one entry each
      -> header_screen.json       saved in the store; editable; reused for headers seen before
      -> apply_screen             per source: removes the dropped columns from every row, picks
                                  the ID column, so intake and everything after only get the rest

On top of the screen, intake's name blocklist (intake.DEFAULT_FIELDS "drop" fields) still
removes names, contact details and protected characteristics, so both have to miss a column
for it to get through. The ID column is never sent to an LLM either way: privacy.pseudonymize
replaces it with a code before any ranker runs, and the Criteria Planner only sees field
summaries without IDs.

The screen only re-runs for headers it hasn't seen, so processing the same files again makes
no LLM call. If the call fails, processing fails: data is never stored unscreened unless the
caller turns screening off.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any

from .agent_trace import run_agent, trace_step
from .intake import id_columns, normalize
from .llm_agents import build_header_screen
from .schemas import ID_FIELD, ColumnDecision, FieldSpec, HeaderScreen, HeaderScreenOutput

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
NOT_SCREENED = "Not screened (the model left this header out); change to keep if it is safe."


class ScreeningError(RuntimeError):
    """The Header Screen call failed, so the data was not processed."""


def load_screen(path: Path) -> HeaderScreen | None:
    """Read a saved screen; None if there isn't one.

    Raises:
        ValueError: If the file was edited into something that isn't a valid screen.
    """
    if not path.exists():
        return None
    try:
        return HeaderScreen.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValueError(f"{path.name} is not a valid header screen (check your edits): {e}") from e


def write_screen(screen: HeaderScreen, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(screen.model_dump_json(indent=2), encoding="utf-8")
    return path


async def run_header_screen(headers: list[str]) -> HeaderScreenOutput:
    """One Header Screen call over `headers` (no values are sent).

    Raises:
        ScreeningError: If every attempt fails.
    """
    payload = json.dumps({"headers": headers}, indent=2, ensure_ascii=False)
    agent = build_header_screen()
    logger.info("[screen] Screening %d headers (model: %s)", len(headers), agent.model or "SDK default")
    logger.debug("[screen] Payload:\n%s", payload)
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        start = time.perf_counter()
        try:
            output = await run_agent(agent, payload, HeaderScreenOutput)
            logger.info("[screen] Done in %.1fs (attempt %d)", time.perf_counter() - start, attempt)
            logger.debug("[screen] Output:\n%s", output.model_dump_json(indent=2))
            return output
        except Exception as e:  # retry on API/parse failures
            last_error = e
            logger.warning("[screen] Attempt %d/%d failed after %.1fs: %s",
                           attempt, MAX_ATTEMPTS, time.perf_counter() - start, e)
    raise ScreeningError(f"The header screen failed after {MAX_ATTEMPTS} attempts, so the data "
                         f"was not processed: {last_error}")


def validate_screen(output: HeaderScreenOutput, headers: list[str]) -> tuple[list[ColumnDecision], list[str]]:
    """Exactly one decision per header, in the given order.

    Headers the model invented are ignored, a repeated header keeps its first decision, and
    a header the model left out is dropped (privacy first) with a reason saying so.

    Returns:
        (decisions, warnings)
    """
    given = set(headers)
    by_header: dict[str, ColumnDecision] = {}
    for d in output.columns:
        if d.column in given and d.column not in by_header:
            reason = d.reason.strip() or ("Sensitive." if d.action == "drop" else "")
            by_header[d.column] = d.model_copy(update={"reason": reason})
    missing = [h for h in headers if h not in by_header]
    for h in missing:
        by_header[h] = ColumnDecision(column=h, action="drop", reason=NOT_SCREENED)
    warnings = [f"The header screen left out {len(missing)} header(s), so they were dropped: "
                f"{missing}. Set them to keep in header_screen.json if they are safe."] if missing else []
    return [by_header[h] for h in headers], warnings


async def screen_headers(headers: list[str], path: Path) -> tuple[HeaderScreen, bool, list[str]]:
    """The screen for `headers`: the saved one, plus one LLM call for headers it doesn't have.

    Saved decisions (including hand edits) are kept. New decisions are added to the file.

    Returns:
        (screen, screened_now, warnings): screened_now is True if the agent ran.

    Raises:
        ScreeningError: If the agent call fails.
        ValueError: If the saved file is invalid.
    """
    screen = load_screen(path) or HeaderScreen(columns=[])
    known = screen.decisions()
    new = [h for h in dict.fromkeys(headers) if h not in known]
    if not new:
        logger.info("[screen] All %d headers already screened (%s)", len(set(headers)), path.name)
        trace_step("screen", "Header screen reused", headers=len(set(headers)), file=path.name,
                   dropped=[d.column for d in screen.columns if d.action == "drop" and d.column in headers])
        return screen, False, []
    decisions, warnings = validate_screen(await run_header_screen(new), new)
    screen = HeaderScreen(columns=screen.columns + decisions)
    write_screen(screen, path)
    dropped = [d.column for d in decisions if d.action == "drop"]
    unsure = [d.column for d in decisions if d.reason.lower().startswith("unsure")]
    logger.info("[screen] %d new headers: dropped %s", len(new), dropped)
    trace_step("screen", "Header screen applied", new_headers=len(new),
               id=[d.column for d in decisions if d.action == "id"], dropped=dropped,
               kept=[d.column for d in decisions if d.action == "keep"], unsure=unsure, fixes=warnings)
    if unsure:
        warnings.append(f"The header screen was unsure about {unsure} and dropped them; review "
                        f"them in {path.name}.")
    return screen, True, warnings


def headers_of(rows: list[dict[str, Any]]) -> list[str]:
    """Every column name in `rows`, in first-seen order (rows may differ)."""
    return list(dict.fromkeys(k for r in rows for k in r))


def apply_screen(
    rows: list[dict[str, Any]],
    screen: HeaderScreen,
    fields: list[FieldSpec] | None = None,
    column_mapping: dict[str, str | None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str | None], list[str], list[str]]:
    """Remove one source's screened-out columns from every row and settle its ID column.

    - Columns screened "drop" are removed. So is any column the screen has no decision for.
    - The ID column is the one intake already recognizes (by alias or `column_mapping`);
      if there is none, the first column the screen marked "id" becomes it (added to the
      mapping). Other "id" columns are identifiers, so they are removed too. The ID column
      itself is never removed: it doesn't reach an LLM (it's pseudonymized).

    Args:
        rows: One source's records, keyed by original column header.
        screen: The header screen covering these headers.
        fields, column_mapping: As for intake.standardize.

    Returns:
        (rows, column_mapping, dropped, warnings): the rows without the dropped columns, the
        mapping to pass to intake.standardize, the dropped column names, and warnings.
    """
    decisions = screen.decisions()
    columns = headers_of(rows)
    recognized = id_columns(columns, fields, column_mapping)
    marked = [c for c in columns if c in decisions and decisions[c].action == "id"]
    mapping = dict(column_mapping or {})
    chosen = recognized[0] if recognized else (marked[0] if marked else None)
    if chosen and not recognized:
        mapping[chosen] = ID_FIELD

    dropped = [c for c in columns
               if c not in recognized and c != chosen
               and (c not in decisions or decisions[c].action in ("drop", "id"))]
    warnings = []
    unscreened = [c for c in dropped if c not in decisions]
    if unscreened:
        warnings.append(f"Columns {unscreened} were never screened, so they were dropped; process the store again.")
    explicit = {normalize(k): v for k, v in mapping.items()}
    overridden = [c for c in dropped if explicit.get(normalize(c))]
    if overridden:
        warnings.append(f"Columns {overridden} are in column_mapping, but the header screen dropped them. "
                        "To use them, set them to keep in header_screen.json.")

    gone = set(dropped)
    rows = [{k: v for k, v in r.items() if k not in gone} for r in rows]
    # Column names only; values are never logged.
    logger.info("[screen] Applied: id column %r, dropped %s", chosen, dropped)
    return rows, mapping, dropped, warnings
