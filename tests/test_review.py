"""Offline tests for the Manager's final review and focused ranker input. Run: python -m unittest"""

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import hiring_agents.orchestrator as orchestrator
import hiring_agents.review as review
from hiring_agents.review import (
    MAX_ADJUSTMENT, apply_review, has_ties, other_info_for, shortlist_size, tie_groups, validate_review,
    wants_review,
)
from hiring_agents.schemas import (
    CandidateReview, CandidateScore, FinalRanking, HiringRequest, RankedCandidate, RankerOutput, ReviewOutput,
)
from tests.stubs import patch_review, patch_screen, ta_registry

ROWS = [{"UCID": f"3002000{i}", "teaching_experience": f"Tutored {i} terms"} for i in range(1, 9)]


def _ranking(scores):
    return FinalRanking(role="TA", excluded=[], warnings=[], ranking=[
        RankedCandidate(candidate_ref=ref, total_score=s, per_criterion={"t": 5}, rationales={"t": "r"})
        for ref, s in scores])


def _rev(ref, adj, reason="Applies the lab emphasis.", tiebreak=0.0, tiebreak_reason=""):
    return CandidateReview(candidate_ref=ref, adjustment=adj, reason=reason, justification=f"j {ref}",
                           tiebreak=tiebreak, tiebreak_reason=tiebreak_reason)


class ReviewLogicTests(unittest.TestCase):
    def test_when_it_runs_and_how_big_the_shortlist_is(self):
        self.assertFalse(wants_review(HiringRequest(role="TA", criteria={"a": 1})))
        self.assertTrue(wants_review(HiringRequest(role="TA", criteria={"a": 1}, client_instructions="labs")))
        self.assertTrue(wants_review(HiringRequest(role="TA", criteria={"a": 1}, notes="tie-break on labs")))
        self.assertFalse(wants_review(HiringRequest(role="TA", criteria={"a": 1}, notes="x", final_review=False)))
        self.assertEqual(shortlist_size(5), 10)
        self.assertEqual(shortlist_size(1), 6)
        self.assertEqual(shortlist_size(40), 40)

    def test_adjustments_are_bounded_and_need_a_reason(self):
        output = ReviewOutput(summary="s", candidates=[
            _rev("C-a", 0.5), _rev("C-b", -0.03, reason=""), _rev("C-a", -0.1), _rev("C-zzz", 0.1)])
        reviews, warnings = validate_review(output, ["C-a", "C-b", "C-c"])
        self.assertEqual(reviews["C-a"].adjustment, MAX_ADJUSTMENT)       # clamped; repeat ignored
        self.assertEqual(reviews["C-b"].adjustment, 0.0)                  # no reason -> not applied
        self.assertNotIn("C-zzz", reviews)                                # not on the shortlist
        self.assertNotIn("C-c", reviews)
        self.assertEqual(len(warnings), 3)                                # clamp, no reason, skipped

    def test_final_score_is_weighted_plus_adjustment_resorted_and_cut(self):
        ranking = _ranking([("C-a", 0.80), ("C-b", 0.75), ("C-c", 0.70)])
        reviews = {"C-c": _rev("C-c", 0.10), "C-a": _rev("C-a", 0.0, "")}
        final, _ = apply_review(ranking, reviews, top_x=2)
        self.assertEqual([r.candidate_ref for r in final], ["C-a", "C-c"])   # C-c moved in at 0.80
        self.assertEqual((final[1].weighted_score, final[1].total_score, final[1].adjustment), (0.70, 0.8, 0.10))
        self.assertEqual(final[0].justification, "j C-a")


class TieBreakTests(unittest.TestCase):
    def test_tie_groups_chain_close_scores(self):
        self.assertEqual(tie_groups([0.9, 0.9, 0.85, 0.845, 0.84, 0.7]), [[0, 1], [2, 3, 4]])
        self.assertEqual(tie_groups([0.9, 0.8]), [])
        self.assertEqual(tie_groups([]), [])

    def test_tiebreak_orders_tied_candidates_only(self):
        ranking = _ranking([("C-a", 0.80), ("C-b", 0.70), ("C-c", 0.70), ("C-d", 0.695)])
        reviews = {"C-a": _rev("C-a", 0.0, "", tiebreak=-1.0),
                   "C-b": _rev("C-b", 0.0, "", tiebreak=-0.5),
                   "C-d": _rev("C-d", 0.0, "", tiebreak=1.0, tiebreak_reason="Motivation names this course.")}
        final, decided = apply_review(ranking, reviews, top_x=4)
        # C-a isn't tied with anyone, so its poor tiebreak can't move it; C-d tops its tie group.
        self.assertEqual([r.candidate_ref for r in final], ["C-a", "C-d", "C-c", "C-b"])
        self.assertEqual([r.total_score for r in final], [0.8, 0.695, 0.7, 0.7])   # scores unchanged
        self.assertEqual(final[1].tiebreak_reason, "Motivation names this course.")
        self.assertEqual(final[0].tiebreak_reason, "")
        self.assertEqual(decided, [["C-d", "C-c", "C-b"]])

    def test_other_info_and_when_ties_trigger_a_review(self):
        from hiring_agents.schemas import Candidate
        cands = [Candidate(ref="C-a", fields={"why_ta": "x" * 400, "python": "Expert"}),
                 Candidate(ref="C-b", fields={"python": "Beginner"})]
        info = other_info_for(cands, ["why_ta"], {"why_ta": "Why do you want to TA?"})
        self.assertEqual(list(info), ["C-a"])                                   # only candidates with some
        self.assertEqual(len(info["C-a"]["Why do you want to TA?"]), review.OTHER_CHARS + 1)
        self.assertNotIn("python", json.dumps(info))                            # tiebreak fields only
        tied = _ranking([("C-a", 0.7), ("C-b", 0.7)])
        self.assertTrue(has_ties(tied, info))
        self.assertFalse(has_ties(_ranking([("C-a", 0.9), ("C-b", 0.7)]), info))
        self.assertFalse(has_ties(tied, {}))                                    # nothing to break it with
        self.assertTrue(wants_review(HiringRequest(role="TA", criteria={"a": 1}), has_ties=True))


