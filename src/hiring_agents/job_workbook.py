"""Job workbooks: a job spreadsheet as a self-describing Excel file, for export and re-import.

A job workbook carries its own design, so importing it skips the Manager's design and the
Data Agent's mapping: the columns are already fitted to the job. Sheets:

    Candidates  row 1: each criterion group's name over its columns; row 2: column labels
                ("Candidate ID" first); one row per candidate after that. Lists are "a; b".
    Design      one row per column: group id/name/purpose, column id, label, kind, allowed
                values ("; "-separated), description, free_text (yes/no), and where it came
                from (sources, method), for reference only.
    Job         key/value: format (FORMAT), role, summary, job id, created, instructions.

A client can edit values, delete rows or columns, or add a column by adding a Design row and
a Candidates column with the same label. Reading checks everything (see `read_workbook`); the
import itself (jobs.import_job) also runs the header screen over the column labels.

The workbook holds real candidate IDs, like table.csv: keep it local.
"""

from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .schemas import JobResult, JobSchema, SchemaField, SchemaGroup

FORMAT = "hiring-agents-job/1"
ID_HEADER = "Candidate ID"
KINDS = {"number", "category", "yes_no", "text", "list"}
_DESIGN_HEADER = ["group_id", "group_name", "group_purpose", "column_id", "label", "kind", "values",
                  "description", "free_text", "sources", "method"]


class WorkbookError(ValueError):
    """The file isn't a usable job workbook. The message says what to fix."""


def _cell(value: Any) -> Any:
    if isinstance(value, list):
        return "; ".join(map(str, value))
    if isinstance(value, dict):
        return "; ".join(f"{k}: {v}" for k, v in value.items())
    return value


