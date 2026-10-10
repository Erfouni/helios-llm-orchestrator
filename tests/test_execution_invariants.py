"""Regression coverage for transactional project execution boundaries."""
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.project_memory import ProjectMemoryError, ProjectStore


class ExecutionInvariantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "helios.db"
        self.store = ProjectStore(self.db_path)
        self.sequence = 0

    def running_project(self, count=1, dependencies=False, **overrides):
        self.sequence += 1
        spec = {
            "name": "Hardening", "objective": "Verified engineering output",
            "scope": "Scoped factory", "constraints": ["Do not fabricate measurements"],
            "budget_usd": 10, "token_budget": 100_000, "max_concurrency": 4,
        }
        spec.update(overrides)
        project = self.store.create_project(spec, f"create-{self.sequence}")
        tasks = [{
            "key": f"task-{i}", "title": f"Task {i}", "description": "Produce work",
            "acceptance_criteria": ["No fabricated values"], "timeout_seconds": 60,
            "dependencies": [f"task-{i-1}"] if dependencies and i else [],
        } for i in range(count)]
        plan = self.store.plan_project(project["id"], {"tasks": tasks}, f"plan-{self.sequence}", project["version"])
        self.store.transition_project(project["id"], "start", {}, f"start-{self.sequence}", plan["project"]["version"])
        return self.store.get_project(project["id"]), plan["tasks"]

    def prepare(self, task, store=None, key=None, **overrides):
        request = {"model": "vendor/producer", "max_tokens": 100, "reservation_cost_usd": 1, "reservation_tokens": 2000}
        request.update(overrides)
        return (store or self.store).prepare_task_execution(task["id"], request, key or f"run-{task['id']}", task["version"])

    def complete(self, prepared, **overrides):
        result = {"model_used": "vendor/producer", "answer": "VERIFIED PREDECESSOR CONTENT", "usage": {"cost": .5, "total_tokens": 120}}
        result.update(overrides)
        return self.store.complete_task_execution(prepared["execution_id"], result)

    def evidence(self, task):
        artifact = next(a for a in self.store.list_artifacts(task["project_id"])["artifacts"] if a["id"] == task["result_artifact_id"])
        return {"artifact_id": artifact["id"], "checksum_sha256": artifact["checksum_sha256"],
                "checks": [{"criterion": c, "passed": True, "details": "Inspected output against supplied facts"} for c in task["acceptance_criteria"]],
                "host_check": {"command": "python -m unittest acceptance", "exit_code": 0, "output": "1 acceptance check passed"}}

    def verify(self, task, **overrides):
        request = {"decision": "pass", "evidence": self.evidence(task)}
        request.update(overrides)
        return self.store.verify_task(task["id"], request, f"verify-{task['id']}", task["version"])

    def cancel(self, project):
        current = self.store.get_project(project["id"])
        return self.store.transition_project(project["id"], "cancel", {}, "cancel-" + project["id"], current["version"])

    def legacy_execution_schema(self):
        """Keep real rows while rebuilding the execution table as release 2.1."""
        fields = ("id", "project_id", "task_id", "request_fingerprint", "idempotency_key", "attempt",
                  "model_requested", "model_used", "status", "latency_ms", "usage_json", "cost_usd", "error", "created_at", "finished_at")
        with sqlite3.connect(self.db_path) as db:
            columns = {row[1]: row for row in db.execute("PRAGMA table_info(executions)")}
            declarations = [name + " " + columns[name][2] + (" PRIMARY KEY" if name == "id" else "") for name in fields]
            db.execute("ALTER TABLE executions RENAME TO previous_executions")
            db.execute("CREATE TABLE executions(" + ",".join(declarations) + ")")
            db.execute("INSERT INTO executions SELECT " + ",".join(fields) + " FROM previous_executions")
            db.execute("DROP TABLE previous_executions")
            db.execute("DROP TABLE project_schema_migrations")

    def test_cancel_racing_success_never_revives_task_and_accounts_once(self):
        project, (task,) = self.running_project()
        prepared = self.prepare(task)
        self.cancel(project)
        first = self.complete(prepared)
        second = self.complete(prepared)
        self.assertEqual(first["status"], "cancelled")
        self.assertEqual(second["status"], "cancelled")
        self.assertEqual(self.store.get_project(project["id"])["status"], "cancelled")
        usage = self.store.usage(project["id"])
        self.assertEqual(usage["actual_cost_usd"], .5)
        self.assertEqual(usage["token_usage"], 120)
        self.assertEqual(len(self.store.list_artifacts(project["id"])["artifacts"]), 1)

    def test_cancel_racing_error_never_revives_task(self):
        project, (task,) = self.running_project()
        prepared = self.prepare(task)
        self.cancel(project)
        done = self.store.complete_task_execution(prepared["execution_id"], None, error="socket disconnected")
        self.assertEqual(done["status"], "cancelled")

    def test_duplicate_completion_cannot_overwrite_verified_task(self):
        project, (task,) = self.running_project()
        prepared = self.prepare(task)
        verified = self.verify(self.complete(prepared))
        duplicate = self.complete(prepared, answer="DUPLICATE")
        self.assertEqual(duplicate["status"], "succeeded")
        self.assertEqual(duplicate["result_artifact_id"], verified["result_artifact_id"])
        self.assertEqual(self.store.usage(project["id"])["actual_cost_usd"], .5)
        self.assertEqual(len(self.store.list_artifacts(project["id"])["artifacts"]), 1)

    def test_invalid_execution_inputs_do_not_claim_or_emit_events(self):
        project, (task,) = self.running_project()
        before = self.store.list_events(project["id"])
        for invalid in ({"prompt": " "}, {"system": 12}, {"max_tokens": 0}, {"max_tokens": float("inf")},
                        {"model": []}, {"reasoning_effort": "unbounded"},
                        {"reservation_cost_usd": float("nan")}, {"reservation_tokens": float("inf")}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ProjectMemoryError):
                    self.prepare(task, **invalid)
                current = self.store.get_task(task["id"])
                self.assertEqual(current["status"], "ready")
                self.assertEqual(current["attempt_count"], 0)
                self.assertEqual(current["version"], task["version"])
                self.assertEqual(self.store.list_events(project["id"]), before)

    def test_preview_validates_and_adds_context_without_claiming(self):
        project, (task,) = self.running_project()
        self.assertTrue(callable(getattr(self.store, "preview_task_execution", None)), "pricing requires a read-only normalized request preview")
        preview = self.store.preview_task_execution(task["id"], {"model": "vendor/producer", "reasoning_effort": "max"})
        self.assertIn("Scoped factory", preview["prompt"])
        self.assertIn("Do not fabricate measurements", preview["prompt"])
        self.assertEqual(preview["reasoning_effort"], "max")
        self.assertEqual(self.store.get_task(task["id"])["status"], "ready")

    def test_project_concurrency_is_atomic_between_stores(self):
        _, tasks = self.running_project(count=2, max_concurrency=1)
        second = ProjectStore(self.db_path)
        barrier = threading.Barrier(2)
        def claim(pair):
            store, task = pair
            barrier.wait()
            try:
                self.prepare(task, store=store)
                return "claimed"
            except ProjectMemoryError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, [(self.store, tasks[0]), (second, tasks[1])]))
        self.assertCountEqual(outcomes, ["claimed", "concurrency_limit"])

    def test_inflight_cost_reservations_are_atomic_between_stores(self):
        _, tasks = self.running_project(count=2, budget_usd=10)
        second = ProjectStore(self.db_path)
        barrier = threading.Barrier(2)
        def claim(pair):
            store, task = pair
            barrier.wait()
            try:
                self.prepare(task, store=store, reservation_cost_usd=6)
                return "claimed"
            except ProjectMemoryError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, [(self.store, tasks[0]), (second, tasks[1])]))
        self.assertCountEqual(outcomes, ["claimed", "budget_exhausted"])

    def test_token_headroom_includes_pending_reservations(self):
        project, tasks = self.running_project(count=2, token_budget=5000)
        self.prepare(tasks[0], reservation_tokens=3500)
        with self.assertRaises(ProjectMemoryError) as caught:
            self.prepare(tasks[1], reservation_tokens=3500)
        self.assertEqual(caught.exception.code, "budget_exhausted")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(self.store.usage(project["id"])["reserved_tokens"], 3500)

    def test_unpriced_request_reserves_remaining_budget(self):
        project, tasks = self.running_project(count=2)
        prepared = self.store.prepare_task_execution(tasks[0]["id"], {"model": "vendor/producer", "max_tokens": 100}, "unpriced", tasks[0]["version"])
        self.assertEqual(prepared.get("reservation_cost_usd"), 10)
        with self.assertRaises(ProjectMemoryError):
            self.prepare(tasks[1])

    def test_unknown_billing_blocks_retries_and_retains_reservation(self):
        project, (task,) = self.running_project()
        prepared = self.prepare(task)
        failed = self.store.complete_task_execution(prepared["execution_id"], None, error="network timeout")
        self.assertEqual(failed["status"], "blocked")
        usage = self.store.usage(project["id"])
        self.assertEqual(usage.get("reserved_cost_usd"), 1)
        self.assertEqual(usage["executions"][0].get("billing_status"), "unknown")
        self.assertIsNone(usage["executions"][0]["cost_usd"])
        self.store.transition_project(project["id"], "pause", {}, "pause", self.store.get_project(project["id"])["version"])
        self.store.transition_project(project["id"], "resume", {}, "resume", self.store.get_project(project["id"])["version"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "blocked")
        with self.assertRaises(ProjectMemoryError):
            self.prepare(self.store.get_task(task["id"]), key="unsafe-retry")

    def test_missing_success_cost_is_unknown_and_not_free(self):
        project, (task,) = self.running_project()
        done = self.complete(self.prepare(task), usage={"total_tokens": 120})
        self.assertEqual(done["status"], "blocked")
        usage = self.store.usage(project["id"])
        self.assertEqual(usage.get("reserved_cost_usd"), 1)
        self.assertIsNone(usage["executions"][0]["cost_usd"])

    def test_known_uncharged_failure_releases_reservations(self):
        project, (task,) = self.running_project()
        prepared = self.prepare(task)
        done = self.store.complete_task_execution(prepared["execution_id"], None, error={"message": "request never sent", "billing_status": "not_charged"})
        self.assertEqual(done["status"], "ready")
        self.assertEqual(self.store.usage(project["id"]).get("reserved_cost_usd"), 0)
        self.assertEqual(self.store.usage(project["id"]).get("reserved_tokens"), 0)

    def test_second_store_never_resets_live_work(self):
        _, (task,) = self.running_project()
        prepared = self.prepare(task)
        second = ProjectStore(self.db_path)
        self.assertEqual(second.get_task(task["id"])["status"], "running")
        self.assertEqual(second.recover_orphaned_tasks(), 0)
        self.assertEqual(second.get_task(task["id"])["status"], "running")
        self.assertIn("deadline_at", prepared)
        self.assertIn("lease_expires_at", prepared)
        deadline = datetime.fromisoformat(prepared["deadline_at"].replace("Z", "+00:00"))
        expiry = datetime.fromisoformat(prepared["lease_expires_at"].replace("Z", "+00:00"))
        self.assertGreaterEqual((expiry - deadline).total_seconds(), 30)

    def test_expired_lease_is_ambiguous_and_cannot_be_resumed(self):
        project, (task,) = self.running_project()
        prepared = self.prepare(task)
        self.assertIn("lease_expires_at", prepared)
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE executions SET lease_expires_at = '2000-01-01T00:00:00.000Z' WHERE id = ?", (prepared["execution_id"],))
        self.assertEqual(self.store.recover_orphaned_tasks(), 1)
        recovered = self.store.get_task(task["id"])
        self.assertEqual(recovered["status"], "blocked")
        self.assertEqual(self.store.usage(project["id"]).get("reserved_cost_usd"), 1)
        self.store.transition_project(project["id"], "pause", {}, "pause", self.store.get_project(project["id"])["version"])
        self.store.transition_project(project["id"], "resume", {}, "resume", self.store.get_project(project["id"])["version"])
        self.assertEqual(self.store.get_task(task["id"])["status"], "blocked")

    def test_verified_predecessor_content_with_provenance_is_injected(self):
        _, tasks = self.running_project(count=2, dependencies=True)
        first = self.verify(self.complete(self.prepare(tasks[0])))
        request = self.prepare(self.store.get_task(tasks[1]["id"]), prompt="Use approved evidence")
        self.assertIn("VERIFIED PREDECESSOR CONTENT", request["prompt"])
        self.assertIn(first["result_artifact_id"], request["prompt"])
        self.assertIn("checksum_sha256", request["prompt"])
        self.assertIn("Use approved evidence", request["prompt"])
        self.assertIn("Scoped factory", request["prompt"])

    def test_global_context_is_opt_in_and_project_scoped(self):
        project, tasks = self.running_project(count=2)
        self.store.update_global_context({"summary": "PRIVATE UNRELATED CHAT", "state": {}, "scope": "all_chats"}, "global", 0)
        request = self.prepare(tasks[0])
        self.assertNotIn("PRIVATE UNRELATED CHAT", request["prompt"])
        with self.assertRaises(ProjectMemoryError):
            self.prepare(tasks[1], include_global_context=True, global_context_scope="all_chats")
        self.store.update_global_context({"summary": "AUTHORIZED PROJECT CONTEXT", "state": {"fact": "approved"}, "scope": "project:" + project["id"]}, "scoped", 1)
        request = self.prepare(tasks[1], include_global_context=True, global_context_scope="project:" + project["id"])
        self.assertIn("AUTHORIZED PROJECT CONTEXT", request["prompt"])
        self.assertIn("global_context", request["prompt"])

    def test_pass_rejects_empty_and_unbound_evidence(self):
        _, (task,) = self.running_project()
        done = self.complete(self.prepare(task))
        for evidence in ([], ["looks good"], {}, {"host_check": {"exit_code": 0}}):
            with self.subTest(evidence=evidence):
                with self.assertRaises(ProjectMemoryError) as caught:
                    self.verify(done, evidence=evidence)
                self.assertEqual(caught.exception.code, "verification_evidence_required")
        self.assertEqual(self.store.get_task(task["id"])["status"], "verifying")

    def test_pass_rejects_wrong_artifact_and_incomplete_checks(self):
        _, (task,) = self.running_project()
        done = self.complete(self.prepare(task))
        for override in ({"artifact_id": "not-real"}, {"checksum_sha256": "0" * 64}, {"checks": []}, {"host_check": {"command": "check", "exit_code": 1, "output": "FAILED"}}):
            evidence = self.evidence(done)
            evidence.update(override)
            with self.subTest(override=override):
                with self.assertRaises(ProjectMemoryError):
                    self.verify(done, evidence=evidence)

    def test_client_actor_and_model_label_do_not_prove_independent_review(self):
        _, (task,) = self.running_project()
        done = self.complete(self.prepare(task))
        evidence = self.evidence(done)
        del evidence["host_check"]
        evidence["independent_review"] = {"model": "other/model", "model_family": "other", "artifact_id": done["result_artifact_id"], "verdict": "pass", "rationale": "Looks good"}
        with self.assertRaises(ProjectMemoryError):
            self.verify(done, actor="trusted-independent-verifier", evidence=evidence)

    def test_model_review_requires_real_artifact_and_different_family(self):
        _, tasks = self.running_project(count=3)
        done = self.complete(self.prepare(tasks[0], model="openai/gpt-5"), model_used="openai/gpt-5")
        target = self.evidence(done)
        review = {"target_artifact_id": target["artifact_id"], "target_checksum_sha256": target["checksum_sha256"],
                  "verdict": "pass", "rationale": "All supplied facts checked", "checks": target["checks"]}
        same_prepared = self.prepare(tasks[1], model="openai/gpt-4.1")
        same = self.complete(same_prepared, model_used="openai/gpt-4.1", answer=json.dumps(review))
        evidence = dict(target)
        del evidence["host_check"]
        evidence["independent_review"] = {"execution_id": same_prepared["execution_id"],
                                          "artifact_id": same["result_artifact_id"],
                                          "checksum_sha256": self.evidence(same)["checksum_sha256"]}
        with self.assertRaises(ProjectMemoryError):
            self.verify(done, evidence=evidence)
        other_prepared = self.prepare(tasks[2], model="anthropic/claude-sonnet-4")
        other = self.complete(other_prepared, model_used="anthropic/claude-sonnet-4", answer=json.dumps(review))
        evidence["independent_review"] = {"execution_id": other_prepared["execution_id"],
                                          "artifact_id": other["result_artifact_id"],
                                          "checksum_sha256": self.evidence(other)["checksum_sha256"]}
        self.assertEqual(self.verify(done, evidence=evidence)["status"], "succeeded")

    def test_public_request_replay_does_not_depend_on_new_internal_quote(self):
        _, (task,) = self.running_project()
        self.assertTrue(callable(getattr(self.store, "replay_task_execution", None)), "public request must replay before live catalog pricing")
        public = {"model": "vendor/model-alias", "max_tokens": 100}
        prepared = self.store.prepare_task_execution(task["id"], {"model": "vendor/resolved-v1", "max_tokens": 100,
            "reservation_cost_usd": 1, "reservation_tokens": 2000}, "stable", task["version"], request_fingerprint_data=public)
        duplicate = self.store.replay_task_execution(task["id"], public, "stable")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["execution_id"], prepared["execution_id"])
        with self.assertRaises(ProjectMemoryError):
            self.store.replay_task_execution(task["id"], {"model": "vendor/other"}, "stable")

    def test_observed_overage_is_accounted_but_blocks_further_dispatch(self):
        project, tasks = self.running_project(count=2)
        done = self.complete(self.prepare(tasks[0]), usage={"cost": 2, "total_tokens": 120})
        self.assertEqual(done["status"], "blocked")
        self.assertEqual(done["blocked_reason"], "budget_overrun")
        self.assertEqual(self.store.get_project(project["id"])["status"], "paused")
        self.assertEqual(self.store.usage(project["id"])["actual_cost_usd"], 2)
        with self.assertRaises(ProjectMemoryError):
            self.prepare(tasks[1])

    def test_zero_cost_reservation_requires_verified_price_boundary(self):
        _, (task,) = self.running_project(budget_usd=0)
        with self.assertRaises(ProjectMemoryError):
            self.prepare(task, reservation_cost_usd=0)
        self.assertIn("reservation_priced", __import__("inspect").signature(self.store.prepare_task_execution).parameters)
        prepared = self.store.prepare_task_execution(task["id"], {"model": "vendor/free", "max_tokens": 100,
            "reservation_cost_usd": 0, "reservation_tokens": 2000}, "free", task["version"], reservation_priced=True)
        self.assertEqual(self.complete(prepared, usage={"cost": 0, "total_tokens": 120})["status"], "verifying")

    def test_unknown_billing_reconciliation_accounts_delta_and_releases_reservation(self):
        project, (task,) = self.running_project()
        prepared = self.prepare(task)
        blocked = self.complete(prepared, usage={"total_tokens": 120})
        self.assertTrue(callable(getattr(self.store, "reconcile_execution", None)), "unknown billing requires a usable reconciliation path")
        data = {"cost_usd": .75, "tokens": 120,
                "billing_evidence": {"source": "provider usage export", "reference": "generation-123", "details": "Settled usage matched execution"}}
        result = self.store.reconcile_execution(prepared["execution_id"], data, "settle", blocked["version"])
        self.assertEqual(result["task"]["status"], "verifying")
        self.assertEqual(result["execution"]["billing_status"], "known")
        self.assertEqual(self.store.usage(project["id"])["token_usage"], 120)
        self.assertEqual(self.store.usage(project["id"])["actual_cost_usd"], .75)
        self.assertEqual(self.store.usage(project["id"])["reserved_cost_usd"], 0)
        self.assertEqual(self.store.reconcile_execution(prepared["execution_id"], data, "settle", blocked["version"]), result)
        with self.assertRaises(ProjectMemoryError):
            self.store.reconcile_execution(prepared["execution_id"], {"cost_usd": 0, "tokens": 0}, "no-evidence")

    def test_reconciliation_never_revives_cancelled_work(self):
        project, (task,) = self.running_project()
        prepared = self.prepare(task)
        self.cancel(project)
        self.store.complete_task_execution(prepared["execution_id"], None, error="disconnected")
        self.assertTrue(callable(getattr(self.store, "reconcile_execution", None)))
        result = self.store.reconcile_execution(prepared["execution_id"], {
            "confirmed_not_charged": True, "retry_authorized": True,
            "billing_evidence": {"source": "provider", "reference": "confirmed-no-generation", "details": "No request was billed"}}, "settle")
        self.assertEqual(result["task"]["status"], "cancelled")
        self.assertEqual(self.store.get_project(project["id"])["status"], "cancelled")
        self.assertEqual(self.store.usage(project["id"])["reserved_cost_usd"], 0)

    def test_reconciled_uncharged_error_retries_only_after_explicit_authorization(self):
        _, (task,) = self.running_project()
        prepared = self.prepare(task)
        self.store.complete_task_execution(prepared["execution_id"], None, error="disconnected")
        self.assertTrue(callable(getattr(self.store, "reconcile_execution", None)))
        result = self.store.reconcile_execution(prepared["execution_id"], {
            "confirmed_not_charged": True, "billing_evidence": {"source": "provider", "reference": "no-charge", "details": "Verified no billing"}}, "settle")
        self.assertEqual(result["task"]["status"], "blocked")
        revised = self.store.request_revision(task["id"], {"reason": "Host authorizes another attempt"}, "revision", result["task"]["version"])
        self.assertEqual(revised["status"], "revision_required")
        self.assertIsNone(revised["blocked_reason"])
        self.assertFalse(self.prepare(revised, key="retry")["duplicate"])

    def test_human_review_gate_cannot_bypass_artifact_evidence(self):
        _, (task,) = self.running_project()
        done = self.complete(self.prepare(task))
        gated = self.store.verify_task(task["id"], {"decision": "human_review_required"}, "human-gate", done["version"])
        with self.assertRaises(ProjectMemoryError) as caught:
            self.store.approve_task(task["id"], {"rationale": "looks good"}, "empty-approve", gated["version"])
        self.assertEqual(caught.exception.code, "verification_evidence_required")
        approved = self.store.approve_task(task["id"], {"rationale": "checked", "evidence": self.evidence(done)}, "checked-approve", gated["version"])
        self.assertEqual(approved["status"], "succeeded")

    def test_uncharged_error_label_cannot_erase_observed_usage(self):
        project, (task,) = self.running_project()
        prepared = self.prepare(task)
        done = self.store.complete_task_execution(prepared["execution_id"], None, error={
            "message": "contradictory provider outcome", "billing_status": "not_charged",
            "usage": {"cost": .25, "total_tokens": 20}})
        self.assertEqual(done["status"], "blocked")
        self.assertEqual(self.store.usage(project["id"])["actual_cost_usd"], .25)
        settled = self.store.reconcile_execution(prepared["execution_id"], {
            "cost_usd": .25, "tokens": 20,
            "billing_evidence": {"source": "provider", "reference": "billing-record", "details": "Confirmed charged failed request"}}, "reconcile")
        self.assertEqual(settled["task"]["blocked_reason"], "reconciled_execution")
        self.assertEqual(self.store.usage(project["id"])["actual_cost_usd"], .25)

    def test_provider_nonfinite_usage_is_retained_as_unknown(self):
        project, (task,) = self.running_project()
        done = self.complete(self.prepare(task), usage={"cost": float("inf"), "total_tokens": float("nan")})
        self.assertEqual(done["status"], "blocked")
        usage = self.store.usage(project["id"])
        self.assertEqual(usage["actual_cost_usd"], 0)
        self.assertEqual(usage["token_usage"], 0)
        self.assertEqual(usage["reserved_cost_usd"], 1)
        self.assertIsNone(usage["executions"][0]["usage"]["cost"])
        self.assertIsNone(usage["executions"][0]["usage"]["total_tokens"])

    def test_artifact_corruption_prevents_dispatch_before_claim(self):
        project, tasks = self.running_project(count=2, dependencies=True)
        done = self.verify(self.complete(self.prepare(tasks[0])))
        artifact = next(a for a in self.store.list_artifacts(project["id"])["artifacts"] if a["id"] == done["result_artifact_id"])
        (self.store.artifact_root / artifact["path"]).write_text("CHANGED OUTSIDE REVIEW")
        with self.assertRaises(ProjectMemoryError) as caught:
            self.prepare(self.store.get_task(tasks[1]["id"]))
        self.assertEqual(caught.exception.code, "artifact_integrity_error")
        self.assertEqual(self.store.get_task(tasks[1]["id"])["status"], "ready")

    def test_late_expired_success_requires_reconciliation_to_verify(self):
        _, (task,) = self.running_project()
        prepared = self.prepare(task)
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE executions SET lease_expires_at = '2000-01-01T00:00:00.000Z' WHERE id = ?", (prepared["execution_id"],))
        self.store.recover_orphaned_tasks()
        done = self.complete(prepared)
        self.assertEqual(done["status"], "blocked")
        result = self.store.reconcile_execution(prepared["execution_id"], {
            "cost_usd": .5, "tokens": 120,
            "billing_evidence": {"source": "provider", "reference": "late-outcome", "details": "Confirmed late successful response"}}, "late-reconcile")
        self.assertEqual(result["task"]["status"], "verifying")
        self.assertIsNotNone(result["task"]["result_artifact_id"])

    def test_additive_migration_preserves_live_legacy_tasks_and_reserves_unknown_cost(self):
        project, (task,) = self.running_project()
        prepared = self.prepare(task)
        before = self.store.get_task(task["id"])
        self.legacy_execution_schema()
        migrated = ProjectStore(self.db_path)
        self.assertEqual(migrated.get_task(task["id"]), before)
        self.assertEqual(migrated.usage(project["id"])["reserved_cost_usd"], 10)
        self.assertEqual(migrated.find_task_execution(task["id"], "run-" + task["id"])["id"], prepared["execution_id"])
        self.assertEqual(migrated.recover_orphaned_tasks(), 1)
        self.assertEqual(migrated.get_task(task["id"])["status"], "blocked")

    def test_additive_migration_retains_unresolved_legacy_billing(self):
        project, (task,) = self.running_project()
        prepared = self.prepare(task)
        self.complete(prepared, usage={"total_tokens": 120})
        self.legacy_execution_schema()
        # Prior releases represented missing provider billing as numeric zero.
        migrated = ProjectStore(self.db_path)
        usage = migrated.usage(project["id"])
        self.assertEqual(usage["reserved_cost_usd"], 10)
        self.assertIsNone(usage["executions"][0]["cost_usd"])
        self.assertEqual(migrated.get_task(task["id"])["status"], "blocked")

    def test_terminal_project_fences_review_and_revision(self):
        project, (task,) = self.running_project()
        done = self.complete(self.prepare(task))
        self.cancel(project)
        cancelled = self.store.get_task(task["id"])
        with self.assertRaises(ProjectMemoryError):
            self.store.verify_task(task["id"], {"decision": "pass", "evidence": self.evidence(done)}, "late-verify", cancelled["version"])
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE tasks SET status = 'awaiting_approval' WHERE id = ?", (task["id"],))
        with self.assertRaises(ProjectMemoryError):
            self.store.approve_task(task["id"], {}, "late-approval", cancelled["version"])
        with self.assertRaises(ProjectMemoryError):
            self.store.request_revision(task["id"], {}, "late-revision", cancelled["version"])
        self.assertEqual(self.store.get_project(project["id"])["status"], "cancelled")

    def test_completion_helper_does_not_change_terminal_project(self):
        project, (task,) = self.running_project()
        self.cancel(project)
        with self.store._connect() as db:
            db.execute("UPDATE tasks SET status = 'succeeded' WHERE id = ?", (task["id"],))
            self.store._maybe_complete_project(db, project["id"])
        self.assertEqual(self.store.get_project(project["id"])["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
