#!/usr/bin/env python3
"""Loopback-only Helios gateway for OpenRouter, Manus, and project routing."""

from __future__ import annotations

import hmac
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from agent.benchmark_registry import (
        BenchmarkRegistryError,
        refresh_registry,
        registry_status,
        registry_view,
        select_benchmark_model,
    )
    from agent.project_memory import (
        ProjectMemoryError,
        ProjectStore,
        store_from_environment,
    )
except ModuleNotFoundError:
    from benchmark_registry import (  # type: ignore
        BenchmarkRegistryError,
        refresh_registry,
        registry_status,
        registry_view,
        select_benchmark_model,
    )
    from project_memory import (  # type: ignore
        ProjectMemoryError,
        ProjectStore,
        store_from_environment,
    )


MODULE_DIR = Path(__file__).resolve().parent
BASE_DIR = MODULE_DIR.parent if (MODULE_DIR.parent / "config").exists() else MODULE_DIR
ENV_FILE = BASE_DIR / ".env"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MANUS_BASE_URL = "https://api.manus.ai/v2"
MODEL_CACHE_TTL_SECONDS = 600
REQUEST_BODY_LIMIT = 2 * 1024 * 1024
ALLOWED_MESSAGE_ROLES = {"system", "user", "assistant", "tool"}

_model_cache: dict[str, Any] = {"loaded_at": 0.0, "models": []}
_model_lock = threading.Lock()
_project_store: ProjectStore | None = None
_project_store_lock = threading.Lock()


class GatewayError(Exception):
    def __init__(
        self,
        message: str,
        status: int = 400,
        details: Any = None,
        code: str = "gateway_error",
        retryable: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.details = details
        self.code = code
        self.retryable = retryable


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


load_env_file(ENV_FILE)

HOST = os.environ.get("OPENROUTER_AGENT_HOST", "127.0.0.1")
PORT = _env_int("OPENROUTER_AGENT_PORT", 3188, 1, 65535)
MAX_OUTPUT_TOKENS = _env_int("MAX_OUTPUT_TOKENS", 8192, 1, 200000)
MAX_PROMPT_CHARS = _env_int("MAX_PROMPT_CHARS", 400000, 1000, 2_000_000)
MAX_COMPARE_MODELS = _env_int("MAX_COMPARE_MODELS", 4, 2, 8)
MAX_CONCURRENT_REQUESTS = _env_int("HELIOS_MAX_CONCURRENT_REQUESTS", 4, 1, 32)
LOCAL_API_KEY = os.environ.get("HELIOS_LOCAL_API_KEY", "").strip()
KEYCHAIN_SERVICE = os.environ.get("OPENROUTER_KEYCHAIN_SERVICE", "helios-multimodel-router")
KEYCHAIN_ACCOUNT = os.environ.get("OPENROUTER_KEYCHAIN_ACCOUNT", "openrouter-api-key")
MANUS_BASE_URL = os.environ.get("MANUS_API_BASE_URL", DEFAULT_MANUS_BASE_URL).rstrip("/")
MANUS_API_KEY_FILE = os.environ.get("MANUS_API_KEY_FILE", "").strip()
_paid_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)

if HOST not in {"127.0.0.1", "::1", "localhost"}:
    raise RuntimeError("Helios must bind to loopback only (127.0.0.1 or ::1)")


def project_store() -> ProjectStore:
    global _project_store
    if _project_store is None:
        with _project_store_lock:
            if _project_store is None:
                _project_store = store_from_environment(BASE_DIR)
    return _project_store

STATIC_ALIASES = {
    "glm": os.environ.get("DEFAULT_GLM_MODEL", "z-ai/glm-5.2"),
    "glm-5": os.environ.get("DEFAULT_GLM_MODEL", "z-ai/glm-5.2"),
    "glm5": os.environ.get("DEFAULT_GLM_MODEL", "z-ai/glm-5.2"),
    "جی‌ال‌ام": os.environ.get("DEFAULT_GLM_MODEL", "z-ai/glm-5.2"),
    "جی ال ام": os.environ.get("DEFAULT_GLM_MODEL", "z-ai/glm-5.2"),
    "gemini-flash": os.environ.get("DEFAULT_GEMINI_FLASH_MODEL", ""),
    "جمنای-فلش": os.environ.get("DEFAULT_GEMINI_FLASH_MODEL", ""),
}


