#!/usr/bin/env python3
"""Helios local multi-model gateway for OpenRouter.

The service binds to loopback only. The OpenRouter credential is loaded from the
process environment or macOS Keychain and is never returned by an endpoint.
"""

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

BASE_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = BASE_DIR / ".env"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL_CACHE_TTL_SECONDS = 600
REQUEST_BODY_LIMIT = 2 * 1024 * 1024

_model_cache: dict[str, Any] = {"loaded_at": 0.0, "models": []}
_model_lock = threading.Lock()


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_env_file(ENV_FILE)

HOST = os.environ.get("OPENROUTER_AGENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("OPENROUTER_AGENT_PORT", "3188"))
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "8192"))
MAX_PROMPT_CHARS = int(os.environ.get("MAX_PROMPT_CHARS", "400000"))
MAX_COMPARE_MODELS = int(os.environ.get("MAX_COMPARE_MODELS", "4"))
LOCAL_API_KEY = os.environ.get("HELIOS_LOCAL_API_KEY", "").strip()
KEYCHAIN_SERVICE = os.environ.get("OPENROUTER_KEYCHAIN_SERVICE", "helios-multimodel-router")
KEYCHAIN_ACCOUNT = os.environ.get("OPENROUTER_KEYCHAIN_ACCOUNT", "openrouter-api-key")

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


class GatewayError(Exception):
    def __init__(self, message: str, status: int = 400, details: Any = None):
        super().__init__(message)
        self.status = status
        self.details = details


def keychain_api_key() -> str:
    """Read the OpenRouter key from macOS Keychain without logging it."""
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
    value = str(headers.get("Authorization", ""))
    expected = "Bearer " + LOCAL_API_KEY
    return hmac.compare_digest(value, expected)


def openrouter_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 180,
) -> dict[str, Any]:
    url = OPENROUTER_BASE_URL + path
    headers = {
        "Authorization": "Bearer " + api_key(),
        "Content-Type": "application/json",
        "HTTP-Referer": "https://localhost.invalid/helios",
        "X-OpenRouter-Title": "Helios Multi-Model Router",
    }
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            details = json.loads(raw)
        except json.JSONDecodeError:
            details = raw[:2000]
        raise GatewayError(
            "OpenRouter request failed",
            status=502,
            details={"upstream_status": exc.code, "upstream": details},
        ) from exc
    except urllib.error.URLError as exc:
        raise GatewayError(
            "Could not reach OpenRouter",
            status=502,
            details=str(exc.reason),
        ) from exc
    except json.JSONDecodeError as exc:
        raise GatewayError("OpenRouter returned invalid JSON", status=502) from exc


def get_models(force: bool = False) -> list[dict[str, Any]]:
    now = time.time()
    with _model_lock:
        cached = _model_cache["models"]
        if cached and not force and now - _model_cache["loaded_at"] < MODEL_CACHE_TTL_SECONDS:
            return cached

    response = openrouter_request("GET", "/models", timeout=60)
    models = response.get("data")
    if not isinstance(models, list):
        raise GatewayError("OpenRouter model catalog response was invalid", 502)

    with _model_lock:
        _model_cache["models"] = models
        _model_cache["loaded_at"] = now
    return models


def newest_matching(
    prefix: str,
    required_terms: tuple[str, ...] = (),
    excluded_terms: tuple[str, ...] = (),
) -> str:
    candidates: list[dict[str, Any]] = []
    for item in get_models():
        model_id = str(item.get("id", "")).lower()
        name = str(item.get("name", "")).lower()
        haystack = model_id + " " + name
        if not model_id.startswith(prefix.lower()):
            continue
        if any(term not in haystack for term in required_terms):
            continue
        if any(term in haystack for term in excluded_terms):
            continue
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

    static = STATIC_ALIASES.get(normalized)
    if static:
        return static

    if normalized in {"gemini", "gemeni", "جمنای", "جمنایی"}:
        configured = os.environ.get("DEFAULT_GEMINI_MODEL", "").strip()
        return configured or newest_matching(
            "google/gemini",
            required_terms=("pro",),
            excluded_terms=("image", "audio", "embedding", "customtools"),
        )

    if normalized in {"gemini-flash", "gemeni-flash", "جمنای فلش"}:
        configured = os.environ.get("DEFAULT_GEMINI_FLASH_MODEL", "").strip()
        return configured or newest_matching(
            "google/gemini",
            required_terms=("flash",),
            excluded_terms=("image", "audio", "embedding", "customtools"),
        )

    if normalized in {"claude", "کلاد"}:
        configured = os.environ.get("DEFAULT_CLAUDE_MODEL", "").strip()
        return configured or newest_matching(
            "anthropic/claude",
            required_terms=("sonnet",),
        )

    if normalized in {"deepseek", "دیپ‌سیک", "دیپ سیک"}:
        configured = os.environ.get("DEFAULT_DEEPSEEK_MODEL", "").strip()
        return configured or newest_matching("deepseek/")

    if normalized in {"qwen", "کوئن"}:
        configured = os.environ.get("DEFAULT_QWEN_MODEL", "").strip()
        return configured or newest_matching("qwen/")

    if "/" in value:
        return value

    models = get_models()
    exact: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    for item in models:
        model_id = str(item.get("id", ""))
        name = str(item.get("name", ""))
        if normalized == model_id.lower() or normalized == name.lower():
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


