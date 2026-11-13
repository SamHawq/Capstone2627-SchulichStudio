"""Offline tests for job spreadsheets (Manager schema task + Data Agent stubbed). Run: python -m unittest"""

import asyncio
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hiring_agents.jobs as jobs
import hiring_agents.text_extraction as text_extraction
from hiring_agents.api import create_app
from hiring_agents.datastore import Store, write_store
from hiring_agents.jobs import build_table, fill_value, sample_rows, validate_mapping, validate_schema
from hiring_agents.planning import profile_fields
from hiring_agents.schemas import (
    DataMapping, FieldMapping, FieldProfile, Gap, JobSchema, RequestBrief, SchemaField, SchemaGroup, ValuePair,
)
from hiring_agents.service import (
    ServiceError, create_job, export_job, get_job, import_job, list_jobs, reapply_job,
)
from openpyxl import Workbook, load_workbook
from tests.stubs import patch_screen

# Distinctive IDs and answers, so the tests can check they never reach an agent's payload.
RECORDS = [
    {"candidate_id": "30010001", "program": "Software", "skill_checklist_python": "Expert",
     "skill_checklist_c": "No", "skill_checklist_cpp": "Yes", "years_tutoring": "3 years",
     "review": "Superb lab TA, very patient with first-year students", "comments": "Prefers mornings",
     "availability": {"Fall 2026": {"available": [], "unavailable": [], "conflicting": [], "in_person_portion": None}}},
    {"candidate_id": "30010002", "program": "Electrical", "skill_checklist_python": "beginner",
     "skill_checklist_c": "Yes", "years_tutoring": "none"},
    {"candidate_id": "30010003", "program": "Software", "skill_checklist_python": "Guru"},
]
LABELS = {"skill_checklist_python": "Skill checklist / Python", "skill_checklist_c": "Skill checklist / C",
          "skill_checklist_cpp": "Skill checklist / C++", "years_tutoring": "Years tutoring",
          "review": "Instructor review", "comments": "Comments", "program": "Program"}


def _field(fid, kind="text", values=(), label=None):
    return SchemaField(id=fid, label=label or fid.title(), description="d", kind=kind, values=list(values))


def _profiles(**fields):
    """Field profiles: a list of common values -> category, "text" -> free text."""
    return [FieldProfile(field=f, filled=3, kind="text" if v == "text" else "category",
                         common_values=[] if v == "text" else v) for f, v in fields.items()]


def _map(fid, sources, method="copy", pairs=()):
    return FieldMapping(field_id=fid, sources=list(sources), method=method,
                        value_map=[ValuePair(source_value=a, value=b) for a, b in pairs], note="n")


SCHEMA = JobSchema(role="TA for ENGG 233", summary="Python labs.", groups=[
    SchemaGroup(id="python", name="Python", purpose="Labs use Python.", fields=[
        _field("python_skill", "category", ["None", "Beginner", "Intermediate", "Expert"])]),
    SchemaGroup(id="other_languages", name="Other languages", purpose="A plus.", fields=[
        _field("languages", "list"), _field("language_count", "number")]),
    SchemaGroup(id="teaching", name="Teaching", purpose="Labs need explaining.", fields=[
        _field("tutoring_years", "number"), _field("reviews", "text"), _field("gpa", "number")]),
])
MAPPING = DataMapping(
    fields=[_map("python_skill", ["skill_checklist_python"], "value_map", [("expert", "Expert")]),
            _map("languages", ["skill_checklist_c", "skill_checklist_cpp"], "list_yes"),
            _map("language_count", ["skill_checklist_c", "skill_checklist_cpp"], "count_yes"),
            _map("tutoring_years", ["years_tutoring"]),
            _map("reviews", ["review"])],
    unfilled=[Gap(name="gpa", reason="No grades in the data.")], unplaced=[], other=["comments"],
    free_text=["review"])


