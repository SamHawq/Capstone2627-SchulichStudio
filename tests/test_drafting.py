"""Offline tests for drafting a request from free text (Manager agent stubbed). Run: python -m unittest"""

import asyncio
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import hiring_agents.agent_trace as agent_trace
import hiring_agents.orchestrator as orchestrator
import hiring_agents.planning as planning
from hiring_agents.datastore import Store
from hiring_agents.documents import (
    MAX_DOC_CHARS, DocumentError, add_documents, describe, extract_text, find_documents, load_context,
    remove_document, scrub,
)
from hiring_agents.drafting import validate_choices
from hiring_agents.llm_agents import build_ranker
from hiring_agents.registry import default_registry
from hiring_agents.schemas import (
    CourseSession, CriteriaCatalog, Criterion, CriterionChoice, DraftCriterion, FieldNote, HiringRequest,
    ManagerOutput, RequestBrief,
)
from hiring_agents.service import (
    ServiceError, delete_document, draft_request, list_documents, rank, upload_documents, upload_files,
    validate_request,
)
from tests.stubs import patch_review, patch_screen, runner_by_agent
from tests.test_service import APPS, DRAFT, _fake_ranker, _planner

AUTO = [
    Criterion(id="technical_skills", name="Technical Skills", description="d", scoring_guidance="g",
              relevant_fields=["python_skill"], type="auto"),
    Criterion(id="engg_204_fit", name="ENGG 204 Fit", description="d", scoring_guidance="g",
              relevant_fields=["ta_204"], type="auto", importance=3, courses=["ENGG 204"]),
]


def _criteria():
    registry = default_registry()
    return [registry.get(cid) for cid in registry.ids()] + AUTO


def _choice(cid, weight, instructions="", reason="r"):
    return CriterionChoice(criterion_id=cid, weight=weight, instructions=instructions, reason=reason)


def _output(choices, role="TA for ENGG 233", top_x=0, notes="", not_covered=(), sessions=()):
    return ManagerOutput(role=role, top_x=top_x, notes=notes, criteria=choices, sessions=list(sessions),
                         not_covered=list(not_covered))


