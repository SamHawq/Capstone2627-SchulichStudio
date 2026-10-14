"""Offline tests for the deterministic parts (no LLM calls). Run: python -m unittest"""

import unittest

from hiring_agents.aggregation import aggregate
from hiring_agents.orchestrator import resolve_criteria, validate_output
from hiring_agents.privacy import pseudonymize, view_for
from hiring_agents.registry import default_registry
from hiring_agents.schemas import (
    CandidateScore, Criterion, ExcludedCandidate, HiringRequest, RankerOutput,
)

RAW = [
    {"ucid": "1", "name": "X", "sex": "F", "program": "SE", "previous_ta_courses": ["ENGG 233"]},
    {"ucid": "2", "name": "Y", "ethnicity": "Z", "program": "ME", "previous_ta_courses": []},
]


class PrivacyTests(unittest.TestCase):
    def test_sensitive_fields_removed_and_ids_mapped(self):
        candidates, ref_map = pseudonymize(RAW)
        for c in candidates:
            self.assertFalse({"ucid", "name", "sex", "ethnicity"} & c.fields.keys())
        self.assertEqual(sorted(ref_map.values()), ["1", "2"])

    def test_view_only_includes_relevant_fields(self):
        candidates, _ = pseudonymize(RAW)
        criterion = default_registry().get("teaching_experience")
        self.assertEqual(set(view_for(candidates[0], criterion)), {"candidate_ref", "previous_ta_courses"})


class ResolveTests(unittest.TestCase):
    def test_unknown_and_zero_weight_criteria_sleep(self):
        req = HiringRequest(role="r", criteria={"teaching_experience": 1, "nope": 1, "past_performance": 0})
        active, warnings = resolve_criteria(req, default_registry())
        self.assertEqual([c.id for c in active], ["teaching_experience"])
        self.assertEqual(len(warnings), 2)

    def test_custom_bonus_criterion(self):
        bonus = Criterion(id="leadership", name="L", description="d", scoring_guidance="g", relevant_fields=[])
        req = HiringRequest(role="r", criteria={"leadership": 1}, custom_criteria=[bonus])
        active, _ = resolve_criteria(req, default_registry())
        self.assertEqual(active[0].type, "bonus")


class ValidationTests(unittest.TestCase):
    criterion = default_registry().get("subject_knowledge")

    def test_fixes_bad_llm_output(self):
        out = RankerOutput(criterion_id="wrong", scores=[
            CandidateScore(candidate_ref="a", score=15, rationale=""),
            CandidateScore(candidate_ref="a", score=3, rationale=""),
            CandidateScore(candidate_ref="ghost", score=5, rationale=""),
        ], excluded=[ExcludedCandidate(candidate_ref="b", reason="x")])
        fixed, warnings = validate_output(out, self.criterion, {"a", "b"})
        self.assertEqual(fixed.criterion_id, "subject_knowledge")
        self.assertEqual([(s.candidate_ref, s.score) for s in fixed.scores], [("a", 10)])
        self.assertEqual(fixed.excluded, [])  # not a hard constraint
        self.assertEqual(len(warnings), 5)  # clamp, duplicate, unknown, exclusion ignored, missing 'b'


class AggregationTests(unittest.TestCase):
    def test_weighted_ranking_and_exclusion(self):
        outputs = {
            "x": RankerOutput(criterion_id="x", scores=[
                CandidateScore(candidate_ref="a", score=10, rationale=""),
                CandidateScore(candidate_ref="b", score=0, rationale=""),
            ], excluded=[]),
            "y": RankerOutput(criterion_id="y", scores=[
                CandidateScore(candidate_ref="b", score=10, rationale=""),
            ], excluded=[ExcludedCandidate(candidate_ref="c", reason="unavailable")]),
        }
        final = aggregate("r", outputs, {"x": 3, "y": 1}, ["a", "b", "c"], top_x=5, warnings=[])
        self.assertEqual([(r.candidate_ref, r.total_score) for r in final.ranking], [("a", 0.75), ("b", 0.25)])
        self.assertEqual(final.excluded[0].candidate_ref, "c")


if __name__ == "__main__":
    unittest.main()
