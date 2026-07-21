import importlib.util
import os
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "agent" / "server.py"
SPEC = importlib.util.spec_from_file_location("helios_server", MODULE_PATH)
server = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(server)


class HeliosServerTests(unittest.TestCase):
    def test_glm_alias_is_local_and_deterministic(self):
        self.assertEqual(server.resolve_model("glm"), os.environ.get("DEFAULT_GLM_MODEL", "z-ai/glm-5.2"))

    def test_exact_slug_does_not_need_network(self):
        self.assertEqual(server.resolve_model("provider/model"), "provider/model")

    def test_prompt_building(self):
        messages = server.build_messages({"prompt": "hello", "system": "be concise"})
        self.assertEqual(messages[0], {"role": "system", "content": "be concise"})
        self.assertEqual(messages[1], {"role": "user", "content": "hello"})

    def test_non_loopback_host_is_not_the_default(self):
        self.assertIn(server.HOST, {"127.0.0.1", "::1", "localhost"})


if __name__ == "__main__":
    unittest.main()