def build_messages(data: dict[str, Any]) -> list[dict[str, Any]]:
    provided = data.get("messages")
    if provided is not None:
        if not isinstance(provided, list) or not provided:
            raise GatewayError("messages must be a non-empty array")
        return provided

    prompt = data.get("prompt", data.get("task", ""))
    if not isinstance(prompt, str) or not prompt.strip():
        raise GatewayError("prompt or task is required")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise GatewayError(
            "Prompt is too large",
            status=413,
            details={"max_prompt_chars": MAX_PROMPT_CHARS},
        )

    messages: list[dict[str, Any]] = []
    system = data.get("system")
    if isinstance(system, str) and system.strip():
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return messages


def run_model(data: dict[str, Any]) -> dict[str, Any]:
    requested = str(data.get("model", "")).strip()
    resolved = resolve_model(requested)
    max_tokens = int(data.get("max_tokens", MAX_OUTPUT_TOKENS))
    max_tokens = max(1, min(max_tokens, MAX_OUTPUT_TOKENS))

    payload: dict[str, Any] = {
        "model": resolved,
        "messages": build_messages(data),
        "max_tokens": max_tokens,
        "stream": False,
    }

    if data.get("temperature") is not None:
        payload["temperature"] = float(data["temperature"])
    if data.get("top_p") is not None:
        payload["top_p"] = float(data["top_p"])
    if data.get("reasoning_effort") in {"low", "medium", "high", "xhigh"}:
        payload["reasoning"] = {"effort": data["reasoning_effort"]}

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
    requested_models = data.get("models")
    if not isinstance(requested_models, list) or len(requested_models) < 2:
        raise GatewayError("models must contain at least two model names")
    if len(requested_models) > MAX_COMPARE_MODELS:
        raise GatewayError(
            "Too many models requested",
            details={"max_compare_models": MAX_COMPARE_MODELS},
        )

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(requested_models)) as executor:
        futures = {}
        for model in requested_models:
            request_data = dict(data)
            request_data.pop("models", None)
            request_data["model"] = str(model)
            futures[executor.submit(run_model, request_data)] = str(model)
        for future in as_completed(futures):
            model = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                if isinstance(exc, GatewayError):
                    results.append(
                        {
                            "model_requested": model,
                            "error": str(exc),
                            "details": exc.details,
                        }
                    )
                else:
                    results.append({"model_requested": model, "error": str(exc)})

    order = {str(model): index for index, model in enumerate(requested_models)}
    results.sort(key=lambda result: order.get(str(result.get("model_requested")), 999))
    return {"results": results}


def public_model(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "created": item.get("created"),
        "context_length": item.get("context_length"),
        "pricing": item.get("pricing"),
        "supported_parameters": item.get("supported_parameters"),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "HeliosRouter/1.0"

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
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(encoded)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise GatewayError("JSON request body is required")
        if length > REQUEST_BODY_LIMIT:
            raise GatewayError("Request body is too large", status=413)
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayError("Request body must be valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise GatewayError("Request body must be a JSON object")
        return value

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
                        "service": "helios-multimodel-router",
                        "configured": api_key_configured(),
                        "loopback_only": True,
                    },
                )
                return
            if parsed.path == "/models":
                query = urllib.parse.parse_qs(parsed.query)
                search = (query.get("search") or [""])[0].lower().strip()
                limit = min(max(int((query.get("limit") or ["50"])[0]), 1), 200)
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
                return
            self.send_json(404, {"error": "Not found"})
        except GatewayError as exc:
            self.send_json(exc.status, {"error": str(exc), "details": exc.details})
        except Exception as exc:
            self.send_json(500, {"error": "Internal error", "details": str(exc)})

    def do_POST(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            if not local_request_authorized(self.headers):
                self.send_json(401, {"error": "Unauthorized"})
                return
            data = self.read_json()
            if parsed.path == "/run":
                self.send_json(200, run_model(data))
                return
            if parsed.path == "/compare":
                self.send_json(200, compare_models(data))
                return
            if parsed.path == "/refresh-models":
                models = get_models(force=True)
                self.send_json(200, {"ok": True, "model_count": len(models)})
                return
            self.send_json(404, {"error": "Not found"})
        except GatewayError as exc:
            self.send_json(exc.status, {"error": str(exc), "details": exc.details})
        except Exception as exc:
            self.send_json(500, {"error": "Internal error", "details": str(exc)})


def main() -> None:
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(
        json.dumps(
            {
                "service": "helios-multimodel-router",
                "url": "http://%s:%d" % (HOST, PORT),
                "env_file": str(ENV_FILE),
            }
        ),
        flush=True,
    )
    httpd.serve_forever()


if __name__ == "__main__":
    main()
