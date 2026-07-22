import json
from pathlib import Path
import tempfile
import unittest

from agent import benchmark_registry


class BenchmarkRegistryTests(unittest.TestCase):
    def test_hash_is_stable_and_ignores_hash_field(self):
        value = {"categories": {"coding": {"selected": "x"}}, "registry_hash": None}
        first = benchmark_registry.registry_hash(value)
        value["registry_hash"] = "different"
        self.assertEqual(first, benchmark_registry.registry_hash(value))

    def test_resolves_public_name_to_openrouter_catalog(self):
        models = [
            {"id": "moonshotai/kimi-k3", "name": "MoonshotAI: Kimi K3", "created": 20},
            {"id": "moonshotai/kimi-k2", "name": "MoonshotAI: Kimi K2", "created": 10},
        ]
        selected = benchmark_registry.resolve_available_model("Kimi K3", models)
        self.assertIsNotNone(selected)
        self.assertEqual(selected["id"], "moonshotai/kimi-k3")

    def test_select_uses_task_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text(
                json.dumps(
                    {
                        "registry_hash": "abc",
                        "updated_at": "2026-07-22T00:00:00Z",
                        "valid_until": "2026-07-30T00:00:00Z",
                        "task_aliases": {"backend": "coding"},
                        "categories": {
                            "coding": {
                                "selected_model": {"id": "provider/model", "name": "Model"},
                                "benchmark_name": "SWE-bench Verified",
                                "benchmark_date": "2026-07-20",
                                "selected_score": 80.0,
                                "selected_score_text": "80.0%",
                                "selection_source_url": "https://example.com/leaderboard",
                                "selection_policy": "highest_cited_public_rank_available_on_openrouter",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            selected = benchmark_registry.select_benchmark_model("backend", path)
            self.assertEqual(selected["category"], "coding")
            self.assertEqual(selected["model"]["id"], "provider/model")


if __name__ == "__main__":
    unittest.main()
