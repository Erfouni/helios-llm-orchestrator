import json
import os
from pathlib import Path
import tempfile
import unittest

from agent import benchmark_registry

SOURCE = "https://official.example/leaderboard"


def search_response(ranking, benchmark_date=None):
    """A stand-in for OpenRouter's web-search reply with one citation."""
    extracted = {"benchmark_name": "Official", "ranking": ranking}
    if benchmark_date is not None:
        extracted["benchmark_date"] = benchmark_date
    return {
        "model": "search/model",
        "choices": [
            {
                "message": {
                    "content": json.dumps(extracted),
                    "annotations": [{"url_citation": {"url": SOURCE}}],
                }
            }
        ],
    }


def ranked(*names):
    return [
        {"rank": rank, "model_name": name, "score": 100 - rank, "score_text": "x", "source_url": SOURCE}
        for rank, name in enumerate(names, start=1)
    ]


def current_entry():
    return {
        "selected_model": {"id": "provider/model-1"},
        "valid_until": "2099-01-01T00:00:00Z",
        "ranking": [{"model_name": f"Model {n}", "source_url": SOURCE} for n in range(1, 4)],
    }


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
                                "selection_source_url": "https://www.swebench.com/",
                                "selection_policy": "highest_cited_public_rank_available_on_openrouter",
                                "valid_until": "2099-07-30T00:00:00Z",
                                "ranking": [
                                    {"model_name": "Model A", "source_url": "https://www.swebench.com/"},
                                    {"model_name": "Model B", "source_url": "https://www.swebench.com/"},
                                    {"model_name": "Model C", "source_url": "https://www.swebench.com/"},
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

    def test_non_allowlisted_source_is_not_selectable(self):
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
                                    {
                                        "model_name": f"Model {number}",
                                        "source_url": "https://llm-stats.example/leaderboard",
                                    }
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
            self.assertIn("allowlist", error.exception.details["reason"])

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

    def test_ranked_name_does_not_resolve_to_a_sibling_model(self):
        models = [
            {"id": "openai/gpt-5", "name": "OpenAI: GPT-5", "created": 100},
            {"id": "openai/gpt-5-nano", "name": "OpenAI: GPT-5 Nano", "created": 300},
            {"id": "openai/gpt-5-mini", "name": "OpenAI: GPT-5 Mini", "created": 200},
            {"id": "openai/gpt-5.1", "name": "OpenAI: GPT-5.1", "created": 400},
            {"id": "google/gemini-3-pro", "name": "Google: Gemini 3 Pro", "created": 100},
            {"id": "google/gemini-3-pro-image", "name": "Google: Gemini 3 Pro Image", "created": 400},
        ]
        resolve = benchmark_registry.resolve_available_model
        self.assertEqual(resolve("GPT-5", models)["id"], "openai/gpt-5")
        self.assertEqual(resolve("Gemini 3 Pro", models)["id"], "google/gemini-3-pro")

    def test_no_sibling_stands_in_for_a_missing_ranked_model(self):
        models = [
            {"id": "openai/gpt-5-mini", "name": "OpenAI: GPT-5 Mini", "created": 200},
            {"id": "openai/gpt-5-nano", "name": "OpenAI: GPT-5 Nano", "created": 300},
            {"id": "openai/gpt-5.1", "name": "OpenAI: GPT-5.1", "created": 400},
        ]
        self.assertIsNone(benchmark_registry.resolve_available_model("GPT-5", models))

    def test_snapshots_and_leaderboard_decorations_still_resolve(self):
        resolve = benchmark_registry.resolve_available_model
        snapshot = {"id": "moonshotai/kimi-k2-0905", "name": "MoonshotAI: Kimi K2 0905", "created": 2}
        self.assertEqual(resolve("Kimi K2", [snapshot])["id"], "moonshotai/kimi-k2-0905")
        plain = {"id": "moonshotai/kimi-k2", "name": "MoonshotAI: Kimi K2", "created": 1}
        self.assertEqual(resolve("Kimi K2", [snapshot, plain])["id"], "moonshotai/kimi-k2")
        opus = {"id": "anthropic/claude-opus-4.1", "name": "Anthropic: Claude Opus 4.1", "created": 1}
        self.assertEqual(resolve("Claude Opus 4.1 (Thinking)", [opus])["id"], "anthropic/claude-opus-4.1")
        self.assertEqual(resolve("Claude 4.1 Opus", [opus])["id"], "anthropic/claude-opus-4.1")

    def test_written_ranks_do_not_fail_the_category(self):
        self.assertEqual(benchmark_registry._rank("1st", 5), 1)
        self.assertEqual(benchmark_registry._rank("#2", 5), 2)
        self.assertEqual(benchmark_registry._rank(3, 5), 3)
        for value in (None, True, "n/a"):
            self.assertEqual(benchmark_registry._rank(value, 5), 5)

        ranking = ranked("Model 1", "Model 2", "Model 3")
        for item, text in zip(ranking, ("1st", "2nd", "3rd")):
            item["rank"] = text
        models = [{"id": f"provider/model-{n}", "name": f"Model {n}", "created": n} for n in range(1, 4)]
        result = benchmark_registry._refresh_category(
            "coding",
            {"query": "coding"},
            {"min_ranked_models": 3},
            lambda *_a, **_k: search_response(ranking),
            models,
            "2026-07-22",
        )
        self.assertEqual([item["rank"] for item in result["ranking"]], [1, 2, 3])

    def test_old_evidence_is_rejected_at_refresh(self):
        models = [{"id": f"provider/model-{n}", "name": f"Model {n}", "created": n} for n in range(1, 4)]
        ranking = ranked("Model 1", "Model 2", "Model 3")
        config = {"min_ranked_models": 3, "max_evidence_age_days": 180}

        def refresh(benchmark_date):
            return benchmark_registry._refresh_category(
                "reasoning",
                {"query": "reasoning"},
                config,
                lambda *_a, **_k: search_response(ranking, benchmark_date),
                models,
                "2026-07-22",
            )

        with self.assertRaises(benchmark_registry.BenchmarkRegistryError) as error:
            refresh("2024-01-15")
        self.assertEqual(error.exception.status, 422)
        self.assertEqual(error.exception.details["benchmark_date"], "2024-01-15")
        # Recent, month-only, and unknown dates are accepted.
        for benchmark_date in ("2026-07-01", "2026-06", None):
            self.assertEqual(refresh(benchmark_date)["selected_model"]["id"], "provider/model-1")


class RegistryRefreshTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config_path = self.root / "sources.json"
        self.registry_path = self.root / "registry.json"
        self.prompts = []
        self.models = [
            {"id": f"provider/model-{n}", "name": f"Model {n}", "created": n} for n in range(1, 4)
        ]

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def request(self, _method, _path, payload, **_kwargs):
        self.prompts.append(payload["messages"][-1]["content"])
        return search_response(ranked("Model 1", "Model 2", "Model 3"))

    def refresh(self, only_if_stale):
        return benchmark_registry.refresh_registry(
            self.request,
            self.models,
            only_if_stale=only_if_stale,
            config_path=self.config_path,
            registry_path=self.registry_path,
        )

    def refreshed_categories(self):
        return sorted(
            line.split(": ", 1)[1]
            for prompt in self.prompts
            for line in prompt.splitlines()
            if line.startswith("Category: ")
        )

    def test_removed_category_neither_stays_stale_nor_survives_a_refresh(self):
        self.write(self.config_path, {"categories": {"coding": {"query": "coding"}}})
        retired = dict(current_entry(), valid_until="2020-01-01T00:00:00Z")
        self.write(
            self.registry_path,
            {"valid_until": "2099-01-01T00:00:00Z", "categories": {"coding": current_entry(), "retired": retired}},
        )

        status = benchmark_registry.registry_status(self.registry_path, self.config_path)
        self.assertFalse(status["stale"])
        self.assertEqual(status["category_count"], 1)
        skipped = self.refresh(only_if_stale=True)
        self.assertTrue(skipped["skipped"])
        self.assertEqual(self.prompts, [], "a fresh registry must not pay for searches")

        refreshed = self.refresh(only_if_stale=False)
        self.assertEqual(refreshed["dropped_categories"], ["retired"])
        published = benchmark_registry.load_registry(self.registry_path)
        self.assertEqual(sorted(published["categories"]), ["coding"])

    def test_stale_only_refresh_pays_only_for_due_categories(self):
        self.write(
            self.config_path,
            {
                "categories": {
                    "coding": {"query": "coding"},
                    "frontend": {"query": "frontend"},
                    "mathematics": {"query": "mathematics"},
                    "vision": {"query": "vision", "enabled": False},
                }
            },
        )
        expired = dict(current_entry(), valid_until="2020-01-01T00:00:00Z")
        self.write(
            self.registry_path,
            {"valid_until": "2020-01-01T00:00:00Z", "categories": {"coding": current_entry(), "frontend": expired}},
        )
        self.assertEqual(
            benchmark_registry.registry_status(self.registry_path, self.config_path)["missing_categories"],
            ["mathematics"],
        )

        refreshed = self.refresh(only_if_stale=True)
        self.assertEqual(refreshed["refreshed_categories"], ["frontend", "mathematics"])
        self.assertEqual(self.refreshed_categories(), ["frontend", "mathematics"])
        published = benchmark_registry.load_registry(self.registry_path)
        self.assertEqual(published["categories"]["coding"], current_entry())
        self.assertEqual(published["status"], "current")

    def test_refresh_is_refused_while_another_process_holds_the_lock(self):
        self.write(self.config_path, {"categories": {"coding": {"query": "coding"}}})
        lock_path = self.registry_path.with_name(self.registry_path.name + ".lock")
        descriptor = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
        try:
            benchmark_registry._lock_file(descriptor)
            with self.assertRaises(benchmark_registry.BenchmarkRegistryError) as error:
                self.refresh(only_if_stale=False)
            self.assertEqual(error.exception.status, 409)
            self.assertEqual(self.prompts, [])
            benchmark_registry._unlock_file(descriptor)
        finally:
            os.close(descriptor)
        self.assertFalse(self.refresh(only_if_stale=False)["skipped"])


if __name__ == "__main__":
    unittest.main()