class ValidateSchemaTests(unittest.TestCase):
    def test_ids_cleaned_and_sensitive_or_reserved_columns_handled(self):
        design = JobSchema(role="Manager's role", summary=" s ", groups=[
            SchemaGroup(id="Tech Skills", name="Tech", purpose="p", fields=[
                _field("Python Skill", "category", [" Expert ", ""]), _field("python skill"),
                _field("candidate_id"), _field("notes", "text", ["stray"])]),
            SchemaGroup(id="tech skills", name="Who", purpose="p", fields=[
                _field("applicant_gender"), _field("Email Address")]),
        ])
        schema, warnings = validate_schema(design, client_role="TA for ENGG 233")
        self.assertEqual(schema.role, "TA for ENGG 233")
        self.assertEqual([g.id for g in schema.groups], ["tech_skills"])   # second group emptied
        fields = schema.groups[0].fields
        self.assertEqual([f.id for f in fields], ["python_skill", "python_skill_2", "candidate_id_value", "notes"])
        self.assertEqual(fields[0].values, ["Expert"])
        self.assertEqual(fields[3].values, [])                             # not a category
        self.assertEqual(sum("personal or protected" in w for w in warnings), 2)

    def test_caps_and_empty_design(self):
        many = JobSchema(role="r", summary="", groups=[
            SchemaGroup(id=f"g{i}", name=f"G{i}", purpose="p", fields=[_field(f"f{i}_{j}") for j in range(4)])
            for i in range(12)])
        schema, warnings = validate_schema(many)
        self.assertEqual(len(schema.columns()), jobs.MAX_COLUMNS)
        self.assertLessEqual(len(schema.groups), jobs.MAX_GROUPS)
        self.assertTrue(warnings)
        with self.assertRaises(ValueError):
            validate_schema(JobSchema(role="r", summary="", groups=[
                SchemaGroup(id="g", name="g", purpose="p", fields=[_field("gender")])]))


class ValidateMappingTests(unittest.TestCase):
    def test_bad_entries_fixed_and_every_column_accounted_for(self):
        available = _profiles(skill_checklist_python=["Expert", "Beginner"], skill_checklist_c=["Yes", "No"],
                              review="text", comments="text", program=["Software"])
        raw = DataMapping(fields=[
            _map("python_skill", ["skill_checklist_python", "skill_checklist_c"]),   # copy, 2 sources
            _map("python_skill", ["review"]),                                        # repeat
            _map("made_up", ["review"]),                                             # unknown column
            _map("reviews", ["invented_field"]),                                     # unknown source only
        ], unfilled=[Gap(name="gpa", reason="No grades.")],
            unplaced=[Gap(name="program", reason="r"), Gap(name="nope", reason="r"),
                      Gap(name="skill_checklist_c", reason="used")],
            other=["comments", "comments", "availability", "skill_checklist_python"],
            free_text=["review", "review", "invented_field"])
        mapping, warnings = validate_mapping(raw, SCHEMA, available)
        self.assertEqual([(m.field_id, m.method) for m in mapping.fields], [("python_skill", "combine")])
        unfilled = {g.name: g.reason for g in mapping.unfilled}
        self.assertEqual(set(unfilled), {"languages", "language_count", "tutoring_years", "reviews", "gpa"})
        self.assertEqual(unfilled["gpa"], "No grades.")
        self.assertEqual([g.name for g in mapping.unplaced], ["program"])
        self.assertEqual(mapping.other, ["comments"])
        self.assertEqual(mapping.free_text, ["review"])
        self.assertEqual(len(warnings), 4)

    def test_yes_no_or_free_text_never_becomes_levels(self):
        profiles = _profiles(arduino=["Yes", "No"], experience="text", python=["Expert", "None"])
        schema = JobSchema(role="r", summary="", groups=[SchemaGroup(id="g", name="g", purpose="p", fields=[
            _field("arduino_level", "category", ["None", "Beginner", "Expert"]),
            _field("experience_level", "category", ["Low", "High"]),
            _field("python_level", "category", ["None", "Expert"]),
            _field("arduino_count", "number")])])
        raw = DataMapping(fields=[
            _map("arduino_level", ["arduino"], "value_map", [("Yes", "Beginner")]),
            _map("experience_level", ["experience"], "value_map", [("TAed twice", "High")]),
            _map("python_level", ["python"]),
            _map("arduino_count", ["arduino"], "count_yes")], unfilled=[], unplaced=[], other=[], free_text=[])
        mapping, _ = validate_mapping(raw, schema, profiles)
        self.assertEqual([m.field_id for m in mapping.fields], ["python_level", "arduino_count"])
        reasons = {g.name: g.reason for g in mapping.unfilled}
        self.assertIn("yes/no only", reasons["arduino_level"])
        self.assertIn("free text", reasons["experience_level"])


