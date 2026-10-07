import importlib.util
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "agent" / "server.py"
SPEC = importlib.util.spec_from_file_location("helios_http_server", MODULE_PATH)
server = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(server)


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def call(self, path, data=None):
        request = urllib.request.Request(
            self.base_url + path,
            data=None if data is None else json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="GET" if data is None else "POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.load(response), response.headers
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.load(error), error.headers
            finally:
                error.close()

    def test_health_and_security_headers(self):
        status, body, headers = self.call("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "helios-llm-orchestrator")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Content-Security-Policy"], "default-src 'none'")

    def test_providers_reports_manus_without_exposing_credentials(self):
        with mock.patch.object(server, "manus_api_key_configured", return_value=True):
            status, body, _headers = self.call("/providers")
        self.assertEqual(status, 200)
        self.assertTrue(body["manus"]["configured"])
        self.assertEqual(body["manus"]["api_version"], "v2")
        self.assertNotIn("key", body["manus"])

    def test_run_can_dispatch_to_manus(self):
        with mock.patch.object(
            server, "manus_request", return_value={"ok": True, "task_id": "task-http"}
        ):
            status, body, _headers = self.call(
                "/run", {"provider": "manus", "prompt": "hello"}
            )
        self.assertEqual(status, 200)
        self.assertEqual(body["provider"], "manus")
        self.assertEqual(body["task_id"], "task-http")

    def test_invalid_model_limit_is_400_without_catalog_call(self):
        status, body, _headers = self.call("/models?limit=abc")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "limit must be an integer")

    def test_invalid_run_number_is_400_without_provider_call(self):
        status, body, _headers = self.call(
            "/run",
            {"model": "provider/model", "prompt": "x", "max_tokens": "bad"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "max_tokens must be an integer")

    def test_refresh_boolean_is_validated_before_provider_call(self):
        status, body, _headers = self.call(
            "/benchmarks/refresh", {"only_if_stale": "false"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "only_if_stale must be a boolean")


if __name__ == "__main__":
    unittest.main()
