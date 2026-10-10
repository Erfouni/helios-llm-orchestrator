import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent.project_memory import ProjectMemoryError, ProjectStore

try:
    from agent.task_queue import TaskQueue
except ModuleNotFoundError:
    TaskQueue = None


class TaskQueueTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = ProjectStore(self.root / "helios.db", self.root / "artifacts")
        self.queues = []
        self.calls = []

    def tearDown(self):
        for queue in self.queues:
            queue.stop(timeout=1)
        self.tempdir.cleanup()

    def queue(self, execute=None, **kwargs):
        self.assertIsNotNone(TaskQueue, "Explicit durable task queue is not implemented")
        queue = TaskQueue(kwargs.pop("store", self.store), execute or self.execute, **kwargs)
        self.queues.append(queue)
        return queue

    def project(self, tasks=None, start=True):
        project = self.store.create_project(
            {
                "name": "Queue regression",
                "objective": "Execute only explicitly submitted model work",
                "budget_usd": 100,
                "token_budget": 100_000,
                "max_concurrency": 2,
            },
            "create",
        )
        planned = self.store.plan_project(
            project["id"],
            {
                "tasks": tasks or [
                    {
                        "key": "first",
                        "title": "First",
                        "description": "Create a draft",
                        "acceptance_criteria": ["Draft exists"],
                        "preferred_models": ["provider/model"],
                    }
                ]
            },
            "plan",
            project["version"],
        )
        if start:
            self.store.transition_project(
                project["id"], "start", {}, "start", planned["project"]["version"]
            )
        return self.store.get_project(project["id"]), self.store.list_tasks(project["id"])["tasks"]

    def execute(self, task_id, data, key, version):
        self.calls.append((task_id, data, key, version))
        prepared = self.store.prepare_task_execution(task_id, data, key, version)
        if prepared.get("duplicate"):
            return prepared
        task = self.store.complete_task_execution(
            prepared["execution_id"],
            {"model_used": "provider/model", "answer": "A draft", "usage": {"total_tokens": 10, "cost": 0.01}},
        )
        return {"execution_id": prepared["execution_id"], "task": task}

    def enqueue(self, queue, task, key="queued-1", **data):
        return queue.enqueue(
            task["id"], {"model": "provider/model", "max_tokens": 200, **data}, key, task["version"]
        )

    def change_project(self, project, action):
        current = self.store.get_project(project["id"])
        return self.store.transition_project(project["id"], action, {}, action, current["version"])

    def db_execute(self, query, parameters=()):
        with sqlite3.connect(self.store.database_path) as db:
            db.execute(query, parameters)

    def expire_claim(self, job):
        self.db_execute(
            "UPDATE task_queue_jobs SET status='running', lease_token='old-owner', lease_expires_at=? WHERE id=?",
            (time.time() - 1, job["id"]),
        )

    def test_existing_project_never_runs_without_explicit_enqueue(self):
        project, _ = self.project()
        queue = self.queue()
        self.assertFalse(queue.run_once())
        self.assertEqual(self.calls, [])
        self.assertEqual(queue.list_jobs(project["id"]), {"jobs": []})

    def test_enqueued_request_survives_restart_and_stops_at_verification(self):
        project, (task,) = self.project()
        first = self.queue()
        request = {"model": "provider/model", "max_tokens": 200, "prompt": "Original request"}
        job = first.enqueue(task["id"], request, "queued-1", task["version"])
        request["prompt"] = "Changed after enqueue"
        restarted_store = ProjectStore(self.root / "helios.db", self.root / "artifacts")
        restarted = self.queue(store=restarted_store)
        self.assertTrue(restarted.run_once())
        self.assertEqual(self.calls[0][1]["prompt"], "Original request")
        self.assertEqual(self.store.get_task(task["id"])["status"], "verifying")
        self.assertEqual(restarted.get_job(job["id"])["status"], "succeeded")
        self.assertEqual(len(restarted.list_jobs(project["id"])["jobs"]), 1)
        self.assertFalse(restarted.run_once())

    def test_concurrent_instances_claim_job_once(self):
        _, (task,) = self.project()
        entered = threading.Event()
        release = threading.Event()

        def execute(*args):
            entered.set()
            self.assertTrue(release.wait(2))
            return self.execute(*args)

        first = self.queue(execute)
        second = self.queue(execute)
        self.enqueue(first, task)
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(first.run_once)
            self.assertTrue(entered.wait(2))
            two = pool.submit(second.run_once)
            self.assertFalse(two.result(timeout=2))
            release.set()
            self.assertTrue(one.result(timeout=2))
        self.assertEqual(len(self.calls), 1)
        with sqlite3.connect(self.store.database_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM executions").fetchone()[0], 1)

    def test_paused_job_waits_then_resumes(self):
        project, (task,) = self.project()
        self.change_project(project, "pause")
        queue = self.queue()
        job = self.enqueue(queue, task)
        self.assertFalse(queue.run_once())
        self.assertEqual(self.calls, [])
        self.assertEqual(queue.get_job(job["id"])["status"], "queued")
        self.change_project(project, "resume")
        self.assertTrue(queue.run_once())
        self.assertEqual(len(self.calls), 1)

    def test_cancelled_project_never_dispatches_queued_job(self):
        project, (task,) = self.project()
        queue = self.queue()
        job = self.enqueue(queue, task)
        self.change_project(project, "cancel")
        self.assertFalse(queue.run_once())
        self.assertEqual(self.calls, [])
        self.assertEqual(queue.get_job(job["id"])["status"], "cancelled")

    def test_expired_claim_without_record_is_blocked_never_reissued(self):
        _, (task,) = self.project()
        queue = self.queue()
        job = self.enqueue(queue, task)
        self.expire_claim(job)
        restarted = self.queue()
        self.assertFalse(restarted.run_once())
        recovered = restarted.get_job(job["id"])
        self.assertEqual(recovered["status"], "blocked")
        self.assertEqual(recovered["error_code"], "execution_outcome_unknown")
        self.assertFalse(self.queue().run_once())
        self.assertEqual(self.calls, [])

    def test_expired_claim_with_running_execution_is_not_paid_again(self):
        _, (task,) = self.project()
        queue = self.queue()
        job = self.enqueue(queue, task)
        self.store.prepare_task_execution(
            task["id"], {"model": "provider/model", "max_tokens": 200}, "queued-1", task["version"]
        )
        self.expire_claim(job)
        restarted = self.queue()
        self.assertFalse(restarted.run_once())
        self.assertEqual(restarted.get_job(job["id"])["status"], "blocked")
        self.assertEqual(self.calls, [])

    def test_expired_claim_reconciles_completed_execution(self):
        _, (task,) = self.project()
        queue = self.queue()
        job = self.enqueue(queue, task)
        result = self.execute(task["id"], {"model": "provider/model", "max_tokens": 200}, "queued-1", task["version"])
        self.expire_claim(job)
        restarted = self.queue()
        self.assertFalse(restarted.run_once())
        recovered = restarted.get_job(job["id"])
        self.assertEqual(recovered["status"], "succeeded")
        self.assertEqual(recovered["execution_id"], result["execution_id"])
        self.assertEqual(len(self.calls), 1)

    def test_unexpired_claim_is_not_reset_by_second_instance(self):
        _, (task,) = self.project()
        queue = self.queue()
        job = self.enqueue(queue, task)
        self.db_execute(
            "UPDATE task_queue_jobs SET status='running', lease_token='live-owner', lease_expires_at=? WHERE id=?",
            (time.time() + 120, job["id"]),
        )
        second = self.queue()
        self.assertFalse(second.run_once())
        self.assertEqual(second.get_job(job["id"])["status"], "running")
        self.assertEqual(self.calls, [])

    def test_dependency_waiting_job_becomes_eligible_with_current_version(self):
        _, tasks = self.project(
            tasks=[
                {"key": "a", "title": "First", "description": "Draft"},
                {"key": "b", "title": "Second", "description": "Review", "dependencies": ["a"]},
            ]
        )
        first, second = tasks
        queue = self.queue()
        job = self.enqueue(queue, second)
        self.assertFalse(queue.run_once())
        self.assertEqual(self.calls, [])
        self.execute(first["id"], {"model": "provider/model", "max_tokens": 200}, "upstream", first["version"])
        self.calls.clear()
        # Simulate the store's separately tested evidence-backed verification transition.
        self.db_execute("UPDATE tasks SET status='succeeded', version=version+1 WHERE id=?", (first["id"],))
        self.db_execute("UPDATE tasks SET status='ready', version=version+1 WHERE id=?", (second["id"],))
        self.assertTrue(queue.run_once())
        self.assertEqual(self.calls[0][3], second["version"] + 1)
        self.assertEqual(queue.get_job(job["id"])["status"], "succeeded")

    def test_idempotency_replay_preserves_job_and_different_request_conflicts(self):
        _, (task,) = self.project()
        queue = self.queue()
        job = self.enqueue(queue, task, prompt="First request")
        self.assertEqual(job["id"], self.enqueue(queue, task, prompt="First request")["id"])
        with self.assertRaises(ProjectMemoryError) as error:
            self.enqueue(queue, task, prompt="Other request")
        self.assertEqual(error.exception.code, "idempotency_conflict")

    def test_normalization_runs_once_and_result_is_persisted(self):
        _, (task,) = self.project()
        validations = []

        def validator(task_id, data):
            validations.append(task_id)
            return {**data, "prompt": "Normalized request"}

        queue = self.queue(validator=validator)
        self.enqueue(queue, task)
        self.enqueue(queue, task)
        self.assertTrue(self.queue().run_once())
        self.assertEqual(validations, [task["id"]])
        self.assertEqual(self.calls[0][1]["prompt"], "Normalized request")

    def test_safe_capacity_error_requeues_with_backoff(self):
        _, (task,) = self.project()
        attempts = []

        def execute(*args):
            attempts.append(1)
            if len(attempts) == 1:
                raise ProjectMemoryError("Capacity is occupied", 429, "concurrency_limit", retryable=True)
            return self.execute(*args)

        queue = self.queue(execute, retry_delay=30)
        job = self.enqueue(queue, task)
        self.assertTrue(queue.run_once())
        self.assertEqual(queue.get_job(job["id"])["status"], "queued")
        self.assertFalse(queue.run_once())
        self.assertEqual(len(attempts), 1)
        self.db_execute("UPDATE task_queue_jobs SET available_at=0 WHERE id=?", (job["id"],))
        self.assertTrue(queue.run_once())
        self.assertEqual(queue.get_job(job["id"])["status"], "succeeded")

    def test_unclassified_callback_failure_blocks_without_retry(self):
        _, (task,) = self.project()
        attempts = []

        def execute(*args):
            attempts.append(1)
            raise TimeoutError("A provider may have received the request")

        queue = self.queue(execute)
        job = self.enqueue(queue, task)
        self.assertTrue(queue.run_once())
        self.assertEqual(queue.get_job(job["id"])["status"], "blocked")
        self.assertFalse(queue.run_once())
        self.assertEqual(len(attempts), 1)

    def test_queue_rejects_unapproved_project(self):
        _, (task,) = self.project(start=False)
        queue = self.queue()
        with self.assertRaises(ProjectMemoryError) as error:
            self.enqueue(queue, task)
        self.assertEqual(error.exception.code, "invalid_state")

    def test_queue_rejects_host_tool_tasks(self):
        _, (task,) = self.project(tasks=[{
            "key": "host", "title": "Host", "description": "Write files", "capabilities_required": ["terminal"]
        }])
        with self.assertRaises(ProjectMemoryError) as error:
            self.enqueue(self.queue(), task)
        self.assertEqual(error.exception.code, "unsupported_queue_task")

    def test_queue_rejects_manus(self):
        _, (task,) = self.project()
        with self.assertRaises(ProjectMemoryError) as error:
            self.enqueue(self.queue(), task, provider="manus")
        self.assertEqual(error.exception.code, "unsupported_queue_task")

    def test_task_changed_after_enqueue_does_not_run_under_old_authorization(self):
        _, (task,) = self.project()
        queue = self.queue()
        job = self.enqueue(queue, task)
        self.db_execute("UPDATE tasks SET description='Other scope', version=version+1 WHERE id=?", (task["id"],))
        self.assertFalse(queue.run_once())
        self.assertEqual(queue.get_job(job["id"])["status"], "blocked")
        self.assertEqual(queue.get_job(job["id"])["error_code"], "version_conflict")
        self.assertEqual(self.calls, [])

    def test_job_views_and_persisted_request_do_not_expose_credentials(self):
        project, (task,) = self.project()
        queue = self.queue()
        secret = "sk-" + "test-credential-123456789"
        job = self.enqueue(queue, task, prompt="User-visible prompt", api_key=secret)
        with sqlite3.connect(self.store.database_path) as db:
            request = db.execute("SELECT request_json FROM task_queue_jobs WHERE id=?", (job["id"],)).fetchone()[0]
        self.assertNotIn(secret, request)
        self.assertEqual(json.loads(request)["api_key"], "[REDACTED]")
        for value in (job, queue.get_job(job["id"]), queue.list_jobs(project["id"]), queue.snapshot()):
            exposed = json.dumps(value)
            self.assertNotIn("User-visible prompt", exposed)
            self.assertNotIn("queued-1", exposed)
            self.assertNotIn(secret, exposed)

    def test_bounded_workers_start_and_stop(self):
        _, (task,) = self.project()
        queue = self.queue(poll_interval=0.01)
        job = self.enqueue(queue, task)
        queue.start(workers=2)
        deadline = time.monotonic() + 2
        while queue.get_job(job["id"])["status"] != "succeeded" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(queue.get_job(job["id"])["status"], "succeeded")
        queue.stop(timeout=1)
        self.assertEqual(queue.snapshot()["active_workers"], 0)
        self.assertEqual(len(self.calls), 1)

    def test_successful_output_with_unknown_billing_remains_blocked(self):
        _, (task,) = self.project()

        def execute(task_id, data, key, version):
            prepared = self.store.prepare_task_execution(task_id, data, key, version)
            completed = self.store.complete_task_execution(
                prepared["execution_id"], {"model_used": "provider/model", "answer": "Draft without billing"}
            )
            return {"execution_id": prepared["execution_id"], "task": completed}

        queue = self.queue(execute)
        job = self.enqueue(queue, task)
        self.assertTrue(queue.run_once())
        self.assertEqual(queue.get_job(job["id"])["status"], "blocked")
        self.assertEqual(queue.get_job(job["id"])["error_code"], "execution_outcome_unknown")
        self.assertFalse(queue.run_once())

    def test_queue_rejects_key_already_used_by_another_task_execution(self):
        _, (first, second) = self.project(tasks=[
            {"key": "a", "title": "First", "description": "First"},
            {"key": "b", "title": "Second", "description": "Second"},
        ])
        self.execute(first["id"], {"model": "provider/model", "max_tokens": 200}, "used-key", first["version"])
        with self.assertRaises(ProjectMemoryError) as error:
            self.enqueue(self.queue(), second, key="used-key")
        self.assertEqual(error.exception.code, "idempotency_conflict")

    def test_claimed_cancelled_job_reconciliation_preserves_cancellation(self):
        project, (task,) = self.project()
        queue = self.queue()
        job = self.enqueue(queue, task)
        prepared = self.store.prepare_task_execution(
            task["id"], {"model": "provider/model", "max_tokens": 200}, "queued-1", task["version"]
        )
        self.change_project(project, "cancel")
        self.store.complete_task_execution(
            prepared["execution_id"], {"answer": "Late response", "usage": {"cost": 0.01, "total_tokens": 10}}
        )
        self.expire_claim(job)
        self.assertFalse(queue.run_once())
        self.assertEqual(queue.get_job(job["id"])["status"], "cancelled")
        self.assertEqual(self.store.get_task(task["id"])["status"], "cancelled")

    def test_request_cannot_be_rewritten_in_place(self):
        _, (task,) = self.project()
        queue = self.queue()
        job = self.enqueue(queue, task)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db_execute("UPDATE task_queue_jobs SET request_json='{}' WHERE id=?", (job["id"],))
        self.assertTrue(queue.run_once())

    def test_enqueue_validation_failure_leaves_no_job_or_execution(self):
        project, (task,) = self.project()

        def validator(task_id, data):
            raise ProjectMemoryError("Unsupported model parameter")

        queue = self.queue(validator=validator)
        with self.assertRaises(ProjectMemoryError):
            self.enqueue(queue, task)
        self.assertEqual(queue.list_jobs(project["id"]), {"jobs": []})
        self.assertEqual(self.store.get_task(task["id"])["attempt_count"], 0)

    def test_cancellation_during_validation_prevents_enqueue(self):
        project, (task,) = self.project()

        def validator(task_id, data):
            self.change_project(project, "cancel")
            return data

        queue = self.queue(validator=validator)
        with self.assertRaises(ProjectMemoryError):
            self.enqueue(queue, task)
        self.assertEqual(queue.list_jobs(project["id"]), {"jobs": []})

    def test_capacity_failure_after_execution_claim_never_auto_retries(self):
        _, (task,) = self.project()

        def execute(task_id, data, key, version):
            self.store.prepare_task_execution(task_id, data, key, version)
            raise ProjectMemoryError("Capacity lost", 429, "concurrency_limit", retryable=True)

        queue = self.queue(execute)
        job = self.enqueue(queue, task)
        self.assertTrue(queue.run_once())
        self.assertEqual(queue.get_job(job["id"])["status"], "blocked")
        self.assertFalse(queue.run_once())
        self.assertEqual(self.store.get_task(task["id"])["attempt_count"], 1)

    def test_new_key_cannot_bypass_ambiguous_queue_claim(self):
        _, (task,) = self.project()
        queue = self.queue()
        job = self.enqueue(queue, task)
        self.expire_claim(job)
        self.assertFalse(queue.run_once())
        with self.assertRaises(ProjectMemoryError) as error:
            self.enqueue(queue, task, key="another-key")
        self.assertEqual(error.exception.code, "queue_conflict")

    def test_stop_returns_within_bound_while_execution_is_in_flight(self):
        _, (task,) = self.project()
        entered = threading.Event()
        release = threading.Event()

        def execute(*args):
            entered.set()
            self.assertTrue(release.wait(2))
            return self.execute(*args)

        queue = self.queue(execute, poll_interval=0.01)
        self.enqueue(queue, task)
        queue.start()
        self.assertTrue(entered.wait(2))
        started = time.monotonic()
        self.assertFalse(queue.stop(timeout=0.01))
        self.assertLess(time.monotonic() - started, 0.2)
        release.set()
        self.assertTrue(queue.stop(timeout=1))

    def test_polling_quarantines_expired_execution_lease(self):
        _, (task,) = self.project()
        queue = self.queue()
        job = self.enqueue(queue, task)
        prepared = self.store.prepare_task_execution(
            task["id"], {"model": "provider/model", "max_tokens": 200}, "queued-1", task["version"]
        )
        self.db_execute("UPDATE executions SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?", (prepared["execution_id"],))
        self.expire_claim(job)
        self.assertFalse(queue.run_once())
        self.assertEqual(self.store.get_task(task["id"])["status"], "blocked")
        self.assertEqual(self.store.get_execution(prepared["execution_id"])["status"], "ambiguous")
        self.assertEqual(self.calls, [])

    def test_successful_execution_with_budget_overrun_is_blocked(self):
        _, (task,) = self.project()

        def execute(*args):
            result = self.execute(*args)
            # An accounting overrun is a store-owned reconciliation gate.
            self.db_execute("UPDATE tasks SET status='blocked', blocked_reason='budget_overrun' WHERE id=?", (task["id"],))
            return result

        queue = self.queue(execute)
        job = self.enqueue(queue, task)
        self.assertTrue(queue.run_once())
        self.assertEqual(queue.get_job(job["id"])["status"], "blocked")
        self.assertEqual(queue.get_job(job["id"])["error_code"], "budget_overrun")

    def test_queued_job_reconciles_same_request_finished_outside_worker(self):
        _, (task,) = self.project()
        queue = self.queue()
        job = self.enqueue(queue, task)
        self.execute(task["id"], {"model": "provider/model", "max_tokens": 200}, "queued-1", task["version"])
        self.assertFalse(queue.run_once())
        self.assertEqual(queue.get_job(job["id"])["status"], "succeeded")
        self.assertEqual(len(self.calls), 1)

    def test_queued_job_does_not_adopt_different_request_from_execution_ledger(self):
        _, (task,) = self.project()
        queue = self.queue()
        job = self.enqueue(queue, task, prompt="Queued scope")
        self.execute(task["id"], {"model": "provider/model", "max_tokens": 200, "prompt": "Other scope"}, "queued-1", task["version"])
        self.assertFalse(queue.run_once())
        self.assertEqual(queue.get_job(job["id"])["status"], "blocked")
        self.assertEqual(queue.get_job(job["id"])["error_code"], "idempotency_conflict")
        self.assertEqual(len(self.calls), 1)

    def test_enqueue_rejects_changed_request_for_existing_failed_execution_key(self):
        _, (task,) = self.project()
        prepared = self.store.prepare_task_execution(
            task["id"], {"model": "provider/model", "max_tokens": 200, "prompt": "Original scope"}, "used-key", task["version"]
        )
        ready = self.store.complete_task_execution(
            prepared["execution_id"], None, error={"message": "Admission rejected", "billing_status": "not_charged"}
        )
        with self.assertRaises(ProjectMemoryError) as error:
            self.enqueue(self.queue(), ready, key="used-key", prompt="Changed scope")
        self.assertEqual(error.exception.code, "idempotency_conflict")


if __name__ == "__main__":
    unittest.main()