class FillTests(unittest.TestCase):
    def fill(self, record, mapping, column):
        return fill_value(record, mapping, column, LABELS)

    def test_methods(self):
        rec = RECORDS[0]
        self.assertEqual(self.fill(rec, _map("x", ["years_tutoring"]), _field("x", "number")), (3, None))
        self.assertEqual(self.fill(rec, _map("x", ["skill_checklist_c", "skill_checklist_cpp"], "list_yes"),
                                   _field("x", "list")), (["C++"], None))
        self.assertEqual(self.fill(rec, _map("x", ["skill_checklist_c", "skill_checklist_cpp"], "count_yes"),
                                   _field("x", "number")), (1, None))
        self.assertEqual(self.fill(rec, _map("x", ["skill_checklist_c", "skill_checklist_cpp"], "combine"),
                                   _field("x", "yes_no")), ("yes", None))
        self.assertEqual(self.fill(rec, _map("x", ["review", "comments"], "combine"), _field("x")),
                         ("Instructor review: Superb lab TA, very patient with first-year students; Comments: Prefers mornings", None))
        self.assertEqual(self.fill({"a": "2", "b": ["5", "x"]}, _map("x", ["a", "b"], "max"), _field("x", "number")),
                         (5, None))
        self.assertEqual(self.fill({"a": "2", "b": "1.5"}, _map("x", ["a", "b"], "sum"), _field("x", "number")),
                         (3.5, None))

    def test_value_map_and_category_values(self):
        column = _field("p", "category", ["None", "Beginner", "Expert"])
        mapping = _map("p", ["skill_checklist_python"], "value_map", [("EXPERT", "Expert")])
        self.assertEqual(self.fill(RECORDS[0], mapping, column), ("Expert", None))
        self.assertEqual(self.fill(RECORDS[1], mapping, column), ("Beginner", None))   # already allowed; case snapped
        self.assertEqual(self.fill(RECORDS[2], mapping, column), ("Guru", "unmapped"))
        self.assertEqual(self.fill(RECORDS[2], _map("p", ["skill_checklist_python"]), column),
                         ("Guru", "not_in_values"))

    def test_missing_and_non_numbers(self):
        self.assertEqual(self.fill(RECORDS[2], _map("x", ["review"]), _field("x")), (None, None))
        self.assertEqual(self.fill(RECORDS[1], _map("x", ["years_tutoring"]), _field("x", "number")),
                         (None, "not_a_number"))
        # A checklist answered with nothing ticked is an answer, not missing data.
        self.assertEqual(self.fill(RECORDS[1] | {"skill_checklist_c": "No"},
                                   _map("x", ["skill_checklist_c"], "list_yes"), _field("x", "list")), ([], None))

    def test_table_carries_other_and_availability_and_counts_problems(self):
        rows, warnings = build_table(RECORDS, SCHEMA, MAPPING, LABELS)
        self.assertEqual(rows[0]["other"], {"comments": "Prefers mornings"})
        self.assertIn("availability", rows[0])
        self.assertNotIn("gpa", rows[0])
        self.assertEqual(rows[1]["languages"], ["C"])
        self.assertEqual(rows[1]["python_skill"], "Beginner")
        self.assertTrue(any("tutoring_years" in w and "1 candidate" in w for w in warnings))
        self.assertTrue(all("Guru" not in w and "none" not in w for w in warnings))   # counts, not values


