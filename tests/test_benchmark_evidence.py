"""Offline regressions for evidence validity and runnable evaluated settings."""

import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent import benchmark_registry as registry


NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
SOURCE = "https://arcprize.org/leaderboard"
SPEC = {"allowed_domains": ["arcprize.org"], "benchmark_name": "ARC-AGI-2"}
CONFIG = {"min_ranked_models": 3, "max_evidence_age_days": 180,
          "categories": {"reasoning": SPEC}}


def evidence():
    rows = [{"rank": n, "model_name": f"Model {n}", "score": 100 - n,
             "source_url": SOURCE, "openrouter_model": {"id": f"p/model-{n}"}}
            for n in range(1, 4)]
    return {"benchmark_name": "ARC-AGI-2", "benchmark_date": "2026-10-05",
            "valid_until": "2026-10-18T00:00:00Z", "ranking": rows,
            "selected_model": {"id": "p/model-1"}}


def catalog_model(number, **changes):
    value = {"id": f"p/model-{number}", "name": f"Model {number}",
             "context_length": 128000,
             "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
             "pricing": {"prompt": "0.000001", "completion": "0.000002"},
             "supported_parameters": ["reasoning", "tools", "temperature", "max_tokens"],
             "reasoning": {"supported_efforts": ["low", "medium", "high", "max"]}}
    value.update(changes)
    return value


class EvidenceGateTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(registry, "utc_now", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_future_and_obsolete_dates_fail_for_stored_evidence(self):
        for value in (None, "", "not-a-date", "2026-10-11", "2025-05-06"):
            with self.subTest(date=value):
                entry = dict(evidence(), benchmark_date=value)
                self.assertIsNotNone(registry._result_quality_error("reasoning", entry, CONFIG))

    def test_score_must_be_a_finite_number_not_a_boolean_or_estimate(self):
        for value in (None, True, "99", "estimated 99", float("nan"), float("inf")):
            with self.subTest(score=value):
                entry = evidence()
                entry["ranking"][0]["score"] = value
                self.assertIsNotNone(registry._result_quality_error("reasoning", entry, CONFIG))

    def test_three_configurations_of_one_model_are_not_three_models(self):
        entry = evidence()
        for row, effort in zip(entry["ranking"], ("High", "Medium", "Low")):
            row["model_name"] = f"GPT-5 ({effort})"
        self.assertIsNotNone(registry._result_quality_error("reasoning", entry, CONFIG))

    def test_explicit_model_groups_cannot_inflate_distinct_model_count(self):
        entry = evidence()
        for row in entry["ranking"]:
            row["model_group"] = "same-base-model"
        self.assertIsNotNone(registry._result_quality_error("reasoning", entry, CONFIG))

    def test_allowlist_is_required_and_non_http_sources_fail(self):
        self.assertIsNotNone(registry._result_quality_error("reasoning", evidence(), {"categories": {"reasoning": {}}}))
        entry = evidence()
        entry["ranking"][0]["source_url"] = "file://arcprize.org/leaderboard"
        self.assertIsNotNone(registry._result_quality_error("reasoning", entry, CONFIG))

    def test_scores_from_different_datasets_or_metrics_are_not_comparable(self):
        for key in ("dataset_id", "score_metric"):
            entry = evidence()
            for row, value in zip(entry["ranking"], ("same", "same", "different")):
                row[key] = value
            with self.subTest(key=key):
                self.assertIsNotNone(registry._result_quality_error("reasoning", entry, CONFIG))

    def test_unrelated_leaderboard_pages_do_not_form_comparable_evidence(self):
        entry = evidence()
        entry["ranking"][0]["source_url"] = "https://arcprize.org/different-benchmark"
        self.assertIsNotNone(registry._result_quality_error("reasoning", entry, CONFIG))

    def test_csv_missing_future_and_obsolete_dates_cannot_refresh(self):
        csv_spec = dict(SPEC, data_url="https://arcprize.org/data.csv", source_url=SOURCE,
                        date_regex=r"Updated: (\d{4}-\d{2}-\d{2})")
        text = "Rank,Model,Score\n1,Model 1,99\n2,Model 2,98\n3,Model 3,97\n"
        for date in ("missing", "2026-10-11", "2025-05-06"):
            with self.subTest(date=date), mock.patch.object(registry, "_fetch_text", side_effect=[text, f"Updated: {date}"]):
                with self.assertRaises(registry.BenchmarkRegistryError):
                    registry._refresh_official_csv_category("reasoning", csv_spec, CONFIG, [])

    def test_csv_cannot_extract_a_numeric_score_from_a_qualitative_estimate(self):
        text = "Rank,Model,Score\n1,Model 1,estimated 99\n2,Model 2,98\n3,Model 3,97\n"
        csv_spec = dict(SPEC, data_url="https://arcprize.org/data.csv", source_url=SOURCE,
                        date_regex=r"Updated: (\d{4}-\d{2}-\d{2})")
        with mock.patch.object(registry, "_fetch_text", side_effect=[text, "Updated: 2026-10-05"]):
            with self.assertRaises(registry.BenchmarkRegistryError):
                registry._refresh_official_csv_category("reasoning", csv_spec, CONFIG, [])

    def test_csv_looks_past_duplicate_configurations_to_find_distinct_models(self):
        text = "Rank,Model,Score\n" + "\n".join(
            f"{n},Model 1 ({effort}),{100-n}" for n, effort in enumerate(("Max", "High", "Medium", "Low", "Thinking"), 1)
        ) + "\n6,Model 2,90\n7,Model 3,80\n"
        csv_spec = dict(SPEC, data_url="https://arcprize.org/data.csv", source_url=SOURCE,
                        date_regex=r"Updated: (\d{4}-\d{2}-\d{2})")
        with mock.patch.object(registry, "_fetch_text", side_effect=[text, "Updated: 2026-10-05"]):
            result = registry._refresh_official_csv_category("reasoning", csv_spec, CONFIG, [])
        self.assertEqual([row["model_name"] for row in result["ranking"]], ["Model 1 (Max)", "Model 2", "Model 3"])

    def test_model_identity_markers_are_never_dropped_as_run_settings(self):
        for ranked_name, model in (
            ("GPT-5 Codex", {"id": "openai/gpt-5", "name": "GPT-5"}),
            ("Sonar Reasoning Pro", {"id": "perplexity/sonar-pro", "name": "Sonar Pro"}),
            ("Qwen3 Max", {"id": "qwen/qwen3", "name": "Qwen3"}),
            ("GPT-5 (Mini)", {"id": "openai/gpt-5", "name": "GPT-5"}),
            ("Gemini 3 Pro", {"id": "google/gemini-3-pro-preview", "name": "Gemini 3 Pro Preview"}),
        ):
            with self.subTest(name=ranked_name):
                self.assertIsNone(registry.resolve_available_model(ranked_name, [model]))

    def test_refresh_expiry_never_outlives_evidence_age_limit(self):
        spec = dict(SPEC, data_url="https://arcprize.org/data.csv", source_url=SOURCE,
                    date_regex=r"Updated: (\d{4}-\d{2}-\d{2})")
        text = "Rank,Model,Score\n1,Model 1,99\n2,Model 2,98\n3,Model 3,97\n"
        with mock.patch.object(registry, "_fetch_text", side_effect=[text, "Updated: 2026-10-09"]):
            result = registry._refresh_official_csv_category("reasoning", spec, dict(CONFIG, max_evidence_age_days=1), [])
        self.assertEqual(result["valid_until"], "2026-10-11T00:00:00Z")


class ArcAdapterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "registry.json"
        self.config_path = self.root / "sources.json"
        self.spec = dict(SPEC, source_adapter="arc_agi_json",
                         data_url="https://arcprize.org/media/data/leaderboard/v2.json",
                         source_url=SOURCE, dataset_id="v2_Semi_Private")
        self.config_path.write_text(json.dumps(dict(CONFIG, categories={"reasoning": self.spec})))
        patcher = mock.patch.object(registry, "utc_now", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def row(number, score, suffix="", **changes):
        value = {"datasetId": "v2_Semi_Private", "modelId": f"model-{number}{suffix}",
                 "modelDisplayName": f"Model {number}{suffix}", "modelType": "CoT",
                 "modelGroup": f"model-{number}", "modelReleaseDate": "2026-09-01",
                 "providerId": "p", "score": score, "costPerTask": 2.0,
                 "resultsUrl": f"/results/model-{number}", "display": True}
        value.update(changes)
        return value

    def payload(self):
        return {"version": 2, "generatedAt": "2026-10-05T19:34:27.105Z", "datasets": [],
                "evaluations": [self.row(1, 0.8, " (Max)"), self.row(1, 0.7, " (High)"),
                                self.row(2, 0.6), self.row(3, 0.5),
                                self.row(4, 1, modelType="Human"),
                                self.row(5, 1, modelType="Custom"),
                                self.row(6, 1, estimated=True),
                                self.row(7, 1, " Preview"),
                                self.row(8, 1, display=False),
                                self.row(9, 1, datasetId="v1_Semi_Private")]}

    def refresh(self, payload):
        def no_paid_requests(*_args, **_kwargs):
            self.fail("official JSON refresh must not make a paid model request")
        with mock.patch.object(registry, "_fetch_text", return_value=json.dumps(payload)):
            return registry.refresh_registry(no_paid_requests, [catalog_model(n) for n in (1, 2, 3)],
                                             config_path=self.config_path, registry_path=self.path)

    def test_official_arc_json_preserves_best_distinct_variants_without_paid_extraction(self):
        self.refresh(self.payload())
        result = registry.load_registry(self.path)["categories"]["reasoning"]
        self.assertEqual(result["benchmark_date"], "2026-10-05")
        self.assertEqual([row["model_group"] for row in result["ranking"]], ["model-1", "model-2", "model-3"])
        self.assertEqual(result["ranking"][0]["model_name"], "Model 1 (Max)")
        self.assertEqual(result["ranking"][0]["evaluation_settings"], {"reasoning_effort": "max"})
        self.assertEqual(result["ranking"][0]["score"], 80.0)
        self.assertEqual(result["evidence_method"], "approved_official_arc_agi_json")

    def test_failed_refresh_leaves_last_good_bytes_and_expiry_unchanged(self):
        self.path.write_text(json.dumps({"categories": {"reasoning": evidence()}, "registry_hash": "last-good"}))
        original = self.path.read_bytes()
        for value in (None, "2026-10-11T00:00:00Z", "2025-05-06T00:00:00Z"):
            payload = dict(self.payload(), generatedAt=value)
            with self.subTest(date=value), self.assertRaises(registry.BenchmarkRegistryError):
                self.refresh(payload)
            self.assertEqual(self.path.read_bytes(), original)

    def test_future_generated_timestamp_is_rejected_even_on_the_same_day(self):
        with self.assertRaises(registry.BenchmarkRegistryError):
            self.refresh(dict(self.payload(), generatedAt="2026-10-10T13:00:00Z"))

    def test_invalid_arc_scores_cannot_be_published(self):
        for value in (-0.1, 1.1, True, "0.8", None):
            payload = self.payload()
            payload["evaluations"][0]["score"] = value
            with self.subTest(score=value), self.assertRaises(registry.BenchmarkRegistryError):
                self.refresh(payload)


class SelectionEligibilityTests(unittest.TestCase):
    def test_catalog_support_cannot_override_gateway_parameter_contract(self):
        for settings in ({'seed':42}, {'max_tokens':16384}, {'temperature':3.0}):
            with self.subTest(settings=settings):
                self.entry['ranking'][0]['evaluation_settings']=settings
                catalog=[catalog_model(n) for n in (1,2,3)]
                catalog[0]['supported_parameters']=['seed','temperature','max_tokens','reasoning']
                result=self.select(catalog)
                self.assertEqual(result['model']['id'],'p/model-2')

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "registry.json"
        self.config_path = self.root / "sources.json"
        self.config_path.write_text(json.dumps(CONFIG))
        self.entry = evidence()
        patcher = mock.patch.object(registry, "utc_now", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def select(self, catalog, requirements=None):
        self.path.write_text(json.dumps({"categories": {"reasoning": self.entry}, "registry_hash": "proof"}))
        return registry.select_benchmark_model("reasoning", self.path, catalog=catalog,
                                               requirements=requirements, config_path=self.config_path)

    def test_live_catalog_selects_first_eligible_model_for_modalities_context_parameters_and_cost(self):
        catalog = [catalog_model(1, context_length=1000), catalog_model(2), catalog_model(3)]
        result = self.select(catalog, {"input_modalities": ["image"], "input_tokens": 2000,
                                       "max_tokens": 1000, "max_cost_usd": 0.01,
                                       "requested_parameters": ["tools"]})
        self.assertEqual(result["model"]["id"], "p/model-2")
        self.assertEqual(result["score"], 98)
        self.assertEqual(result["selected_rank"], 2)
        self.assertEqual(result["eligibility"]["estimated_max_cost_usd"], 0.004)

    def test_each_unsupported_capability_or_unknown_price_blocks_candidate(self):
        changes = [dict(architecture={"input_modalities": ["text"], "output_modalities": ["image"]}),
                   dict(supported_parameters=[]), dict(supported_parameters=['tools']),
                   dict(pricing={"prompt": "unknown", "completion": "0.01"}),
                   dict(pricing={"prompt": "0.1", "completion": "0.1"}),
                   dict(top_provider={"max_completion_tokens": 2}), dict(context_length=None)]
        for change in changes:
            with self.subTest(change=change):
                result = self.select([catalog_model(1, **change), catalog_model(2), catalog_model(3)],
                                     {"input_tokens": 1000, "max_tokens": 1000, "max_cost_usd": 0.01,
                                      "requested_parameters": ["tools"]})
                self.assertEqual(result["model"]["id"], "p/model-2")

    def test_exact_evaluated_effort_is_required_and_returned_for_execution(self):
        self.entry["ranking"][0].update(model_name="Model 1 (Max)", evaluation_settings={"reasoning_effort": "max"})
        result = self.select([catalog_model(n) for n in (1, 2, 3)])
        self.assertEqual(result["evaluation_settings"], {"reasoning_effort": "max"})
        self.assertEqual(result["execution_parameters"]["reasoning_effort"], "max")
        self.assertEqual(result.get("evaluated_model_name"), "Model 1 (Max)")

    def test_unknown_or_unsupported_evaluated_effort_cannot_inherit_score(self):
        self.entry["ranking"][0].update(model_name="Model 1 (Max)", evaluation_settings={"reasoning_effort": "max"})
        for reasoning in ({}, {"supported_efforts": ["low", "high"]}):
            with self.subTest(reasoning=reasoning):
                result = self.select([catalog_model(1, reasoning=reasoning), catalog_model(2), catalog_model(3)])
                self.assertEqual(result["model"]["id"], "p/model-2")
        result = self.select([catalog_model(1, reasoning={"supported_efforts": None})])
        self.assertEqual(result["model"]["id"], "p/model-1")

    def test_empty_or_conflicting_structured_settings_do_not_erase_variant_label(self):
        for settings in ({}, {"reasoning_effort": "high"}):
            self.entry["ranking"][0].update(model_name="Model 1 (Max)", evaluation_settings=settings)
            with self.subTest(settings=settings):
                result = self.select([catalog_model(1, reasoning={"supported_efforts": ["high"]}), catalog_model(2)])
                self.assertEqual(result["model"]["id"], "p/model-2")

    def test_callers_cannot_override_evaluated_effort_and_retain_score(self):
        self.entry["ranking"][0].update(model_name="Model 1 (Max)", evaluation_settings={"reasoning_effort": "max"})
        result = self.select([catalog_model(n) for n in (1, 2, 3)], {"reasoning_effort": "high"})
        self.assertEqual(result["model"]["id"], "p/model-2")

    def test_removed_ranked_model_is_not_replaced_by_mini(self):
        with self.assertRaises(registry.BenchmarkRegistryError) as error:
            self.select([catalog_model(1, id="p/model-1-mini", name="Model 1 Mini")])
        self.assertEqual(error.exception.status, 422)

    def test_stored_decorated_variant_is_checked_even_without_new_settings_field(self):
        self.entry["ranking"][0]["model_name"] = "Model 1 (Max)"
        result = self.select([catalog_model(1, reasoning={}), catalog_model(2)])
        self.assertEqual(result["model"]["id"], "p/model-2")

    def test_legacy_selection_cannot_bypass_variant_check_when_row_mapping_is_absent(self):
        self.entry["ranking"][0]["model_name"] = "Model 1 (Max)"
        for row in self.entry["ranking"]:
            row.pop("openrouter_model", None)
        self.path.write_text(json.dumps({"categories": {"reasoning": self.entry}}))
        with self.assertRaises(registry.BenchmarkRegistryError):
            registry.select_benchmark_model("reasoning", self.path, config_path=self.config_path)

    def test_legacy_selection_cannot_point_to_a_model_absent_from_evidence(self):
        self.entry["selected_model"] = {"id": "p/model-999"}
        self.path.write_text(json.dumps({"categories": {"reasoning": self.entry}}))
        with self.assertRaises(registry.BenchmarkRegistryError):
            registry.select_benchmark_model("reasoning", self.path, config_path=self.config_path)

    def test_unmapped_evaluation_settings_are_not_silently_discarded(self):
        self.entry["ranking"][0]["model_name"] = "Model 1 (Custom Harness)"
        result = self.select([catalog_model(1), catalog_model(2)])
        self.assertEqual(result["model"]["id"], "p/model-2")

    def test_unknown_request_requirements_are_not_silently_ignored(self):
        with self.assertRaises(registry.BenchmarkRegistryError) as error:
            self.select([catalog_model(1)], {"max_cost_uds": 0.01})
        self.assertEqual(error.exception.status, 400)

    def test_requested_output_limit_is_returned_for_execution(self):
        model = catalog_model(1)
        model['supported_parameters'].append('max_tokens')
        result = self.select([model], {'max_tokens': 100})
        self.assertEqual(result['execution_parameters'].get('max_tokens'), 100)

    def test_named_reasoning_parameter_requires_effort_metadata(self):
        result = self.select([catalog_model(1, reasoning={}), catalog_model(2)],
                             {"requested_parameters": ["reasoning_effort"]})
        self.assertEqual(result["model"]["id"], "p/model-2")

    def test_conflicting_nested_reasoning_cannot_disable_evaluated_effort(self):
        self.entry["ranking"][0].update(model_name="Model 1 (Max)", evaluation_settings={"reasoning_effort": "max"})
        # This setting is not executable by the gateway, even on the fallback.
        with self.assertRaises(registry.BenchmarkRegistryError):
            self.select([catalog_model(1), catalog_model(2)],
                        {"requested_parameters": {"reasoning": {"enabled": False}}})

    def test_evaluated_output_limit_cannot_bypass_requested_cost_and_context_bounds(self):
        self.entry["ranking"][0]["evaluation_settings"] = {"max_tokens": 64000}
        model = catalog_model(1)
        model["supported_parameters"].append("max_tokens")
        result = self.select([model, catalog_model(2)],
                             {"input_tokens": 1000, "max_tokens": 1000, "max_cost_usd": 0.01})
        self.assertEqual(result["model"]["id"], "p/model-2")

    def test_evaluation_values_survive_web_extraction(self):
        rows = evidence()["ranking"]
        rows[0]["evaluation_settings"] = {"temperature": 0.2}
        reply = {"choices": [{"message": {"content": json.dumps(dict(evidence(), ranking=rows)),
                                          "annotations": [{"url_citation": {"url": SOURCE}}]}}]}
        result = registry._refresh_category("reasoning", SPEC, CONFIG, lambda *_a, **_k: reply, [], "2026-10-10")
        self.assertEqual(result["ranking"][0]["evaluation_settings"], {"temperature": 0.2})


if __name__ == "__main__":
    unittest.main()
