import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "agent" / "server.py"
SPEC = importlib.util.spec_from_file_location("helios_server", MODULE_PATH)
server = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(server)


class HeliosServerTests(unittest.TestCase):
    def test_glm_alias_is_local_and_deterministic(self):
        self.assertEqual(
            server.resolve_model("glm"),
            os.environ.get("DEFAULT_GLM_MODEL", "z-ai/glm-5.2"),
        )

    def test_exact_slug_does_not_need_network(self):
        self.assertEqual(server.resolve_model("provider/model"), "provider/model")

    def test_prompt_building(self):
        messages = server.build_messages({"prompt": "hello", "system": "be concise"})
        self.assertEqual(messages[0], {"role": "system", "content": "be concise"})
        self.assertEqual(messages[1], {"role": "user", "content": "hello"})

    def test_messages_are_schema_validated(self):
        with self.assertRaises(server.GatewayError):
            server.build_messages([{"role": "user", "content": "hello"}])
        with self.assertRaises(server.GatewayError):
            server.build_messages({"messages": [{"role": "owner", "content": "hello"}]})
        with self.assertRaises(server.GatewayError):
            server.build_messages({"messages": [{"role": "user", "content": ""}]})

    def test_messages_size_limit_cannot_be_bypassed(self):
        with mock.patch.object(server, "MAX_PROMPT_CHARS", 20):
            with self.assertRaises(server.GatewayError) as error:
                server.build_messages(
                    {"messages": [{"role": "user", "content": "x" * 30}]}
                )
        self.assertEqual(error.exception.status, 413)

    def test_run_rejects_invalid_numeric_parameters_before_network(self):
        with self.assertRaises(server.GatewayError):
            server.run_model({"model": "provider/model", "prompt": "x", "max_tokens": "bad"})
        with self.assertRaises(server.GatewayError):
            server.run_model({"model": "provider/model", "prompt": "x", "temperature": 3})
        with self.assertRaises(server.GatewayError):
            server.run_model({"model": "provider/model", "prompt": "x", "top_p": -1})

    def test_manus_key_can_use_systemd_credential_file(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as handle:
            handle.write("temporary-test-key\n")
            handle.flush()
            with mock.patch.object(server, "MANUS_API_KEY_FILE", handle.name), mock.patch.dict(
                os.environ, {"MANUS_API_KEY": ""}
            ):
                self.assertEqual(server.manus_api_key(), "temporary-test-key")

    def test_manus_provider_creates_private_v2_task(self):
        with mock.patch.object(
            server, "manus_request", return_value={"ok": True, "task_id": "task-123"}
        ) as request:
            result = server.run_provider(
                {
                    "provider": "manus",
                    "prompt": "research this",
                    "agent_profile": "manus-1.6-max",
                }
            )
        self.assertEqual(result["provider"], "manus")
        self.assertEqual(result["task_id"], "task-123")
        method, operation, payload = request.call_args.args[:3]
        self.assertEqual((method, operation), ("POST", "task.create"))
        self.assertEqual(payload["message"]["content"], "research this")
        self.assertEqual(payload["share_visibility"], "private")
        self.assertEqual(payload["agent_profile"], "manus-1.6-max")

    def test_manus_provider_validates_before_network(self):
        with self.assertRaises(server.GatewayError):
            server.create_manus_task({"prompt": "x", "share_visibility": "internet"})
        with self.assertRaises(server.GatewayError):
            server.create_manus_task({"prompt": "x", "agent_profile": "unknown"})
        with self.assertRaises(server.GatewayError):
            server.manus_task_messages("task", {"verbose": ["maybe"]})

    def test_compare_rejects_duplicate_or_invalid_models(self):
        with self.assertRaises(server.GatewayError):
            server.compare_models({"models": ["a/model", "a/model"], "prompt": "x"})
        with self.assertRaises(server.GatewayError):
            server.compare_models({"models": ["a/model", 3], "prompt": "x"})

    def test_bounded_integer_validation(self):
        self.assertEqual(server.bounded_int("10", "limit", 5, 1, 20), 10)
        for value in ("bad", True, 1.5, 0, 21):
            with self.assertRaises(server.GatewayError):
                server.bounded_int(value, "limit", 5, 1, 20)

    def test_refresh_boolean_is_strict(self):
        self.assertTrue(server.strict_bool(True, "only_if_stale"))
        self.assertFalse(server.strict_bool(None, "only_if_stale"))
        with self.assertRaises(server.GatewayError):
            server.strict_bool("false", "only_if_stale")

    def test_non_loopback_host_is_not_the_default(self):
        self.assertIn(server.HOST, {"127.0.0.1", "::1", "localhost"})


if __name__ == "__main__":
    unittest.main()
