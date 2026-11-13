"""Offline tests for compiling many data files into the candidate store. Run: python -m unittest"""

import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from hiring_agents.datastore import compile_sources, load_store, write_store
from hiring_agents.intake import report_warnings, standardize


def _xlsx(path, sheets):
    """Write {sheet name: rows} to an .xlsx file."""
    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for row in rows:
            ws.append(row)
    wb.save(path)


class CompileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        _xlsx(self.dir / "applications.xlsx", {
            "Instructions": [["One row per applicant."]],
            "Fall": [["UCID", "First Name", "Program", "Shirt Size"],
                     [123, "A", "SE", "M"],
                     [456, "B", "CE", "L"]],
        })
        _xlsx(self.dir / "reviews.xlsx", {
            "Reviews": [["Student ID", "Instructor Feedback"],
                        ["00000123", "Great"],
                        ["789", "Good"]],
        })
        (self.dir / "registrar.csv").write_text("ucid,program,sex\n456,ME,M\n")
        (self.dir / "~$applications.xlsx").write_text("excel lock file")

    def tearDown(self):
        self.tmp.cleanup()

    def compile(self):
        return compile_sources([self.dir])

    def test_merges_by_id_across_files_and_keeps_leading_zeros(self):
        records, reports, _ = self.compile()
        by_id = {r["candidate_id"]: r for r in records}
        self.assertEqual(set(by_id), {"00000123", "456", "789"})
        self.assertEqual(by_id["00000123"]["instructor_feedback"], "Great")   # own name, not a guessed field
        self.assertEqual(by_id["00000123"]["program"], "SE")
        self.assertEqual(len(reports), 3)  # instructions sheet and lock file are not sources

    def test_conflicting_values_kept_as_list_with_warning(self):
        records, _, warnings = self.compile()
        r456 = next(r for r in records if r["candidate_id"] == "456")
        self.assertEqual(r456["program"], ["CE", "ME"])
        self.assertTrue(any("'program' differed" in w for w in warnings))
        self.assertTrue(any("[Instructions]" in w and "skipped" in w for w in warnings))

    def test_sensitive_dropped_and_extras_kept(self):
        records, _, _ = self.compile()
        keys = {k for r in records for k in r}
        self.assertFalse({"first_name", "name", "sex"} & keys)
        self.assertIn("shirt_size", keys)

    def test_no_id_anywhere_raises(self):
        _xlsx(self.dir / "only.xlsx", {"S": [["Major", "Year"], ["SE", 3]]})
        with self.assertRaises(ValueError):
            compile_sources([self.dir / "only.xlsx"])

    def test_store_round_trip_feeds_intake_quietly(self):
        records, _, _ = self.compile()
        path = write_store(records, self.dir / "out" / "candidates.json")
        again, report, _ = standardize(load_store(path), {"program", "instructor_feedback"})
        self.assertNotIn("shirt_size", again[0])        # extras never reach rankers unless needed
        self.assertEqual(report_warnings(report, unmatched=False), [])

    def test_missing_store_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_store(self.dir / "nope.json")


if __name__ == "__main__":
    unittest.main()