class ReviewPipelineTests(unittest.TestCase):
    def run_pipeline(self, request, reviewer=None):
        async def ranker(criterion, candidates, req):
            return RankerOutput(criterion_id=criterion.id, excluded=[], scores=[
                CandidateScore(candidate_ref=c.ref, score=10 - i, rationale=f"ran {i} labs")
                for i, c in enumerate(candidates)])

        import tempfile
        from pathlib import Path

        from hiring_agents.datastore import Store
        # A fixed pseudonym key, so refs match between the two runs a test compares.
        with tempfile.TemporaryDirectory() as d, patch_screen(), patch.object(orchestrator, "run_ranker", ranker), \
             patch.dict(os.environ, {"PSEUDONYM_SALT": "test-only"}), (reviewer or patch_review()) as fake:
            final, ref_map = asyncio.run(orchestrator.rank_candidates(request, ROWS, store=Store(Path(d)),
                                                                      registry=ta_registry()))
        return final, ref_map, fake

    def test_review_sees_shortlist_and_instructions_and_can_move_someone_in(self):
        request = HiringRequest(role="TA for ENGG 233", criteria={"teaching_experience": 1}, top_x=3,
                                auto_criteria=False, client_instructions="Hands-on lab experience matters most.",
                                notes="First-year course with weekly labs.")
        # Find who is 4th by weighted score, then let the review lift them by the maximum.
        plain, _, _ = self.run_pipeline(request.model_copy(update={"final_review": False, "top_x": 8}))
        third, fourth = plain.ranking[2].candidate_ref, plain.ranking[3].candidate_ref
        # Scores are 0.1 apart: +0.10 alone would only tie (ties go to the higher weighted
        # score), so the review also marks the 3rd down a little.
        final, _, fake = self.run_pipeline(request, patch_review({fourth: MAX_ADJUSTMENT, third: -0.05}))
        payload = fake.payloads[0]
        self.assertEqual(len(payload["shortlist"]), shortlist_size(3))
        self.assertEqual(payload["client_instructions"], "Hands-on lab experience matters most.")
        self.assertEqual(payload["notes"], "First-year course with weekly labs.")
        self.assertNotIn("3002000", json.dumps(payload))                 # refs only, never real IDs
        self.assertTrue(final.reviewed)
        self.assertIn(fourth, [r.candidate_ref for r in final.ranking])
        moved = next(r for r in final.ranking if r.candidate_ref == fourth)
        self.assertAlmostEqual(moved.total_score - moved.weighted_score, MAX_ADJUSTMENT)
        self.assertTrue(moved.adjustment_reason)
        self.assertNotIn(third, [r.candidate_ref for r in final.ranking])
        self.assertEqual(len(final.ranking), 3)

    def test_a_maximum_nudge_alone_only_ties_the_next_candidate(self):
        request = HiringRequest(role="TA", criteria={"teaching_experience": 1}, top_x=3, auto_criteria=False,
                                client_instructions="labs")
        plain, _, _ = self.run_pipeline(request.model_copy(update={"final_review": False, "top_x": 8}))
        fourth = plain.ranking[3].candidate_ref
        final, _, _ = self.run_pipeline(request, patch_review({fourth: MAX_ADJUSTMENT}))
        self.assertNotIn(fourth, [r.candidate_ref for r in final.ranking])   # tie -> higher weighted wins

    def test_no_instructions_means_no_review_call(self):
        request = HiringRequest(role="TA", criteria={"teaching_experience": 1}, top_x=3, auto_criteria=False)
        final, _, fake = self.run_pipeline(request)
        self.assertEqual(fake.payloads, [])
        self.assertFalse(final.reviewed)
        self.assertTrue(all(r.weighted_score == r.total_score for r in final.ranking))

    def test_failed_review_keeps_the_weighted_order(self):
        request = HiringRequest(role="TA", criteria={"teaching_experience": 1}, top_x=3, auto_criteria=False,
                                notes="prefer labs")
        failing = patch.object(review, "run_with_retries", AsyncMock(side_effect=RuntimeError("api down")))
        final, _, _ = self.run_pipeline(request, failing)
        self.assertFalse(final.reviewed)
        self.assertEqual(len(final.ranking), 3)
        self.assertTrue(any("final review failed" in w for w in final.warnings))

    def test_ties_are_broken_from_other_info_without_instructions(self):
        from hiring_agents.schemas import CriteriaCatalog, SkippedField
        rows = [{"UCID": f"3003000{i}", "teaching_experience": "Tutored", "why_ta": f"Reason {i}",
                 "timestamp": "2026-01-01"} for i in range(1, 6)]
        catalog = CriteriaCatalog(purpose="p", fields=["teaching_experience", "why_ta", "timestamp"],
                                  extensions=[], criteria=[], skipped=[
                                      SkippedField(field="why_ta", reason="motivation", use="tiebreak"),
                                      SkippedField(field="timestamp", reason="metadata", use="administrative")])

        async def ranker(criterion, candidates, req):   # everyone tied on 7
            return RankerOutput(criterion_id=criterion.id, excluded=[], scores=[
                CandidateScore(candidate_ref=c.ref, score=7, rationale="r") for c in candidates])

        import tempfile
        from pathlib import Path

        from hiring_agents.datastore import Store
        from hiring_agents.planning import write_catalog
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d))
            write_catalog(catalog, store.criteria)
            request = HiringRequest(role="TA", criteria={"teaching_experience": 1}, top_x=2)
            with patch_screen(), patch.object(orchestrator, "run_ranker", ranker),                  patch.object(orchestrator, "_get_catalog", AsyncMock(return_value=(catalog, []))),                  patch.dict(os.environ, {"PSEUDONYM_SALT": "test-only"}), patch_review() as probe:
                asyncio.run(orchestrator.rank_candidates(request, rows, store=store, registry=ta_registry()))
            last = sorted(c["candidate_ref"] for c in probe.payloads[0]["shortlist"])[-1]
            with patch_screen(), patch.object(orchestrator, "run_ranker", ranker),                  patch.object(orchestrator, "_get_catalog", AsyncMock(return_value=(catalog, []))),                  patch.dict(os.environ, {"PSEUDONYM_SALT": "test-only"}), patch_review(tiebreaks={last: 1.0}) as fake:
                final, _ = asyncio.run(orchestrator.rank_candidates(request, rows, store=store,
                                                                    registry=ta_registry()))
        payload = fake.payloads[0]
        self.assertEqual(payload["client_instructions"], "")                  # no instructions: ties alone
        self.assertTrue(all("why_ta" in c["other_info"] for c in payload["shortlist"]))
        self.assertNotIn("timestamp", json.dumps(payload))                    # administrative never shown
        self.assertNotIn("3003000", json.dumps(payload))
        self.assertEqual(final.ranking[0].candidate_ref, last)                 # the tie went its way
        self.assertTrue(final.ranking[0].tiebreak_reason)
        self.assertEqual({r.total_score for r in final.ranking}, {0.7})        # scores unchanged
        self.assertEqual(final.tiebreak_fields, ["why_ta"])

    def test_rankers_get_only_the_role_and_their_fields(self):
        captured = {}

        async def fake_run(agent, payload, output_type, log_dir=None):
            captured["payload"] = json.loads(payload)
            captured["instructions"] = agent.instructions
            data = captured["payload"]["candidates"]
            return RankerOutput(criterion_id="teaching_experience", excluded=[], scores=[
                CandidateScore(candidate_ref=c["candidate_ref"], score=5, rationale="r") for c in data])

        request = HiringRequest(role="TA for ENGG 233", criteria={"teaching_experience": 1},
                                auto_criteria=False, notes="SECRET-NOTES", client_instructions="SECRET-BRIEF",
                                instructions={"teaching_experience": "Credit lab teaching."}, final_review=False)
        import tempfile
        from pathlib import Path

        from hiring_agents.datastore import Store
        with tempfile.TemporaryDirectory() as d, patch_screen(), patch.object(orchestrator, "run_agent", fake_run):
            asyncio.run(orchestrator.rank_candidates(request, ROWS, store=Store(Path(d)), registry=ta_registry()))
        self.assertEqual(set(captured["payload"]), {"role", "candidates"})
        everything = json.dumps(captured["payload"]) + captured["instructions"]
        self.assertNotIn("SECRET-NOTES", everything)
        self.assertNotIn("SECRET-BRIEF", everything)
        self.assertIn("Credit lab teaching.", captured["instructions"])   # its own instructions only


if __name__ == "__main__":
    unittest.main()
