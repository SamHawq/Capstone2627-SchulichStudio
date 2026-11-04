"""Offline tests for the service layer (planner and rankers stubbed). Run: python -m unittest"""

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import hiring_agents.orchestrator as orchestrator
import hiring_agents.planning as planning
from hiring_agents.datastore import Store
from hiring_agents.schemas import (
    CandidateScore, CriteriaDraft, DraftCriterion, FieldNote, HiringRequest, RankerOutput,
)
from hiring_agents.service import (
    ServiceError, data_status, list_criteria, process_store, rank, save_result, upload_files,
    validate_request,
)

APPS = "UCID,First Name,Program,Year,Python skill\n" + "".join(
    f"{i},N{i},Software,{i % 4 + 1},{['Expert', 'Beginner', 'Intermediate'][i % 3]}\n" for i in range(1, 8))
REVIEWS = "UCID,Reviews\n1,Great TA\n2,Often late\n"

DRAFT = CriteriaDraft(extensions=[], skipped=[], criteria=[DraftCriterion(
    id="technical_skills", name="Technical Skills", description="d", scoring_guidance="g",
    fields=[FieldNote(field="python_skill", meaning="self-rated level")], importance=2, courses=[])])


def _planner():
    return AsyncMock(return_value=SimpleNamespace(final_output_as=lambda *a, **k: DRAFT))


async def _fake_ranker(criterion, candidates, request):
    return RankerOutput(criterion_id=criterion.id, excluded=[], scores=[
        CandidateScore(candidate_ref=c.ref, score=i % 10, rationale="r") for i, c in enumerate(candidates)])


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "store")
        (self.dir / "apps.csv").write_text(APPS)
        (self.dir / "reviews.csv").write_text(REVIEWS)
        self.planner = patch.object(planning.Runner, "run", _planner())
        self.planner_mock = self.planner.start()

    def tearDown(self):
        self.planner.stop()
        self.tmp.cleanup()

    def upload(self, *names, **kw):
        return asyncio.run(upload_files([self.dir / n for n in names], store=self.store, **kw))

    def test_upload_processes_and_plans(self):
        result = self.upload("apps.csv", "reviews.csv")
        self.assertEqual(result.added, ["apps.csv", "reviews.csv"])
        self.assertTrue(result.criteria_planned)
        self.assertEqual(result.status.candidates, 7)
        self.assertFalse(result.status.stale)
        self.assertNotIn("First Name", result.sources["apps.csv"].column_mapping)   # sensitive dropped
        ids = {c.id: c for c in list_criteria(self.store)}
        self.assertEqual(ids["technical_skills"].type, "auto")
        self.assertEqual(ids["technical_skills"].suggested_weight, 1.0)
        self.assertIn("program", ids["subject_knowledge"].fields)

    def test_name_clash_needs_replace(self):
        self.upload("apps.csv")
        with self.assertRaises(ServiceError):
            self.upload("apps.csv")
        result = self.upload("apps.csv", replace=True)
        self.assertEqual(result.replaced, ["apps.csv"])
        self.assertEqual(self.planner_mock.await_count, 1)      # same fields -> criteria.json reused

    def test_bad_upload_leaves_store_as_it_was(self):
        self.upload("apps.csv")
        before = self.store.candidates.read_text()
        (self.dir / "bad.csv").write_text("Name,Notes\nA,no id column\n")
        (self.dir / "notes.txt").write_text("x")
        with self.assertRaises(ServiceError):
            self.upload("notes.txt")
        # A file with no ID column alone is fine (skipped with a warning); two columns
        # mapping to one field is not, and must be rolled back.
        (self.dir / "dup.csv").write_text("UCID,Student ID\n1,2\n")
        with self.assertRaises(ServiceError):
            self.upload("dup.csv")
        self.assertEqual(data_status(self.store).files, ["apps.csv"])
        self.assertEqual(self.store.candidates.read_text(), before)

    def test_column_mapping_is_saved_with_the_store(self):
        (self.dir / "odd.csv").write_text("Stu #,Program\n9,Math\n")
        self.upload("odd.csv", column_mapping={"Stu #": "candidate_id"}, plan_criteria=False)
        sources, _, _ = asyncio.run(process_store(self.store, plan_criteria=False))
        self.assertEqual(sources["odd.csv"].column_mapping["Stu #"], "candidate_id")

    def test_status_notices_changed_files(self):
        self.upload("apps.csv", plan_criteria=False)
        later = time.time() + 5
        os.utime(self.store.raw / "apps.csv", (later, later))
        self.assertTrue(data_status(self.store).stale)

    def test_request_validation(self):
        self.upload("apps.csv")
        ok = HiringRequest(role="TA", criteria={"technical_skills": 1})
        validate_request(ok, self.store)
        for bad in ({"tecnical_skills": 1}, {"subject_knowledge": -1}, {"subject_knowledge": 0}):
            with self.assertRaises(ServiceError):
                validate_request(HiringRequest(role="TA", criteria=bad), self.store)
        with self.assertRaises(ServiceError):   # auto criteria need auto_criteria on
            validate_request(ok.model_copy(update={"auto_criteria": False}), self.store)
        with self.assertRaises(ValueError):
            HiringRequest(role="TA", criteria={"subject_knowledge": 1}, top_x=0)

    def test_rank_returns_ids_for_shown_candidates_only(self):
        with self.assertRaises(ServiceError):
            asyncio.run(rank(HiringRequest(role="TA", criteria={"subject_knowledge": 1}), self.store))
        self.upload("apps.csv", "reviews.csv")
        request = HiringRequest(role="TA for ENGG 233", criteria={"subject_knowledge": 2, "past_performance": 1},
                                top_x=3)
        with patch.object(orchestrator, "run_ranker", _fake_ranker):
            result = asyncio.run(rank(request, self.store))
        self.assertEqual(len(result.ranking.ranking), 3)
        self.assertEqual(set(result.candidate_ids), {c.candidate_ref for c in result.ranking.ranking})
        self.assertEqual({c.id: c.source for c in result.ranking.criteria},
                         {"subject_knowledge": "requested", "past_performance": "requested",
                          "technical_skills": "auto"})
        json_path, html_path = save_result(result, self.dir / "results")
        self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["request"]["top_x"], 3)
        self.assertIn(next(iter(result.candidate_ids.values())), html_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
