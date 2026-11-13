"""Offline tests for intake (column mapping) and the as-needed criteria step. Run: python -m unittest"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from openpyxl import Workbook

from hiring_agents.intake import DEFAULT_FIELDS, load_records, report_warnings, standardize
from hiring_agents.orchestrator import criteria_with_data
from hiring_agents.privacy import pseudonymize
from hiring_agents.registry import default_registry
from hiring_agents.schemas import ID_FIELD
from tests.stubs import TA_FIELDS, ta_registry

NEEDED = {"program", "relevant_courses", "previous_ta_reviews"}

ALT = [
    {"Student Number": "1", "Full Name": "X", "Pronouns": "they/them", "Degree Program": "SE",
     "Courses & Grades": "ENGG 233: A", "Instructor Feedback": "", "Year": "3", "Favourite Colour": "blue"},
    {"Student Number": "2", "Full Name": "Y", "Pronouns": "she/her", "Degree Program": "ME",
     "Courses & Grades": "ENGG 233: B", "Instructor Feedback": "Great", "Year": "4", "Favourite Colour": "red"},
]


class StandardizeTests(unittest.TestCase):
    def test_aliases_mapped_and_everything_else_dropped(self):
        records, report, _ = standardize(ALT, NEEDED, fields=TA_FIELDS)
        self.assertEqual(report.column_mapping, {
            "Student Number": ID_FIELD, "Degree Program": "program",
            "Courses & Grades": "relevant_courses", "Instructor Feedback": "previous_ta_reviews",
        })
        self.assertEqual(report.dropped_sensitive, ["Full Name", "Pronouns"])
        self.assertEqual(report.dropped_unused, ["Year"])  # known field, but no active criterion needs it
        self.assertEqual(report.dropped_unmatched, ["Favourite Colour"])
        self.assertEqual(records[1], {ID_FIELD: "2", "program": "ME", "relevant_courses": "ENGG 233: B",
                                      "previous_ta_reviews": "Great"})

    def test_empty_values_are_left_out(self):
        records, _, _ = standardize(ALT, NEEDED, fields=TA_FIELDS)
        self.assertNotIn("previous_ta_reviews", records[0])

    def test_explicit_mapping_but_sensitive_names_always_drop(self):
        mapping = {"Favourite Colour": "program", "Degree Program": None, "Full Name": "relevant_courses"}
        _, report, _ = standardize(ALT, NEEDED, fields=TA_FIELDS, column_mapping=mapping)
        self.assertEqual(report.column_mapping["Favourite Colour"], "program")
        self.assertIn("Degree Program", report.dropped_unused)
        self.assertIn("Full Name", report.dropped_sensitive)

    def test_missing_id_duplicate_and_unknown_target_raise(self):
        with self.assertRaises(ValueError):
            standardize([{"program": "SE"}], NEEDED, fields=TA_FIELDS)
        with self.assertRaises(ValueError):
            standardize([{"ucid": "1", "major": "SE", "degree": "SE"}], NEEDED, fields=TA_FIELDS)
        with self.assertRaises(ValueError):
            standardize(ALT, NEEDED, fields=TA_FIELDS, column_mapping={"Year": "nope"})

    def test_missing_needed_field_warns(self):
        _, report, _ = standardize([{"ucid": "1", "major": "SE", "x": 1}], NEEDED, fields=TA_FIELDS)
        self.assertEqual(report.missing_fields, ["previous_ta_reviews", "relevant_courses"])
        self.assertTrue(any("relevant_courses" in w for w in report_warnings(report)))
        self.assertEqual(len(report_warnings(report)), 3)
        self.assertEqual(len(report_warnings(report, unmatched=False)), 2)

    def test_keep_unmatched_uses_normalized_name_but_still_drops_sensitive(self):
        records, report, _ = standardize(ALT, NEEDED, keep_unmatched=True)
        self.assertEqual(report.column_mapping["Favourite Colour"], "favourite_colour")
        self.assertEqual(records[0]["favourite_colour"], "blue")
        self.assertIn("Pronouns", report.dropped_sensitive)

    def test_bonus_field_kept_unless_sensitive(self):
        rows = [{"ucid": "1", "Hackathon Wins": "2", "email": "a@b.c"}]
        records, _, warnings = standardize(rows, {"hackathon_wins", "email"})
        self.assertEqual(records[0], {ID_FIELD: "1", "hackathon_wins": "2"})
        self.assertTrue(any("'email' is sensitive" in w for w in warnings))

    def test_pseudonymize_removes_id_column(self):
        records, _, _ = standardize(ALT, NEEDED, fields=TA_FIELDS)
        candidates, ref_map = pseudonymize(records)
        self.assertNotIn(ID_FIELD, candidates[0].fields)
        self.assertEqual(sorted(ref_map.values()), ["1", "2"])


class LoadTests(unittest.TestCase):
    def test_csv_with_excel_bom(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.csv"
            path.write_text("\ufeffUCID,Major\n1,SE\n", encoding="utf-8")
            self.assertEqual(load_records(path), [{"UCID": "1", "Major": "SE"}])


class SurveyExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "s.csv"

    def tearDown(self):
        self.tmp.cleanup()

    def test_group_row_above_header_and_rows_without_id(self):
        self.path.write_text(
            "MSc students,,,,\n"                                  # one-cell title
            "Student details,,,Wish to TA,TA experience\n"         # group row
            "Student ID,First Name,Dept,ENGG 225,ENGG 225\n"       # real header
            "1,A,ENBI,Yes,No\n"
            ",,,,\n"
            ",B,ENME,Yes,Yes\n", encoding="utf-8")
        rows = load_records(self.path)
        self.assertEqual(list(rows[0]), ["Student details / Student ID", "Student details / First Name",
                                         "Student details / Dept", "Wish to TA / ENGG 225",
                                         "TA experience / ENGG 225"])
        records, report, warnings = standardize(rows, set(), keep_unmatched=True)
        self.assertEqual(report.column_mapping["Student details / Student ID"], "candidate_id")
        self.assertIn("Student details / First Name", report.dropped_sensitive)
        self.assertEqual(len(records), 1)
        self.assertTrue(any("1 row(s) had no value in the ID column" in w for w in warnings))

    def test_windows_1252_with_mac_mojibake_is_repaired(self):
        # "’" saved as UTF-8, misread as Mac Roman, then saved as Windows-1252.
        garbled = "’".encode("utf-8").decode("mac_roman")
        self.path.write_bytes(f"UCID,Notes\n1,I{garbled}m café\n".encode("cp1252"))
        self.assertEqual(load_records(self.path), [{"UCID": "1", "Notes": "I’m café"}])


class ExcelTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "a.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "First"
        ws.append(["Fall 2026 applicants"])            # one-cell title row above the header
        ws.append([])
        ws.append(["UCID", "Major", None, "Applied On"])  # blank header column is ignored
        ws.append([1, "SE", "stray", date(2026, 9, 2)])
        ws.append([])                                  # empty rows are skipped
        ws.append([2, "ME", None, None])
        wb.create_sheet("Second").append(["UCID", "Major"])
        wb["Second"].append([3, "CE"])
        wb.save(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def test_header_detection_dates_and_empty_rows(self):
        self.assertEqual(load_records(self.path), [
            {"UCID": 1, "Major": "SE", "Applied On": "2026-09-02"},
            {"UCID": 2, "Major": "ME", "Applied On": None},
        ])

    def test_sheet_selection(self):
        self.assertEqual(load_records(self.path, sheet="Second"), [{"UCID": 3, "Major": "CE"}])
        with self.assertRaises(ValueError):
            load_records(self.path, sheet="Nope")

    def test_duplicate_headers_raise(self):
        wb = Workbook()
        wb.active.append(["UCID", "Major", "Major"])
        wb.save(self.path)
        with self.assertRaises(ValueError):
            load_records(self.path)

    def test_standardized_output_round_trips(self):
        keep = {f.id for f in TA_FIELDS if f.handling == "keep"}
        records, _, _ = standardize(load_records(self.path), keep, fields=TA_FIELDS)
        out = Path(self.dir.name) / "out.json"
        out.write_text(json.dumps(records))
        again, report, _ = standardize(load_records(out), keep, fields=TA_FIELDS)
        self.assertEqual(again, records)
        self.assertEqual(set(report.column_mapping.values()), {"candidate_id", "program"})


class CriteriaWithDataTests(unittest.TestCase):
    def test_criterion_without_any_data_sleeps(self):
        records, _, _ = standardize([{"ucid": "1", "major": "SE"}], NEEDED, fields=TA_FIELDS)
        candidates, _ = pseudonymize(records)
        reg = ta_registry()
        active = [reg.get("subject_knowledge"), reg.get("past_performance")]
        runnable, warnings = criteria_with_data(active, candidates)
        self.assertEqual([c.id for c in runnable], ["subject_knowledge"])
        self.assertIn("ranked on ['program'] only", warnings[0])    # partly missing data
        self.assertIn("past_performance' skipped", warnings[1])

    def test_built_in_criteria_get_no_fields_by_default(self):
        # Built-ins describe what to judge; the planner assigns fields. Unplanned, they sleep.
        reg = default_registry()
        self.assertTrue(all(not reg.get(cid).relevant_fields for cid in reg.ids()))
        candidates, _ = pseudonymize([{ID_FIELD: "1", "program": "SE"}])
        runnable, warnings = criteria_with_data([reg.get("subject_knowledge")], candidates)
        self.assertEqual(runnable, [])
        self.assertIn("no data field was assigned", warnings[0])


class DefaultCatalogTests(unittest.TestCase):
    def test_no_column_is_renamed_to_a_guessed_field(self):
        records, report, _ = standardize(ALT, set(), keep_unmatched=True)
        self.assertEqual(report.column_mapping["Student Number"], ID_FIELD)
        self.assertEqual(report.column_mapping["Degree Program"], "degree_program")      # not "program"
        self.assertEqual(report.column_mapping["Courses & Grades"], "courses_grades")    # not "relevant_courses"
        self.assertEqual(report.dropped_sensitive, ["Full Name", "Pronouns"])
        self.assertEqual({f.id for f in DEFAULT_FIELDS if f.handling == "keep"}, set())


if __name__ == "__main__":
    unittest.main()
