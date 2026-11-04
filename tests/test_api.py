"""Offline tests for the REST API (planner and rankers stubbed). Run: python -m unittest

Endpoints are called as functions (FastAPI's TestClient needs httpx, which isn't a
dependency); the error handler is tested on its own.
"""

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.datastructures import UploadFile

import hiring_agents.drafting as drafting
import hiring_agents.orchestrator as orchestrator
import hiring_agents.planning as planning
from hiring_agents.api import ProcessRequest, create_app
from hiring_agents.datastore import Store
from hiring_agents.schemas import HiringRequest, RequestBrief
from hiring_agents.service import ServiceError
from tests.test_drafting import _choice, _output
from tests.test_service import APPS, _fake_ranker, _planner


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.results = d / "results"
        self.app = create_app(Store(d / "store"), self.results)
        self.planner = patch.object(planning.Runner, "run", _planner())
        self.planner.start()

    def tearDown(self):
        self.planner.stop()
        self.tmp.cleanup()

    def call(self, path, method, *args, **kwargs):
        route = next(r for r in self.app.routes if getattr(r, "path", None) == path and method in r.methods)
        return asyncio.run(route.endpoint(*args, **kwargs))

    def upload(self, name="apps.csv", text=APPS, **kw):
        files = [UploadFile(io.BytesIO(text.encode()), filename=name)]
        return self.call("/api/data/files", "POST", files=files, replace=kw.get("replace", False),
                         plan_criteria=True, purpose="TA hiring")

    def test_upload_status_criteria_rank(self):
        self.assertEqual(self.call("/api/data", "GET").candidates, 0)
        result = self.upload()
        self.assertEqual(result.added, ["apps.csv"])
        self.assertEqual(self.call("/api/data", "GET").candidates, 7)
        self.assertIn("technical_skills", [c.id for c in self.call("/api/criteria", "GET")])

        request = HiringRequest(role="TA", criteria={"subject_knowledge": 1}, top_x=2)
        with patch.object(orchestrator, "run_ranker", _fake_ranker):
            response = self.call("/api/rankings", "POST", request)
        self.assertEqual(len(response.result.ranking.ranking), 2)
        name = response.report_url.rsplit("/", 1)[1]
        self.assertTrue((self.results / name).exists())
        self.assertEqual(Path(self.call("/results/{name}", "GET", name).path).name, name)

    def test_upload_path_parts_are_stripped(self):
        result = self.upload(name="../../evil.csv")
        self.assertEqual(result.added, ["evil.csv"])

    def test_process_and_bad_input(self):
        with self.assertRaises(ServiceError):
            self.call("/api/data/process", "POST", ProcessRequest())
        self.upload()
        out = self.call("/api/data/process", "POST", ProcessRequest(plan_criteria=False))
        self.assertEqual(out.status.candidates, 7)
        with self.assertRaises(ServiceError):
            self.upload()                                   # name clash without replace
        with self.assertRaises(ServiceError):
            self.call("/api/rankings", "POST", HiringRequest(role="TA", criteria={"nope": 1}))

    def test_draft_endpoint(self):
        self.upload()
        output = _output([_choice("technical_skills", 3, "Python first")])
        reply = SimpleNamespace(final_output_as=lambda *a, **k: output)
        with patch.object(drafting.Runner, "run", AsyncMock(return_value=reply)):
            draft = self.call("/api/requests/draft", "POST", RequestBrief(instructions="Python TA"))
        self.assertEqual(draft.request.criteria["technical_skills"], 3)

    def test_service_errors_become_400(self):
        handler = self.app.exception_handlers[ServiceError]
        response = asyncio.run(handler(None, ServiceError("Unknown criteria ['nope']")))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body), {"detail": "Unknown criteria ['nope']"})

    def test_results_route_only_serves_saved_pages(self):
        self.results.mkdir()
        for name in ("missing.html", "../store/candidates.json"):
            with self.assertRaises(HTTPException):
                self.call("/results/{name}", "GET", name)

    def test_index_page(self):
        html = self.call("/", "GET")
        self.assertIn("<title>Hiring Agents</title>", html)
        self.assertNotIn("innerHTML", html)                 # values are inserted as text only


if __name__ == "__main__":
    unittest.main()