class FreeTextTests(unittest.TestCase):
    def test_profile_measures_prose_over_every_answer(self):
        records = [{"candidate_id": str(i), "notes": t, "courses": f"ENGG 233: A, ENSF 338: B+, ENEL {i}: A"}
                   for i, t in enumerate(["", "Explains debugging clearly and is always on time for labs",
                                          "Great TA", "Helped first-year students with their weekly labs"])]
        by_field = {p.field: p for p in profile_fields(records, exclude=set())}
        self.assertEqual(by_field["notes"].sentence_share, 0.5)    # 2 of 4 answers are prose
        self.assertEqual(by_field["courses"].sentence_share, 0.0)  # codes, not prose
        self.assertTrue(jobs.looks_like_free_text(by_field["notes"]))
        self.assertFalse(jobs.looks_like_free_text(by_field["courses"]))

    def test_agent_and_length_check_combine_and_columns_follow(self):
        profiles = profile_fields(RECORDS, exclude=set(), labels=LABELS)
        agent_says = MAPPING.model_copy(update={"free_text": ["comments"]})   # short, caught by the agent only
        mapping, _ = jobs.add_detected_free_text(agent_says, profiles)
        self.assertEqual(mapping.free_text, ["comments", "review"])           # review caught by length
        schema = SCHEMA.model_copy(deep=True)
        schema.groups[2].fields.append(_field("review_words", "number"))
        mapping.fields.append(_map("review_words", ["review"]))
        # The text column fed by review and the free text in `other`; not the number column.
        self.assertEqual(jobs.free_text_columns(schema, mapping), ["reviews", "other.comments"])


class ExtractionHandoffTests(unittest.TestCase):
    ROWS = [{"candidate_id": "30010001", "reviews": "She explains well", "tutoring_years": 3,
             "other": {"comments": "Prefers mornings", "program": "Software"}},
            {"candidate_id": "30010002", "tutoring_years": 1},
            {"candidate_id": "30010003", "reviews": ["Good", " "]}]
    HEADERS = {"reviews": "Instructor reviews", "other.comments": "Comments"}

    def test_tool_gets_a_cut_down_sheet_and_its_text_comes_back(self):
        sent = []

        async def tool(sheet):
            sent.append(sheet)
            upper = lambda v: v.upper() if isinstance(v, str) else [x.upper() for x in v]   # noqa: E731
            return sheet.model_copy(update={"rows": [
                {k: (v if k == "candidate_ref" else upper(v)) for k, v in r.items()} for r in sheet.rows]})

        with patch.object(text_extraction, "extract", tool), patch.object(text_extraction, "EXTRACTION_ENABLED", True):
            rows, sheet, warnings = asyncio.run(text_extraction.extract_free_text(self.ROWS, self.HEADERS))
        self.assertEqual(sent, [sheet])
        self.assertEqual([(c.key, c.header) for c in sheet.columns],
                         [("reviews", "Instructor reviews"), ("other.comments", "Comments")])
        self.assertEqual(len(sheet.rows), 2)                    # the candidate with no text isn't sent
        self.assertTrue(all(set(r) <= {"candidate_ref", "reviews", "other.comments"} for r in sheet.rows))
        self.assertTrue(all(r["candidate_ref"].startswith("C-") for r in sheet.rows))
        self.assertNotIn("30010001", sheet.model_dump_json())
        self.assertNotIn("Software", sheet.model_dump_json())   # non-free-text fields stay home
        lines = sheet.to_csv().splitlines()
        self.assertEqual(lines[0], "Candidate,Instructor reviews,Comments")
        self.assertTrue(lines[1].endswith(",She explains well,Prefers mornings"))
        self.assertTrue(lines[2].endswith(",Good,"))
        self.assertEqual(rows[0]["reviews"], "SHE EXPLAINS WELL")
        self.assertEqual(rows[0]["other"], {"comments": "PREFERS MORNINGS", "program": "Software"})
        self.assertEqual(rows[2]["reviews"], ["GOOD"])
        self.assertEqual(self.ROWS[0]["reviews"], "She explains well")   # input not modified
        self.assertEqual(warnings, [])

    def test_bad_replies_ignored_and_placeholder_warns(self):
        async def tool(sheet):
            ref = sheet.rows[0]["candidate_ref"]
            return sheet.model_copy(update={"rows": [
                {"candidate_ref": ref, "reviews": 5, "tutoring_years": "x"},
                {"candidate_ref": "C-unknown", "reviews": "y"}]})
        with patch.object(text_extraction, "extract", tool):
            rows, _, warnings = asyncio.run(text_extraction.extract_free_text(self.ROWS, {"reviews": "Reviews"}))
        self.assertEqual([r.get("reviews") for r in rows], ["She explains well", None, ["Good", " "]])
        self.assertTrue(any("isn't connected" in w for w in warnings))


