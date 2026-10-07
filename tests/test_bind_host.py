import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request


SERVER = Path(__file__).resolve().parents[1] / "agent" / "server.py"
# A system proxy (HTTP_PROXY, or Windows Internet settings) would otherwise get
# the [::1] request: urllib's bypass list never matches a bracketed IPv6 host.
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def ipv6_loopback_available() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True


def free_port(family: int, host: str) -> int:
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


class BindHostTests(unittest.TestCase):
    """Start the real gateway on each loopback host that SECURITY.md allows."""

    def start_gateway(self, host: str, port: int) -> subprocess.Popen:
        env = dict(os.environ)
        env.update(
            {
                "OPENROUTER_AGENT_HOST": host,
                "OPENROUTER_AGENT_PORT": str(port),
                "PYTHONUNBUFFERED": "1",
            }
        )
        process = subprocess.Popen(
            [sys.executable, str(SERVER)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.addCleanup(self.stop_gateway, process)
        return process

    @staticmethod
    def stop_gateway(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()

    def wait_for_health(self, process: subprocess.Popen, url: str) -> dict:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                _stdout, stderr = process.communicate()
                self.fail(f"gateway exited with {process.returncode}:\n{stderr[-800:]}")
            try:
                with DIRECT.open(url + "/health", timeout=1) as response:
                    self.assertEqual(response.status, 200)
                    return json.load(response)
            except (urllib.error.URLError, ConnectionError, socket.timeout):
                time.sleep(0.1)
        self.fail(f"gateway did not answer on {url} within 15 s")

    def test_ipv4_loopback(self):
        port = free_port(socket.AF_INET, "127.0.0.1")
        process = self.start_gateway("127.0.0.1", port)
        body = self.wait_for_health(process, f"http://127.0.0.1:{port}")
        self.assertEqual(body["service"], "helios-llm-orchestrator")

    @unittest.skipUnless(ipv6_loopback_available(), "no IPv6 loopback on this host")
    def test_ipv6_loopback(self):
        port = free_port(socket.AF_INET6, "::1")
        process = self.start_gateway("::1", port)
        body = self.wait_for_health(process, f"http://[::1]:{port}")
        self.assertEqual(body["service"], "helios-llm-orchestrator")
        startup = json.loads(process.stdout.readline())
        self.assertEqual(startup["url"], f"http://[::1]:{port}")


if __name__ == "__main__":
    unittest.main()
