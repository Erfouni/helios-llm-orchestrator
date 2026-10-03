import importlib.util
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request


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

    def call(self, path, data=None, headers=None):
        request = urllib.request.Request(
            self.base_url + path,
            data=None if data is None else json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
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

    def test_non_json_post_is_refused_before_validation(self):
        # A cross-site <form enctype="text/plain"> can send this without a
        # CORS preflight, so it must not reach the paid endpoints.
        for content_type in ("text/plain", "application/x-www-form-urlencoded", ""):
            with self.subTest(content_type=content_type):
                status, body, _headers = self.call(
                    "/run",
                    {"model": "provider/model", "prompt": "x", "max_tokens": "bad"},
                    headers={"Content-Type": content_type},
                )
                self.assertEqual(status, 415)
                self.assertEqual(body["error"], "Content-Type must be application/json")

    def test_json_content_type_with_charset_is_accepted(self):
        status, body, _headers = self.call(
            "/run",
            {"model": "provider/model", "prompt": "x", "max_tokens": "bad"},
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "max_tokens must be an integer")

    def test_foreign_host_header_is_refused(self):
        # A DNS-rebound page reaches the loopback port with its own Host name.
        for host in ("evil.example", "evil.example:3188", "127.0.0.1.evil.example"):
            with self.subTest(host=host):
                status, body, _headers = self.call(
                    "/models?limit=abc", headers={"Host": host}
                )
                self.assertEqual(status, 421)
                self.assertEqual(body["error"], "Host must be a loopback address")
                status, _body, _headers = self.call(
                    "/run",
                    {"model": "provider/model", "prompt": "x", "max_tokens": "bad"},
                    headers={"Host": host},
                )
                self.assertEqual(status, 421)

    def test_loopback_host_names_are_accepted(self):
        for host in ("127.0.0.1", "127.0.0.1:3188", "localhost:3188", "LOCALHOST", "[::1]:3188"):
            with self.subTest(host=host):
                status, body, _headers = self.call(
                    "/models?limit=abc", headers={"Host": host}
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["error"], "limit must be an integer")


if __name__ == "__main__":
    unittest.main()