class ValidateChoicesTests(unittest.TestCase):
    brief = RequestBrief(instructions="Python matters most")

    def test_weights_instructions_and_defaults(self):
        draft = validate_choices(_output([
            _choice("technical_skills", 5, "Python above all"),
            _choice("teaching_experience", 2, "  "),
            _choice("subject_knowledge", 0, "ignored because weight 0"),
        ], top_x=8, not_covered=["Monday availability: scheduling is a separate step"]), _criteria(), self.brief)
        req = draft.request
        self.assertEqual(req.role, "TA for ENGG 233")
        self.assertEqual(req.top_x, 8)
        self.assertEqual(req.criteria["technical_skills"], 5)
        self.assertEqual(req.instructions, {"technical_skills": "Python above all"})
        # Every criterion is listed; ones the Manager left out get 0, so auto criteria can't sneak in.
        self.assertEqual(set(req.criteria), {c.id for c in _criteria()})
        self.assertEqual(req.criteria["engg_204_fit"], 0)
        self.assertEqual(draft.reasons["engg_204_fit"], "Not weighted by the Manager.")
        self.assertEqual(len(draft.not_covered), 1)
        self.assertEqual(draft.warnings, [])

    def test_bad_output_is_fixed_and_flagged(self):
        draft = validate_choices(_output([
            _choice("made_up", 3),
            _choice("technical_skills", 9),
            _choice("technical_skills", 1),
            _choice("engg_204_fit", 2),           # course-specific, but the role is ENGG 233
            _choice("past_performance", float("nan")),
        ]), _criteria(), self.brief)
        self.assertEqual(draft.request.criteria["technical_skills"], 5)
        self.assertNotIn("made_up", draft.request.criteria)
        self.assertEqual(draft.request.criteria["past_performance"], 0)
        text = " ".join(draft.warnings)
        for part in ("made_up", "twice", "clamped", "ENGG 204"):
            self.assertIn(part, text)

    def test_client_role_and_top_win(self):
        brief = RequestBrief(instructions="x", role="TA for ENGG 204", top_x=3)
        draft = validate_choices(_output([_choice("engg_204_fit", 3)], role="something else", top_x=10),
                                 _criteria(), brief)
        self.assertEqual((draft.request.role, draft.request.top_x), ("TA for ENGG 204", 3))
        self.assertEqual(draft.warnings, [])

    def test_no_weights_is_an_error(self):
        with self.assertRaises(ValueError):
            validate_choices(_output([_choice("technical_skills", 0)]), _criteria(), self.brief)

    def test_sessions_are_normalized_and_checked(self):
        sessions = [CourseSession(label="Lab B01", day="Monday", start="9:00", end="11:50"),
                    CourseSession(label="Lab B01", day="mon", start="09:00", end="11:50"),   # repeat
                    CourseSession(label="Lab B02", day="someday", start="13:00", end="15:00"),
                    CourseSession(label="Tut T01", day="fri", start="15:00", end="14:00"),
                    CourseSession(label="Tut T02", day="thu", start="2pm", end="3pm")]
        draft = validate_choices(_output([_choice("technical_skills", 3)], sessions=sessions),
                                 _criteria(), RequestBrief(instructions="x", documents=["outline.pdf"]))
        self.assertEqual([s.model_dump() for s in draft.request.sessions],
                         [{"label": "Lab B01", "day": "mon", "start": "09:00", "end": "11:50"}])
        self.assertEqual(sum("Dropped session" in w for w in draft.warnings), 3)
        self.assertEqual(draft.request.documents, ["outline.pdf"])

    def test_brief_needs_text_or_documents(self):
        with self.assertRaises(ValueError):
            RequestBrief(instructions="  ")
        self.assertEqual(RequestBrief(documents=["jd.txt"]).instructions, "")


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.folder = self.dir / "documents"

    def tearDown(self):
        self.tmp.cleanup()

    def _docx(self, name, paragraphs):
        w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
        path = self.dir / name
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("word/document.xml", f'<w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>')
        return path

    def _pdf(self, name, text):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        writer = PdfWriter()
        page = writer.add_blank_page(612, 792)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject(
            {NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
        path = self.dir / name
        with open(path, "wb") as f:
            writer.write(f)
        return path

    def test_formats(self):
        txt = self.dir / "jd.txt"
        txt.write_text("Job description\n\n\n\nPython required")
        self.assertEqual(extract_text(txt), "Job description\n\nPython required")
        self.assertEqual(extract_text(self._docx("outline.docx", ["ENGG 233", "Labs use Arduino"])),
                         "ENGG 233\nLabs use Arduino")
        self.assertIn("Weekly labs in Python", extract_text(self._pdf("outline.pdf", "Weekly labs in Python")))
        bad = self.dir / "x.pdf"
        bad.write_bytes(b"not a pdf")
        for path in (bad, self.dir / "x.rtf"):
            path.touch()
            with self.assertRaises(DocumentError):
                extract_text(path)
        (self.dir / "empty.md").write_text("  \n")
        with self.assertRaises(DocumentError):
            extract_text(self.dir / "empty.md")

    def test_scrub_removes_contact_details_and_ids(self):
        text = scrub("Email jane.doe@ucalgary.ca or call (403) 220-1234; UCID 30012345. ENGG 233, Lab 14:00, 2026.")
        self.assertNotIn("jane.doe", text)
        self.assertNotIn("220-1234", text)
        self.assertNotIn("30012345", text)
        for kept in ("ENGG 233", "14:00", "2026"):
            self.assertIn(kept, text)

    def test_add_list_load_and_remove(self):
        txt = self.dir / "outline.txt"
        txt.write_text("Contact prof@example.com. " + "x" * (MAX_DOC_CHARS + 100))
        added, replaced = add_documents([txt], self.folder)
        self.assertEqual((added, replaced), (["outline.txt"], []))
        with self.assertRaises(DocumentError):
            add_documents([txt], self.folder)                  # name clash without replace
        self.assertEqual(add_documents([txt], self.folder, replace=True), ([], ["outline.txt"]))
        info = describe(self.folder / "outline.txt")
        self.assertTrue(info.truncated)
        docs, warnings = load_context(["outline.txt"], self.folder)
        self.assertEqual(len(docs[0]["text"]), MAX_DOC_CHARS)
        self.assertIn("[email]", docs[0]["text"])
        self.assertEqual(len(warnings), 1)
        with self.assertRaises(DocumentError):
            load_context(["missing.pdf"], self.folder)
        remove_document("outline.txt", self.folder)
        self.assertEqual(find_documents(self.folder), [])
        with self.assertRaises(DocumentError):
            remove_document("../candidates.json", self.folder)


class RequestUseTests(unittest.TestCase):
    def test_ranker_gets_its_instructions(self):
        criterion = default_registry().get("teaching_experience")
        self.assertIn("Prefer lab TAs", build_ranker(criterion, "Prefer lab TAs").instructions)
        self.assertNotIn("client's instructions", build_ranker(criterion).instructions)

    def test_zero_weight_keeps_auto_criterion_out(self):
        catalog = CriteriaCatalog(purpose="p", fields=[], extensions=[], criteria=AUTO[:1], skipped=[])
        active = [default_registry().get("teaching_experience")]
        joined, _, _ = planning.add_auto_criteria(
            catalog, active, HiringRequest(role="TA", criteria={"teaching_experience": 1}))
        self.assertIn("technical_skills", [c.id for c in joined])
        kept_out, _, _ = planning.add_auto_criteria(
            catalog, active, HiringRequest(role="TA", criteria={"teaching_experience": 1, "technical_skills": 0}))
        self.assertNotIn("technical_skills", [c.id for c in kept_out])


class DraftServiceTests(unittest.TestCase):
    def setUp(self):
        screen = patch_screen()   # keep-all Header Screen, no LLM call
        screen.start()
        self.addCleanup(screen.stop)
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.store = Store(d / "store")
        (d / "apps.csv").write_text(APPS)
        with patch.object(agent_trace.Runner, "run", _planner()):
            asyncio.run(upload_files([d / "apps.csv"], store=self.store))

    def tearDown(self):
        self.tmp.cleanup()

    def manager(self, output):
        # Drafting plans criteria for the instructions first (planner), then the Manager weights them.
        return patch.object(agent_trace.Runner, "run", runner_by_agent({"Criteria Planner": DRAFT, "Manager": output}))

    def test_draft_then_rank(self):
        output = _output([_choice("technical_skills", 4, "Python matters most"), _choice("subject_knowledge", 1)])
        with self.manager(output) as run:
            draft = asyncio.run(draft_request(RequestBrief(instructions="Python TA"), self.store))
        payload = json.loads(run.await_args.args[1])
        self.assertIn("technical_skills", [c["id"] for c in payload["criteria"]])   # auto criteria offered
        self.assertNotIn("candidates", payload)                                      # no candidate data
        tech = next(c for c in payload["criteria"] if c["id"] == "technical_skills")
        self.assertEqual(tech["fields"], [{"field": "python_skill", "holds": "self-rated level", "kind": "category"}])  # what it holds
        self.assertEqual(draft.request.instructions, {"technical_skills": "Python matters most"})
        self.assertEqual(draft.request.client_instructions, "Python TA")   # kept for the final review

        seen = {}

        async def ranker(criterion, candidates, request):
            seen[criterion.id] = request.instructions.get(criterion.id)
            return await _fake_ranker(criterion, candidates, request)

        with patch.object(orchestrator, "run_ranker", ranker), patch_review() as review:
            result = asyncio.run(rank(draft.request, self.store))
        self.assertEqual(seen, {"technical_skills": "Python matters most", "subject_knowledge": None})
        self.assertEqual({c.id for c in result.ranking.criteria}, {"technical_skills", "subject_knowledge"})
        self.assertTrue(result.ranking.reviewed)
        self.assertEqual(review.payloads[0]["client_instructions"], "Python TA")

    def test_criteria_are_planned_from_the_instructions(self):
        # The request's own plan: Python split out, as "Python matters most" asks.
        plan = DRAFT.model_copy(update={"criteria": [DraftCriterion(
            id="python_proficiency", name="Python", description="d", scoring_guidance="g",
            fields=[FieldNote(field="python_skill", meaning="self-rated level")], importance=3, courses=[])]})
        output = _output([_choice("python_proficiency", 5, "Python matters most")])
        planner_and_manager = runner_by_agent({"Criteria Planner": plan, "Manager": output})
        with patch.object(agent_trace.Runner, "run", planner_and_manager):
            draft = asyncio.run(draft_request(RequestBrief(instructions="Python matters most for ENGG 233"), self.store))
        calls = {c.args[0].name: json.loads(c.args[1]) for c in planner_and_manager.await_args_list}
        self.assertIn("Python matters most for ENGG 233", calls["Criteria Planner"]["purpose"])
        offered = [c["id"] for c in calls["Manager"]["criteria"]]
        self.assertIn("python_proficiency", offered)                  # the request's own criterion
        self.assertNotIn("technical_skills", offered)                 # not the store's general one
        self.assertNotIn("teaching_experience", offered)              # no fields in this data: not offered
        self.assertEqual({c.id for c in draft.criteria}, set(offered))
        self.assertFalse(draft.request.auto_criteria)
        self.assertEqual([c.id for c in draft.request.catalog.criteria], ["python_proficiency"])

        # The saved request ranks with its own criteria, even if the store's catalog changes.
        saved = HiringRequest.model_validate_json(draft.request.model_dump_json())
        self.store.criteria.unlink()
        seen = {}

        async def ranker(criterion, candidates, request):
            seen[criterion.id] = criterion.relevant_fields
            return await _fake_ranker(criterion, candidates, request)

        with patch.object(orchestrator, "run_ranker", ranker), patch_review():
            asyncio.run(rank(saved, self.store))
        self.assertEqual(seen, {"python_proficiency": ["python_skill"]})

        # Redrafting the same text reuses the plan: no new planner call.
        with patch.object(agent_trace.Runner, "run", runner_by_agent({"Manager": output})):
            asyncio.run(draft_request(RequestBrief(instructions="Python matters most for ENGG 233"), self.store))

    def test_documents_reach_the_manager_scrubbed(self):
        outline = Path(self.tmp.name) / "ENGG233 outline.txt"
        outline.write_text("ENGG 233. Weekly Python labs, Mon 14:00-16:50. Instructor: dr.x@ucalgary.ca")
        result = upload_documents([outline], self.store)
        self.assertEqual(result.added, ["ENGG233 outline.txt"])
        self.assertEqual([d.name for d in list_documents(self.store)], ["ENGG233 outline.txt"])

        output = _output([_choice("technical_skills", 5, "The labs use Python")],
                         sessions=[CourseSession(label="Lab", day="mon", start="14:00", end="16:50")])
        with self.manager(output) as run:
            draft = asyncio.run(draft_request(RequestBrief(documents=["ENGG233 outline.txt"]), self.store))
        sent = json.loads(run.await_args.args[1])["documents"]
        self.assertEqual(sent[0]["name"], "ENGG233 outline.txt")
        self.assertIn("Weekly Python labs", sent[0]["text"])
        self.assertNotIn("dr.x@", sent[0]["text"])
        self.assertEqual(len(draft.request.sessions), 1)
        self.assertEqual(draft.request.documents, ["ENGG233 outline.txt"])

        with self.assertRaises(ServiceError):
            asyncio.run(draft_request(RequestBrief(documents=["nope.pdf"]), self.store))
        self.assertEqual(delete_document("ENGG233 outline.txt", self.store), [])
        with self.assertRaises(ServiceError):
            upload_documents([Path(self.tmp.name) / "apps.csv"], self.store)   # not a document type

    def test_draft_errors(self):
        with self.manager(_output([_choice("technical_skills", 0)])):
            with self.assertRaises(ServiceError):
                asyncio.run(draft_request(RequestBrief(instructions="nothing useful"), self.store))
        with self.assertRaises(ServiceError):
            asyncio.run(draft_request(RequestBrief(instructions="x"), Store(Path(self.tmp.name) / "empty")))

    def test_instructions_for_unknown_criteria_rejected(self):
        request = HiringRequest(role="TA", criteria={"technical_skills": 1}, instructions={"nope": "x"})
        with self.assertRaises(ServiceError):
            validate_request(request, self.store)


if __name__ == "__main__":
    unittest.main()
