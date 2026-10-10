import tempfile
import unittest
from pathlib import Path

from agent.project_memory import ProjectMemoryError, ProjectStore, redact


# Built at runtime so the repository secret scan does not flag a test value.
FAKE_API_KEY = "sk-" + "secret-value-123456789"

class ProjectMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.store = ProjectStore(root / "helios.db", root / "artifacts")

    def tearDown(self):
        self.tempdir.cleanup()

    def create_project(self, key="create-1", **overrides):
        data = {
            "name": "Reference project",
            "objective": "Produce a verified design plan",
            "budget_usd": 20,
            "token_budget": 100_000,
            "max_concurrency": 2,
            **overrides,
        }
        return self.store.create_project(data, key)

    def task_plan(self):
        return {
            "tasks": [
                {
                    "key": "requirements",
                    "workstream": "requirements",
                    "title": "Requirements",
                    "description": "List known requirements and missing inputs.",
                    "acceptance_criteria": ["No fabricated values"],
                    "preferred_models": ["provider/producer"],
                },
                {
                    "key": "review",
                    "workstream": "verification",
                    "title": "Independent review",
                    "description": "Review the requirements.",
                    "dependencies": ["requirements"],
                    "acceptance_criteria": ["All claims checked"],
                    "requires_human_approval": True,
                    "preferred_models": ["provider/reviewer"],
                },
            ]
        }

    def host_evidence(self, task):
        artifact = next(item for item in self.store.list_artifacts(task["project_id"])["artifacts"]
                        if item["id"] == task["result_artifact_id"])
        return {
            "artifact_id": artifact["id"], "checksum_sha256": artifact["checksum_sha256"],
            "checks": [{"criterion": criterion, "passed": True, "details": "Output checked against supplied source facts"}
                       for criterion in task["acceptance_criteria"]],
            "host_check": {"command": "python -m unittest acceptance", "exit_code": 0,
                           "output": "Acceptance checks passed"},
        }

    def test_create_is_idempotent_and_redacts_credentials(self):
        first = self.create_project(source_brief="Bearer abcdefghijklmnop")
        replay = self.create_project(source_brief="Bearer abcdefghijklmnop")
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(first["source_brief"], "[REDACTED]")
        self.assertEqual(first["token_budget"], 100_000)

        with self.assertRaises(ProjectMemoryError) as error:
            self.create_project(key="create-1", objective="different")
        self.assertEqual(error.exception.code, "idempotency_conflict")

    def test_plan_rejects_dependency_cycle(self):
        project = self.create_project()
        plan = {
            "tasks": [
                {"key": "a", "title": "A", "description": "A", "dependencies": ["b"]},
                {"key": "b", "title": "B", "description": "B", "dependencies": ["a"]},
            ]
        }
        with self.assertRaises(ProjectMemoryError) as error:
            self.store.plan_project(project["id"], plan, "plan-cycle", project["version"])
        self.assertEqual(error.exception.code, "dependency_cycle")
        self.assertEqual(self.store.list_tasks(project["id"])["tasks"], [])

    def test_project_lifecycle_persists_usage_artifacts_and_gates(self):
        project = self.create_project()
        planned = self.store.plan_project(
            project["id"], self.task_plan(), "plan-1", project["version"]
        )
        replay = self.store.plan_project(
            project["id"], self.task_plan(), "plan-1", project["version"]
        )
        self.assertEqual(planned, replay)
        self.assertEqual(planned["project"]["status"], "awaiting_plan_approval")
        first, second = planned["tasks"]
        self.assertEqual(first["status"], "ready")
        self.assertEqual(second["status"], "blocked")

        running = self.store.transition_project(
            project["id"],
            "start",
            {"reason": "plan approved"},
            "start-1",
            planned["project"]["version"],
        )
        self.assertEqual(running["status"], "running")

        prepared = self.store.prepare_task_execution(
            first["id"],
            {"model": "provider/producer", "max_tokens": 200},
            "run-1",
            first["version"],
        )
        duplicate = self.store.prepare_task_execution(
            first["id"],
            {"model": "provider/producer", "max_tokens": 200},
            "run-1",
            first["version"],
        )
        self.assertTrue(duplicate["duplicate"])
        completed = self.store.complete_task_execution(
            prepared["execution_id"],
            {
                "model_used": "provider/producer",
                "answer": "Reviewed output",
                "usage": {"total_tokens": 123, "cost": 0.25},
            },
            latency_ms=50,
        )
        self.assertEqual(completed["status"], "verifying")

        verified = self.store.verify_task(
            first["id"],
            {"decision": "pass", "evidence": self.host_evidence(completed)},
            "verify-1",
            completed["version"],
        )
        self.assertEqual(verified["status"], "succeeded")
        second = self.store.get_task(second["id"])
        self.assertEqual(second["status"], "ready")

        prepared_second = self.store.prepare_task_execution(
            second["id"],
            {"model": "provider/reviewer"},
            "run-2",
            second["version"],
        )
        completed_second = self.store.complete_task_execution(
            prepared_second["execution_id"],
            {
                "model_used": "provider/reviewer",
                "answer": "Independent review",
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.10},
            },
        )
        gated = self.store.verify_task(
            second["id"],
            {"decision": "pass", "evidence": self.host_evidence(completed_second)},
            "verify-2",
            completed_second["version"],
        )
        self.assertEqual(gated["status"], "awaiting_approval")
        approved = self.store.approve_task(
            second["id"],
            {"rationale": "human approved"},
            "approve-2",
            gated["version"],
        )
        self.assertEqual(approved["status"], "succeeded")
        self.assertEqual(self.store.get_project(project["id"])["status"], "completed")

        usage = self.store.usage(project["id"])
        self.assertEqual(usage["token_usage"], 273)
        self.assertAlmostEqual(usage["actual_cost_usd"], 0.35)
        artifacts = self.store.list_artifacts(project["id"])["artifacts"]
        self.assertEqual(len(artifacts), 2)
        self.assertTrue(all(len(item["checksum_sha256"]) == 64 for item in artifacts))
        events = self.store.list_events(project["id"])["events"]
        self.assertIn("project.completed", [event["event_type"] for event in events])

    def test_second_store_preserves_running_execution_without_duplicate_call(self):
        project = self.create_project()
        planned = self.store.plan_project(
            project["id"], self.task_plan(), "plan-1", project["version"]
        )
        self.store.transition_project(
            project["id"],
            "start",
            {},
            "start-1",
            planned["project"]["version"],
        )
        task = planned["tasks"][0]
        prepared = self.store.prepare_task_execution(
            task["id"], {"model": "provider/model"}, "run-1", task["version"]
        )

        recovered_store = ProjectStore(
            Path(self.tempdir.name) / "helios.db",
            Path(self.tempdir.name) / "artifacts",
        )
        recovered = recovered_store.get_task(task["id"])
        self.assertEqual(recovered["status"], "running")
        self.assertEqual(recovered_store.recover_orphaned_tasks(), 0)
        duplicate = recovered_store.prepare_task_execution(
            task["id"], {"model": "provider/model"}, "run-1", recovered["version"]
        )
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["execution_id"], prepared["execution_id"])
        self.assertEqual(duplicate["status"], "running")

    def test_redact_preserves_usage_fields_but_removes_secrets(self):
        value = redact(
            {
                "token_budget": 1000,
                "total_tokens": 10,
                "api_key": "secret-value",
                "cookie": "session=value",
            }
        )
        self.assertEqual(value["token_budget"], 1000)
        self.assertEqual(value["total_tokens"], 10)
        self.assertEqual(value["api_key"], "[REDACTED]")
        self.assertEqual(value["cookie"], "[REDACTED]")

    def test_global_context_is_versioned_redacted_and_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "helios.db"
            store = ProjectStore(path)
            empty = store.get_global_context()
            self.assertEqual(empty["version"], 0)
            saved = store.update_global_context(
                {
                    "summary": "Global system state",
                    "state": {"release": "r10", "api_key": FAKE_API_KEY},
                    "scope": "all_chats",
                    "reason": "test",
                },
                "global-test-1",
                0,
            )
            self.assertEqual(saved["version"], 1)
            self.assertEqual(saved["state"]["api_key"], "[REDACTED]")
            reopened = ProjectStore(path).get_global_context()
            self.assertEqual(reopened["version"], 1)
            self.assertEqual(reopened["state"]["release"], "r10")
            duplicate = store.update_global_context(
                {
                    "summary": "Global system state",
                    "state": {"release": "r10", "api_key": FAKE_API_KEY},
                    "scope": "all_chats",
                    "reason": "test",
                },
                "global-test-1",
                0,
            )
            self.assertEqual(duplicate["version"], 1)
            with self.assertRaises(ProjectMemoryError) as error:
                store.update_global_context(
                    {"summary":"changed","state":{},"scope":"all_chats"},
                    "global-test-2",
                    0,
                )
            self.assertEqual(error.exception.code, "version_conflict")


if __name__ == "__main__":
    unittest.main()
