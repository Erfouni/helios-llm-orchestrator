"""Durable project memory for Helios.

The store deliberately contains only user-visible project state. Credentials,
hidden prompts, and provider request headers must never be passed into it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


PROJECT_STATUSES = {
    "draft",
    "planning",
    "awaiting_plan_approval",
    "running",
    "paused",
    "blocked",
    "completed",
    "failed",
    "cancelled",
}
TASK_STATUSES = {
    "draft",
    "blocked",
    "ready",
    "running",
    "verifying",
    "awaiting_approval",
    "succeeded",
    "revision_required",
    "failed",
    "cancelled",
}
TERMINAL_PROJECT_STATUSES = {"completed", "failed", "cancelled"}
TERMINAL_TASK_STATUSES = {"succeeded", "failed", "cancelled"}
SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|bearer|cookie|credential|password|secret|token)",
    re.IGNORECASE,
)
SAFE_TOKEN_KEYS = {
    "token_budget",
    "token_usage",
    "max_tokens",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "tokens",
}
SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]{12,}|(?:sk|sess|token)[-_][A-Za-z0-9._-]{12,})"
)


class ProjectMemoryError(Exception):
    def __init__(
        self,
        message: str,
        status: int = 400,
        code: str = "invalid_request",
        details: Any = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details
        self.retryable = retryable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def redact(value: Any) -> Any:
    """Return a JSON-compatible copy with obvious credentials removed."""
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if (
                    SENSITIVE_KEY_RE.search(str(key))
                    and str(key).lower() not in SAFE_TOKEN_KEYS
                )
                else redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return SENSITIVE_VALUE_RE.sub("[REDACTED]", value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _required_text(data: dict[str, Any], name: str, max_length: int = 200_000) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ProjectMemoryError(f"{name} must be a non-empty string")
    value = value.strip()
    if len(value) > max_length:
        raise ProjectMemoryError(f"{name} is too large", 413, "payload_too_large")
    return redact(value)


def _optional_text(data: dict[str, Any], name: str, max_length: int = 200_000) -> str:
    value = data.get(name, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProjectMemoryError(f"{name} must be a string")
    if len(value) > max_length:
        raise ProjectMemoryError(f"{name} is too large", 413, "payload_too_large")
    return redact(value.strip())


def _number(
    data: dict[str, Any],
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    value = data.get(name, default)
    if isinstance(value, bool):
        raise ProjectMemoryError(f"{name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ProjectMemoryError(f"{name} must be a number") from exc
    if not minimum <= parsed <= maximum:
        raise ProjectMemoryError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _integer(
    data: dict[str, Any],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = data.get(name, default)
    if isinstance(value, bool):
        raise ProjectMemoryError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ProjectMemoryError(f"{name} must be an integer") from exc
    if str(parsed) != str(value).strip() and not isinstance(value, int):
        raise ProjectMemoryError(f"{name} must be an integer")
    if not minimum <= parsed <= maximum:
        raise ProjectMemoryError(f"{name} must be between {minimum} and {maximum}")
    return parsed


class ProjectStore:
    def __init__(self, database_path: Path, artifact_root: Path | None = None) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.artifact_root = (
            Path(artifact_root).expanduser().resolve()
            if artifact_root is not None
            else self.database_path.parent / "artifacts"
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._initialize()
        self.recover_orphaned_tasks()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._schema_lock, closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = FULL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT '',
                    constraints_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    budget_usd REAL NOT NULL,
                    token_budget INTEGER NOT NULL,
                    deadline TEXT,
                    max_concurrency INTEGER NOT NULL,
                    source_brief TEXT NOT NULL,
                    assumptions_json TEXT NOT NULL DEFAULT '[]',
                    success_criteria_json TEXT NOT NULL DEFAULT '[]',
                    actual_cost_usd REAL NOT NULL DEFAULT 0,
                    token_usage INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    position INTEGER NOT NULL DEFAULT 0,
                    workstream TEXT NOT NULL DEFAULT 'general',
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    inputs_json TEXT NOT NULL DEFAULT '[]',
                    expected_outputs_json TEXT NOT NULL DEFAULT '[]',
                    acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
                    risk_level TEXT NOT NULL DEFAULT 'low',
                    requires_human_approval INTEGER NOT NULL DEFAULT 0,
                    capabilities_json TEXT NOT NULL DEFAULT '[]',
                    preferred_models_json TEXT NOT NULL DEFAULT '[]',
                    preferred_tools_json TEXT NOT NULL DEFAULT '[]',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 2,
                    timeout_seconds INTEGER NOT NULL DEFAULT 180,
                    estimated_cost_usd REAL NOT NULL DEFAULT 0,
                    actual_cost_usd REAL NOT NULL DEFAULT 0,
                    token_usage INTEGER NOT NULL DEFAULT 0,
                    assigned_worker TEXT,
                    model_used TEXT,
                    result_artifact_id TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS dependencies (
                    upstream_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    downstream_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    dependency_type TEXT NOT NULL DEFAULT 'finish_to_start',
                    PRIMARY KEY (upstream_task_id, downstream_task_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_project_id ON events(project_id, id);
                CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project_id, status);
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    producer_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    checksum_sha256 TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    provenance_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS executions (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    request_fingerprint TEXT NOT NULL,
                    idempotency_key TEXT,
                    attempt INTEGER NOT NULL,
                    model_requested TEXT,
                    model_used TEXT,
                    status TEXT NOT NULL,
                    latency_ms INTEGER,
                    usage_json TEXT NOT NULL DEFAULT '{}',
                    cost_usd REAL NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_idempotency
                    ON executions(project_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS idempotency (
                    scope TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (scope, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS global_context (
                    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
                    summary TEXT NOT NULL DEFAULT '',
                    state_json TEXT NOT NULL DEFAULT '{}',
                    scope TEXT NOT NULL DEFAULT 'all_chats',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS global_context_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version INTEGER NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    state_json TEXT NOT NULL DEFAULT '{}',
                    scope TEXT NOT NULL DEFAULT 'all_chats',
                    actor TEXT NOT NULL DEFAULT 'user',
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _event(
        db: sqlite3.Connection,
        project_id: str,
        event_type: str,
        payload: Any,
        task_id: str | None = None,
        actor: str = "helios",
    ) -> None:
        db.execute(
            """
            INSERT INTO events(project_id, task_id, event_type, actor, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, task_id, event_type, actor, _json(redact(payload)), utc_now()),
        )

    @staticmethod
    def _fingerprint(value: Any) -> str:
        return hashlib.sha256(_json(redact(value)).encode("utf-8")).hexdigest()

    def _idempotent(
        self,
        db: sqlite3.Connection,
        scope: str,
        key: str | None,
        request: Any,
        operation: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        if not key:
            raise ProjectMemoryError(
                "Idempotency-Key is required",
                400,
                "idempotency_key_required",
            )
        fingerprint = self._fingerprint(request)
        existing = db.execute(
            "SELECT fingerprint, response_json FROM idempotency WHERE scope = ? AND idempotency_key = ?",
            (scope, key),
        ).fetchone()
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise ProjectMemoryError(
                    "Idempotency-Key was already used for a different request",
                    409,
                    "idempotency_conflict",
                )
            return _loads(existing["response_json"], {})
        response = operation()
        db.execute(
            """
            INSERT INTO idempotency(scope, idempotency_key, fingerprint, response_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (scope, key, fingerprint, _json(response), utc_now()),
        )
        return response

    def _existing_idempotent(
        self,
        db: sqlite3.Connection,
        scope: str,
        key: str | None,
        request: Any,
    ) -> dict[str, Any] | None:
        if not key:
            return None
        fingerprint = self._fingerprint(request)
        existing = db.execute(
            "SELECT fingerprint, response_json FROM idempotency WHERE scope = ? AND idempotency_key = ?",
            (scope, key),
        ).fetchone()
        if existing is None:
            return None
        if existing["fingerprint"] != fingerprint:
            raise ProjectMemoryError(
                "Idempotency-Key was already used for a different request",
                409,
                "idempotency_conflict",
            )
        return _loads(existing["response_json"], {})

    @staticmethod
    def _global_context_dict(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {
                "scope": "all_chats",
                "summary": "",
                "state": {},
                "created_at": None,
                "updated_at": None,
                "version": 0,
            }
        return {
            "scope": row["scope"],
            "summary": row["summary"],
            "state": _loads(row["state_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "version": row["version"],
        }

    def get_global_context(self) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM global_context WHERE singleton_id = 1"
            ).fetchone()
            return self._global_context_dict(row)

    def update_global_context(
        self,
        data: dict[str, Any],
        idempotency_key: str | None,
        expected_version: int | None,
    ) -> dict[str, Any]:
        cleaned = redact(data)
        state = cleaned.get("state")
        if not isinstance(state, dict):
            raise ProjectMemoryError("state must be an object")
        summary = _optional_text(cleaned, "summary", 12_000)
        scope = str(cleaned.get("scope", "all_chats")).strip() or "all_chats"
        if scope != "all_chats":
            raise ProjectMemoryError("scope must be all_chats")
        actor = str(cleaned.get("actor", "user"))[:200]
        reason = str(cleaned.get("reason", ""))[:2000]
        serialized = _json(state)
        if len(serialized) > 300_000:
            raise ProjectMemoryError("state is too large", 413, "payload_too_large")

        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            scope_name = "update_global_context"
            existing = self._existing_idempotent(
                db, scope_name, idempotency_key, cleaned
            )
            if existing is not None:
                db.rollback()
                return existing
            row = db.execute(
                "SELECT * FROM global_context WHERE singleton_id = 1"
            ).fetchone()
            current_version = int(row["version"]) if row is not None else 0
            if expected_version is None:
                raise ProjectMemoryError(
                    "version is required for state-changing requests",
                    400,
                    "version_required",
                )
            if int(expected_version) != current_version:
                raise ProjectMemoryError(
                    "version does not match current global context",
                    409,
                    "version_conflict",
                    {"current_version": current_version},
                )

            def operation() -> dict[str, Any]:
                now = utc_now()
                next_version = current_version + 1
                if row is None:
                    db.execute(
                        """
                        INSERT INTO global_context(
                            singleton_id, summary, state_json, scope, created_at, updated_at, version
                        ) VALUES (1, ?, ?, ?, ?, ?, ?)
                        """,
                        (summary, serialized, scope, now, now, next_version),
                    )
                else:
                    db.execute(
                        """
                        UPDATE global_context
                        SET summary = ?, state_json = ?, scope = ?, updated_at = ?, version = ?
                        WHERE singleton_id = 1
                        """,
                        (summary, serialized, scope, now, next_version),
                    )
                db.execute(
                    """
                    INSERT INTO global_context_history(
                        version, summary, state_json, scope, actor, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (next_version, summary, serialized, scope, actor, reason, now),
                )
                updated = db.execute(
                    "SELECT * FROM global_context WHERE singleton_id = 1"
                ).fetchone()
                assert updated is not None
                return self._global_context_dict(updated)

            result = self._idempotent(
                db, scope_name, idempotency_key, cleaned, operation
            )
            db.commit()
            return result

    @staticmethod
    def _project_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "objective": row["objective"],
            "scope": row["scope"],
            "constraints": _loads(row["constraints_json"], []),
            "status": row["status"],
            "budget_usd": row["budget_usd"],
            "token_budget": row["token_budget"],
            "deadline": row["deadline"],
            "max_concurrency": row["max_concurrency"],
            "source_brief": row["source_brief"],
            "assumptions": _loads(row["assumptions_json"], []),
            "success_criteria": _loads(row["success_criteria_json"], []),
            "actual_cost_usd": row["actual_cost_usd"],
            "token_usage": row["token_usage"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "version": row["version"],
        }

    def _task_dict(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        dependencies = [
            item["upstream_task_id"]
            for item in db.execute(
                "SELECT upstream_task_id FROM dependencies WHERE downstream_task_id = ? ORDER BY upstream_task_id",
                (row["id"],),
            )
        ]
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "position": row["position"],
            "workstream": row["workstream"],
            "title": row["title"],
            "description": row["description"],
            "status": row["status"],
            "dependencies": dependencies,
            "inputs": _loads(row["inputs_json"], []),
            "expected_outputs": _loads(row["expected_outputs_json"], []),
            "acceptance_criteria": _loads(row["acceptance_criteria_json"], []),
            "risk_level": row["risk_level"],
            "requires_human_approval": bool(row["requires_human_approval"]),
            "capabilities_required": _loads(row["capabilities_json"], []),
            "preferred_models": _loads(row["preferred_models_json"], []),
            "preferred_tools": _loads(row["preferred_tools_json"], []),
            "attempt_count": row["attempt_count"],
            "max_attempts": row["max_attempts"],
            "timeout_seconds": row["timeout_seconds"],
            "estimated_cost_usd": row["estimated_cost_usd"],
            "actual_cost_usd": row["actual_cost_usd"],
            "token_usage": row["token_usage"],
            "assigned_worker": row["assigned_worker"],
            "model_used": row["model_used"],
            "result_artifact_id": row["result_artifact_id"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "version": row["version"],
        }

    @staticmethod
    def _expect_version(row: sqlite3.Row, expected_version: int | None) -> None:
        if expected_version is None:
            raise ProjectMemoryError(
                "version is required for state-changing requests",
                400,
                "version_required",
            )
        if int(row["version"]) != int(expected_version):
            raise ProjectMemoryError(
                "version does not match current state",
                409,
                "version_conflict",
                {"current_version": row["version"]},
            )

    def create_project(self, data: dict[str, Any], idempotency_key: str | None) -> dict[str, Any]:
        cleaned = redact(data)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")

            def operation() -> dict[str, Any]:
                project_id = str(uuid.uuid4())
                now = utc_now()
                name = _required_text(cleaned, "name", 300)
                objective = _required_text(cleaned, "objective")
                source_brief = _optional_text(cleaned, "source_brief") or objective
                constraints = cleaned.get("constraints", [])
                assumptions = cleaned.get("assumptions", [])
                success_criteria = cleaned.get("success_criteria", [])
                for field, value in (
                    ("constraints", constraints),
                    ("assumptions", assumptions),
                    ("success_criteria", success_criteria),
                ):
                    if not isinstance(value, list):
                        raise ProjectMemoryError(f"{field} must be an array")
                budget_usd = _number(cleaned, "budget_usd", 25.0, 0.0, 1_000_000.0)
                token_budget = _integer(
                    cleaned, "token_budget", 500_000, 1_000, 2_000_000_000
                )
                max_concurrency = _integer(cleaned, "max_concurrency", 4, 1, 16)
                deadline = cleaned.get("deadline")
                if deadline is not None and not isinstance(deadline, str):
                    raise ProjectMemoryError("deadline must be an ISO 8601 string or null")
                db.execute(
                    """
                    INSERT INTO projects(
                        id, name, objective, scope, constraints_json, status,
                        budget_usd, token_budget, deadline, max_concurrency,
                        source_brief, assumptions_json, success_criteria_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        name,
                        objective,
                        _optional_text(cleaned, "scope"),
                        _json(constraints),
                        budget_usd,
                        token_budget,
                        deadline,
                        max_concurrency,
                        source_brief,
                        _json(assumptions),
                        _json(success_criteria),
                        now,
                        now,
                    ),
                )
                self._event(db, project_id, "project.created", {"name": name})
                row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
                assert row is not None
                return self._project_dict(row)

            result = self._idempotent(
                db, "create_project", idempotency_key, cleaned, operation
            )
            db.commit()
            return result

    def list_projects(self, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM projects ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return {"projects": [self._project_dict(row) for row in rows]}

    def get_project(self, project_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if row is None:
                raise ProjectMemoryError("Project not found", 404, "not_found")
            return self._project_dict(row)

    def plan_project(
        self,
        project_id: str,
        data: dict[str, Any],
        idempotency_key: str | None,
        expected_version: int | None,
    ) -> dict[str, Any]:
        cleaned = redact(data)
        tasks = cleaned.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ProjectMemoryError("tasks must be a non-empty array")
        if len(tasks) > 200:
            raise ProjectMemoryError("tasks may contain at most 200 items")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            scope_name = f"plan_project:{project_id}"
            existing = self._existing_idempotent(
                db, scope_name, idempotency_key, cleaned
            )
            if existing is not None:
                db.rollback()
                return existing
            project = db.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise ProjectMemoryError("Project not found", 404, "not_found")
            self._expect_version(project, expected_version)
            if project["status"] not in {"draft", "planning", "awaiting_plan_approval"}:
                raise ProjectMemoryError(
                    "Project cannot be planned in its current state",
                    409,
                    "invalid_state",
                    {"status": project["status"]},
                )

            def operation() -> dict[str, Any]:
                db.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))
                aliases: dict[str, str] = {}
                normalized: list[tuple[dict[str, Any], str]] = []
                now = utc_now()
                for index, item in enumerate(tasks):
                    if not isinstance(item, dict):
                        raise ProjectMemoryError(f"tasks[{index}] must be an object")
                    alias = str(item.get("key") or item.get("id") or f"task-{index + 1}").strip()
                    if not alias or alias in aliases:
                        raise ProjectMemoryError(f"tasks[{index}].key must be unique")
                    task_id = str(uuid.uuid4())
                    aliases[alias] = task_id
                    normalized.append((item, task_id))
                    risk_level = str(item.get("risk_level", "low")).lower()
                    if risk_level not in {"low", "medium", "high", "critical"}:
                        raise ProjectMemoryError(f"tasks[{index}].risk_level is invalid")
                    requires_approval = bool(
                        item.get("requires_human_approval", risk_level in {"high", "critical"})
                    )
                    list_fields = {
                        "inputs": item.get("inputs", []),
                        "expected_outputs": item.get("expected_outputs", []),
                        "acceptance_criteria": item.get("acceptance_criteria", []),
                        "capabilities_required": item.get("capabilities_required", []),
                        "preferred_models": item.get("preferred_models", []),
                        "preferred_tools": item.get("preferred_tools", []),
                    }
                    if any(not isinstance(value, list) for value in list_fields.values()):
                        raise ProjectMemoryError(f"tasks[{index}] list fields must be arrays")
                    db.execute(
                        """
                        INSERT INTO tasks(
                            id, project_id, position, workstream, title, description, status,
                            inputs_json, expected_outputs_json, acceptance_criteria_json,
                            risk_level, requires_human_approval, capabilities_json,
                            preferred_models_json, preferred_tools_json, max_attempts,
                            timeout_seconds, estimated_cost_usd, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'blocked', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            task_id,
                            project_id,
                            index,
                            str(redact(item.get("workstream", "general")))[:300],
                            _required_text(item, "title", 500),
                            _required_text(item, "description"),
                            _json(list_fields["inputs"]),
                            _json(list_fields["expected_outputs"]),
                            _json(list_fields["acceptance_criteria"]),
                            risk_level,
                            int(requires_approval),
                            _json(list_fields["capabilities_required"]),
                            _json(list_fields["preferred_models"]),
                            _json(list_fields["preferred_tools"]),
                            _integer(item, "max_attempts", 2, 1, 10),
                            _integer(item, "timeout_seconds", 180, 10, 3600),
                            _number(item, "estimated_cost_usd", 0.0, 0.0, 100_000.0),
                            now,
                            now,
                        ),
                    )
                graph: dict[str, set[str]] = {task_id: set() for task_id in aliases.values()}
                for index, (item, task_id) in enumerate(normalized):
                    dependencies = item.get("dependencies", [])
                    if not isinstance(dependencies, list):
                        raise ProjectMemoryError(f"tasks[{index}].dependencies must be an array")
                    for alias in dependencies:
                        upstream_id = aliases.get(str(alias))
                        if upstream_id is None:
                            raise ProjectMemoryError(
                                f"tasks[{index}] references unknown dependency: {alias}"
                            )
                        if upstream_id == task_id:
                            raise ProjectMemoryError("A task cannot depend on itself")
                        graph[upstream_id].add(task_id)
                        db.execute(
                            """
                            INSERT INTO dependencies(upstream_task_id, downstream_task_id)
                            VALUES (?, ?)
                            """,
                            (upstream_id, task_id),
                        )
                self._validate_acyclic(graph)
                db.execute(
                    """
                    UPDATE tasks
                    SET status = CASE
                        WHEN EXISTS(
                            SELECT 1 FROM dependencies d WHERE d.downstream_task_id = tasks.id
                        ) THEN 'blocked' ELSE 'ready' END,
                        version = version + 1,
                        updated_at = ?
                    WHERE project_id = ?
                    """,
                    (now, project_id),
                )
                db.execute(
                    """
                    UPDATE projects
                    SET status = 'awaiting_plan_approval', updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (now, project_id),
                )
                self._event(
                    db,
                    project_id,
                    "project.plan_created",
                    {"task_count": len(tasks), "aliases": aliases},
                )
                row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
                assert row is not None
                return {
                    "project": self._project_dict(row),
                    "tasks": self._list_tasks_with_db(db, project_id),
                }

            result = self._idempotent(
                db, scope_name, idempotency_key, cleaned, operation
            )
            db.commit()
            return result

    @staticmethod
    def _validate_acyclic(graph: dict[str, set[str]]) -> None:
        indegree = {node: 0 for node in graph}
        for downstreams in graph.values():
            for downstream in downstreams:
                indegree[downstream] += 1
        ready = [node for node, degree in indegree.items() if degree == 0]
        visited = 0
        while ready:
            node = ready.pop()
            visited += 1
            for downstream in graph[node]:
                indegree[downstream] -= 1
                if indegree[downstream] == 0:
                    ready.append(downstream)
        if visited != len(graph):
            raise ProjectMemoryError(
                "Task dependency graph contains a cycle",
                400,
                "dependency_cycle",
            )

    def _list_tasks_with_db(
        self, db: sqlite3.Connection, project_id: str
    ) -> list[dict[str, Any]]:
        rows = db.execute(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY position, id",
            (project_id,),
        ).fetchall()
        return [self._task_dict(db, row) for row in rows]

    def list_tasks(self, project_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            exists = db.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone()
            if exists is None:
                raise ProjectMemoryError("Project not found", 404, "not_found")
            return {"tasks": self._list_tasks_with_db(db, project_id)}

    def get_task(self, task_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise ProjectMemoryError("Task not found", 404, "not_found")
            return self._task_dict(db, row)

    def transition_project(
        self,
        project_id: str,
        action: str,
        data: dict[str, Any],
        idempotency_key: str | None,
        expected_version: int | None,
    ) -> dict[str, Any]:
        allowed = {
            "start": ({"awaiting_plan_approval", "paused", "blocked"}, "running"),
            "pause": ({"running", "blocked"}, "paused"),
            "resume": ({"paused", "blocked"}, "running"),
            "cancel": (
                PROJECT_STATUSES - TERMINAL_PROJECT_STATUSES,
                "cancelled",
            ),
        }
        if action not in allowed:
            raise ProjectMemoryError("Unsupported project action")
        source_statuses, destination = allowed[action]
        cleaned = redact(data)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            scope_name = f"project_action:{project_id}:{action}"
            existing = self._existing_idempotent(
                db, scope_name, idempotency_key, cleaned
            )
            if existing is not None:
                db.rollback()
                return existing
            row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if row is None:
                raise ProjectMemoryError("Project not found", 404, "not_found")
            self._expect_version(row, expected_version)
            if row["status"] not in source_statuses:
                raise ProjectMemoryError(
                    f"Project cannot {action} in its current state",
                    409,
                    "invalid_state",
                    {"status": row["status"]},
                )

            def operation() -> dict[str, Any]:
                now = utc_now()
                db.execute(
                    "UPDATE projects SET status = ?, updated_at = ?, version = version + 1 WHERE id = ?",
                    (destination, now, project_id),
                )
                if action == "cancel":
                    db.execute(
                        """
                        UPDATE tasks SET status = 'cancelled', updated_at = ?, version = version + 1
                        WHERE project_id = ? AND status NOT IN ('succeeded', 'failed', 'cancelled')
                        """,
                        (now, project_id),
                    )
                elif destination == "running":
                    self._refresh_readiness(db, project_id)
                self._event(
                    db,
                    project_id,
                    f"project.{action}",
                    {"from": row["status"], "to": destination, "reason": cleaned.get("reason", "")},
                    actor=str(cleaned.get("actor", "user"))[:200],
                )
                updated = db.execute(
                    "SELECT * FROM projects WHERE id = ?", (project_id,)
                ).fetchone()
                assert updated is not None
                return self._project_dict(updated)

            result = self._idempotent(
                db, scope_name, idempotency_key, cleaned, operation
            )
            db.commit()
            return result

    @staticmethod
    def _refresh_readiness(db: sqlite3.Connection, project_id: str) -> None:
        now = utc_now()
        db.execute(
            """
            UPDATE tasks
            SET status = 'ready', updated_at = ?, version = version + 1
            WHERE project_id = ?
              AND status IN ('blocked', 'revision_required')
              AND NOT EXISTS (
                  SELECT 1
                  FROM dependencies d
                  JOIN tasks upstream ON upstream.id = d.upstream_task_id
                  WHERE d.downstream_task_id = tasks.id
                    AND upstream.status != 'succeeded'
              )
            """,
            (now, project_id),
        )

    def prepare_task_execution(
        self,
        task_id: str,
        data: dict[str, Any],
        idempotency_key: str | None,
        expected_version: int | None,
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise ProjectMemoryError(
                "Idempotency-Key is required",
                400,
                "idempotency_key_required",
            )
        cleaned = redact(data)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise ProjectMemoryError("Task not found", 404, "not_found")
            project = db.execute(
                "SELECT * FROM projects WHERE id = ?", (task["project_id"],)
            ).fetchone()
            assert project is not None
            existing = db.execute(
                "SELECT * FROM executions WHERE project_id = ? AND idempotency_key = ?",
                (project["id"], idempotency_key),
            ).fetchone()
            fingerprint = self._fingerprint(cleaned)
            if existing:
                if existing["request_fingerprint"] != fingerprint:
                    raise ProjectMemoryError(
                        "Idempotency-Key was already used for a different execution",
                        409,
                        "idempotency_conflict",
                    )
                db.rollback()
                return {
                    "duplicate": True,
                    "execution_id": existing["id"],
                    "status": existing["status"],
                    "task": self._task_dict(db, task),
                }
            self._expect_version(task, expected_version)
            if project["status"] != "running":
                raise ProjectMemoryError(
                    "Project must be running before a task can execute",
                    409,
                    "invalid_state",
                    {"project_status": project["status"]},
                )
            if task["status"] not in {"ready", "revision_required"}:
                raise ProjectMemoryError(
                    "Task is not ready to run",
                    409,
                    "invalid_state",
                    {"task_status": task["status"]},
                )
            if task["attempt_count"] >= task["max_attempts"]:
                raise ProjectMemoryError(
                    "Task retry limit has been reached",
                    409,
                    "retry_limit",
                )
            blocked = db.execute(
                """
                SELECT COUNT(*) AS count
                FROM dependencies d
                JOIN tasks upstream ON upstream.id = d.upstream_task_id
                WHERE d.downstream_task_id = ? AND upstream.status != 'succeeded'
                """,
                (task_id,),
            ).fetchone()["count"]
            if blocked:
                raise ProjectMemoryError(
                    "Task dependencies are not satisfied",
                    409,
                    "dependencies_blocked",
                )
            if project["actual_cost_usd"] >= project["budget_usd"]:
                self._pause_for_budget(db, project["id"], "dollar budget exhausted")
                raise ProjectMemoryError("Project dollar budget is exhausted", 409, "budget_exhausted")
            if project["token_usage"] >= project["token_budget"]:
                self._pause_for_budget(db, project["id"], "token budget exhausted")
                raise ProjectMemoryError("Project token budget is exhausted", 409, "budget_exhausted")
            if project["budget_usd"] > 0 and project["actual_cost_usd"] >= project["budget_usd"] * 0.8:
                self._pause_for_budget(db, project["id"], "80% dollar budget threshold")
                raise ProjectMemoryError(
                    "Project paused at 80% of its dollar budget",
                    409,
                    "budget_pause",
                )
            execution_id = str(uuid.uuid4())
            now = utc_now()
            attempt = task["attempt_count"] + 1
            model = cleaned.get("model")
            if model is None:
                preferred = _loads(task["preferred_models_json"], [])
                model = preferred[0] if preferred else os.environ.get("DEFAULT_GLM_MODEL", "z-ai/glm-5.2")
            if not isinstance(model, str) or not model.strip():
                raise ProjectMemoryError("model must be a non-empty string")
            db.execute(
                """
                INSERT INTO executions(
                    id, project_id, task_id, request_fingerprint, idempotency_key,
                    attempt, model_requested, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    execution_id,
                    project["id"],
                    task_id,
                    fingerprint,
                    idempotency_key,
                    attempt,
                    model.strip(),
                    now,
                ),
            )
            db.execute(
                """
                UPDATE tasks
                SET status = 'running', attempt_count = ?, assigned_worker = 'openrouter',
                    started_at = ?, updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (attempt, now, now, task_id),
            )
            self._event(
                db,
                project["id"],
                "task.execution_started",
                {"execution_id": execution_id, "attempt": attempt, "model": model},
                task_id,
            )
            db.commit()
            return {
                "duplicate": False,
                "execution_id": execution_id,
                "project_id": project["id"],
                "task_id": task_id,
                "model": model.strip(),
                "prompt": self._execution_prompt(task, cleaned),
                "system": _optional_text(cleaned, "system") or (
                    "You are a Helios specialist. Return only user-visible work product. "
                    "Do not request credentials, invent missing facts, or claim tool execution."
                ),
                "max_tokens": _integer(cleaned, "max_tokens", 4096, 1, 8192),
                "timeout_seconds": task["timeout_seconds"],
            }

    @staticmethod
    def _execution_prompt(task: sqlite3.Row, data: dict[str, Any]) -> str:
        override = data.get("prompt")
        if override is not None:
            if not isinstance(override, str) or not override.strip():
                raise ProjectMemoryError("prompt must be a non-empty string")
            return redact(override.strip())
        return (
            f"Task: {task['title']}\n\n"
            f"Description:\n{task['description']}\n\n"
            f"Inputs:\n{_json(_loads(task['inputs_json'], []))}\n\n"
            f"Expected outputs:\n{_json(_loads(task['expected_outputs_json'], []))}\n\n"
            f"Acceptance criteria:\n{_json(_loads(task['acceptance_criteria_json'], []))}"
        )

    @staticmethod
    def _pause_for_budget(db: sqlite3.Connection, project_id: str, reason: str) -> None:
        now = utc_now()
        db.execute(
            "UPDATE projects SET status = 'paused', updated_at = ?, version = version + 1 WHERE id = ?",
            (now, project_id),
        )
        ProjectStore._event(db, project_id, "project.budget_paused", {"reason": reason})
        db.commit()

    def complete_task_execution(
        self,
        execution_id: str,
        result: dict[str, Any] | None,
        error: str | None = None,
        latency_ms: int | None = None,
    ) -> dict[str, Any]:
        safe_result = redact(result or {})
        safe_error = redact(error or "")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            execution = db.execute(
                "SELECT * FROM executions WHERE id = ?", (execution_id,)
            ).fetchone()
            if execution is None:
                raise ProjectMemoryError("Execution not found", 404, "not_found")
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (execution["task_id"],)).fetchone()
            assert task is not None
            project = db.execute(
                "SELECT * FROM projects WHERE id = ?", (execution["project_id"],)
            ).fetchone()
            assert project is not None
            now = utc_now()
            if error:
                retryable = task["attempt_count"] < task["max_attempts"]
                next_status = "ready" if retryable else "failed"
                db.execute(
                    """
                    UPDATE executions SET status = 'failed', error = ?, latency_ms = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (str(safe_error)[:4000], latency_ms, now, execution_id),
                )
                db.execute(
                    """
                    UPDATE tasks SET status = ?, finished_at = ?, updated_at = ?,
                        version = version + 1
                    WHERE id = ?
                    """,
                    (next_status, now, now, task["id"]),
                )
                self._event(
                    db,
                    project["id"],
                    "task.execution_failed",
                    {"execution_id": execution_id, "retryable": retryable, "error": safe_error},
                    task["id"],
                )
            else:
                usage = safe_result.get("usage", {})
                if not isinstance(usage, dict):
                    usage = {}
                tokens = self._usage_tokens(usage)
                cost = self._usage_cost(usage)
                artifact = self._write_artifact(
                    db,
                    project["id"],
                    task["id"],
                    f"execution-{execution_id}.json",
                    "application/json",
                    _json(safe_result).encode("utf-8"),
                    {"execution_id": execution_id, "model_used": safe_result.get("model_used")},
                )
                db.execute(
                    """
                    UPDATE executions
                    SET status = 'succeeded', model_used = ?, latency_ms = ?, usage_json = ?,
                        cost_usd = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (
                        safe_result.get("model_used"),
                        latency_ms,
                        _json(usage),
                        cost,
                        now,
                        execution_id,
                    ),
                )
                db.execute(
                    """
                    UPDATE tasks
                    SET status = 'verifying', model_used = ?, actual_cost_usd = actual_cost_usd + ?,
                        token_usage = token_usage + ?, result_artifact_id = ?,
                        finished_at = ?, updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (
                        safe_result.get("model_used"),
                        cost,
                        tokens,
                        artifact["id"],
                        now,
                        now,
                        task["id"],
                    ),
                )
                db.execute(
                    """
                    UPDATE projects
                    SET actual_cost_usd = actual_cost_usd + ?, token_usage = token_usage + ?,
                        updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (cost, tokens, now, project["id"]),
                )
                self._event(
                    db,
                    project["id"],
                    "task.execution_succeeded",
                    {
                        "execution_id": execution_id,
                        "artifact_id": artifact["id"],
                        "model_used": safe_result.get("model_used"),
                        "tokens": tokens,
                        "cost_usd": cost,
                    },
                    task["id"],
                )
            db.commit()
            return self.get_task(task["id"])

    @staticmethod
    def _usage_tokens(usage: dict[str, Any]) -> int:
        for key in ("total_tokens", "tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return max(0, int(value))
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        return max(0, int(prompt or 0) + int(completion or 0))

    @staticmethod
    def _usage_cost(usage: dict[str, Any]) -> float:
        for key in ("cost", "total_cost", "cost_usd"):
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return max(0.0, float(value))
        return 0.0

    def _write_artifact(
        self,
        db: sqlite3.Connection,
        project_id: str,
        task_id: str | None,
        name: str,
        mime_type: str,
        content: bytes,
        provenance: Any,
    ) -> dict[str, Any]:
        artifact_id = str(uuid.uuid4())
        project_dir = (self.artifact_root / project_id).resolve()
        if self.artifact_root not in project_dir.parents:
            raise ProjectMemoryError("Artifact path escaped the allowlist", 500, "path_error")
        project_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-") or "artifact"
        path = project_dir / f"{artifact_id}-{safe_name}"
        path.write_bytes(content)
        checksum = hashlib.sha256(content).hexdigest()
        version = db.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM artifacts WHERE project_id = ? AND name = ?",
            (project_id, safe_name),
        ).fetchone()["version"]
        created_at = utc_now()
        relative_path = str(path.relative_to(self.artifact_root))
        db.execute(
            """
            INSERT INTO artifacts(
                id, project_id, producer_task_id, name, path, mime_type,
                checksum_sha256, version, provenance_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                project_id,
                task_id,
                safe_name,
                relative_path,
                mime_type,
                checksum,
                version,
                _json(redact(provenance)),
                created_at,
            ),
        )
        return {
            "id": artifact_id,
            "project_id": project_id,
            "producer_task_id": task_id,
            "name": safe_name,
            "path": relative_path,
            "mime_type": mime_type,
            "checksum_sha256": checksum,
            "version": version,
            "provenance": redact(provenance),
            "created_at": created_at,
        }

    def verify_task(
        self,
        task_id: str,
        data: dict[str, Any],
        idempotency_key: str | None,
        expected_version: int | None,
    ) -> dict[str, Any]:
        cleaned = redact(data)
        decision = str(cleaned.get("decision", "")).strip().lower()
        allowed = {
            "pass": "succeeded",
            "revision_required": "revision_required",
            "blocked": "blocked",
            "human_review_required": "awaiting_approval",
        }
        if decision not in allowed:
            raise ProjectMemoryError(
                "decision must be pass, revision_required, blocked, or human_review_required"
            )
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            scope_name = f"verify_task:{task_id}"
            existing = self._existing_idempotent(
                db, scope_name, idempotency_key, cleaned
            )
            if existing is not None:
                db.rollback()
                return existing
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise ProjectMemoryError("Task not found", 404, "not_found")
            self._expect_version(task, expected_version)
            if task["status"] != "verifying":
                raise ProjectMemoryError(
                    "Task is not awaiting verification",
                    409,
                    "invalid_state",
                    {"status": task["status"]},
                )

            def operation() -> dict[str, Any]:
                destination = allowed[decision]
                if decision == "pass" and task["requires_human_approval"]:
                    destination = "awaiting_approval"
                now = utc_now()
                db.execute(
                    """
                    UPDATE tasks SET status = ?, updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (destination, now, task_id),
                )
                self._event(
                    db,
                    task["project_id"],
                    "task.verified",
                    {
                        "decision": decision,
                        "destination": destination,
                        "evidence": cleaned.get("evidence", []),
                        "unsupported_claims": cleaned.get("unsupported_claims", []),
                        "missing_inputs": cleaned.get("missing_inputs", []),
                    },
                    task_id,
                    actor=str(cleaned.get("actor", "verifier"))[:200],
                )
                if destination == "succeeded":
                    self._refresh_readiness(db, task["project_id"])
                    self._maybe_complete_project(db, task["project_id"])
                updated = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                assert updated is not None
                return self._task_dict(db, updated)

            result = self._idempotent(
                db, scope_name, idempotency_key, cleaned, operation
            )
            db.commit()
            return result

    def approve_task(
        self,
        task_id: str,
        data: dict[str, Any],
        idempotency_key: str | None,
        expected_version: int | None,
    ) -> dict[str, Any]:
        cleaned = redact(data)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            scope_name = f"approve_task:{task_id}"
            existing = self._existing_idempotent(
                db, scope_name, idempotency_key, cleaned
            )
            if existing is not None:
                db.rollback()
                return existing
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise ProjectMemoryError("Task not found", 404, "not_found")
            self._expect_version(task, expected_version)
            if task["status"] != "awaiting_approval":
                raise ProjectMemoryError(
                    "Task is not awaiting approval",
                    409,
                    "invalid_state",
                    {"status": task["status"]},
                )

            def operation() -> dict[str, Any]:
                now = utc_now()
                db.execute(
                    """
                    UPDATE tasks SET status = 'succeeded', updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (now, task_id),
                )
                self._event(
                    db,
                    task["project_id"],
                    "task.approved",
                    {"rationale": cleaned.get("rationale", "")},
                    task_id,
                    actor=str(cleaned.get("actor", "user"))[:200],
                )
                self._refresh_readiness(db, task["project_id"])
                self._maybe_complete_project(db, task["project_id"])
                updated = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                assert updated is not None
                return self._task_dict(db, updated)

            result = self._idempotent(
                db, scope_name, idempotency_key, cleaned, operation
            )
            db.commit()
            return result

    def request_revision(
        self,
        task_id: str,
        data: dict[str, Any],
        idempotency_key: str | None,
        expected_version: int | None,
    ) -> dict[str, Any]:
        cleaned = redact(data)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            scope_name = f"request_revision:{task_id}"
            existing = self._existing_idempotent(
                db, scope_name, idempotency_key, cleaned
            )
            if existing is not None:
                db.rollback()
                return existing
            task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise ProjectMemoryError("Task not found", 404, "not_found")
            self._expect_version(task, expected_version)
            if task["status"] not in {"verifying", "awaiting_approval", "succeeded"}:
                raise ProjectMemoryError(
                    "Task cannot be revised in its current state",
                    409,
                    "invalid_state",
                    {"status": task["status"]},
                )

            def operation() -> dict[str, Any]:
                now = utc_now()
                db.execute(
                    """
                    UPDATE tasks SET status = 'revision_required', updated_at = ?,
                        version = version + 1 WHERE id = ?
                    """,
                    (now, task_id),
                )
                self._event(
                    db,
                    task["project_id"],
                    "task.revision_requested",
                    {"reason": cleaned.get("reason", "")},
                    task_id,
                    actor=str(cleaned.get("actor", "user"))[:200],
                )
                updated = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                assert updated is not None
                return self._task_dict(db, updated)

            result = self._idempotent(
                db, scope_name, idempotency_key, cleaned, operation
            )
            db.commit()
            return result

    @staticmethod
    def _maybe_complete_project(db: sqlite3.Connection, project_id: str) -> None:
        counts = db.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded
            FROM tasks WHERE project_id = ?
            """,
            (project_id,),
        ).fetchone()
        if counts["total"] and counts["total"] == counts["succeeded"]:
            now = utc_now()
            db.execute(
                """
                UPDATE projects SET status = 'completed', updated_at = ?, version = version + 1
                WHERE id = ?
                """,
                (now, project_id),
            )
            ProjectStore._event(db, project_id, "project.completed", {})

    def list_events(self, project_id: str, limit: int = 200) -> dict[str, Any]:
        limit = max(1, min(int(limit), 1000))
        with closing(self._connect()) as db:
            exists = db.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone()
            if exists is None:
                raise ProjectMemoryError("Project not found", 404, "not_found")
            rows = db.execute(
                """
                SELECT * FROM events WHERE project_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
            events = [
                {
                    "id": row["id"],
                    "project_id": row["project_id"],
                    "task_id": row["task_id"],
                    "event_type": row["event_type"],
                    "actor": row["actor"],
                    "payload": _loads(row["payload_json"], {}),
                    "created_at": row["created_at"],
                }
                for row in reversed(rows)
            ]
            return {"events": events}

    def list_artifacts(self, project_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM artifacts WHERE project_id = ? ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
            return {
                "artifacts": [
                    {
                        "id": row["id"],
                        "project_id": row["project_id"],
                        "producer_task_id": row["producer_task_id"],
                        "name": row["name"],
                        "path": row["path"],
                        "mime_type": row["mime_type"],
                        "checksum_sha256": row["checksum_sha256"],
                        "version": row["version"],
                        "provenance": _loads(row["provenance_json"], {}),
                        "created_at": row["created_at"],
                    }
                    for row in rows
                ]
            }

    def usage(self, project_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            project = db.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if project is None:
                raise ProjectMemoryError("Project not found", 404, "not_found")
            executions = db.execute(
                """
                SELECT status, model_used, cost_usd, usage_json, created_at, finished_at
                FROM executions WHERE project_id = ? ORDER BY created_at
                """,
                (project_id,),
            ).fetchall()
            return {
                "project_id": project_id,
                "budget_usd": project["budget_usd"],
                "actual_cost_usd": project["actual_cost_usd"],
                "token_budget": project["token_budget"],
                "token_usage": project["token_usage"],
                "executions": [
                    {
                        "status": row["status"],
                        "model_used": row["model_used"],
                        "cost_usd": row["cost_usd"],
                        "usage": _loads(row["usage_json"], {}),
                        "created_at": row["created_at"],
                        "finished_at": row["finished_at"],
                    }
                    for row in executions
                ],
            }

    def recover_orphaned_tasks(self) -> int:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM tasks WHERE status = 'running'").fetchall()
            recovered = 0
            for task in rows:
                destination = (
                    "ready"
                    if task["attempt_count"] < task["max_attempts"]
                    else "blocked"
                )
                now = utc_now()
                db.execute(
                    """
                    UPDATE tasks SET status = ?, updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (destination, now, task["id"]),
                )
                db.execute(
                    """
                    UPDATE executions
                    SET status = 'orphaned', error = 'Service restarted during execution',
                        finished_at = ?
                    WHERE task_id = ? AND status = 'running'
                    """,
                    (now, task["id"]),
                )
                self._event(
                    db,
                    task["project_id"],
                    "task.recovered_after_restart",
                    {"destination": destination},
                    task["id"],
                )
                recovered += 1
            db.commit()
            return recovered


def store_from_environment(base_dir: Path) -> ProjectStore:
    data_dir = Path(os.environ.get("HELIOS_DATA_DIR", str(base_dir / "data")))
    database_path = Path(
        os.environ.get("HELIOS_DATABASE_PATH", str(data_dir / "helios.db"))
    )
    artifact_root = Path(
        os.environ.get("HELIOS_ARTIFACT_ROOT", str(data_dir / "artifacts"))
    )
    return ProjectStore(database_path, artifact_root)