def keychain_api_key() -> str:
    if sys.platform != "darwin":
        return ""
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip() or keychain_api_key()
    if not key:
        raise GatewayError(
            "OpenRouter credential is not configured in the environment or macOS Keychain",
            503,
        )
    return key


def api_key_configured() -> bool:
    try:
        return bool(api_key())
    except GatewayError:
        return False


def manus_api_key() -> str:
    key = os.environ.get("MANUS_API_KEY", "").strip()
    if not key and MANUS_API_KEY_FILE:
        try:
            key = Path(MANUS_API_KEY_FILE).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise GatewayError("Manus credential file is unavailable", 503) from exc
    if not key:
        raise GatewayError("Manus credential is not configured", 503)
    return key


def manus_api_key_configured() -> bool:
    try:
        return bool(manus_api_key())
    except GatewayError:
        return False


def manus_request(
    method: str,
    operation: str,
    payload: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    timeout: int = 180,
) -> dict[str, Any]:
    url = MANUS_BASE_URL + "/" + operation.lstrip("/")
    if query:
        url += "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(
        url,
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={
            "x-manus-api-key": manus_api_key(),
            "Content-Type": "application/json",
            "User-Agent": "Helios/2.0 ManusProvider",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            upstream = json.loads(raw)
        except json.JSONDecodeError:
            upstream = raw[:2000]
        raise GatewayError(
            "Manus request failed",
            502,
            {"upstream_status": exc.code, "upstream": upstream},
            code="manus_upstream_error",
            retryable=exc.code in {408, 429, 500, 502, 503, 504},
        ) from exc
    except urllib.error.URLError as exc:
        raise GatewayError(
            "Could not reach Manus", 502, code="manus_unreachable", retryable=True
        ) from exc
    except json.JSONDecodeError as exc:
        raise GatewayError("Manus returned invalid JSON", 502) from exc
    if not isinstance(value, dict):
        raise GatewayError("Manus returned an invalid response", 502)
    return value


def local_request_authorized(headers: Any) -> bool:
    if not LOCAL_API_KEY:
        return True
    return hmac.compare_digest(
        str(headers.get("Authorization", "")), "Bearer " + LOCAL_API_KEY
    )


def openrouter_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 180,
) -> dict[str, Any]:
    headers = {
        "Authorization": "Bearer " + api_key(),
        "Content-Type": "application/json",
        "HTTP-Referer": "https://localhost.invalid/helios",
        "X-OpenRouter-Title": "Helios LLM Orchestrator",
    }
    request = urllib.request.Request(
        OPENROUTER_BASE_URL + path,
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            upstream = json.loads(raw)
        except json.JSONDecodeError:
            upstream = raw[:2000]
        raise GatewayError(
            "OpenRouter request failed",
            502,
            {"upstream_status": exc.code, "upstream": upstream},
        ) from exc
    except urllib.error.URLError as exc:
        raise GatewayError("Could not reach OpenRouter", 502) from exc
    except json.JSONDecodeError as exc:
        raise GatewayError("OpenRouter returned invalid JSON", 502) from exc


def get_models(force: bool = False) -> list[dict[str, Any]]:
    now = time.time()
    with _model_lock:
        cached = _model_cache["models"]
        if cached and not force and now - _model_cache["loaded_at"] < MODEL_CACHE_TTL_SECONDS:
            return cached
    models = openrouter_request("GET", "/models", timeout=60).get("data")
    if not isinstance(models, list):
        raise GatewayError("OpenRouter model catalog response was invalid", 502)
    with _model_lock:
        _model_cache.update({"models": models, "loaded_at": now})
    return models


def newest_matching(
    prefix: str,
    required_terms: tuple[str, ...] = (),
    excluded_terms: tuple[str, ...] = (),
) -> str:
    candidates = []
    for item in get_models():
        model_id = str(item.get("id", "")).lower()
        haystack = model_id + " " + str(item.get("name", "")).lower()
        if (
            model_id.startswith(prefix.lower())
            and all(term in haystack for term in required_terms)
            and not any(term in haystack for term in excluded_terms)
        ):
            candidates.append(item)
    if not candidates:
        raise GatewayError("No matching model is currently available in OpenRouter")
    candidates.sort(key=lambda item: int(item.get("created") or 0), reverse=True)
    return str(candidates[0]["id"])


def resolve_model(requested: str) -> str:
    value = (requested or "").strip()
    if not value:
        raise GatewayError("model is required")
    normalized = value.lower().replace("_", "-")
    if STATIC_ALIASES.get(normalized):
        return STATIC_ALIASES[normalized]
    if normalized in {"gemini", "gemeni", "جمنای", "جمنایی"}:
        return os.environ.get("DEFAULT_GEMINI_MODEL", "").strip() or newest_matching(
            "google/gemini",
            ("pro",),
            ("image", "audio", "embedding", "customtools"),
        )
    if normalized in {"gemini-flash", "gemeni-flash", "جمنای فلش"}:
        return os.environ.get("DEFAULT_GEMINI_FLASH_MODEL", "").strip() or newest_matching(
            "google/gemini",
            ("flash",),
            ("image", "audio", "embedding", "customtools"),
        )
    if normalized in {"claude", "کلاد"}:
        return os.environ.get("DEFAULT_CLAUDE_MODEL", "").strip() or newest_matching(
            "anthropic/claude", ("sonnet",)
        )
    if normalized in {"deepseek", "دیپ‌سیک", "دیپ سیک"}:
        return os.environ.get("DEFAULT_DEEPSEEK_MODEL", "").strip() or newest_matching(
            "deepseek/", ("pro",), ("vision", "flash", "image")
        )
    if normalized in {"qwen", "کوئن"}:
        return os.environ.get("DEFAULT_QWEN_MODEL", "").strip() or newest_matching(
            "qwen/", ("max",), ("vision", "image", "audio", "flash")
        )
    if "/" in value:
        return value

    exact, partial = [], []
    for item in get_models():
        model_id, name = str(item.get("id", "")), str(item.get("name", ""))
        if normalized in {model_id.lower(), name.lower()}:
            exact.append(item)
        elif normalized in model_id.lower() or normalized in name.lower():
            partial.append(item)
    candidates = exact or partial
    if not candidates:
        raise GatewayError(
            "Unknown model alias",
            details={"requested": value, "hint": "Call GET /models?search=<name>"},
        )
    candidates.sort(key=lambda item: int(item.get("created") or 0), reverse=True)
    return str(candidates[0]["id"])


def bounded_int(value: Any, name: str, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or (
        isinstance(value, float) and not value.is_integer()
    ):
        raise GatewayError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise GatewayError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise GatewayError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def bounded_float(
    value: Any, name: str, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool):
        raise GatewayError(f"{name} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise GatewayError(f"{name} must be a number") from exc
    if not minimum <= parsed <= maximum:
        raise GatewayError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def strict_bool(value: Any, name: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise GatewayError(f"{name} must be a boolean")
    return value


def build_messages(data: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        raise GatewayError("request data must be an object")
    provided = data.get("messages")
    if provided is not None:
        if not isinstance(provided, list) or not provided:
            raise GatewayError("messages must be a non-empty array")
        for index, message in enumerate(provided):
            if not isinstance(message, dict):
                raise GatewayError(f"messages[{index}] must be an object")
            if message.get("role") not in ALLOWED_MESSAGE_ROLES:
                raise GatewayError(f"messages[{index}].role is invalid")
            content = message.get("content")
            if not isinstance(content, (str, list)) or content in ("", []):
                raise GatewayError(f"messages[{index}].content must be non-empty")
        try:
            serialized = json.dumps(provided, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise GatewayError("messages must be JSON-serializable") from exc
        if len(serialized) > MAX_PROMPT_CHARS:
            raise GatewayError(
                "Messages are too large", 413, {"max_prompt_chars": MAX_PROMPT_CHARS}
            )
        return provided

    prompt = data.get("prompt", data.get("task", ""))
    if not isinstance(prompt, str) or not prompt.strip():
        raise GatewayError("prompt or task is required")
    system = data.get("system")
    if system is not None and not isinstance(system, str):
        raise GatewayError("system must be a string")
    if len(prompt) + len(system or "") > MAX_PROMPT_CHARS:
        raise GatewayError(
            "Prompt is too large", 413, {"max_prompt_chars": MAX_PROMPT_CHARS}
        )
    messages = []
    if system and system.strip():
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return messages


def run_model(data: dict[str, Any]) -> dict[str, Any]:
    requested = data.get("model")
    if not isinstance(requested, str):
        raise GatewayError("model must be a string")
    resolved = resolve_model(requested)
    payload: dict[str, Any] = {
        "model": resolved,
        "messages": build_messages(data),
        "max_tokens": bounded_int(
            data.get("max_tokens"), "max_tokens", MAX_OUTPUT_TOKENS, 1, MAX_OUTPUT_TOKENS
        ),
        "stream": False,
    }
    if data.get("temperature") is not None:
        payload["temperature"] = bounded_float(data["temperature"], "temperature", 0, 2)
    if data.get("top_p") is not None:
        payload["top_p"] = bounded_float(data["top_p"], "top_p", 0, 1)
    if data.get("reasoning_effort") is not None:
        effort = data["reasoning_effort"]
        if effort not in {"low", "medium", "high", "xhigh"}:
            raise GatewayError("reasoning_effort must be low, medium, high, or xhigh")
        payload["reasoning"] = {"effort": effort}

    response = openrouter_request("POST", "/chat/completions", payload)
    choices = response.get("choices") or []
    message = choices[0].get("message", {}) if choices else {}
    return {
        "model_requested": requested,
        "model_resolved": resolved,
        "model_used": response.get("model", resolved),
        "answer": message.get("content", ""),
        "usage": response.get("usage", {}),
        "finish_reason": choices[0].get("finish_reason") if choices else None,
        "generation_id": response.get("id"),
    }


def compare_models(data: dict[str, Any]) -> dict[str, Any]:
    requested = data.get("models")
    if not isinstance(requested, list) or len(requested) < 2:
        raise GatewayError("models must contain at least two model names")
    if len(requested) > MAX_COMPARE_MODELS:
        raise GatewayError(
            "Too many models requested", details={"max_compare_models": MAX_COMPARE_MODELS}
        )
    if any(not isinstance(model, str) or not model.strip() for model in requested):
        raise GatewayError("every model must be a non-empty string")
    if len(set(requested)) != len(requested):
        raise GatewayError("models must not contain duplicates")

    results = []
    with ThreadPoolExecutor(max_workers=len(requested)) as executor:
        futures = {}
        for model in requested:
            request_data = dict(data)
            request_data.pop("models", None)
            request_data["model"] = model
            futures[executor.submit(run_model, request_data)] = model
        for future in as_completed(futures):
            model = futures[future]
            try:
                results.append(future.result())
            except GatewayError as exc:
                results.append({"model_requested": model, "error": str(exc), "details": exc.details})
            except Exception:
                results.append({"model_requested": model, "error": "Internal model execution error"})
    order = {model: index for index, model in enumerate(requested)}
    results.sort(key=lambda item: order.get(item.get("model_requested"), 999))
    return {"results": results}


def _string_list(value: Any, name: str, maximum: int = 100) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > maximum:
        raise GatewayError(f"{name} must be an array with at most {maximum} items")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise GatewayError(f"every {name} item must be a non-empty string")
    return [item.strip() for item in value]


def create_manus_task(data: dict[str, Any]) -> dict[str, Any]:
    prompt = data.get("prompt", data.get("task", ""))
    if not isinstance(prompt, str) or not prompt.strip():
        raise GatewayError("prompt or task is required")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise GatewayError(
            "Prompt is too large", 413, {"max_prompt_chars": MAX_PROMPT_CHARS}
        )
    message: dict[str, Any] = {"content": prompt.strip()}
    for field in ("connectors", "enable_skills", "force_skills"):
        values = _string_list(data.get(field), field)
        if values is not None:
            message[field] = values
    payload: dict[str, Any] = {"message": message}
    for field in ("project_id", "locale", "title"):
        value = data.get(field)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise GatewayError(f"{field} must be a non-empty string")
            payload[field] = value.strip()
    for field in ("interactive_mode", "hide_in_task_list"):
        if data.get(field) is not None:
            payload[field] = strict_bool(data[field], field)
    share_visibility = data.get("share_visibility", "private")
    if share_visibility not in {"private", "team", "public"}:
        raise GatewayError("share_visibility must be private, team, or public")
    payload["share_visibility"] = share_visibility
    agent_profile = data.get("agent_profile", "manus-1.6")
    if agent_profile not in {"manus-1.6", "manus-1.6-lite", "manus-1.6-max"}:
        raise GatewayError(
            "agent_profile must be manus-1.6, manus-1.6-lite, or manus-1.6-max"
        )
    payload["agent_profile"] = agent_profile
    if data.get("structured_output_schema") is not None:
        schema = data["structured_output_schema"]
        if not isinstance(schema, dict):
            raise GatewayError("structured_output_schema must be an object")
        payload["structured_output_schema"] = schema
    response = manus_request("POST", "task.create", payload, timeout=60)
    return {"provider": "manus", "operation": "task.create", **response}


def manus_task_detail(task_id: str) -> dict[str, Any]:
    if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 300:
        raise GatewayError("task_id must be a non-empty string")
    return {
        "provider": "manus",
        "operation": "task.detail",
        **manus_request(
            "GET", "task.detail", query={"task_id": task_id.strip()}, timeout=60
        ),
    }


def manus_task_messages(task_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
    if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 300:
        raise GatewayError("task_id must be a non-empty string")
    limit = bounded_int((query.get("limit") or [None])[0], "limit", 50, 1, 200)
    order = str((query.get("order") or ["desc"])[0]).strip()
    if order not in {"asc", "desc"}:
        raise GatewayError("order must be asc or desc")
    params: dict[str, Any] = {"task_id": task_id.strip(), "limit": limit, "order": order}
    cursor = str((query.get("cursor") or [""])[0]).strip()
    if cursor:
        params["cursor"] = cursor
    verbose = str((query.get("verbose") or ["false"])[0]).lower()
    if verbose not in {"true", "false", "1", "0", "yes", "no"}:
        raise GatewayError("verbose must be a boolean")
    if verbose in {"true", "1", "yes"}:
        params["verbose"] = "true"
    slides_format = str((query.get("slides_format") or [""])[0]).strip()
    if slides_format:
        if slides_format not in {"html", "pptx"}:
            raise GatewayError("slides_format must be html or pptx")
        params["slides_format"] = slides_format
    return {
        "provider": "manus",
        "operation": "task.listMessages",
        **manus_request("GET", "task.listMessages", query=params, timeout=60),
    }


def stop_manus_task(task_id: str) -> dict[str, Any]:
    if not isinstance(task_id, str) or not task_id.strip() or len(task_id) > 300:
        raise GatewayError("task_id must be a non-empty string")
    return {
        "provider": "manus",
        "operation": "task.stop",
        **manus_request(
            "POST", "task.stop", {"task_id": task_id.strip()}, timeout=60
        ),
    }


def run_provider(data: dict[str, Any]) -> dict[str, Any]:
    provider = str(data.get("provider", "openrouter")).strip().lower()
    if provider == "openrouter":
        return run_model(data)
    if provider == "manus":
        return create_manus_task(data)
    raise GatewayError("provider must be openrouter or manus")


def public_model(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "id",
            "name",
            "created",
            "context_length",
            "pricing",
            "supported_parameters",
        )
    }


def safe_benchmark_status() -> dict[str, Any]:
    try:
        return registry_status()
    except BenchmarkRegistryError:
        return {"status": "error"}


class Handler(BaseHTTPRequestHandler):
    server_version = "HeliosOrchestrator/2.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.client_address[0], self.log_date_time_string(), fmt % args)
        )

    def send_json(self, status: int, value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.end_headers()
        self.wfile.write(encoded)

    def read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as exc:
            raise GatewayError("Content-Length must be a valid integer") from exc
        if length <= 0:
            raise GatewayError("JSON request body is required")
        if length > REQUEST_BODY_LIMIT:
            raise GatewayError("Request body is too large", 413)
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayError("Request body must be valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise GatewayError("Request body must be a JSON object")
        return value

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, ProjectMemoryError):
            self.send_json(
                exc.status,
                {
                    "error": str(exc),
                    "code": exc.code,
                    "retryable": exc.retryable,
                    "details": exc.details,
                },
            )
            return
        if isinstance(exc, (GatewayError, BenchmarkRegistryError)):
            self.send_json(
                exc.status,
                {
                    "error": str(exc),
                    "code": getattr(exc, "code", "gateway_error"),
                    "retryable": getattr(exc, "retryable", False),
                    "details": exc.details,
                },
            )
            return
        self.log_error("Unhandled error: %s", repr(exc))
        self.send_json(
            500,
            {
                "error": "Internal error",
                "code": "internal_error",
                "retryable": False,
                "details": None,
            },
        )

    def _idempotency_key(self, data: dict[str, Any]) -> str | None:
        value = self.headers.get("Idempotency-Key") or data.get("idempotency_key")
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise ProjectMemoryError("Idempotency-Key must be a non-empty string")
        return value.strip()

    def _expected_version(self, data: dict[str, Any]) -> int | None:
        value: Any = self.headers.get("If-Match")
        if value is not None:
            value = value.strip().strip('"')
        else:
            value = data.get("version")
        if value is None:
            return None
        if isinstance(value, bool):
            raise ProjectMemoryError("version must be an integer")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ProjectMemoryError("version must be an integer") from exc

    @staticmethod
    def _v2_parts(path: str) -> list[str]:
        return [urllib.parse.unquote(part) for part in path.strip("/").split("/") if part]

    def _handle_v2_get(self, parsed: urllib.parse.ParseResult) -> bool:
        if not parsed.path.startswith("/v2/"):
            return False
        parts = self._v2_parts(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        store = project_store()
        if parts == ["v2", "global-context"]:
            self.send_json(200, store.get_global_context())
            return True
        if parts == ["v2", "projects"]:
            limit = bounded_int((query.get("limit") or [None])[0], "limit", 50, 1, 200)
            self.send_json(200, store.list_projects(limit))
            return True
        if len(parts) == 3 and parts[:2] == ["v2", "projects"]:
            self.send_json(200, store.get_project(parts[2]))
            return True
        if len(parts) == 4 and parts[:2] == ["v2", "projects"]:
            project_id, collection = parts[2], parts[3]
            if collection == "tasks":
                self.send_json(200, store.list_tasks(project_id))
            elif collection == "artifacts":
                self.send_json(200, store.list_artifacts(project_id))
            elif collection == "events":
                limit = bounded_int(
                    (query.get("limit") or [None])[0], "limit", 200, 1, 1000
                )
                self.send_json(200, store.list_events(project_id, limit))
            elif collection == "usage":
                self.send_json(200, store.usage(project_id))
            else:
                return False
            return True
        if len(parts) == 3 and parts[:2] == ["v2", "tasks"]:
            self.send_json(200, store.get_task(parts[2]))
            return True
        return False

    def _handle_v2_post(
        self, path: str, data: dict[str, Any]
    ) -> tuple[bool, bool]:
        """Return (handled, paid_slot_required)."""
        if not path.startswith("/v2/"):
            return False, False
        parts = self._v2_parts(path)
        store = project_store()
        key = self._idempotency_key(data)
        version = self._expected_version(data)
        if parts == ["v2", "global-context"]:
            self.send_json(
                200, store.update_global_context(data, key, version)
            )
            return True, False
        if parts == ["v2", "projects"]:
            self.send_json(201, store.create_project(data, key))
            return True, False
        if len(parts) == 4 and parts[:2] == ["v2", "projects"]:
            project_id, action = parts[2], parts[3]
            if action == "plan":
                self.send_json(
                    200,
                    store.plan_project(project_id, data, key, version),
                )
                return True, False
            if action in {"start", "pause", "resume", "cancel"}:
                self.send_json(
                    200,
                    store.transition_project(
                        project_id, action, data, key, version
                    ),
                )
                return True, False
            return False, False
        if len(parts) == 4 and parts[:2] == ["v2", "tasks"]:
            task_id, action = parts[2], parts[3]
            if action == "run":
                acquired = _paid_slots.acquire(blocking=False)
                if not acquired:
                    raise GatewayError(
                        "Helios is busy; retry later",
                        429,
                        code="concurrency_limit",
                        retryable=True,
                    )
                try:
                    prepared = store.prepare_task_execution(
                        task_id, data, key, version
                    )
                    if prepared.get("duplicate"):
                        self.send_json(200, prepared)
                        return True, False
                    started = time.monotonic()
                    try:
                        result = run_model(
                            {
                                "model": prepared["model"],
                                "prompt": prepared["prompt"],
                                "system": prepared["system"],
                                "max_tokens": prepared["max_tokens"],
                                **(
                                    {"reasoning_effort": data["reasoning_effort"]}
                                    if data.get("reasoning_effort") is not None
                                    else {}
                                ),
                            }
                        )
                    except Exception as exc:
                        latency_ms = int((time.monotonic() - started) * 1000)
                        store.complete_task_execution(
                            prepared["execution_id"],
                            None,
                            error=str(exc),
                            latency_ms=latency_ms,
                        )
                        raise
                    latency_ms = int((time.monotonic() - started) * 1000)
                    task = store.complete_task_execution(
                        prepared["execution_id"], result, latency_ms=latency_ms
                    )
                    self.send_json(
                        200,
                        {
                            "execution_id": prepared["execution_id"],
                            "task": task,
                            "result": result,
                        },
                    )
                    return True, False
                finally:
                    _paid_slots.release()
            if action == "verify":
                self.send_json(
                    200, store.verify_task(task_id, data, key, version)
                )
                return True, False
            if action == "approve":
                self.send_json(
                    200, store.approve_task(task_id, data, key, version)
                )
                return True, False
            if action == "request-revision":
                self.send_json(
                    200, store.request_revision(task_id, data, key, version)
                )
                return True, False
        return False, False

    def _handle_manus_get(self, parsed: urllib.parse.ParseResult) -> bool:
        parts = [
            urllib.parse.unquote(part)
            for part in parsed.path.strip("/").split("/")
            if part
        ]
        if len(parts) == 3 and parts[:2] == ["manus", "tasks"]:
            self.send_json(200, manus_task_detail(parts[2]))
            return True
        if (
            len(parts) == 4
            and parts[:2] == ["manus", "tasks"]
            and parts[3] == "messages"
        ):
            self.send_json(
                200,
                manus_task_messages(parts[2], urllib.parse.parse_qs(parsed.query)),
            )
            return True
        return False

    def do_GET(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/health" and not local_request_authorized(self.headers):
                self.send_json(401, {"error": "Unauthorized"})
                return
            if parsed.path == "/health":
                project_store()
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "service": "helios-llm-orchestrator",
                        "version": "2.0.0",
                        "configured": api_key_configured(),
                        "loopback_only": True,
                        "durable_project_memory": True,
                        "providers": {
                            "openrouter": {"configured": api_key_configured()},
                            "manus": {
                                "configured": manus_api_key_configured(),
                                "api_version": "v2",
                            },
                        },
                        "benchmarks": safe_benchmark_status(),
                    },
                )
            elif parsed.path == "/providers":
                self.send_json(
                    200,
                    {
                        "openrouter": {"configured": api_key_configured()},
                        "manus": {
                            "configured": manus_api_key_configured(),
                            "api_version": "v2",
                        },
                    },
                )
            elif self._handle_manus_get(parsed):
                return
            elif self._handle_v2_get(parsed):
                return
            elif parsed.path == "/benchmarks/status":
                self.send_json(200, registry_status())
            elif parsed.path == "/benchmarks":
                query = urllib.parse.parse_qs(parsed.query)
                self.send_json(200, registry_view((query.get("category") or [None])[0]))
            elif parsed.path == "/benchmarks/select":
                query = urllib.parse.parse_qs(parsed.query)
                category = str((query.get("category") or [""])[0]).strip()
                if not category:
                    raise BenchmarkRegistryError("category is required")
                self.send_json(200, select_benchmark_model(category))
            elif parsed.path == "/models":
                query = urllib.parse.parse_qs(parsed.query)
                search = str((query.get("search") or [""])[0]).lower().strip()
                limit = bounded_int(
                    (query.get("limit") or [None])[0], "limit", 50, 1, 200
                )
                models = get_models()
                if search:
                    models = [
                        item
                        for item in models
                        if search in str(item.get("id", "")).lower()
                        or search in str(item.get("name", "")).lower()
                    ]
                models.sort(key=lambda item: int(item.get("created") or 0), reverse=True)
                self.send_json(200, {"models": [public_model(item) for item in models[:limit]]})
            else:
                self.send_json(404, {"error": "Not found"})
        except Exception as exc:
            self._handle_error(exc)

    def do_POST(self) -> None:
        acquired = False
        try:
            parsed = urllib.parse.urlparse(self.path)
            if not local_request_authorized(self.headers):
                self.send_json(401, {"error": "Unauthorized"})
                return
            data = self.read_json()
            handled, _paid = self._handle_v2_post(parsed.path, data)
            if handled:
                return
            if parsed.path in {
                "/run",
                "/compare",
                "/benchmarks/refresh",
                "/manus/tasks",
            }:
                acquired = _paid_slots.acquire(blocking=False)
                if not acquired:
                    raise GatewayError("Helios is busy; retry later", 429)
            if parsed.path == "/run":
                self.send_json(200, run_provider(data))
            elif parsed.path == "/manus/tasks":
                self.send_json(200, create_manus_task(data))
            elif parsed.path.startswith("/manus/tasks/") and parsed.path.endswith("/stop"):
                parts = [
                    urllib.parse.unquote(part)
                    for part in parsed.path.strip("/").split("/")
                    if part
                ]
                if len(parts) != 4 or parts[:2] != ["manus", "tasks"]:
                    self.send_json(404, {"error": "Not found"})
                else:
                    self.send_json(200, stop_manus_task(parts[2]))
            elif parsed.path == "/compare":
                self.send_json(200, compare_models(data))
            elif parsed.path == "/refresh-models":
                self.send_json(200, {"ok": True, "model_count": len(get_models(force=True))})
            elif parsed.path == "/benchmarks/refresh":
                only_if_stale = strict_bool(
                    data.get("only_if_stale"), "only_if_stale", False
                )
                self.send_json(
                    200,
                    refresh_registry(
                        openrouter_request,
                        get_models(force=True),
                        only_if_stale=only_if_stale,
                    ),
                )
            else:
                self.send_json(404, {"error": "Not found"})
        except Exception as exc:
            self._handle_error(exc)
        finally:
            if acquired:
                _paid_slots.release()


def main() -> None:
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(
        json.dumps(
            {
                "service": "helios-llm-orchestrator",
                "version": "2.0.0",
                "url": f"http://{HOST}:{PORT}",
                "env_file": str(ENV_FILE),
                "durable_project_memory": True,
            }
        ),
        flush=True,
    )
    httpd.serve_forever()


if __name__ == "__main__":
    main()
