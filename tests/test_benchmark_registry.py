import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

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

    def test_citation_matching_ignores_query_and_www_only(self):
        citations = {"https://www.swebench.com/?utm_source=test"}
        accepted = benchmark_registry._safe_source_url(
            "https://swebench.com/", citations, set(), {"swebench.com"}
        )
        self.assertEqual(accepted, "https://swebench.com")
        rejected = benchmark_registry._safe_source_url(
            "https://swebench.com/verified", citations, set(), {"swebench.com"}
        )
        self.assertIsNone(rejected)

    def test_resolves_reasoning_variant_and_dated_slug_to_base_model(self):
        models = [
            {
                "id": "deepseek/deepseek-v4-pro-0813",
                "name": "DeepSeek: DeepSeek V4 Pro 0813",
                "created": 2,
            },
            {
                "id": "openai/gpt-5.6-sol-pro",
                "name": "OpenAI: GPT-5.6 Sol Pro",
                "created": 1,
            },
        ]
        deepseek = benchmark_registry.resolve_available_model(
            "deepseek-v4-pro-high-20260813", models
        )
        self.assertEqual(deepseek["id"], "deepseek/deepseek-v4-pro-0813")
        gpt = benchmark_registry.resolve_available_model(
            "gpt-5.6-sol-xhigh (codex-harness)", models
        )
        self.assertEqual(gpt["id"], "openai/gpt-5.6-sol-pro")

    def test_fresh_evidence_is_kept_when_no_openrouter_model_matches(self):
        source = "https://official.example/leaderboard"
        extracted = {
            "benchmark_name": "Generation Benchmark",
            "benchmark_date": "2026-08-23",
            "ranking": [
                {"rank": rank, "model_name": f"Unavailable {rank}", "score": 100-rank, "score_text": str(100-rank), "source_url": source}
                for rank in range(1, 4)
            ],
        }
        def request(*_args, **_kwargs):
            return {
                "model": "search/model",
                "choices": [{"message": {
                    "content": json.dumps(extracted),
                    "annotations": [{"url_citation": {"url": source}}],
                }}],
            }
        result = benchmark_registry._refresh_category(
            "video_generation",
            {"query": "video", "benchmark_hints": [], "allowed_domains": ["official.example"]},
            {"min_ranked_models": 3, "valid_days": 8},
            request, [], "2026-08-23",
        )
        self.assertFalse(result["openrouter_match_available"])
        self.assertIsNone(result["selected_model"])
        self.assertEqual(result["top_public_model"], "Unavailable 1")
        self.assertEqual(result["evidence_model_count"], 3)


    def test_official_csv_adapter_uses_approved_source_and_maps_models(self):
        csv_text = "Rank,Overall Acc,Model\n1,88.0%,Model One\n2,80.0%,Model Two\n3,75.0%,Model Three\n"
        page_text = "<p>Last Updated: 2026-08-23</p>"
        class Response:
            def __init__(self, text): self.text = text
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return self.text.encode()
        def fake_urlopen(request, timeout=45):
            url = request.full_url
            return Response(csv_text if url.endswith('.csv') else page_text)
        models = [{"id":"p/model-one","name":"Model One","created":1}]
        spec = {
            "source_adapter":"official_csv",
            "benchmark_name":"BFCL V4",
            "data_url":"https://official.example/data.csv",
            "source_url":"https://official.example/leaderboard.html",
            "rank_column":"Rank",
            "model_column":"Model",
            "score_column":"Overall Acc",
            "date_regex":r"Last Updated:\s*(\d{4}-\d{2}-\d{2})",
            "allowed_domains":["official.example"],
        }
        with mock.patch.object(benchmark_registry.urllib.request, "urlopen", fake_urlopen):
            result = benchmark_registry._refresh_official_csv_category(
                "agentic_tool_use", spec, {"min_ranked_models":3,"valid_days":8}, models
            )
        self.assertEqual(result["benchmark_date"], "2026-08-23")
        self.assertEqual(result["selected_model"]["id"], "p/model-one")
        self.assertEqual(result["selected_score"], 88.0)
        self.assertEqual(result["evidence_method"], "approved_official_csv")

    def test_resolver_prefers_base_text_model_over_image_or_customtools_variant(self):
        models = [
            {"id":"google/gemini-3-pro-preview","name":"Google: Gemini 3 Pro Preview","created":10},
            {"id":"google/gemini-3-pro-image","name":"Google: Gemini 3 Pro Image","created":30},
            {"id":"google/gemini-3-pro-preview-customtools","name":"Google: Gemini 3 Pro Preview Custom Tools","created":40},
        ]
        selected = benchmark_registry.resolve_available_model(
            "Gemini-3-Pro-Preview (Prompt)", models
        )
        self.assertEqual(selected["id"], "google/gemini-3-pro-preview")

    def test_resolver_ignores_full_date_suffix_when_base_slug_has_no_date(self):
        models = [
            {"id":"anthropic/claude-opus-4.5","name":"Anthropic: Claude Opus 4.5","created":10},
        ]
        selected = benchmark_registry.resolve_available_model(
            "Claude-Opus-4-5-20251101 (FC)", models
        )
        self.assertEqual(selected["id"], "anthropic/claude-opus-4.5")


if __name__ == "__main__":
    unittest.main()
