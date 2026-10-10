"""Explicit, durable model-task dispatch with conservative restart recovery.

The queue owns dispatch intent, not execution or acceptance. The executor must
use ProjectStore's transactional execution claim and completion APIs. A lease
that expires after dispatch is never permission to repeat a provider request.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from typing import Any, Callable

if __package__:
    from .project_memory import ProjectMemoryError, ProjectStore, redact, utc_now
else:  # The service also supports launching ``python agent/server.py``.
    from project_memory import ProjectMemoryError, ProjectStore, redact, utc_now


_MODEL_CAPABILITIES = {
    "text", "text_generation", "language_model", "llm", "model", "reasoning",
    "coding", "code", "math", "analysis", "long_context",
}
_SAFE_RETRY_CODES = {
    "concurrency_limit", "budget_exhausted", "budget_reserved", "reservation_limit",
    "capacity_exhausted", "capacity_timeout", "provider_capacity",
}
_TERMINAL_PROJECTS = {"completed", "cancelled", "failed"}
_TASK_DEFINITION_FIELDS = (
    "title", "description", "workstream", "inputs_json", "expected_outputs_json",
    "acceptance_criteria_json", "risk_level", "requires_human_approval",
    "capabilities_json", "preferred_models_json", "preferred_tools_json",
    "max_attempts", "timeout_seconds", "estimated_cost_usd", "attempt_count",
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class TaskQueue:
    """Persist opt-in requests and execute them through an injected dispatcher.

    ``validator(task_id, data)`` optionally returns a normalized request at
    enqueue time. ``execute(task_id, data, idempotency_key, expected_version)``
    performs one synchronous, durably accounted execution. Neither callback is
    called while a queue SQLite transaction is held.
    """

    def __init__(
        self,
        store: ProjectStore,
        execute: Callable[[str, dict[str, Any], str, int], dict[str, Any]],
        validator: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        *,
        poll_interval: float = 0.25,
        retry_delay: float = 1.0,
    ) -> None:
        self.store = store
        self.execute = execute
        self.validator = validator
        self.poll_interval = max(0.01, min(30.0, float(poll_interval)))
        self.retry_delay = max(0.1, min(60.0, float(retry_delay)))
        self._stop = threading.Event()
        self._worker_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._last_error_code: str | None = None
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.store.database_path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 5000")
        db.execute("PRAGMA synchronous = FULL")
        return db

    def _initialize(self) -> None:
        with closing(self._connect()) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS task_queue_jobs (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    idempotency_key TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    task_fingerprint TEXT NOT NULL,
                    expected_version INTEGER NOT NULL,
                    initial_status TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    dispatch_version INTEGER,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    execution_id TEXT,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT,
                    UNIQUE(project_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS idx_task_queue_dispatch
                    ON task_queue_jobs(status, available_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_task_queue_project
                    ON task_queue_jobs(project_id, created_at);
                CREATE TRIGGER IF NOT EXISTS task_queue_immutable_request
                BEFORE UPDATE OF project_id, task_id, idempotency_key, request_json,
                    request_fingerprint, task_fingerprint, expected_version, initial_status
                    ON task_queue_jobs
                BEGIN
                    SELECT RAISE(ABORT, 'Queued request is immutable');
                END;
                """
            )

    @staticmethod
    def _request(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ProjectMemoryError("Execution request must be an object")
        try:
            encoded = _json(redact(data))
            if len(encoded.encode("utf-8")) > 1_000_000:
                raise ProjectMemoryError("Execution request is too large", 413, "payload_too_large")
            return json.loads(encoded)
        except (ValueError, TypeError, RecursionError) as exc:
            raise ProjectMemoryError("Execution request must contain finite JSON values") from exc

    @staticmethod
    def _task(db: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ProjectMemoryError("Task not found", 404, "not_found")
        return row

    @staticmethod
    def _dependencies_pending(db: sqlite3.Connection, task_id: str) -> bool:
        return db.execute(
            """SELECT 1 FROM dependencies d JOIN tasks t ON t.id=d.upstream_task_id
               WHERE d.downstream_task_id=? AND t.status!='succeeded' LIMIT 1""",
            (task_id,),
        ).fetchone() is not None

    @staticmethod
    def _task_fingerprint(db: sqlite3.Connection, task: sqlite3.Row) -> str:
        definition = {key: task[key] for key in _TASK_DEFINITION_FIELDS}
        definition["dependencies"] = [
            row[0] for row in db.execute(
                "SELECT upstream_task_id FROM dependencies WHERE downstream_task_id=? ORDER BY upstream_task_id",
                (task["id"],),
            )
        ]
        return _digest(definition)

    @staticmethod
    def _model_task(task: sqlite3.Row, data: dict[str, Any]) -> None:
        capabilities = json.loads(task["capabilities_json"])
        models = json.loads(task["preferred_models_json"])
        requested_models = [data.get("model", "")] + models
        has_manus = any("manus" in str(model).lower() for model in requested_models)
        supported = all(
            str(item).strip().lower().replace("-", "_") in _MODEL_CAPABILITIES
            for item in capabilities
        )
        if (
            data.get("provider", "openrouter") != "openrouter"
            or has_manus
            or not supported
            or json.loads(task["preferred_tools_json"])
            or data.get("tools")
            or data.get("agent_profile")
            or task["assigned_worker"] == "manus"
        ):
            raise ProjectMemoryError(
                "The durable queue supports model text tasks; host tools and Manus require explicit host execution",
                400,
                "unsupported_queue_task",
            )

    def _enqueue_state(
        self, db: sqlite3.Connection, task: sqlite3.Row, data: dict[str, Any], expected_version: int
    ) -> None:
        project = db.execute("SELECT status FROM projects WHERE id=?", (task["project_id"],)).fetchone()
        if project is None or project["status"] not in {"running", "paused"}:
            raise ProjectMemoryError("Project must have an approved plan before enqueueing", 409, "invalid_state")
        if task["version"] != expected_version:
            raise ProjectMemoryError("version does not match current state", 409, "version_conflict")
        blocked_reason = task["blocked_reason"] if "blocked_reason" in task.keys() else None
        waiting = (
            task["status"] == "blocked"
            and not blocked_reason
            and self._dependencies_pending(db, task["id"])
        )
        if task["status"] not in {"ready", "revision_required"} and not waiting:
            raise ProjectMemoryError("Task cannot be enqueued in its current state", 409, "invalid_state")
        if task["attempt_count"] >= task["max_attempts"]:
            raise ProjectMemoryError("Task retry limit has been reached", 409, "retry_limit")
        self._model_task(task, data)
        unresolved = db.execute(
            """SELECT 1 FROM task_queue_jobs WHERE task_id=? AND
               (status IN ('queued','running') OR (status='blocked' AND error_code='execution_outcome_unknown')) LIMIT 1""",
            (task["id"],),
        ).fetchone()
        if unresolved:
            raise ProjectMemoryError("Task already has queued or unresolved work", 409, "queue_conflict")

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        # Do not expose the request, prompt, idempotency key, or lease owner.
        return {key: row[key] for key in (
            "id", "project_id", "task_id", "status", "expected_version", "dispatch_version",
            "attempts", "available_at", "lease_expires_at", "execution_id", "error_code",
            "created_at", "updated_at", "finished_at",
        )}

    def _existing(
        self, db: sqlite3.Connection, project_id: str, task_id: str, key: str,
        fingerprint: str, request: dict[str, Any],
    ) -> sqlite3.Row | None:
        existing = db.execute(
            "SELECT * FROM task_queue_jobs WHERE project_id=? AND idempotency_key=?", (project_id, key)
        ).fetchone()
        if existing is not None and existing["request_fingerprint"] != fingerprint:
            raise ProjectMemoryError("Idempotency-Key was already used for a different queue request", 409, "idempotency_conflict")
        execution = db.execute(
            "SELECT task_id,request_fingerprint FROM executions WHERE project_id=? AND idempotency_key=?", (project_id, key)
        ).fetchone()
        if execution is not None and (
            execution["task_id"] != task_id
            or (existing is None and execution["request_fingerprint"] != self.store._execution_fingerprint(request))
        ):
            raise ProjectMemoryError("Idempotency-Key was already used for another execution request", 409, "idempotency_conflict")
        return existing

    def enqueue(
        self, task_id: str, data: dict[str, Any], idempotency_key: str | None, expected_version: int | None
    ) -> dict[str, Any]:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ProjectMemoryError("Idempotency-Key is required", 400, "idempotency_key_required")
        if len(idempotency_key) > 512:
            raise ProjectMemoryError("Idempotency-Key is too long")
        if expected_version is None:
            raise ProjectMemoryError("version is required", 400, "version_required")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
            raise ProjectMemoryError("version must be a positive integer")
        cleaned = self._request(data)
        fingerprint = _digest({"task_id": task_id, "request": cleaned, "version": expected_version})
        with closing(self._connect()) as db:
            task = self._task(db, task_id)
            existing = self._existing(db, task["project_id"], task_id, idempotency_key, fingerprint, cleaned)
            if existing is not None:
                return self._public(existing)
            self._enqueue_state(db, task, cleaned, expected_version)
        # Validation may consult the catalog, so it happens outside the write lock.
        request = self._request(self.validator(task_id, cleaned) if self.validator else cleaned)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._task(db, task_id)
            existing = self._existing(db, task["project_id"], task_id, idempotency_key, fingerprint, cleaned)
            if existing is not None:
                db.rollback()
                return self._public(existing)
            self._enqueue_state(db, task, request, expected_version)
            now = utc_now()
            job_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO task_queue_jobs(
                    id, project_id, task_id, idempotency_key, request_json, request_fingerprint,
                    task_fingerprint, expected_version, initial_status, available_at, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, task["project_id"], task_id, idempotency_key, _json(request), fingerprint,
                 self._task_fingerprint(db, task), expected_version, task["status"], time.time(), now, now),
            )
            row = db.execute("SELECT * FROM task_queue_jobs WHERE id=?", (job_id,)).fetchone()
            db.commit()
            assert row is not None
            return self._public(row)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM task_queue_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ProjectMemoryError("Queue job not found", 404, "not_found")
            return self._public(row)

    def list_jobs(self, project_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            if db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                raise ProjectMemoryError("Project not found", 404, "not_found")
            return {"jobs": [self._public(row) for row in db.execute(
                "SELECT * FROM task_queue_jobs WHERE project_id=? ORDER BY created_at,id", (project_id,)
            )]}

    @staticmethod
    def _execution(db: sqlite3.Connection, job: sqlite3.Row) -> sqlite3.Row | None:
        return db.execute(
            "SELECT * FROM executions WHERE project_id=? AND task_id=? AND idempotency_key=?",
            (job["project_id"], job["task_id"], job["idempotency_key"]),
        ).fetchone()

    def _execution_outcome(
        self, db: sqlite3.Connection, execution: sqlite3.Row | None, job: sqlite3.Row,
    ) -> tuple[str, str | None]:
        if execution is not None:
            if execution["request_fingerprint"] != self.store._execution_fingerprint(json.loads(job["request_json"])):
                return "blocked", "idempotency_conflict"
            if "billing_status" in execution.keys() and execution["billing_status"] == "unknown":
                return "blocked", "execution_outcome_unknown"
            if execution["status"] == "succeeded":
                task = self._task(db, execution["task_id"])
                if "blocked_reason" in task.keys() and task["blocked_reason"] == "budget_overrun":
                    return "blocked", "budget_overrun"
                return "succeeded", None
            if execution["status"] == "failed":
                if "billing_status" not in execution.keys() or execution["billing_status"] != "unknown":
                    return "failed", "execution_failed"
        return "blocked", "execution_outcome_unknown"

    @staticmethod
    def _finish(
        db: sqlite3.Connection, job: sqlite3.Row, status: str, error_code: str | None = None,
        execution_id: str | None = None, available_at: float | None = None,
    ) -> None:
        now = utc_now()
        db.execute(
            """UPDATE task_queue_jobs SET status=?, error_code=?, execution_id=COALESCE(?,execution_id),
                lease_token=NULL, lease_expires_at=NULL, updated_at=?, finished_at=?,
                available_at=COALESCE(?,available_at)
               WHERE id=? AND status=? AND lease_token IS ?""",
            (status, error_code, execution_id, now, None if status == "queued" else now,
             available_at, job["id"], job["status"], job["lease_token"]),
        )

    def _reconcile(self, db: sqlite3.Connection, now: float) -> None:
        expired = db.execute(
            """SELECT * FROM task_queue_jobs WHERE (status='running' AND lease_expires_at<=?)
                OR (status='blocked' AND error_code='execution_outcome_unknown')
                OR (status='queued' AND EXISTS (
                    SELECT 1 FROM executions e WHERE e.task_id=task_queue_jobs.task_id
                    AND e.idempotency_key=task_queue_jobs.idempotency_key))""", (now,)
        ).fetchall()
        for job in expired:
            execution = self._execution(db, job)
            status, error = self._execution_outcome(db, execution, job)
            project = db.execute("SELECT status FROM projects WHERE id=?", (job["project_id"],)).fetchone()
            if project and project["status"] == "cancelled":
                status = "cancelled"
            if job["status"] == "blocked" and status == "blocked":
                continue
            self._finish(db, job, status, error, execution["id"] if execution else None)
        # Project cancellation cannot leave pending jobs available for a later restart.
        terminal = db.execute(
            """SELECT q.*, p.status AS project_status, t.status AS task_status FROM task_queue_jobs q
               JOIN projects p ON p.id=q.project_id JOIN tasks t ON t.id=q.task_id
               WHERE q.status='queued' AND (p.status IN ('completed','cancelled','failed') OR t.status='cancelled')"""
        ).fetchall()
        for job in terminal:
            status = "cancelled" if "cancelled" in {job["project_status"], job["task_status"]} else "blocked"
            self._finish(db, job, status, "project_not_running")

    def _claim(self) -> sqlite3.Row | None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            self._reconcile(db, now)
            candidates = db.execute(
                """SELECT q.* FROM task_queue_jobs q JOIN projects p ON p.id=q.project_id
                   JOIN tasks t ON t.id=q.task_id WHERE q.status='queued' AND q.available_at<=?
                   AND p.status='running' AND t.status IN ('ready','revision_required')
                   AND NOT EXISTS (
                     SELECT 1 FROM dependencies d JOIN tasks upstream ON upstream.id=d.upstream_task_id
                     WHERE d.downstream_task_id=q.task_id AND upstream.status!='succeeded')
                   ORDER BY q.created_at,q.id LIMIT 32""", (now,)
            ).fetchall()
            for job in candidates:
                task = self._task(db, job["task_id"])
                execution = self._execution(db, job)
                if execution is not None:
                    status, error = self._execution_outcome(db, execution, job)
                    self._finish(db, job, status, error, execution["id"])
                    continue
                unchanged = self._task_fingerprint(db, task) == job["task_fingerprint"]
                dependency_transition = job["initial_status"] in {"blocked", "revision_required"} and task["status"] == "ready"
                version_ok = task["version"] == job["expected_version"] or (
                    dependency_transition and task["version"] == job["expected_version"] + 1
                )
                if not unchanged or not version_ok:
                    self._finish(db, job, "blocked", "version_conflict")
                    continue
                token = str(uuid.uuid4())
                db.execute(
                    """UPDATE task_queue_jobs SET status='running', attempts=attempts+1,
                        dispatch_version=?, lease_token=?, lease_expires_at=?, updated_at=?, error_code=NULL
                       WHERE id=? AND status='queued'""",
                    (task["version"], token, now + task["timeout_seconds"] + 90, utc_now(), job["id"]),
                )
                claimed = db.execute("SELECT * FROM task_queue_jobs WHERE id=?", (job["id"],)).fetchone()
                db.commit()
                return claimed
            db.commit()
            return None

    def _before_dispatch(self, job: sqlite3.Row) -> bool:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM task_queue_jobs WHERE id=?", (job["id"],)).fetchone()
            if current is None or current["status"] != "running" or current["lease_token"] != job["lease_token"]:
                db.rollback()
                return False
            project = db.execute("SELECT status FROM projects WHERE id=?", (job["project_id"],)).fetchone()
            task = self._task(db, job["task_id"])
            status = project["status"] if project else "cancelled"
            if status != "running" or task["status"] == "cancelled" or self._stop.is_set():
                terminal = status in _TERMINAL_PROJECTS or task["status"] == "cancelled"
                self._finish(db, job, "cancelled" if terminal else "queued", "project_not_running", available_at=time.time() + self.retry_delay)
                db.commit()
                return False
            if task["version"] != job["dispatch_version"] or self._dependencies_pending(db, job["task_id"]):
                self._finish(db, job, "blocked", "version_conflict")
                db.commit()
                return False
            db.commit()
            return True

    def _settle(self, job: sqlite3.Row, result: Any = None, error: Exception | None = None) -> None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            execution = self._execution(db, job)
            execution_id = execution["id"] if execution else None
            status, code = self._execution_outcome(db, execution, job)
            available_at = None
            project = db.execute("SELECT status FROM projects WHERE id=?", (job["project_id"],)).fetchone()
            if execution is None and error is not None:
                error_code = getattr(error, "code", None)
                before_dispatch = getattr(error, 'dispatch_started', None) is False
                if (error_code in _SAFE_RETRY_CODES or before_dispatch) and getattr(error, "retryable", False):
                    status, code = "queued", error_code
                    delay = min(60.0, self.retry_delay * (2 ** min(job["attempts"] - 1, 6)))
                    available_at = time.time() + delay
                elif error_code == "invalid_state" and project and project["status"] == "paused":
                    status, code = "queued", "project_not_running"
                    available_at = time.time() + self.retry_delay
                elif error_code in {"version_conflict", "dependencies_blocked", "idempotency_conflict", "retry_limit"}:
                    status, code = "blocked", error_code
                elif isinstance(error, ProjectMemoryError) and error_code in {"invalid_request", "payload_too_large", "unsupported_queue_task"}:
                    status, code = "failed", error_code
                elif before_dispatch:
                    status, code = 'failed', error_code or 'preflight_failed'
            if project and project["status"] == "cancelled":
                status = "cancelled"
            self._finish(db, job, status, code, execution_id, available_at)
            db.commit()

    def run_once(self) -> bool:
        """Process at most one eligible job; return whether one was claimed."""
        if self._stop.is_set():
            return False
        self.store.recover_orphaned_tasks()
        job = self._claim()
        if job is None:
            return False
        if not self._before_dispatch(job):
            return True
        try:
            result = self.execute(
                job["task_id"], json.loads(job["request_json"]), job["idempotency_key"], job["dispatch_version"]
            )
        except Exception as exc:
            self._settle(job, error=exc)
        else:
            self._settle(job, result=result)
        return True

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.run_once()
                self._last_error_code = None
            except Exception:
                # Never log callback messages, which can contain prompts or credentials.
                self._last_error_code = "queue_unavailable"
                worked = False
            if not worked:
                self._stop.wait(self.poll_interval)

    def start(self, workers: int = 1) -> None:
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 4:
            raise ProjectMemoryError("workers must be between 1 and 4")
        with self._worker_lock:
            if any(thread.is_alive() for thread in self._threads):
                return
            self._stop.clear()
            self._threads = [threading.Thread(target=self._worker, name=f"helios-queue-{index}", daemon=True) for index in range(workers)]
            for thread in self._threads:
                thread.start()

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        seconds = float(timeout)
        if not math.isfinite(seconds):
            seconds = 5.0
        deadline = time.monotonic() + max(0.0, min(30.0, seconds))
        for thread in list(self._threads):
            thread.join(max(0.0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self._threads)

    def snapshot(self) -> dict[str, Any]:
        with closing(self._connect()) as db:
            counts = {row["status"]: row["count"] for row in db.execute(
                "SELECT status, COUNT(*) AS count FROM task_queue_jobs GROUP BY status"
            )}
        return {
            "active_workers": sum(thread.is_alive() for thread in self._threads),
            "worker_limit": 4,
            "counts": counts,
            "healthy": self._last_error_code is None,
            "last_error_code": self._last_error_code,
        }
