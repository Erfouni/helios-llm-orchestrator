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
                        "valid_until": "2099-07-30T00:00:00Z",
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
                                "valid_until": "2099-07-30T00:00:00Z",
                                "ranking": [
                                    {"model_name": "Model A", "source_url": "https://example.com/leaderboard"},
                                    {"model_name": "Model B", "source_url": "https://example.com/leaderboard"},
                                    {"model_name": "Model C", "source_url": "https://example.com/leaderboard"},
                                ],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            selected = benchmark_registry.select_benchmark_model("Backend", path)
            self.assertEqual(selected["category"], "coding")
            self.assertEqual(selected["model"]["id"], "provider/model")

    def test_category_expiry_cannot_be_hidden_by_registry_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text(
                json.dumps(
                    {
                        "status": "current",
                        "updated_at": "2026-07-22T00:00:00Z",
                        "valid_until": "2099-07-30T00:00:00Z",
                        "categories": {
                            "coding": {
                                "selected_model": {"id": "provider/model"},
                                "valid_until": "2020-01-01T00:00:00Z",
                            }
                        },
                        "failures": [],
                    }
                ),
                encoding="utf-8",
            )
            status = benchmark_registry.registry_status(path)
            self.assertTrue(status["stale"])
            self.assertEqual(status["stale_categories"], ["coding"])
            with self.assertRaises(benchmark_registry.BenchmarkRegistryError) as error:
                benchmark_registry.select_benchmark_model("coding", path)
            self.assertEqual(error.exception.status, 503)

    def test_blocked_aggregator_is_not_selectable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text(
                json.dumps(
                    {
                        "valid_until": "2099-01-01T00:00:00Z",
                        "categories": {
                            "coding": {
                                "selected_model": {"id": "provider/model"},
                                "ranking": [
                                    {"model_name": f"Model {number}", "source_url": "https://benchlm.ai/leaderboard"}
                                    for number in range(1, 4)
                                ],
                            }
                        },
                        "failures": [],
                    }
                ),
                encoding="utf-8",
            )
            status = benchmark_registry.registry_status(path)
            self.assertTrue(status["stale"])
            self.assertEqual(status["low_quality_categories"], ["coding"])
            with self.assertRaises(benchmark_registry.BenchmarkRegistryError) as error:
                benchmark_registry.select_benchmark_model("coding", path)
            self.assertEqual(error.exception.status, 422)

    def test_refresh_quality_gate_and_top_public_model_semantics(self):
        source = "https://official.example/leaderboard"
        extracted = {
            "benchmark_name": "Official Benchmark",
            "benchmark_date": "2026-07-20",
            "ranking": [
                {"rank": 1, "model_name": "Unavailable One", "score": 99, "score_text": "99", "source_url": source},
                {"rank": 2, "model_name": "Available Two", "score": 95, "score_text": "95", "source_url": source},
                {"rank": 3, "model_name": "Available Three", "score": 90, "score_text": "90", "source_url": source},
            ],
        }

        def request(*_args, **_kwargs):
            return {
                "model": "search/model",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(extracted),
                            "annotations": [{"url_citation": {"url": source}}],
                        }
                    }
                ],
            }

        models = [
            {"id": "provider/available-two", "name": "Available Two", "created": 2},
            {"id": "provider/available-three", "name": "Available Three", "created": 1},
        ]
        result = benchmark_registry._refresh_category(
            "coding",
            {"query": "coding", "benchmark_hints": []},
            {"min_ranked_models": 3, "valid_days": 8},
            request,
            models,
            "2026-07-22",
        )
        self.assertEqual(result["top_public_model"], "Unavailable One")
        self.assertEqual(result["selected_model"]["id"], "provider/available-two")
        self.assertEqual(result["selected_score"], 95)
        self.assertEqual(result["evidence_model_count"], 3)

    def test_partial_refresh_preserves_old_category_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "sources.json"
            registry_path = root / "registry.json"
            config_path.write_text(
                json.dumps(
                    {
                        "min_ranked_models": 3,
                        "valid_days": 8,
                        "max_parallel": 2,
                        "categories": {
                            "coding": {"query": "coding"},
                            "frontend": {"query": "frontend"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            registry_path.write_text(
                json.dumps(
                    {
                        "status": "current",
                        "updated_at": "2020-01-01T00:00:00Z",
                        "valid_until": "2020-01-09T00:00:00Z",
                        "categories": {
                            "frontend": {
                                "selected_model": {"id": "provider/old"},
                            }
                        },
                        "failures": [],
                    }
                ),
                encoding="utf-8",
            )
            source = "https://official.example/leaderboard"

            def request(_method, _path, payload, **_kwargs):
                prompt = payload["messages"][-1]["content"]
                ranking = []
                if "Category: coding" in prompt:
                    ranking = [
                        {"rank": rank, "model_name": f"Model {rank}", "score": 100 - rank, "score_text": str(100 - rank), "source_url": source}
                        for rank in range(1, 4)
                    ]
                return {
                    "model": "search/model",
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "benchmark_name": "Official",
                                        "ranking": ranking,
                                    }
                                ),
                                "annotations": [{"url_citation": {"url": source}}],
                            }
                        }
                    ],
                }

            models = [
                {"id": f"provider/model-{rank}", "name": f"Model {rank}", "created": rank}
                for rank in range(1, 4)
            ]
            refreshed = benchmark_registry.refresh_registry(
                request,
                models,
                config_path=config_path,
                registry_path=registry_path,
            )
            self.assertEqual(refreshed["status"], "partial")
            published = benchmark_registry.load_registry(registry_path)
            self.assertEqual(
                published["categories"]["frontend"]["valid_until"],
                "2020-01-09T00:00:00Z",
            )
            self.assertEqual(published["valid_until"], "2020-01-09T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
