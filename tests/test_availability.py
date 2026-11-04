"""Offline tests for parsing survey availability into the structured field. Run: python -m unittest"""

import tempfile
import unittest
from pathlib import Path

from hiring_agents.availability import (
    answer, extract_availability, find_slots, in_person, merge_availability, term_of,
)
from hiring_agents.datastore import compile_sources

MON_AM = {"day": "mon", "start": "08:00", "end": "13:00"}
TUE_PM = {"day": "tue", "start": "13:00", "end": "17:00"}


class ParseTests(unittest.TestCase):
    def test_slots_from_text_and_field_names(self):
        self.assertEqual(find_slots("Monday 8.00 am - 1.00 pm,Tuesday 1.00 pm - 5.00 pm"),
                         [("mon", "08:00", "13:00"), ("tue", "13:00", "17:00")])
        self.assertEqual(find_slots("time_available_to_ta_thursday_10_00_am_12_50_pm"),
                         [("thu", "10:00", "12:50")])
        self.assertEqual(find_slots("Friday 12.00 pm to 12.30 am"), [("fri", "12:00", "00:30")])
        self.assertEqual(find_slots("Intermediate"), [])

    def test_answers_in_person_and_terms(self):
        self.assertEqual([answer(v) for v in ("yes", "yes,", " Yes ", "no", "Beginner")],
                         [True, True, True, False, None])
        self.assertEqual(in_person("Yes, I am fully available"), "full")
        self.assertEqual(in_person("No, I will be away for three or more days during that time"), "away")
        self.assertIsNone(in_person("yes"))
        self.assertEqual(term_of("Studio Winter 2026 - TA Survey.csv"), "Winter 2026")
        self.assertEqual(term_of("book.xlsx [Fall_2025]"), "Fall 2025")
        self.assertEqual(term_of("applicants.csv"), "unknown term")


class ExtractTests(unittest.TestCase):
    def test_all_three_shapes_become_one_field(self):
        records = [
            {"candidate_id": "1", "program": "SE",
             "time_available_to_ta_monday_8_00_am_1_00_pm": "yes,",
             "time_available_to_ta_tuesday_1_00_pm_5_00_pm": "no",
             "availability": "Yes, other than one or two days during the in-person portion"},
            {"candidate_id": "2",
             "lab_times": "Monday 8.00 am - 1.00 pm,Tuesday 1.00 pm - 5.00 pm",
             "availability": "Yes, I am fully available"},
            {"candidate_id": "3", "lab_times": "Beginner"},   # shifted column value
        ]
        out, consumed, warnings = extract_availability(records, "Fall 2025")
        self.assertEqual(consumed, {"time_available_to_ta_monday_8_00_am_1_00_pm",
                                    "time_available_to_ta_tuesday_1_00_pm_5_00_pm",
                                    "availability", "lab_times"})
        self.assertEqual(out[0], {"candidate_id": "1", "program": "SE", "availability": {"Fall 2025": {
            "available": [MON_AM], "unavailable": [TUE_PM], "in_person_portion": "minor_conflicts"}}})
        self.assertEqual(out[1]["availability"]["Fall 2025"]["available"], [MON_AM, TUE_PM])
        self.assertEqual(out[2], {"candidate_id": "3"})       # nothing usable -> no field
        self.assertEqual(len(warnings), 1)

    def test_no_availability_fields_leaves_records_alone(self):
        records = [{"candidate_id": "1", "program": "SE"}]
        out, consumed, warnings = extract_availability(records, "Fall 2025")
        self.assertEqual((out, consumed, warnings), (records, set(), []))


class MergeTests(unittest.TestCase):
    def test_same_term_combined_conflicts_flagged_terms_kept_apart(self):
        a = {"Fall 2025": {"available": [MON_AM], "unavailable": [], "in_person_portion": "full"}}
        b = {"Fall 2025": {"available": [TUE_PM], "unavailable": [MON_AM], "in_person_portion": "away"},
             "Winter 2026": {"available": [TUE_PM], "unavailable": []}}
        merged = merge_availability(a, b)
        self.assertEqual(merged["Fall 2025"], {"available": [TUE_PM], "unavailable": [],
                                               "conflicting": [MON_AM], "in_person_portion": "away"})
        self.assertEqual(merged["Winter 2026"]["available"], [TUE_PM])

    def test_compile_files_by_term(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "Fall 2025 ENGG 225.csv").write_text(
                "ucid,Monday 8.00 am - 1.00 pm,Tuesday 1.00 pm - 5.00 pm\n1,yes,no\n")
            (d / "Fall 2025 ENGG 204.csv").write_text(
                "ucid,Monday 8.00 am - 1.00 pm\n1,no\n")
            (d / "Winter 2026 ENGG 200.csv").write_text(
                'ucid,lab times\n1,"Tuesday 1.00 pm - 5.00 pm"\n')
            records, reports, _ = compile_sources([d])
        avail = records[0]["availability"]
        self.assertEqual(avail["Fall 2025"]["conflicting"], [MON_AM])
        self.assertEqual(avail["Fall 2025"]["unavailable"], [TUE_PM])
        self.assertEqual(avail["Winter 2026"]["available"], [TUE_PM])
        self.assertEqual(reports["Winter 2026 ENGG 200.csv"].column_mapping["lab times"],
                         "availability (Winter 2026)")


if __name__ == "__main__":
    unittest.main()