def write_workbook(job: JobResult, rows: list[dict[str, Any]], path: Path) -> Path:
    """Write a job (its design and filled table) as a job workbook."""
    wb = Workbook()
    bold, head_fill = Font(bold=True), PatternFill("solid", fgColor="E8EEFC")
    by_col = {c.id: c for c in job.columns}
    free = set(job.free_text_columns)

    ws = wb.active
    ws.title = "Candidates"
    ws.cell(row=2, column=1, value=ID_HEADER).font = bold
    col = 2
    for group in job.design.groups:
        ws.cell(row=1, column=col, value=group.name).font = bold
        for f in group.fields:
            c = ws.cell(row=2, column=col, value=f.label)
            c.font, c.fill = bold, head_fill
            ws.column_dimensions[get_column_letter(col)].width = 28 if f.kind == "text" else 18
            col += 1
    ws.column_dimensions["A"].width = 16
    ids = [f.id for _, f in job.design.columns()]
    for r, row in enumerate(rows, start=3):
        ws.cell(row=r, column=1, value=str(row.get("candidate_id", "")))
        for i, fid in enumerate(ids, start=2):
            value = _cell(row.get(fid))
            if value is not None:
                ws.cell(row=r, column=i, value=value).alignment = Alignment(wrap_text=isinstance(value, str) and len(value) > 40,
                                                                            vertical="top")
    ws.freeze_panes = "B3"

    ds = wb.create_sheet("Design")
    ds.append(_DESIGN_HEADER)
    for cell in ds[1]:
        cell.font = bold
    for g, f in job.design.columns():
        info = by_col.get(f.id)
        ds.append([g.id, g.name, g.purpose, f.id, f.label, f.kind, "; ".join(f.values), f.description,
                   "yes" if f.id in free else "no", ", ".join(info.sources) if info else "",
                   info.method if info else ""])

    js = wb.create_sheet("Job")
    for key, value in [("format", FORMAT), ("role", job.design.role), ("summary", job.design.summary),
                       ("job id", job.info.id), ("created", job.info.created_at),
                       ("instructions", job.info.brief.instructions)]:
        js.append([key, value])
    js["A1"].font = bold
    js.column_dimensions["B"].width = 80
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def read_workbook(path: Path) -> tuple[JobSchema, set[str], list[dict[str, Any]], list[str]]:
    """Read a job workbook.

    Returns:
        (design, free_text_ids, records, warnings): records are {"candidate_id": ..., column
        id: raw cell value}; empty cells left out. Values are coerced to the column kinds
        later, by jobs.build_table.

    Raises:
        WorkbookError: If it isn't a job workbook, or the design or table can't be used.
    """
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as e:  # openpyxl raises several types for non-Excel files
        raise WorkbookError(f"{Path(path).name} can't be opened as an Excel workbook: {e}") from e
    try:
        if not {"Candidates", "Design", "Job"} <= set(wb.sheetnames):
            raise WorkbookError(f"{Path(path).name} isn't a job workbook: it needs the sheets Candidates, "
                                "Design and Job (export a job to get one).")
        meta = {_text(r[0]).lower(): r[1] for r in wb["Job"].iter_rows(values_only=True) if r and r[0]}
        if _text(meta.get("format")) != FORMAT:
            raise WorkbookError(f"Unsupported job workbook format {meta.get('format')!r}; expected {FORMAT!r}.")

        design_rows = list(wb["Design"].iter_rows(values_only=True))
        header = [_text(h).lower() for h in design_rows[0]] if design_rows else []
        missing = [h for h in ("group_name", "column_id", "label", "kind") if h not in header]
        if missing:
            raise WorkbookError(f"The Design sheet is missing columns {missing}.")
        at = {h: i for i, h in enumerate(header)}
        groups: dict[str, SchemaGroup] = {}
        free, warnings = set(), []
        for n, r in enumerate(design_rows[1:], start=2):
            get = lambda h: _text(r[at[h]]) if h in at and at[h] < len(r) else ""   # noqa: E731
            if not get("column_id") and not get("label"):
                continue
            kind = get("kind").lower()
            if kind not in KINDS:
                raise WorkbookError(f"Design row {n}: kind {kind!r} must be one of {sorted(KINDS)}.")
            name = get("group_name") or "Other"
            group = groups.setdefault(name, SchemaGroup(id=get("group_id") or name, name=name,
                                                        purpose=get("group_purpose"), fields=[]))
            values = [v.strip() for v in get("values").split(";") if v.strip()]
            group.fields.append(SchemaField(id=get("column_id") or get("label"), label=get("label") or get("column_id"),
                                            description=get("description"), kind=kind, values=values))
            if get("free_text").lower() in ("yes", "y", "true", "1"):
                free.add(group.fields[-1].id)
        design = JobSchema(role=_text(meta.get("role")), summary=_text(meta.get("summary")),
                           groups=list(groups.values()))

        table = list(wb["Candidates"].iter_rows(values_only=True))
        if len(table) < 2:
            raise WorkbookError("The Candidates sheet needs group names in row 1 and column labels in row 2.")
        labels = [_text(h) for h in table[1]]
        if not labels or labels[0] != ID_HEADER:
            raise WorkbookError(f"The Candidates sheet's first column must be '{ID_HEADER}' (row 2).")
        by_label, kinds = {}, {}
        for f in (f for g in design.groups for f in g.fields):
            by_label[f.label] = f.id
            by_label.setdefault(f.id, f.id)
            kinds[f.id] = f.kind
        unknown = [h for h in labels[1:] if h and h not in by_label]
        if unknown:
            warnings.append(f"Columns {unknown} aren't in the Design sheet, so they were ignored "
                            "(add a Design row to keep a column).")
        records, no_id = [], 0
        for r in table[2:]:
            if not r or all(v is None or _text(v) == "" for v in r):
                continue
            cid = _text(r[0])
            if not cid:
                no_id += 1
                continue
            rec: dict[str, Any] = {"candidate_id": cid}
            for i, h in enumerate(labels[1:], start=1):
                if h in by_label and i < len(r) and r[i] is not None and _text(r[i]) != "":
                    fid, value = by_label[h], r[i]
                    if kinds[fid] == "list" and isinstance(value, str):   # written as "a; b"
                        value = [v.strip() for v in value.split(";") if v.strip()]
                    rec[fid] = value
            records.append(rec)
        if no_id:
            warnings.append(f"{no_id} row(s) had no Candidate ID and were skipped.")
        if not records:
            raise WorkbookError("The Candidates sheet has no candidate rows.")
        ids = [r["candidate_id"] for r in records]
        if len(set(ids)) < len(ids):
            raise WorkbookError("Two rows share a Candidate ID; each candidate must appear once.")
        return design, free, records, warnings
    finally:
        wb.close()
