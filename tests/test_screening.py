"""Offline tests for the header screen, the prompt files and the agent trace. Run: python -m unittest"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import hiring_agents.agent_trace as agent_trace
import hiring_agents.orchestrator as orchestrator
import hiring_agents.screening as screening
from hiring_agents.datastore import Store, load_store
from hiring_agents.llm_agents import build_criteria_planner, build_header_screen, build_manager, build_ranker
from hiring_agents.prompts import PROMPTS_DIR, render
from hiring_agents.registry import default_registry
from hiring_agents.schemas import (
    ColumnDecision, HeaderScreen, HeaderScreenOutput, HiringRequest, RankerOutput, CandidateScore,
)
from hiring_agents.screening import ScreeningError, apply_screen, load_screen, screen_headers, validate_screen
from hiring_agents.service import process_store, upload_files
from tests.stubs import patch_screen, screen_reply, ta_registry
from tests.test_service import DRAFT, _planner

# "Stu #" isn't a known ID alias and "Nombre completo" isn't on the name blocklist: only the
# screen catches them. "Email Address" is on the blocklist either way.
DATA = "Stu #,Nombre completo,Email Address,Program,Python skill\n" + "".join(
    f"{i},Persona {i},p{i}@x.ca,Software,{['Expert', 'Beginner'][i % 2]}\n" for i in range(1, 6))


def _screen(**actions) -> HeaderScreen:
    return HeaderScreen(columns=[ColumnDecision(column=c, action=a, reason="r") for c, a in actions.items()])


class ValidateScreenTests(unittest.TestCase):
    def test_one_decision_per_header_and_missing_ones_dropped(self):
        output = HeaderScreenOutput(columns=[
            ColumnDecision(column="Program", action="keep", reason="ok"),
            ColumnDecision(column="Program", action="drop", reason="second"),   # repeat: ignored
            ColumnDecision(column="Invented", action="keep", reason=""),        # not given: ignored
            ColumnDecision(column="Name", action="drop", reason=" "),
        ])
        decisions, warnings = validate_screen(output, ["Name", "Program", "Skipped"])
        self.assertEqual([(d.column, d.action) for d in decisions],
                         [("Name", "drop"), ("Program", "keep"), ("Skipped", "drop")])
        self.assertEqual(decisions[0].reason, "Sensitive.")
        self.assertIn("Skipped", warnings[0])


class ScreenFileTests(unittest.TestCase):
    def test_only_new_headers_are_sent_and_edits_are_kept(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "header_screen.json"
            fake = screen_reply(drop=("Nombre",))
            with patch.object(screening, "run_header_screen", fake):
                screen, ran, _ = asyncio.run(screen_headers(["UCID", "Nombre", "Skill"], path))
                self.assertTrue(ran)
                # A person overrides one decision by hand.
                saved = json.loads(path.read_text(encoding="utf-8"))
                saved["columns"][1]["action"] = "keep"
                path.write_text(json.dumps(saved), encoding="utf-8")

                screen, ran, _ = asyncio.run(screen_headers(["UCID", "Nombre", "Skill"], path))
                self.assertFalse(ran)
                screen, ran, _ = asyncio.run(screen_headers(["UCID", "Nombre", "Skill", "Year"], path))
            self.assertEqual(fake.calls, [["UCID", "Nombre", "Skill"], ["Year"]])
            self.assertEqual(screen.decisions()["Nombre"].action, "keep")   # hand edit survived
            self.assertEqual([d.column for d in load_screen(path).columns], ["UCID", "Nombre", "Skill", "Year"])

    def test_unsure_drops_are_flagged(self):
        async def unsure(headers):
            return HeaderScreenOutput(columns=[ColumnDecision(column="Background", action="drop",
                                                              reason="Unsure: may be ethnicity")])
        with tempfile.TemporaryDirectory() as d, patch.object(screening, "run_header_screen", unsure):
            _, _, warnings = asyncio.run(screen_headers(["Background"], Path(d) / "s.json"))
        self.assertTrue(any("unsure" in w and "Background" in w for w in warnings))

    def test_invalid_hand_edit_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "header_screen.json"
            path.write_text('{"columns": [{"column": "A", "action": "maybe", "reason": ""}]}')
            with self.assertRaisesRegex(ValueError, "not a valid header screen"):
                load_screen(path)


class ApplyScreenTests(unittest.TestCase):
    def test_drops_and_picks_the_screens_id_column(self):
        rows = [{"Stu #": "7", "Nombre": "A", "Skill": "x", "Login": "a1"}]
        screen = _screen(**{"Stu #": "id", "Nombre": "drop", "Skill": "keep", "Login": "id"})
        out, mapping, dropped, warnings = apply_screen(rows, screen)
        self.assertEqual(out, [{"Stu #": "7", "Skill": "x"}])
        self.assertEqual(mapping, {"Stu #": "candidate_id"})
        self.assertEqual(dropped, ["Nombre", "Login"])   # a second ID column is an identifier
        self.assertEqual(warnings, [])

    def test_recognized_id_column_is_never_dropped(self):
        rows = [{"UCID": "7", "Skill": "x"}]
        out, mapping, dropped, _ = apply_screen(rows, _screen(UCID="drop", Skill="keep"))
        self.assertEqual(out, rows)
        self.assertEqual((mapping, dropped), ({}, []))   # intake maps UCID itself

    def test_unscreened_columns_dropped_and_mapping_conflicts_flagged(self):
        rows = [{"UCID": "7", "New": "x", "Hidden": "y"}]
        out, _, dropped, warnings = apply_screen(rows, _screen(UCID="id", Hidden="drop"),
                                                 column_mapping={"Hidden": "program"})
        self.assertEqual(out, [{"UCID": "7"}])
        self.assertEqual(dropped, ["New", "Hidden"])
        self.assertTrue(any("never screened" in w for w in warnings))
        self.assertTrue(any("column_mapping" in w and "Hidden" in w for w in warnings))


class StoreScreeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "store")
        (self.dir / "apps.csv").write_text(DATA)
        planner = patch.object(agent_trace.Runner, "run", _planner())
        self.planner = planner.start()
        self.addCleanup(planner.stop)

    def test_screened_columns_never_reach_the_store_or_planner(self):
        with patch_screen(drop=("Nombre completo",), ids=("Stu #",)):
            result = asyncio.run(upload_files([self.dir / "apps.csv"], self.store))
        records = load_store(self.store.candidates)
        self.assertEqual(set(records[0]), {"candidate_id", "program", "python_skill"})
        self.assertEqual(records[0]["candidate_id"], "1")
        report = result.sources["apps.csv"]
        self.assertIn("Nombre completo", report.dropped_sensitive)
        self.assertIn("Email Address", report.dropped_sensitive)
        payload = self.planner.await_args.args[1]
        self.assertNotIn("Persona", payload)
        self.assertNotIn("nombre", payload.lower())

    def test_reprocessing_makes_no_screen_call(self):
        with patch_screen(ids=("Stu #",)):
            asyncio.run(upload_files([self.dir / "apps.csv"], self.store, plan_criteria=False))
        with patch_screen() as fake:
            asyncio.run(process_store(self.store, plan_criteria=False))
        self.assertEqual(fake.calls, [])

    def test_failed_screen_leaves_the_store_unchanged(self):
        failing = AsyncMock(side_effect=ScreeningError("api down"))
        with patch.object(screening, "run_header_screen", failing), self.assertRaises(ScreeningError):
            asyncio.run(upload_files([self.dir / "apps.csv"], self.store))
        self.assertFalse(self.store.candidates.exists())
        self.assertFalse((self.store.raw / "apps.csv").exists())

    def test_screen_off_warns(self):
        (self.dir / "ok.csv").write_text("UCID,Program\n1,Software\n")
        result = asyncio.run(upload_files([self.dir / "ok.csv"], self.store, plan_criteria=False, screen=False))
        self.assertTrue(any("Header screen off" in w for w in result.warnings))
        self.assertFalse(self.store.header_screen.exists())


class DirectDataTests(unittest.TestCase):
    def test_data_passed_in_is_screened_before_rankers(self):
        rows = [{"Stu #": str(i), "Nombre completo": f"Persona {i}", "teaching_experience": "Tutor"}
                for i in range(1, 4)]
        seen = []

        async def fake_ranker(criterion, candidates, request):
            seen.extend(c.fields for c in candidates)
            return RankerOutput(criterion_id=criterion.id, excluded=[], scores=[
                CandidateScore(candidate_ref=c.ref, score=5, rationale="r") for c in candidates])

        request = HiringRequest(role="TA", criteria={"teaching_experience": 1}, auto_criteria=False)
        with tempfile.TemporaryDirectory() as d, \
             patch_screen(drop=("Nombre completo",), ids=("Stu #",)), \
             patch.object(orchestrator, "run_ranker", fake_ranker):
            final, ref_map = asyncio.run(orchestrator.rank_candidates(request, rows, store=Store(Path(d)), registry=ta_registry()))
        self.assertEqual(sorted(ref_map.values()), ["1", "2", "3"])
        self.assertTrue(seen and all("Persona" not in json.dumps(f) for f in seen))


class PromptTests(unittest.TestCase):
    def test_every_agent_builds_from_its_prompt_file(self):
        criterion = default_registry().get("teaching_experience")
        for agent in (build_header_screen(), build_criteria_planner(), build_manager(),
                      build_ranker(criterion, "Prefer lab TAs {not a placeholder}")):
            self.assertTrue(agent.instructions.strip())
            self.assertNotRegex(agent.instructions, r"\{(criterion_id|field_lines|exclusion_rule)\}")

    def test_placeholders_must_match(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "t.md").write_text("Hi {name}, JSON stays: {\"a\": 1}\n")
            with patch("hiring_agents.prompts.PROMPTS_DIR", Path(d)):
                self.assertEqual(render("t", name="{x}"), 'Hi {x}, JSON stays: {"a": 1}')
                with self.assertRaisesRegex(KeyError, "no value"):
                    render("t")
                with self.assertRaisesRegex(KeyError, "no placeholder"):
                    render("t", name="a", typo="b")

    def test_prompt_files_exist(self):
        names = {p.relative_to(PROMPTS_DIR).as_posix() for p in PROMPTS_DIR.rglob("*.md")}
        self.assertTrue({"header_screen.md", "criteria_planner.md", "manager.md", "ranker/main.md"} <= names)


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        agent_trace._path = None
        self.addCleanup(setattr, agent_trace, "_path", None)

    def _run(self, runner_mock, trace):
        agent = build_header_screen()
        with patch.dict(os.environ, {"AGENT_TRACE": trace}), \
             patch.object(agent_trace.Runner, "run", runner_mock):
            return asyncio.run(agent_trace.run_agent(agent, '{"headers": ["UCID"]}', HeaderScreenOutput, self.dir))

    def test_calls_are_written_when_on(self):
        reply = HeaderScreenOutput(columns=[ColumnDecision(column="UCID", action="id", reason="the ID")])
        ok = AsyncMock(return_value=SimpleNamespace(final_output_as=lambda *a, **k: reply))
        self.assertEqual(self._run(ok, "ON"), reply)
        with self.assertRaises(RuntimeError):
            self._run(AsyncMock(side_effect=RuntimeError("api down")), "ON")
        text = agent_trace.trace_path().read_text(encoding="utf-8")
        self.assertIn("## 1. Header Screen", text)
        self.assertIn("You screen the column headers", text)   # the instructions
        self.assertIn('"UCID"', text)                            # the input
        self.assertIn('"the ID"', text)                          # the output
        self.assertIn("## 2. Header Screen", text)
        self.assertIn("RuntimeError: api down", text)

    def test_nothing_written_when_off(self):
        reply = HeaderScreenOutput(columns=[])
        self._run(AsyncMock(return_value=SimpleNamespace(final_output_as=lambda *a, **k: reply)), "OFF")
        self.assertIsNone(agent_trace.trace_path())
        self.assertEqual(list(self.dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
