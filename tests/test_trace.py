"""Offline tests for run traces (.jsonl events) and the /runs viewer endpoints. Run: python -m unittest"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import hiring_agents.agent_trace as agent_trace
import hiring_agents.jobs as jobs
import hiring_agents.orchestrator as orchestrator
from hiring_agents.agent_trace import trace_operation, trace_step, traced_operation
from hiring_agents.api import create_app
from hiring_agents.datastore import Store, write_store
from hiring_agents.llm_agents import build_header_screen
from hiring_agents.schemas import (
    CandidateScore, ColumnDecision, HeaderScreenOutput, HiringRequest, RankerOutput, RequestBrief,
)
from hiring_agents.service import ServiceError, create_job
from tests.stubs import patch_screen, ta_registry
from tests.test_jobs import LABELS, MAPPING, RECORDS, SCHEMA


class TraceCase(unittest.TestCase):
    """Tracing on, into a temp logs/ folder, with fresh trace files per test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.logs = self.dir / "logs"
        for p in (patch.dict(os.environ, {"AGENT_TRACE": "ON"}), patch.object(agent_trace, "LOG_DIR", self.logs),
                  patch.object(agent_trace, "_path", None)):
            p.start()
            self.addCleanup(p.stop)

    def events(self):
        [path] = self.logs.glob("trace-*.jsonl")
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class EventTests(TraceCase):
    def test_operation_steps_and_agent_calls(self):
        reply = HeaderScreenOutput(columns=[ColumnDecision(column="UCID", action="id", reason="the ID")])
        ok = AsyncMock(return_value=SimpleNamespace(final_output_as=lambda *a, **k: reply))

        @traced_operation("demo", lambda x: f"detail {x}")
        async def action(x):
            trace_step("screen", "First step", count=3)
            with patch.object(agent_trace.Runner, "run", ok):
                await agent_trace.run_agent(build_header_screen(), '{"headers": ["UCID"]}', HeaderScreenOutput)
            with trace_operation("nested"):     # inside another operation: no new one
                trace_step("store", "Inner step")

        asyncio.run(action(1))
        events = self.events()
        self.assertEqual([e["type"] for e in events],
                         ["operation_start", "step", "agent", "step", "operation_end"])
        op = events[0]["op"]
        self.assertTrue(all(e["op"] == op for e in events))
        self.assertEqual((events[0]["name"], events[0]["detail"]), ("demo", "detail 1"))
        agent = events[2]
        self.assertEqual(agent["agent"], "Header Screen")
        self.assertEqual(agent["input"], {"headers": ["UCID"]})              # stored as data, not text
        self.assertEqual(agent["output"]["columns"][0]["reason"], "the ID")
        self.assertIn("You screen the column headers", agent["instructions"])
        self.assertTrue(events[-1]["ok"])
        self.assertTrue(agent_trace.trace_path().exists())                  # the readable .md too

    def test_failed_operation_is_recorded(self):
        @traced_operation("broken")
        async def action():
            raise ValueError("bad input")

        with self.assertRaises(ValueError):
            asyncio.run(action())
        end = self.events()[-1]
        self.assertEqual((end["type"], end["ok"]), ("operation_end", False))
        self.assertIn("bad input", end["error"])

    def test_off_writes_nothing(self):
        with patch.dict(os.environ, {"AGENT_TRACE": "OFF"}):
            with trace_operation("x"):
                trace_step("store", "y")
        self.assertFalse(self.logs.exists())


class RunTraceTests(TraceCase):
    def test_ranking_run_is_traced_without_real_ids(self):
        rows = [{"UCID": f"3001000{i}", "teaching_experience": f"Tutor {i}"} for i in range(1, 4)]

        async def fake_ranker(criterion, candidates, request):
            return RankerOutput(criterion_id=criterion.id, excluded=[], scores=[
                CandidateScore(candidate_ref=c.ref, score=5, rationale="r") for c in candidates])

        request = HiringRequest(role="TA for ENGG 233", criteria={"teaching_experience": 1}, auto_criteria=False)
        with patch_screen(), patch.object(orchestrator, "run_ranker", fake_ranker):
            asyncio.run(orchestrator.rank_candidates(request, rows, store=Store(self.dir / "store"),
                                                     registry=ta_registry()))
        events = self.events()
        self.assertEqual(events[0]["name"], "rank")
        self.assertEqual(events[0]["detail"], "TA for ENGG 233")
        steps = [e["name"] for e in events if e["type"] == "step"]
        self.assertEqual(steps, ["Header screen applied", "Criteria selected", "Intake + pseudonymize",
                                 "Data check", "Ranker outputs validated", "Weighted aggregation"])
        text = "\n".join(json.dumps(e) for e in events)
        for real_id in ("30010001", "30010002", "30010003"):
            self.assertNotIn(real_id, text)
        ranking = next(e for e in events if e.get("name") == "Weighted aggregation")["details"]["ranking"]
        self.assertTrue(all(r["ref"].startswith("C-") for r in ranking))

    def test_job_run_is_traced_without_real_ids(self):
        store = Store(self.dir / "store")
        write_store(RECORDS, store.candidates)
        store.field_labels.write_text(json.dumps(LABELS), encoding="utf-8")
        replies = {"job schema": [SCHEMA], "data agent": [MAPPING]}

        async def fake(agent, payload, output_type, tag, attempts=2):
            return replies[tag].pop(0)

        with patch.object(jobs, "run_with_retries", fake):
            asyncio.run(create_job(RequestBrief(instructions="Python TA"), store, revise=False))
        events = self.events()
        self.assertEqual(events[0]["name"], "job create")
        self.assertIn("Mapping validated", [e.get("name") for e in events])
        self.assertIn("Text extraction", [e.get("name") for e in events])
        self.assertNotIn("30010001", "\n".join(json.dumps(e) for e in events))


class ViewerApiTests(TraceCase):
    def call(self, app, path, method="GET", *args):
        route = next(r for r in app.routes if getattr(r, "path", None) == path and method in r.methods)
        return asyncio.run(route.endpoint(*args))

    def test_list_read_and_page(self):
        with trace_operation("process"):
            trace_step("store", "Store compiled", candidates=3)
        trace_step("store", "Outside any operation")
        app = create_app(Store(self.dir / "store"), self.dir / "results", trace_dir=self.logs)

        [trace] = self.call(app, "/api/traces")
        self.assertEqual([(o["name"], o["steps"], o["ok"]) for o in trace["operations"]],
                         [("process", 1, True), ("(no operation)", 1, None)])
        events = self.call(app, "/api/traces/{name}", "GET", trace["name"])
        self.assertEqual(len(events), 4)
        for bad in ("../secrets.jsonl", "trace-1.jsonl", "nope"):
            with self.assertRaises(ServiceError):
                self.call(app, "/api/traces/{name}", "GET", bad)

        html = self.call(app, "/runs")
        self.assertIn("Run viewer", html)
        for unsafe in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write"):
            self.assertNotIn(unsafe, html)                 # values are inserted as text only

    def test_no_traces_yet(self):
        app = create_app(Store(self.dir / "store"), self.dir / "results", trace_dir=self.dir / "none")
        self.assertEqual(self.call(app, "/api/traces"), [])


if __name__ == "__main__":
    unittest.main()
