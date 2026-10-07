"""The cached model catalog is shared by every request thread.

agent/server.py runs on ThreadingHTTPServer, and /compare fans out into a
thread per model, so /models, /run and /compare read the cached catalog
concurrently. GET /models sorts what it reads. CPython empties a list for the
whole duration of an in-place sort, including while the key function runs, so
a caller that sorts the cached list itself blanks the catalog for every other
thread that is reading it.
"""

import importlib.util
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import time
import unittest
import urllib.request
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "agent" / "server.py"
SPEC = importlib.util.spec_from_file_location("helios_model_cache_server", MODULE_PATH)
server = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(server)


def catalog():
    """Deliberately not in the created-descending order /models sorts into."""
    return [
        {"id": "provider/oldest", "name": "Oldest", "created": 1},
        {"id": "provider/middle", "name": "Middle", "created": 2},
        {"id": "provider/newest", "name": "Newest", "created": 3},
    ]


def primed_cache(models):
    return patch.dict(server._model_cache, {"models": models, "loaded_at": time.time()})


class ModelCacheTests(unittest.TestCase):
    def test_get_models_does_not_hand_out_the_cached_list(self):
        with primed_cache(catalog()):
            returned = server.get_models()
            self.assertIsNot(returned, server._model_cache["models"])

            returned.sort(key=lambda item: -item["created"])
            self.assertEqual(
                [item["id"] for item in server._model_cache["models"]],
                ["provider/oldest", "provider/middle", "provider/newest"],
                "a caller sorting its own result must not reorder the cache",
            )

    def test_sorting_a_result_never_empties_the_catalog_for_another_thread(self):
        # A sort key runs while the list being sorted is empty, so it is an exact
        # stand-in for another request thread reading the cache at that moment.
        with primed_cache(catalog()):
            seen_lengths = []

            def key(item):
                seen_lengths.append(len(server._model_cache["models"]))
                return item["created"]

            server.get_models().sort(key=key)

            self.assertEqual(len(seen_lengths), 3)
            self.assertNotIn(
                0, seen_lengths, "the catalog was empty while another caller sorted"
            )

    def test_a_concurrent_reader_still_resolves_a_model(self):
        with primed_cache(catalog()):
            resolved = []

            def key(item):
                resolved.append(server.resolve_model("newest"))
                return item["created"]

            server.get_models().sort(key=key)
            self.assertEqual(set(resolved), {"provider/newest"})


class ModelsEndpointTests(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as response:
            return response.status, response.read().decode("utf-8")

    def test_listing_models_leaves_the_cache_in_its_original_order(self):
        with primed_cache(catalog()):
            status, _ = self.get("/models")
            self.assertEqual(status, 200)
            self.assertEqual(
                [item["id"] for item in server._model_cache["models"]],
                ["provider/oldest", "provider/middle", "provider/newest"],
                "GET /models reordered the shared cache",
            )

    def test_listing_models_still_returns_newest_first(self):
        with primed_cache(catalog()):
            _, body = self.get("/models")
            ids = [item["id"] for item in json.loads(body)["models"]]
            self.assertEqual(ids, ["provider/newest", "provider/middle", "provider/oldest"])

    def test_search_and_limit_still_apply(self):
        with primed_cache(catalog()):
            _, body = self.get("/models?search=middle")
            models = json.loads(body)["models"]
            self.assertEqual([item["id"] for item in models], ["provider/middle"])

            _, body = self.get("/models?limit=2")
            models = json.loads(body)["models"]
            self.assertEqual(
                [item["id"] for item in models], ["provider/newest", "provider/middle"]
            )


if __name__ == "__main__":
    unittest.main()
