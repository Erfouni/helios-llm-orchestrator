#!/usr/bin/env python3
"""Loopback-only Helios gateway for OpenRouter and public benchmark routing."""

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
except ModuleNotFoundError:
    from benchmark_registry import (  # type: ignore
        BenchmarkRegistryError,
        refresh_registry,
        registry_status,
        registry_view,
        select_benchmark_model,
    )


MODULE_DIR = Path(__file__).resolve().parent
BASE_DIR = MODULE_DIR.parent if (MODULE_DIR.parent / "config").exists() else MODULE_DIR
ENV_FILE = BASE_DIR / ".env"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL_CACHE_TTL_SECONDS = 600
REQUEST_BODY_LIMIT = 2 * 1024 * 1024
ALLOWED_MESSAGE_ROLES = {"system", "user", "assistant", "tool"}

_model_cache: dict[str, Any] = {"loaded_at": 0.0, "models": []}
_model_lock = threading.Lock()


class GatewayError(Exception):
    def __init__(self, message: str, status: int = 400, details: Any = None):
        super().__init__(message)
        self.status = status
        self.details = details


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
_paid_slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)

if HOST not in {"127.0.0.1", "::1", "localhost"}:
    raise RuntimeError("Helios must bind to loopback only (127.0.0.1 or ::1)")

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
            "deepseek/"
        )
    if normalized in {"qwen", "کوئن"}:
        return os.environ.get("DEFAULT_QWEN_MODEL", "").strip() or newest_matching("qwen/")
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
    server_version = "HeliosOrchestrator/1.2"

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
        if isinstance(exc, (GatewayError, BenchmarkRegistryError)):
            self.send_json(exc.status, {"error": str(exc), "details": exc.details})
            return
        self.log_error("Unhandled error: %s", repr(exc))
        self.send_json(500, {"error": "Internal error"})

    def do_GET(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/health" and not local_request_authorized(self.headers):
                self.send_json(401, {"error": "Unauthorized"})
                return
            if parsed.path == "/health":
                self.send_json(
                    200,
                    {
                        "ok": True,
                        "service": "helios-llm-orchestrator",
                        "version": "1.2.0",
                        "configured": api_key_configured(),
                        "loopback_only": True,
                        "benchmarks": safe_benchmark_status(),
                    },
                )
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
            if parsed.path in {"/run", "/compare", "/benchmarks/refresh"}:
                acquired = _paid_slots.acquire(blocking=False)
                if not acquired:
                    raise GatewayError("Helios is busy; retry later", 429)
            if parsed.path == "/run":
                self.send_json(200, run_model(data))
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
                "version": "1.2.0",
                "url": f"http://{HOST}:{PORT}",
                "env_file": str(ENV_FILE),
            }
        ),
        flush=True,
    )
    httpd.serve_forever()


if __name__ == "__main__":
    main()