class SampleRowTests(unittest.TestCase):
    def test_pseudonymized_limited_and_clipped(self):
        records = [dict(r) for r in RECORDS] + [{"candidate_id": "30010009", "name": "Zed", "review": "x" * 500}]
        fields = {"skill_checklist_python", "review", "name"}
        samples = sample_rows(records, fields, n=4)
        text = json.dumps(samples)
        for rid in ("30010001", "30010009"):
            self.assertNotIn(rid, text)
        self.assertNotIn("Zed", text)              # sensitive field name removed by pseudonymize
        self.assertNotIn("availability", text)
        self.assertTrue(all(s["candidate_ref"].startswith("C-") for s in samples))
        long = next(s for s in samples if s.get("review", "").startswith("xxx"))
        self.assertEqual(len(long["review"]), jobs.SAMPLE_CHARS + 1)


class JobServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "store")
        write_store(RECORDS, self.store.candidates)
        self.store.field_labels.write_text(json.dumps(LABELS), encoding="utf-8")
        self.payloads: list[tuple[str, dict]] = []

    def fake_agents(self, designs, mappings):
        """Patch the agent calls: replies come from the lists; payloads are recorded."""
        designs, mappings = list(designs), list(mappings)

        async def fake(agent, payload, output_type, tag, attempts=2):
            self.payloads.append((tag, json.loads(payload)))
            return (designs if output_type is JobSchema else mappings).pop(0)
        return patch.object(jobs, "run_with_retries", fake)

    def test_create_revises_once_and_writes_files(self):
        revised = SCHEMA.model_copy(update={"groups": SCHEMA.groups[:2]})   # drops the unfillable gpa
        second = MAPPING.model_copy(update={"unfilled": []})
        with self.fake_agents([SCHEMA, revised], [MAPPING, second]):
            job = asyncio.run(create_job(RequestBrief(instructions="Python labs TA", role="TA for ENGG 233"),
                                         self.store))
        self.assertEqual([t for t, _ in self.payloads], ["job schema", "data agent", "job schema", "data agent"])
        self.assertTrue(job.info.revised)
        self.assertEqual(job.mapping.unfilled, [])
        self.assertEqual({c.id: c.filled for c in job.columns},
                         {"python_skill": 3, "languages": 2, "language_count": 2})

        # The Manager sees names and headers only; the Data Agent sees no real ID.
        manager_payload = json.dumps(self.payloads[0][1])
        self.assertIn("Skill checklist / Python", manager_payload)
        for value in ("Superb", "Expert", "30010001", "Prefers mornings"):
            self.assertNotIn(value, manager_payload)
        self.assertIn("previous_design", self.payloads[2][1])
        self.assertEqual(self.payloads[2][1]["data_agent_report"]["unfilled"][0]["name"], "gpa")
        data_payload = json.dumps(self.payloads[1][1])
        self.assertNotIn("30010001", data_payload)
        self.assertNotIn("availability", data_payload)

        folder = Path(job.table_csv).parent
        self.assertEqual({p.name for p in folder.iterdir()},
                         {"job.json", "schema.json", "mapping.json", "table.json", "table.csv"})   # no free text left
        with open(job.table_csv, encoding="utf-8-sig", newline="") as f:
            table = list(csv.reader(f))
        self.assertEqual(table[0], ["Candidate ID", "Python_Skill", "Languages", "Language_Count", "Other"])
        self.assertEqual(table[1][:4], ["30010001", "Expert", "C++", "1"])
        self.assertEqual([j.id for j in list_jobs(self.store)], [job.info.id])

    def test_no_revision_when_off_or_not_needed(self):
        with self.fake_agents([SCHEMA], [MAPPING]):
            job = asyncio.run(create_job(RequestBrief(instructions="TA"), self.store, revise=False))
        self.assertEqual(len(self.payloads), 2)
        self.assertFalse(job.info.revised)
        # The extraction tool's sheet: refs and the free-text column with its header only.
        sheet = (Path(job.table_csv).parent / "free_text.csv").read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(sheet[0], "Candidate,Reviews")
        self.assertRegex(sheet[1], r"^C-[0-9a-f]{8},\"Superb lab TA")
        self.assertEqual(len(sheet), 2)
        self.assertTrue(any("gpa" in w for w in job.info.warnings))

    def test_every_unused_field_lands_in_other_except_excluded_ones(self):
        from hiring_agents.planning import write_catalog
        from hiring_agents.schemas import CriteriaCatalog, SkippedField
        write_catalog(CriteriaCatalog(purpose="p", fields=[], extensions=[], criteria=[], skipped=[
            SkippedField(field="comments", reason="form metadata", use="administrative")]), self.store.criteria)
        agent_other = MAPPING.model_copy(update={"other": []})   # the agent listed nothing
        with self.fake_agents([SCHEMA], [agent_other]):
            job = asyncio.run(create_job(RequestBrief(instructions="TA"), self.store, revise=False))
        self.assertEqual(job.mapping.other, ["program"])          # unused; "comments" is administrative

    def test_unchanged_revision_skips_the_second_data_agent_call(self):
        with self.fake_agents([SCHEMA, SCHEMA], [MAPPING]):
            job = asyncio.run(create_job(RequestBrief(instructions="TA"), self.store))
        self.assertEqual([t for t, _ in self.payloads], ["job schema", "data agent", "job schema"])
        self.assertFalse(job.info.revised)

    def test_reapply_uses_hand_edits(self):
        with self.fake_agents([SCHEMA], [MAPPING]):
            job = asyncio.run(create_job(RequestBrief(instructions="TA"), self.store, revise=False))
        path = Path(job.table_csv).parent / "mapping.json"
        edited = json.loads(path.read_text(encoding="utf-8"))
        edited["fields"][0]["value_map"].append({"source_value": "guru", "value": "Expert"})
        path.write_text(json.dumps(edited), encoding="utf-8")
        again = asyncio.run(reapply_job(job.info.id, self.store))
        rows = json.loads((Path(job.table_csv).parent / "table.json").read_text(encoding="utf-8"))
        self.assertEqual(rows[2]["python_skill"], "Expert")
        self.assertEqual(get_job(job.info.id, self.store).columns, again.columns)

        # The length check would flag "review", but a hand edit removing it sticks.
        self.assertEqual(again.free_text_columns, ["reviews"])
        edited["free_text"] = []
        path.write_text(json.dumps(edited), encoding="utf-8")
        self.assertEqual(asyncio.run(reapply_job(job.info.id, self.store)).free_text_columns, [])

        path.write_text('{"fields": "broken"}', encoding="utf-8")
        with self.assertRaisesRegex(ServiceError, "check any edits"):
            asyncio.run(reapply_job(job.info.id, self.store))

    def test_export_then_import_skips_design_and_mapping(self):
        with self.fake_agents([SCHEMA], [MAPPING]):
            job = asyncio.run(create_job(RequestBrief(instructions="TA"), self.store, revise=False))
        path = export_job(job.info.id, self.store)
        wb = load_workbook(path)
        self.assertEqual(wb.sheetnames, ["Candidates", "Design", "Job"])
        cand = wb["Candidates"]
        self.assertEqual([c.value for c in cand[1]][:3], [None, "Python", "Other languages"])   # group row
        self.assertEqual([c.value for c in cand[2]][:3], ["Candidate ID", "Python_Skill", "Languages"])
        self.assertEqual([c.value for c in cand[3]][:4], ["30010001", "Expert", "C++", 1])
        calls = len(self.payloads)

        with patch_screen():
            imported = asyncio.run(import_job(path, self.store))
        self.assertEqual(len(self.payloads), calls)                          # no Manager / Data Agent call
        self.assertEqual(imported.info.source, "import")
        self.assertEqual([c.id for c in imported.columns], [c.id for c in job.columns])
        self.assertEqual(imported.free_text_columns, ["reviews"])
        before = json.loads((Path(job.table_csv).parent / "table.json").read_text(encoding="utf-8"))
        after = json.loads((Path(imported.table_csv).parent / "table.json").read_text(encoding="utf-8"))
        strip = lambda rows: [{k: v for k, v in r.items() if k not in ("other", "availability")} for r in rows]  # noqa: E731
        self.assertEqual(strip(after), strip(before))                        # same values, lists and numbers too

        # Rebuilt from its own rows: works without the store.
        self.store.candidates.unlink()
        again = asyncio.run(reapply_job(imported.info.id, self.store))
        self.assertEqual(again.info.candidates, 3)

    def test_import_checks_edits_for_privacy(self):
        with self.fake_agents([SCHEMA], [MAPPING]):
            job = asyncio.run(create_job(RequestBrief(instructions="TA"), self.store, revise=False))
        path = export_job(job.info.id, self.store)
        wb = load_workbook(path)
        cand, design = wb["Candidates"], wb["Design"]
        cand.cell(row=2, column=cand.max_column + 1, value="Full Name")       # not in the design
        cand.cell(row=3, column=cand.max_column, value="Persona Uno")
        design.append(["g", "Who", "", "applicant_gender", "Gender", "text", "", "", "no", "", ""])
        n = cand.max_column + 1
        cand.cell(row=2, column=n, value="Gender")
        cand.cell(row=3, column=n, value="x")
        design.append(["g", "Who", "", "fav", "Favourite lab", "text", "", "", "no", "", ""])
        cand.cell(row=2, column=n + 1, value="Favourite lab")
        cand.cell(row=3, column=n + 1, value="robots")
        wb.save(path)
        with patch_screen(drop=("Favourite lab",)):
            imported = asyncio.run(import_job(path, self.store))
        table = (Path(imported.table_csv).parent / "table.json").read_text(encoding="utf-8")
        for value in ("Persona Uno", "robots"):
            self.assertNotIn(value, table)
        self.assertNotIn("applicant_gender", [c.id for c in imported.columns])
        warnings = " ".join(imported.info.warnings)
        self.assertIn("Full Name", warnings)                     # unknown column ignored
        self.assertIn("personal or protected", warnings)         # Gender removed by the design check
        self.assertIn("header screen dropped", warnings)         # Favourite lab dropped by the screen

    def test_import_rejects_files_that_are_not_job_workbooks(self):
        plain = Path(self.tmp.name) / "plain.xlsx"
        Workbook().save(plain)
        with self.assertRaisesRegex(ServiceError, "isn't a job workbook"):
            asyncio.run(import_job(plain, self.store))
        text = Path(self.tmp.name) / "notes.xlsx"
        text.write_text("not excel")
        with self.assertRaisesRegex(ServiceError, "can't be opened"):
            asyncio.run(import_job(text, self.store))

    def test_bad_ids_and_empty_store(self):
        for bad in ("../store", "nope", "A B"):
            with self.assertRaises(ServiceError):
                get_job(bad, self.store)
        empty = Store(Path(self.tmp.name) / "empty")
        with self.assertRaises(ServiceError):
            asyncio.run(create_job(RequestBrief(instructions="TA"), empty))

    def test_api_endpoints(self):
        app = create_app(self.store, Path(self.tmp.name) / "results")

        def call(path, method, *args, **kwargs):
            route = next(r for r in app.routes if getattr(r, "path", None) == path and method in r.methods)
            result = route.endpoint(*args, **kwargs)
            return asyncio.run(result) if asyncio.iscoroutine(result) else result

        with self.fake_agents([SCHEMA], [MAPPING]):
            job = call("/api/jobs", "POST", RequestBrief(instructions="TA"), revise=False)
        self.assertEqual([j.id for j in call("/api/jobs", "GET")], [job.info.id])
        self.assertEqual(call("/api/jobs/{job_id}", "GET", job.info.id).info.id, job.info.id)
        self.assertEqual(call("/api/jobs/{job_id}/apply", "POST", job.info.id).info.candidates, 3)
        exported = call("/api/jobs/{job_id}/export", "GET", job.info.id)
        self.assertTrue(Path(exported.path).name.endswith(".xlsx"))
        with self.assertRaises(ServiceError):
            call("/api/jobs/{job_id}/export", "GET", "../nope")


if __name__ == "__main__":
    unittest.main()
