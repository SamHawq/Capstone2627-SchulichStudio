"""Offline tests for criteria planning (planner and rankers stubbed). Run: python -m unittest"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import hiring_agents.orchestrator as orchestrator
import hiring_agents.planning as planning
from hiring_agents.datastore import Store
from hiring_agents.llm_agents import build_ranker
from hiring_agents.planning import (
    MAX_AUTO_CRITERIA, MAX_CATALOG_CRITERIA, add_auto_criteria, plan_catalog, profile_fields,
    refresh_catalog, validate_draft, with_catalog,
)
from hiring_agents.privacy import view_for
from hiring_agents.registry import default_registry
from hiring_agents.schemas import (
    CandidateScore, CriteriaDraft, CriterionExtension, DraftCriterion, FieldNote, FieldProfile,
    HiringRequest, RankerOutput, SkippedField,
)

REQUEST = HiringRequest(role="TA for ENGG 233", criteria={"teaching_experience": 0.6, "subject_knowledge": 0.4})
BASE = [default_registry().get(cid) for cid in default_registry().ids()]

# Survey-style data: none of the built-in criteria's standard fields are present.
SURVEY = [
    {"Student ID": str(i), "First Name": f"N{i}",
     "verbal and written skills to communicate": f"answer {i}",
     "Skill checklist / Python": ["Expert", "Beginner", "Expert", "Intermediate"][i % 4],
     "Wish to TA ENGG 204": "yes",
     "Time available / Monday 8.00 am - 1.00 pm": "yes"}
    for i in range(1, 7)
]


def _draft(**kw):
    return CriteriaDraft(**{"extensions": [], "criteria": [], "skipped": [], **kw})


def _notes(*fields):
    return [FieldNote(field=f, meaning=f"how to read {f}") for f in fields]


def _criterion(cid, fields, importance=2, courses=()):
    return DraftCriterion(id=cid, name=cid.title(), description="d", scoring_guidance="g",
                          fields=_notes(*fields), importance=importance, courses=list(courses))


def _profiles(*names):
    return [FieldProfile(field=n, filled=1, kind="text", common_values=[]) for n in names]


def _fake_planner(draft):
    return AsyncMock(return_value=SimpleNamespace(final_output_as=lambda *a, **k: draft))


class ProfileTests(unittest.TestCase):
    def test_kinds_labels_and_only_widely_shared_values(self):
        records = [{"candidate_id": str(i), "skill": s, "years": str(i), "essay": f"long answer {i}" * 5,
                    "used": "x", "availability": {"Fall 2025": {}}}
                   for i, s in enumerate(["Expert"] * 3 + ["Beginner"] * 2 + ["Rare"])]
        profiles = profile_fields(records, exclude={"used"}, labels={"skill": "Skill checklist / Python"})
        by_field = {p.field: p for p in profiles}
        self.assertEqual(set(by_field), {"skill", "years", "essay"})   # ID, availability, used excluded
        self.assertEqual(by_field["skill"].label, "Skill checklist / Python")
        self.assertEqual(by_field["skill"].kind, "category")
        self.assertEqual(by_field["skill"].common_values, ["Expert"])   # shared by < 3 -> not shown
        self.assertEqual(by_field["years"].kind, "number")
        self.assertEqual(by_field["essay"].kind, "text")
        self.assertEqual(by_field["essay"].common_values, [])


class ValidateDraftTests(unittest.TestCase):
    def test_extensions_criteria_notes_and_skipped(self):
        draft = _draft(
            extensions=[CriterionExtension(criterion_id="teaching_experience", fields=_notes("comm"))],
            criteria=[_criterion("technical skills", ["python", "java"], importance=3, courses=[" ENGG 233 "])],
            skipped=[SkippedField(field="timestamp", reason="administrative")],
        )
        catalog, warnings = validate_draft(
            draft, _profiles("comm", "python", "java", "timestamp", "other"), BASE, "TA hiring")
        self.assertEqual(warnings, [])
        self.assertEqual(catalog.extensions[0].fields[0].field, "comm")
        c = catalog.criteria[0]
        self.assertEqual((c.id, c.type, c.importance, c.courses), ("technical_skills", "auto", 3, ["ENGG 233"]))
        self.assertEqual(c.relevant_fields, ["python", "java"])       # one criterion, several fields
        self.assertEqual(c.field_notes[1].meaning, "how to read java")
        self.assertEqual([s.field for s in catalog.skipped], ["timestamp", "other"])   # unnamed reported too
        self.assertEqual(catalog.fields, ["comm", "python", "java", "timestamp", "other"])

    def test_invalid_parts_are_dropped_with_warnings(self):
        draft = _draft(
            extensions=[CriterionExtension(criterion_id="nope", fields=_notes("a")),
                        CriterionExtension(criterion_id="subject_knowledge", fields=_notes("a", "made_up"))],
            criteria=[_criterion("subject_knowledge", ["a", "b"], importance=9),   # id clash + reused field
                      _criterion("empty", ["made_up"])],
        )
        catalog, warnings = validate_draft(draft, _profiles("a", "b"), BASE, "p")
        self.assertEqual([c.id for c in catalog.criteria], ["auto_subject_knowledge"])
        self.assertEqual(catalog.criteria[0].relevant_fields, ["b"])
        self.assertEqual(catalog.criteria[0].importance, 3)            # clamped to 1-3
        self.assertEqual(len(warnings), 5)

    def test_criteria_are_capped(self):
        names = [f"f{i}" for i in range(MAX_CATALOG_CRITERIA + 2)]
        draft = _draft(criteria=[_criterion(n, [n]) for n in names])
        catalog, warnings = validate_draft(draft, _profiles(*names), BASE, "p")
        self.assertEqual(len(catalog.criteria), MAX_CATALOG_CRITERIA)
        self.assertTrue(any("more than" in w for w in warnings))


class RequestTimeTests(unittest.TestCase):
    def setUp(self):
        draft = _draft(
            extensions=[CriterionExtension(criterion_id="teaching_experience", fields=_notes("comm")),
                        CriterionExtension(criterion_id="past_performance", fields=_notes("reviews"))],
            criteria=[_criterion("technical", ["python"], importance=3),
                      _criterion("interest_204", ["wish_204"], courses=["ENGG 204"]),
                      _criterion("interest_233", ["wish_233"], importance=1, courses=["ENGG-233"])],
        )
        self.catalog, _ = validate_draft(
            draft, _profiles("comm", "reviews", "python", "wish_204", "wish_233"), BASE, "p")
        self.registry, self.warnings = with_catalog(default_registry(), self.catalog)

    def test_catalog_extends_built_ins_and_registers_auto_criteria(self):
        self.assertEqual(self.warnings, [])
        te = self.registry.get("teaching_experience")
        self.assertEqual(te.relevant_fields[-1], "comm")
        self.assertEqual(te.field_notes[-1].field, "comm")
        self.assertEqual(self.registry.get("technical").type, "auto")
        self.assertIsNone(default_registry().get("technical"))         # the shared registry is untouched

    def test_auto_criteria_that_apply_to_the_role_join(self):
        active, _ = orchestrator.resolve_criteria(REQUEST, self.registry)
        criteria, weights, skipped = add_auto_criteria(self.catalog, active, REQUEST)
        self.assertEqual([c.id for c in criteria],
                         ["teaching_experience", "subject_knowledge", "technical", "interest_233"])
        self.assertAlmostEqual(weights["technical"], 0.5 * 3 / 2)       # mean(0.6, 0.4) * importance / 2
        self.assertAlmostEqual(weights["interest_233"], 0.5 * 1 / 2)
        reasons = {s.field: s.reason for s in skipped}
        self.assertIn("ENGG 204", reasons["wish_204"])                 # other course -> doesn't join
        self.assertIn("not requested", reasons["reviews"])             # extension of an unrequested criterion
        self.assertNotIn("comm", reasons)

    def test_requested_auto_criterion_keeps_client_weight(self):
        request = HiringRequest(role="TA for ENGG 204", criteria={"interest_204": 2})
        active, _ = orchestrator.resolve_criteria(request, self.registry)
        _, weights, _ = add_auto_criteria(self.catalog, active, request)
        self.assertEqual(weights["interest_204"], 2)
        self.assertAlmostEqual(weights["technical"], 2 * 3 / 2)

    def test_joining_is_capped_most_important_first(self):
        names = [f"f{i}" for i in range(MAX_AUTO_CRITERIA + 1)]
        draft = _draft(criteria=[_criterion(n, [n], importance=1 if n == "f0" else 2) for n in names])
        catalog, _ = validate_draft(draft, _profiles(*names), BASE, "p")
        registry, _ = with_catalog(default_registry(), catalog)
        active, _ = orchestrator.resolve_criteria(REQUEST, registry)
        criteria, _, skipped = add_auto_criteria(catalog, active, REQUEST)
        self.assertEqual(sum(c.type == "auto" for c in criteria), MAX_AUTO_CRITERIA)
        self.assertEqual([s.field for s in skipped], ["f0"])

    def test_extended_built_in_missing_its_own_fields_is_not_a_warning(self):
        from hiring_agents.schemas import Candidate
        candidates = [Candidate(ref="C-1", fields={"comm": "x", "program": "SE"})]
        active = [self.registry.get("teaching_experience"), default_registry().get("subject_knowledge")]
        runnable, warnings = orchestrator.criteria_with_data(active, candidates)
        self.assertEqual(len(runnable), 2)
        # teaching_experience runs on its planned field; subject_knowledge has no planned
        # fields, so its partly missing data is still reported.
        self.assertEqual(len(warnings), 1)
        self.assertIn("subject_knowledge", warnings[0])

    def test_ranker_instructions_include_field_notes(self):
        instructions = build_ranker(self.registry.get("technical")).instructions
        self.assertIn("- python: how to read python", instructions)


class CatalogFileTests(unittest.TestCase):
    RECORDS = [{"candidate_id": str(i), "python": "Expert", "comm": "x"} for i in range(3)]

    def test_unchanged_fields_reuse_the_saved_file(self):
        draft = _draft(criteria=[_criterion("technical", ["python"])])
        with tempfile.TemporaryDirectory() as d, patch.object(planning.Runner, "run", _fake_planner(draft)) as fake:
            path = Path(d) / "criteria.json"
            first, planned, _ = asyncio.run(refresh_catalog(self.RECORDS, BASE, "p", path=path))
            self.assertTrue(planned and path.exists())
            # A hand edit survives a recompile with the same fields.
            path.write_text(path.read_text(encoding="utf-8").replace('"importance": 2', '"importance": 3'),
                            encoding="utf-8")
            again, planned, _ = asyncio.run(refresh_catalog(self.RECORDS, BASE, "p", path=path))
            self.assertFalse(planned)
            self.assertEqual(again.criteria[0].importance, 3)
            # A new field means a new plan, and the old file is kept.
            changed = [{**r, "gpa": "3.5"} for r in self.RECORDS]
            _, planned, _ = asyncio.run(refresh_catalog(changed, BASE, "p", path=path))
            self.assertTrue(planned)
            self.assertTrue((Path(d) / "criteria.prev.json").exists())
            self.assertEqual(fake.await_count, 2)

    def test_planner_failure_is_a_warning_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d, \
             patch.object(planning.Runner, "run", AsyncMock(side_effect=RuntimeError("api down"))):
            path = Path(d) / "criteria.json"
            catalog, _, warnings = asyncio.run(refresh_catalog(self.RECORDS, BASE, "p", path=path))
            self.assertIsNone(catalog)
            self.assertTrue(warnings)
            self.assertFalse(path.exists())

    def test_request_time_plans_are_cached(self):
        draft = _draft(criteria=[_criterion("technical", ["python"])])
        with tempfile.TemporaryDirectory() as d, patch.object(planning.Runner, "run", _fake_planner(draft)) as fake:
            cache = Path(d) / "plans.json"
            first, _ = asyncio.run(plan_catalog(self.RECORDS, BASE, "role", cache_path=cache))
            second, _ = asyncio.run(plan_catalog(self.RECORDS, BASE, "role", cache_path=cache))
            self.assertEqual(fake.await_count, 1)
            self.assertEqual(first, second)


class EndToEndTests(unittest.TestCase):
    """rank_candidates on survey-style data, with the planner and rankers stubbed."""

    DRAFT = _draft(
        extensions=[CriterionExtension(criterion_id="teaching_experience",
                                       fields=_notes("verbal_and_written_skills_to_communicate"))],
        criteria=[_criterion("technical_skills", ["skill_checklist_python"]),
                  _criterion("interest_engg_204", ["wish_to_ta_engg_204"], courses=["ENGG 204"])],
    )

    def run_pipeline(self, request, draft=DRAFT, **patches):
        seen = {}

        async def fake_ranker(criterion, candidates, req):
            seen[criterion.id] = [view_for(c, criterion) for c in candidates]
            return RankerOutput(criterion_id=criterion.id, excluded=[], scores=[
                CandidateScore(candidate_ref=c.ref, score=5, rationale="r") for c in candidates])

        # A temp store, so the plan cache in the real candidate_data/ is never touched.
        with tempfile.TemporaryDirectory() as d, \
             patch.object(planning.Runner, "run", _fake_planner(draft)) as planner, \
             patch.object(orchestrator, "run_ranker", fake_ranker):
            final, _ = asyncio.run(orchestrator.rank_candidates(request, SURVEY, store=Store(Path(d))))
        return final, seen, planner

    def test_survey_fields_fitted_and_grouped(self):
        final, seen, planner = self.run_pipeline(REQUEST)
        self.assertEqual(planner.await_count, 1)
        self.assertEqual({(c.id, c.source) for c in final.criteria},
                         {("teaching_experience", "requested"), ("technical_skills", "auto")})
        self.assertEqual(seen["technical_skills"][0]["skill_checklist_python"], "Beginner")
        sent = {k for views in seen.values() for v in views for k in v}
        self.assertFalse({"first_name", "candidate_id", "availability", "wish_to_ta_engg_204"} & sent)
        self.assertAlmostEqual(sum(c.weight for c in final.criteria), 1.0, places=2)
        self.assertTrue(any("subject_knowledge" in w and "skipped" in w for w in final.warnings))
        self.assertIn("wish_to_ta_engg_204", {s.field for s in final.skipped_fields})

    def test_planner_sees_labels_but_never_availability_or_answers(self):
        _, _, planner = self.run_pipeline(REQUEST)
        payload = planner.await_args.args[1]
        self.assertIn("Skill checklist / Python", payload)
        self.assertNotIn("time_available", payload)                    # availability never planned
        self.assertNotIn("answer 1", payload)                          # free text: no values sent
        self.assertNotIn("N1", payload)

    def test_without_auto_criteria_survey_data_has_no_criteria(self):
        with self.assertRaises(ValueError):
            self.run_pipeline(REQUEST.model_copy(update={"auto_criteria": False}))


if __name__ == "__main__":
    unittest.main()
