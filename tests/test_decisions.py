"""Jev decisions (/decide) and Jev task routing (/route), with no paid calls."""

import importlib.util
from pathlib import Path
import unittest
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "agent" / "server.py"
SPEC = importlib.util.spec_from_file_location("helios_decision_server", MODULE_PATH)
server = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(server)

CONFIG = {
    "categories": {
        "coding": {"description": "Writing code"},
        "frontend": {"description": "Building interfaces"},
        "vision": {"query": "image benchmark", "enabled": False},
    }
}


def jev_reply(answers):
    return {
        "id": "gen-dec-1",
        "model": "typesafe/jev-1.13-20260917",
        "provider": "TypeSafe",
        "answers": answers,
        "usage": {"input_tokens": 275, "output_tokens": 20, "cost": 0.00003},
    }


class DecisionValidationTests(unittest.TestCase):
    def assert_rejected(self, data):
        with mock.patch.object(server, "openrouter_request") as request:
            with self.assertRaises(server.GatewayError) as error:
                server.run_decision(data)
        request.assert_not_called()
        return error.exception

    def test_invalid_questions_are_rejected_before_any_paid_call(self):
        noul = {"type": "noul", "instructions": "Does it hold?"}
        for data in (
            {"questions": {"q": noul}},
            {"state": " ", "questions": {"q": noul}},
            {"state": "x", "questions": {}},
            {"state": "x", "questions": [noul]},
            {"state": "x", "questions": {f"q{n}": noul for n in range(17)}},
            {"state": "x", "questions": {"bad name": noul}},
            {"state": "x", "questions": {"q": {"type": "essay", "instructions": "?"}}},
            {"state": "x", "questions": {"q": {"type": "noul", "instructions": ""}}},
            {"state": "x", "questions": {"q": {**noul, "criteria": {"a": "A", "b": "B"}}}},
            {"state": "x", "questions": {"q": {"type": "choice", "instructions": "?", "criteria": {"a": "A"}}}},
            {"state": "x", "questions": {"q": {"type": "choice", "instructions": "?", "criteria": ["a", "b"]}}},
            {"state": "x", "questions": {"q": {"type": "score", "instructions": "?", "criteria": {"a": "A", "b": "B"}}}},
            {"state": "x", "questions": {"q": {"type": "score", "instructions": "?", "criteria": [str(n) for n in range(11)]}}},
        ):
            with self.subTest(data=data):
                self.assertEqual(self.assert_rejected(data).status, 400)

    def test_oversized_input_is_413(self):
        with mock.patch.object(server, "MAX_DECISION_CHARS", 1000):
            error = self.assert_rejected(
                {"state": "x" * 1001, "questions": {"q": {"type": "noul", "instructions": "?"}}}
            )
        self.assertEqual(error.status, 413)


class RunDecisionTests(unittest.TestCase):
    def test_questions_go_to_the_jev_endpoint_and_answers_come_back(self):
        questions = {
            "refund": {"type": "noul", "instructions": "Is money asked back?", "extra": "dropped"},
            "team": {
                "type": "choice",
                "instructions": "Which team?",
                "criteria": {"billing": "Charges", "technical": "Bugs"},
            },
        }
        reply = jev_reply({"refund": {"type": "noul", "noul": 0.98}})
        with mock.patch.object(server, "openrouter_request", return_value=reply) as request:
            result = server.run_decision({"state": "Charged twice.", "questions": questions})

        method, path, payload = request.call_args.args
        self.assertEqual((method, path), ("POST", "/systemone"))
        self.assertEqual(payload["model"], server.JEV_MODEL)
        self.assertEqual(payload["state"], "Charged twice.")
        self.assertNotIn("extra", payload["questions"]["refund"])
        self.assertEqual(payload["questions"]["team"]["criteria"], {"billing": "Charges", "technical": "Bugs"})
        self.assertEqual(result["answers"], reply["answers"])
        self.assertEqual(result["model_used"], "typesafe/jev-1.13-20260917")
        self.assertEqual(result["usage"]["cost"], 0.00003)

    def test_a_reply_without_answers_is_502(self):
        with mock.patch.object(server, "openrouter_request", return_value={"id": "x"}):
            with self.assertRaises(server.GatewayError) as error:
                server.run_decision(
                    {"state": "x", "questions": {"q": {"type": "noul", "instructions": "?"}}}
                )
        self.assertEqual(error.exception.status, 502)


class RouteTaskTests(unittest.TestCase):
    def route(self, answer, **data):
        selection = {"category": "coding", "model": {"id": "provider/coder"}}
        with mock.patch.object(server, "load_config", return_value=CONFIG), mock.patch.object(
            server, "openrouter_request", return_value=jev_reply({"category": answer})
        ) as request, mock.patch.object(
            server, "select_benchmark_model", return_value=selection
        ) as select:
            result = server.route_task({"task": "Fix the login API", **data})
        return result, request, select

    def test_a_confident_route_returns_the_benchmark_specialist(self):
        result, request, select = self.route(
            {"type": "choice", "choice": "coding", "confidence": 0.91, "probabilities": {"coding": 0.91, "frontend": 0.09}}
        )
        criteria = request.call_args.args[2]["questions"]["category"]["criteria"]
        self.assertEqual(criteria, {"coding": "Writing code", "frontend": "Building interfaces"})
        select.assert_called_once_with("coding")
        self.assertEqual(result["category"], "coding")
        self.assertFalse(result["needs_confirmation"])
        self.assertEqual(result["selection"]["model"]["id"], "provider/coder")
        self.assertEqual(result["router"]["generation_id"], "gen-dec-1")

    def test_a_low_confidence_route_asks_instead_of_selecting(self):
        result, _request, select = self.route(
            {"choice": "frontend", "probabilities": {"coding": 0.45, "frontend": 0.55}}
        )
        select.assert_not_called()
        self.assertEqual(result["confidence"], 0.55)
        self.assertTrue(result["needs_confirmation"])
        self.assertIsNone(result["selection"])

        result, _request, select = self.route(
            {"choice": "frontend", "confidence": 0.55}, min_confidence=0.5
        )
        select.assert_called_once_with("frontend")
        self.assertFalse(result["needs_confirmation"])

    def test_an_unknown_category_from_jev_is_502(self):
        with self.assertRaises(server.GatewayError) as error:
            self.route({"choice": "vision", "confidence": 0.99})
        self.assertEqual(error.exception.status, 502)

    def test_stale_evidence_is_reported_next_to_the_route(self):
        stale = server.BenchmarkRegistryError("stale", 503, {"category": "coding"})
        with mock.patch.object(server, "load_config", return_value=CONFIG), mock.patch.object(
            server, "openrouter_request", return_value=jev_reply({"category": {"choice": "coding", "confidence": 0.9}})
        ), mock.patch.object(server, "select_benchmark_model", side_effect=stale):
            result = server.route_task({"task": "Fix the login API"})
        self.assertIsNone(result["selection"])
        self.assertEqual(result["selection_error"]["status"], 503)

    def test_task_is_required(self):
        with mock.patch.object(server, "openrouter_request") as request:
            with self.assertRaises(server.GatewayError):
                server.route_task({"task": ""})
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
