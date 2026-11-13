"""Keyword extraction contracts, with simulated model results and no API calls."""

import asyncio
import json
import csv
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pandas as pd

from hiring_agents.textagent import extract_csv, extract_keywords
from hiring_agents.textagent_benchmark import benchmark_models, score_keywords


def _response(output):
    return SimpleNamespace(final_output_as=lambda schema, **kwargs: schema.model_validate(output))


HEADERS = [
    "skills, and technical knowledge check.",
    "verbal and written skills to communicate",
    "engaged learning, helped improved understanding",
    "Describe where you have successfully promoted an active learning learning style to engage students.",
]


class TextAgentTests(unittest.TestCase):
    def test_job_csv_preserves_refs_headers_and_literal_na_answers(self):
        async def fake_run(agent, payload, **kwargs):
            answers = json.loads(payload)
            self.assertEqual(answers, {"Technical knowledge?": "NA", "Communication": "Explains circuits"})
            self.assertNotIn("C-001", payload)
            self.assertEqual(agent.model, "test-model")
            return _response({"Technical knowledge?": [], "Communication": ["Explains circuits"]})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "free_text.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as dest:
                writer = csv.writer(dest)
                writer.writerow(["Candidate", "Technical knowledge?", "Communication"])
                writer.writerow(["C-001", "NA", "Explains circuits"])
            with patch("hiring_agents.textagent.Runner.run", side_effect=fake_run):
                result = asyncio.run(extract_csv(path, model="test-model", reasoning_effort="none", batch_size=15))
        self.assertEqual(result, {"C-001": {"Technical knowledge?": [], "Communication": ["Explains circuits"]}})

    def test_job_csv_rejects_duplicate_headers_and_missing_candidate_column(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "free_text.csv"
            with patch("hiring_agents.textagent.Runner.run", new_callable=AsyncMock) as runner:
                for content in ["Candidate,Answer,Answer\nC-1,a,b\n", "UCID,Answer\n1,a\n"]:
                    path.write_text(content)
                    with self.subTest(content=content), self.assertRaises(ValueError):
                        asyncio.run(extract_csv(path))
            runner.assert_not_awaited()

    def test_groups_answers_under_exact_headers_and_ids(self):
        df = pd.DataFrame([
            ["00123", "I use Python.", "I give presentations.", None, "Group work."],
            ["00456", "No MATLAB experience.", "", pd.NA, ""],
        ], columns=["UCID", *HEADERS])

        async def fake_run(agent, payload, **kwargs):
            answers = json.loads(payload)
            self.assertEqual(list(answers), HEADERS)
            self.assertNotIn("UCID", answers)
            self.assertTrue(kwargs["run_config"].tracing_disabled)
            values = ([" Python ", "python", ""] if "Python" in answers[HEADERS[0]]
                      else ["no MATLAB experience"])
            output = dict(zip(HEADERS, [values, ["presentations"], ["invented"], ["group work"]]))
            # Exercise the actual structured output model with punctuation in aliases.
            return _response(agent.output_type.model_validate(output))

        with patch("hiring_agents.textagent.Runner.run", side_effect=fake_run) as runner:
            result = asyncio.run(extract_keywords(df))
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(result, {
            "00123": dict(zip(HEADERS, [["Python"], ["presentations"], [], ["group work"]])),
            "00456": dict(zip(HEADERS, [["no MATLAB experience"], [], [], []])),
        })
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_blank_rows_skip_model_calls(self):
        df = pd.DataFrame([["00123", None, float("nan"), pd.NA, "  "]],
                          columns=["UCID", *HEADERS])
        with patch("hiring_agents.textagent.Runner.run", new_callable=AsyncMock) as runner:
            result = asyncio.run(extract_keywords(df))
        runner.assert_not_awaited()
        self.assertEqual(result, {"00123": {header: [] for header in HEADERS}})

    def test_invalid_inputs_fail_before_model_calls(self):
        invalid = [
            pd.DataFrame({"answer": ["Python"]}),
            pd.DataFrame({"UCID": ["123"]}),
            pd.DataFrame([["123", "x", "y"]], columns=["UCID", "answer", "answer"]),
            pd.DataFrame({"UCID": ["123", "123"], "answer": ["Python", "MATLAB"]}),
            pd.DataFrame({"UCID": ["123", None], "answer": ["Python", "MATLAB"]}),
            pd.DataFrame({"UCID": ["123", " "], "answer": ["Python", "MATLAB"]}),
            pd.DataFrame({"UCID": ["123"], "answer": [42]}),
        ]
        with patch("hiring_agents.textagent.Runner.run", new_callable=AsyncMock) as runner:
            for df in invalid:
                with self.subTest(columns=list(df.columns)), self.assertRaises(ValueError):
                    asyncio.run(extract_keywords(df))
        runner.assert_not_awaited()

    def test_empty_dataframe_returns_empty_dictionary(self):
        self.assertEqual(asyncio.run(extract_keywords(pd.DataFrame(columns=["UCID", *HEADERS]))), {})

    def test_custom_id_header(self):
        df = pd.DataFrame({"student_id": ["001"], "Question?": [None]})
        result = asyncio.run(extract_keywords(df, id_column="student_id"))
        self.assertEqual(result, {"001": {"Question?": []}})

    def test_model_errors_propagate(self):
        df = pd.DataFrame({"UCID": ["123"], "Question?": ["Python"]})
        with patch("hiring_agents.textagent.Runner.run", new_callable=AsyncMock,
                   side_effect=RuntimeError("model unavailable")):
            with self.assertRaisesRegex(RuntimeError, "model unavailable"):
                asyncio.run(extract_keywords(df))

    def test_request_latency_is_collected(self):
        df = pd.DataFrame({"UCID": ["123", "456"], "Question?": ["Python", ""]})
        timings = []
        response = _response({"Question?": ["Python"]})
        with patch("hiring_agents.textagent.Runner.run", new_callable=AsyncMock,
                   return_value=response), patch("hiring_agents.textagent.perf_counter",
                                                 side_effect=[10.0, 12.0]):
            asyncio.run(extract_keywords(df, request_seconds=timings))
        self.assertEqual(timings, [2.0])

    def test_explicit_reasoning_effort_reaches_agent(self):
        df = pd.DataFrame({"UCID": ["123"], "Technical": ["Python"]})

        async def fake_run(agent, payload, **kwargs):
            self.assertEqual(agent.model, "gpt-6-luna")
            self.assertEqual(agent.model_settings.reasoning.effort, "none")
            return _response({"Technical": ["Python"]})

        with patch("hiring_agents.textagent.Runner.run", side_effect=fake_run):
            asyncio.run(extract_keywords(df, model="gpt-6-luna", reasoning_effort="none"))

    def test_batches_limit_concurrency_and_preserve_candidate_mapping(self):
        df = pd.DataFrame({"UCID": [f"{i:05}" for i in range(32)],
                           "Technical": [f"answer {i}" for i in range(32)]})

        async def scenario():
            active = peak = 0
            completed = set()
            started = [0, 0, 0]
            gates = [asyncio.Event() for _ in range(3)]

            async def fake_run(agent, payload, **kwargs):
                nonlocal active, peak
                answer = json.loads(payload)["Technical"]
                index = int(answer.split()[1])
                group = index // 15
                self.assertTrue(set(range(group * 15)).issubset(completed))
                active += 1
                peak = max(peak, active)
                started[group] += 1
                if started[group] == min(15, 32 - group * 15):
                    gates[group].set()
                await gates[group].wait()
                if index % 2 == 0:
                    await asyncio.sleep(0)  # Deliberately complete out of input order.
                active -= 1
                completed.add(index)
                return _response({"Technical": [answer]})

            with patch("hiring_agents.textagent.Runner.run", side_effect=fake_run):
                result = await asyncio.wait_for(extract_keywords(df, batch_size=15), timeout=2)
            self.assertEqual(peak, 15)
            self.assertEqual(list(result), df["UCID"].tolist())
            self.assertEqual(result, {f"{i:05}": {"Technical": [f"answer {i}"]} for i in range(32)})

        asyncio.run(scenario())

    def test_failed_batch_drains_inflight_requests_and_stops_next_batch(self):
        df = pd.DataFrame({"UCID": ["1", "2", "3"], "Technical": ["fail", "peer", "next"]})

        async def scenario():
            completed = []

            async def fake_run(agent, payload, **kwargs):
                answer = json.loads(payload)["Technical"]
                if answer == "fail":
                    raise RuntimeError("model failed")
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                completed.append(answer)
                return _response({"Technical": [answer]})

            with patch("hiring_agents.textagent.Runner.run", side_effect=fake_run) as runner:
                with self.assertRaisesRegex(RuntimeError, "model failed"):
                    await extract_keywords(df, batch_size=2)
                self.assertEqual(completed, ["peer"])
                self.assertEqual(runner.call_count, 2)

        asyncio.run(scenario())

    def test_invalid_batch_size_fails_before_model_calls(self):
        df = pd.DataFrame({"UCID": ["123"], "Technical": ["Python"]})
        with patch("hiring_agents.textagent.Runner.run", new_callable=AsyncMock) as runner:
            for size in [0, -1]:
                with self.subTest(size=size), self.assertRaises(ValueError):
                    asyncio.run(extract_keywords(df, batch_size=size))
        runner.assert_not_awaited()


class BenchmarkTests(unittest.TestCase):
    def test_phrase_scoring_respects_ids_headers_and_negation(self):
        expected = {"123": {"Technical": ["Python", "no MATLAB experience"], "Teaching": []}}
        actual = {"123": {"Technical": [" python ", "MATLAB", "PYTHON"], "Teaching": []}}
        scores = score_keywords(actual, expected)
        self.assertEqual(scores["precision"], 0.5)
        self.assertEqual(scores["recall"], 0.5)
        self.assertEqual(scores["f1"], 0.5)
        self.assertEqual(scores["missed"], 1)
        self.assertEqual(scores["extra"], 1)
        self.assertEqual(score_keywords({}, {})["f1"], 1.0)

    def test_rotates_models_preserves_outputs_and_reports_failures(self):
        df = pd.DataFrame({"UCID": ["00123"], "Technical": ["Python"]})
        expected = {"00123": {"Technical": ["Python"]}}
        order = []

        async def fake_extract(df, *, model, request_seconds, batch_size, **kwargs):
            self.assertEqual(batch_size, 15)
            order.append(model)
            if model == "unavailable":
                raise RuntimeError("model unavailable")
            request_seconds.append(2.0)
            return expected

        with patch("hiring_agents.textagent_benchmark.extract_keywords", side_effect=fake_extract):
            report = asyncio.run(benchmark_models(
                df, ["working", "unavailable"], repeats=2, expected=expected, batch_size=15,
            ))
        self.assertEqual(order, ["working", "unavailable", "unavailable", "working"])
        self.assertEqual(report["batch_size"], 15)
        good = report["models"]["working"]
        self.assertEqual(good["summary"]["median_request_seconds"], 2.0)
        self.assertEqual(good["summary"]["p95_request_seconds"], 2.0)
        self.assertEqual(good["summary"]["mean_f1"], 1.0)
        self.assertEqual(good["runs"][0]["keywords"], expected)
        bad = report["models"]["unavailable"]
        self.assertEqual(bad["summary"]["failed_runs"], 2)
        self.assertIsNone(bad["summary"]["median_dataset_seconds"])
        self.assertIsNone(bad["summary"]["mean_f1"])
        self.assertIn("model unavailable", bad["runs"][0]["error"])

    def test_no_reference_means_no_quality_score(self):
        df = pd.DataFrame({"UCID": ["123"], "Technical": [""]})
        report = asyncio.run(benchmark_models(df, ["unused-model"], repeats=1))
        self.assertIsNone(report["quality_metric"])
        self.assertIsNone(report["models"]["unused-model"]["summary"]["mean_f1"])
        self.assertIsNone(report["models"]["unused-model"]["summary"]["median_request_seconds"])

    def test_invalid_reference_and_parameters_fail_before_extraction(self):
        df = pd.DataFrame({"UCID": ["123"], "Technical": ["Python"]})
        options = [
            {"models": []}, {"models": ["a", "a"]}, {"models": ["a"], "repeats": 0},
            {"models": ["a"], "batch_size": 0},
            {"models": ["a"], "expected": {}},
            {"models": ["a"], "expected": {"123": {"Wrong header": []}}},
            {"models": ["a"], "expected": {"123": {"Technical": "Python"}}},
        ]
        with patch("hiring_agents.textagent_benchmark.extract_keywords", new_callable=AsyncMock) as extract:
            for option in options:
                with self.subTest(option=option), self.assertRaises(ValueError):
                    asyncio.run(benchmark_models(df, **option))
        extract.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
