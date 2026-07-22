#!/usr/bin/env python3
"""Web-only benchmark registry for Helios.

This module never benchmarks models itself. It uses OpenRouter's web-search
plugin to find current public leaderboards, validates citation provenance,
maps ranked names to the live OpenRouter catalog, and atomically publishes a
versioned local registry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


MODULE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MODULE_DIR.parent if (MODULE_DIR.parent / "config").exists() else MODULE_DIR
CONFIG_PATH = Path(
    os.environ.get(
        "HELIOS_BENCHMARK_CONFIG",
        str(PROJECT_ROOT / "config" / "benchmark_sources.json"),
    )
)

if os.environ.get("HELIOS_STATE_DIR"):
    STATE_DIR = Path(os.environ["HELIOS_STATE_DIR"]).expanduser()
elif os.name == "posix" and Path.home().joinpath("Library").exists():
    STATE_DIR = Path.home() / "Library" / "Application Support" / "Helios"
else:
    STATE_DIR = PROJECT_ROOT / "data" / "runtime"

REGISTRY_PATH = STATE_DIR / "benchmark_registry.json"
HISTORY_DIR = STATE_DIR / "benchmark_history"
_refresh_lock = threading.Lock()


class BenchmarkRegistryError(Exception):
    def __init__(self, message: str, status: int = 400, details: Any = None):
        super().__init__(message)
        self.status = status
        self.details = details


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BenchmarkRegistryError(
            "Benchmark source configuration is missing",
            503,
            {"path": str(path)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise BenchmarkRegistryError(
            "Benchmark source configuration is invalid JSON",
            500,
            {"path": str(path)},
        ) from exc
    if not isinstance(value, dict) or not isinstance(value.get("categories"), dict):
        raise BenchmarkRegistryError("Benchmark source configuration has an invalid schema", 500)
    return value


def empty_registry() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "status": "empty",
        "updated_at": None,
        "valid_until": None,
        "registry_hash": None,
        "selection_policy": "public_web_benchmark_only",
        "categories": {},
        "failures": [],
    }


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty_registry()
    except json.JSONDecodeError as exc:
        raise BenchmarkRegistryError(
            "Published benchmark registry is invalid JSON",
            500,
            {"path": str(path)},
        ) from exc
    if not isinstance(value, dict) or not isinstance(value.get("categories"), dict):
        raise BenchmarkRegistryError("Published benchmark registry has an invalid schema", 500)
    return value


def registry_hash(value: dict[str, Any]) -> str:
    canonical = dict(value)
    canonical.pop("registry_hash", None)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(str(temporary), str(path))


def _is_stale(valid_until: Any, now: datetime | None = None) -> bool:
    if not isinstance(valid_until, str):
        return True
    try:
        return datetime.fromisoformat(valid_until.replace("Z", "+00:00")) <= (now or utc_now())
    except ValueError:
        return True


def normalize_category(value: str) -> str:
    return "_".join(re.findall(r"[a-z0-9]+", str(value).strip().lower()))


def _category_valid_until(result: dict[str, Any], registry: dict[str, Any]) -> Any:
    return result.get("valid_until") or registry.get("valid_until")


def _result_quality_error(
    category: str, result: dict[str, Any], config: dict[str, Any] | None = None
) -> str | None:
    config = config or load_config()
    minimum = max(2, min(int(config.get("min_ranked_models", 3)), 5))
    blocked = {str(item).lower() for item in config.get("exclude_domains", [])}
    spec = config.get("categories", {}).get(category, {})
    allowed = {
        str(item).lower()
        for item in spec.get("allowed_domains", [])
        if isinstance(item, str)
    }
    ranking = result.get("ranking")
    if not isinstance(ranking, list):
        return "ranking is missing"
    names: set[str] = set()
    for item in ranking:
        if not isinstance(item, dict):
            continue
        name = _normalize(str(item.get("model_name", "")))
        url = item.get("source_url")
        if not name or not isinstance(url, str):
            continue
        hostname = (urllib.parse.urlsplit(url).hostname or "").lower()
        if any(hostname == domain or hostname.endswith("." + domain) for domain in blocked):
            return "ranking uses a blocked or aggregator source"
        if allowed and not any(
            hostname == domain or hostname.endswith("." + domain) for domain in allowed
        ):
            return "ranking source is not on the category primary-source allowlist"
        names.add(name)
    if len(names) < minimum:
        return f"ranking contains fewer than {minimum} distinct cited models"
    return None


def registry_status(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    registry = load_registry(path)
    valid_until = registry.get("valid_until")
    now = utc_now()
    try:
        config = load_config()
    except BenchmarkRegistryError:
        config = None
    low_quality_categories = sorted(
        category
        for category, result in registry.get("categories", {}).items()
        if not isinstance(result, dict)
        or (
            config is not None
            and _result_quality_error(category, result, config) is not None
        )
    )
    stale_categories = sorted(
        category
        for category, result in registry.get("categories", {}).items()
        if not isinstance(result, dict)
        or _is_stale(_category_valid_until(result, registry), now)
    )
    stale = (
        not registry.get("categories")
        or bool(stale_categories)
        or bool(low_quality_categories)
    )
    return {
        "status": registry.get("status", "unknown"),
        "updated_at": registry.get("updated_at"),
        "valid_until": valid_until,
        "stale": stale,
        "registry_hash": registry.get("registry_hash"),
        "category_count": len(registry.get("categories", {})),
        "stale_categories": stale_categories,
        "low_quality_categories": low_quality_categories,
        "failure_count": len(registry.get("failures", [])),
    }


def _normalize(value: str) -> str:
    value = value.lower().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", value))


def _tokens(value: str) -> set[str]:
    ignored = {
        "ai",
        "model",
        "preview",
        "latest",
        "instruct",
        "chat",
        "anthropic",
        "google",
        "openai",
        "moonshotai",
        "z",
    }
    return {token for token in _normalize(value).split() if token not in ignored}


def resolve_available_model(name: str, models: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Conservatively map a public leaderboard name to an OpenRouter model."""
    requested = _normalize(name)
    if not requested:
        return None
    exact: list[dict[str, Any]] = []
    scored: list[tuple[float, int, dict[str, Any]]] = []
    requested_tokens = _tokens(name)
    requested_numbers = {token for token in requested_tokens if any(ch.isdigit() for ch in token)}

    for item in models:
        model_id = str(item.get("id", ""))
        model_name = str(item.get("name", ""))
        haystack = _normalize(model_id + " " + model_name)
        if requested in {_normalize(model_id), _normalize(model_name)}:
            exact.append(item)
            continue
        candidate_tokens = _tokens(model_id + " " + model_name)
        if not requested_tokens or not candidate_tokens:
            continue
        if requested_numbers and not requested_numbers.issubset(candidate_tokens):
            continue
        overlap = len(requested_tokens & candidate_tokens) / len(requested_tokens)
        containment = 0.25 if requested in haystack else 0.0
        score = overlap + containment
        if score >= 0.67:
            scored.append((score, int(item.get("created") or 0), item))

    candidates = exact
    if not candidates and scored:
        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        candidates = [scored[0][2]]
    if not candidates:
        return None
    candidates.sort(key=lambda item: int(item.get("created") or 0), reverse=True)
    selected = candidates[0]
    return {
        "id": selected.get("id"),
        "name": selected.get("name"),
        "context_length": selected.get("context_length"),
        "pricing": selected.get("pricing"),
    }


def _extract_json(content: Any) -> dict[str, Any]:
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
        )
    if not isinstance(content, str) or not content.strip():
        raise BenchmarkRegistryError("Web-search model returned an empty response", 502)
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, count=1, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text, count=1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise BenchmarkRegistryError("Web-search model returned invalid JSON", 502) from exc
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as nested:
            raise BenchmarkRegistryError("Web-search model returned invalid JSON", 502) from nested
    if not isinstance(value, dict):
        raise BenchmarkRegistryError("Web-search model JSON must be an object", 502)
    return value


def _citation_urls(message: dict[str, Any]) -> set[str]:
    urls: set[str] = set()
    for annotation in message.get("annotations") or []:
        if not isinstance(annotation, dict):
            continue
        citation = annotation.get("url_citation")
        if not isinstance(citation, dict):
            continue
        url = citation.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            urls.add(_canonical_url(url))
    return urls


def _canonical_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query, "")
    )


def _safe_source_url(
    value: Any,
    citations: set[str],
    blocked_domains: set[str],
    allowed_domains: set[str] | None = None,
) -> str | None:
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        return None
    canonical = _canonical_url(value)
    parsed = urllib.parse.urlsplit(canonical)
    hostname = (parsed.hostname or "").lower()
    if any(hostname == domain or hostname.endswith("." + domain) for domain in blocked_domains):
        return None
    if allowed_domains and not any(
        hostname == domain or hostname.endswith("." + domain)
        for domain in allowed_domains
    ):
        return None
    if canonical not in citations:
        return None
    return canonical


def _build_prompt(
    category: str, spec: dict[str, Any], today: str, min_ranked_models: int
) -> str:
    benchmark_hints = ", ".join(str(item) for item in spec.get("benchmark_hints", []))
    allowed_domains = ", ".join(str(item) for item in spec.get("allowed_domains", []))
    return f"""Today is {today}. Perform web search only; do not run or simulate any model tests.

Find the latest publicly reported leaderboard or benchmark results for this task category:
Category: {category}
Search objective: {spec.get('query', '')}
Preferred benchmark names: {benchmark_hints or 'use the most relevant recognized public benchmark'}
Approved primary-source domains: {allowed_domains or 'none configured'}

Return JSON only with this exact shape:
{{
  "category": "{category}",
  "benchmark_name": "exact public benchmark or leaderboard name",
  "benchmark_date": "YYYY-MM-DD or null",
  "ranking": [
    {{
      "rank": 1,
      "model_name": "exact model name",
      "score": 0.0,
      "score_text": "score exactly as reported",
      "source_url": "exact URL from the web-search results",
      "source_title": "source title"
    }}
  ],
  "notes": "short factual note"
}}

Rules:
- Use only web-grounded public benchmark or leaderboard evidence.
- Use an official benchmark site, official leaderboard, or primary paper. Do not use
  an aggregator, marketing summary, display-only table, estimate, or composite ranking.
- Every source URL must be on the approved primary-source domain list above.
- Return between {min_ranked_models} and five distinct models from one comparable
  leaderboard and preserve the source's ranking.
- Every ranked item must contain an exact source URL provided by web search.
- Do not infer, average, estimate, or fabricate a score.
- If reliable comparable results are unavailable, return an empty ranking.
"""


def _refresh_category(
    category: str,
    spec: dict[str, Any],
    config: dict[str, Any],
    openrouter_request: Callable[..., dict[str, Any]],
    models: list[dict[str, Any]],
    today: str,
) -> dict[str, Any]:
    plugin: dict[str, Any] = {
        "id": "web",
        "engine": str(config.get("search_engine", "exa")),
        "max_results": int(config.get("max_results", 8)),
    }
    allowed = spec.get("allowed_domains")
    if isinstance(allowed, list) and allowed:
        plugin["allowed_domains"] = [str(item) for item in allowed]
    else:
        allowed = []
    excluded = config.get("exclude_domains")
    if not allowed and isinstance(excluded, list) and excluded:
        plugin["exclude_domains"] = [str(item) for item in excluded]

    payload = {
        "model": str(config.get("search_model", "openrouter/auto")),
        "messages": [
            {
                "role": "system",
                "content": "You extract current public benchmark rankings from web evidence and output strict JSON.",
            },
            {
                "role": "user",
                "content": _build_prompt(
                    category,
                    spec,
                    today,
                    max(2, min(int(config.get("min_ranked_models", 3)), 5)),
                ),
            },
        ],
        "plugins": [plugin],
        "response_format": {"type": "json_object"},
        "max_tokens": int(config.get("max_output_tokens", 2500)),
        "temperature": 0,
        "stream": False,
    }
    response = openrouter_request("POST", "/chat/completions", payload, timeout=240)
    choices = response.get("choices") or []
    message = choices[0].get("message", {}) if choices else {}
    raw_content = message.get("content")
    try:
        extracted = _extract_json(raw_content)
    except BenchmarkRegistryError:
        repair_payload = {
            "model": str(config.get("search_model", "openrouter/auto")),
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Repair the supplied malformed content into valid JSON only. "
                        "Preserve every factual value and URL; do not add facts."
                    ),
                },
                {
                    "role": "user",
                    "content": "Convert this response to the exact requested JSON object:\n\n"
                    + (
                        raw_content
                        if isinstance(raw_content, str)
                        else json.dumps(raw_content, ensure_ascii=False)
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": int(config.get("max_output_tokens", 2500)),
            "temperature": 0,
            "stream": False,
        }
        repaired = openrouter_request(
            "POST", "/chat/completions", repair_payload, timeout=180
        )
        repair_choices = repaired.get("choices") or []
        repair_message = (
            repair_choices[0].get("message", {}) if repair_choices else {}
        )
        extracted = _extract_json(repair_message.get("content"))
    citations = _citation_urls(message)
    blocked_domains = {str(item).lower() for item in config.get("exclude_domains", [])}
    allowed_domains = {str(item).lower() for item in allowed}

    ranking: list[dict[str, Any]] = []
    seen_models: set[str] = set()
    for position, raw in enumerate(extracted.get("ranking") or [], start=1):
        if not isinstance(raw, dict):
            continue
        model_name = str(raw.get("model_name", "")).strip()
        source_url = _safe_source_url(
            raw.get("source_url"), citations, blocked_domains, allowed_domains
        )
        model_key = _normalize(model_name)
        if not model_name or not source_url or not model_key or model_key in seen_models:
            continue
        seen_models.add(model_key)
        available = resolve_available_model(model_name, models)
        ranking.append(
            {
                "rank": int(raw.get("rank") or position),
                "model_name": model_name,
                "score": raw.get("score"),
                "score_text": str(raw.get("score_text", ""))[:200],
                "source_url": source_url,
                "source_title": str(raw.get("source_title", ""))[:300],
                "openrouter_model": available,
                "available_on_openrouter": available is not None,
            }
        )
    ranking.sort(key=lambda item: item["rank"])
    minimum = max(2, min(int(config.get("min_ranked_models", 3)), 5))
    if len(ranking) < minimum:
        raise BenchmarkRegistryError(
            "Public evidence did not meet the minimum comparable-model quality gate",
            422,
            {
                "category": category,
                "validated_model_count": len(ranking),
                "minimum_required": minimum,
            },
        )
    selected = next((item for item in ranking if item["available_on_openrouter"]), None)
    if not selected:
        raise BenchmarkRegistryError(
            "No cited ranked model could be matched to the live OpenRouter catalog",
            422,
            {"category": category, "cited_result_count": len(ranking)},
        )

    refreshed_at = utc_now()
    valid_days = max(1, min(int(config.get("valid_days", 8)), 31))
    return {
        "category": category,
        "benchmark_name": str(extracted.get("benchmark_name", ""))[:300],
        "benchmark_date": extracted.get("benchmark_date"),
        "selected_model": selected["openrouter_model"],
        "selected_rank": selected["rank"],
        "selected_score": selected["score"],
        "selected_score_text": selected["score_text"],
        "selection_source_url": selected["source_url"],
        "top_public_model": ranking[0]["model_name"],
        "openrouter_match_available": True,
        "evidence_model_count": len(ranking),
        "quality_gate": {
            "passed": True,
            "minimum_ranked_models": minimum,
            "primary_source_required": True,
            "allowed_domains": sorted(allowed_domains),
        },
        "ranking": ranking,
        "notes": str(extracted.get("notes", ""))[:1000],
        "selection_policy": "highest_cited_public_rank_available_on_openrouter",
        "refreshed_at": isoformat(refreshed_at),
        "valid_until": isoformat(refreshed_at + timedelta(days=valid_days)),
        "search_model_used": response.get("model"),
        "usage": response.get("usage", {}),
    }


def refresh_registry(
    openrouter_request: Callable[..., dict[str, Any]],
    models: list[dict[str, Any]],
    *,
    only_if_stale: bool = False,
    config_path: Path = CONFIG_PATH,
    registry_path: Path = REGISTRY_PATH,
) -> dict[str, Any]:
    if only_if_stale and not registry_status(registry_path)["stale"]:
        return {"ok": True, "skipped": True, **registry_status(registry_path)}
    if not _refresh_lock.acquire(blocking=False):
        raise BenchmarkRegistryError("A benchmark refresh is already running", 409)
    try:
        config = load_config(config_path)
        previous = load_registry(registry_path)
        now = utc_now()
        today = now.date().isoformat()
        categories = config.get("categories", {})
        max_parallel = max(1, min(int(config.get("max_parallel", 3)), 6))
        refreshed: dict[str, Any] = {}
        failures: list[dict[str, Any]] = []

        with ThreadPoolExecutor(max_workers=max_parallel) as executor:
            futures = {
                executor.submit(
                    _refresh_category,
                    str(category),
                    spec,
                    config,
                    openrouter_request,
                    models,
                    today,
                ): str(category)
                for category, spec in categories.items()
                if isinstance(spec, dict) and spec.get("enabled", True)
            }
            for future in as_completed(futures):
                category = futures[future]
                try:
                    refreshed[category] = future.result()
                except Exception as exc:
                    details = exc.details if isinstance(exc, BenchmarkRegistryError) else None
                    failures.append(
                        {"category": category, "error": str(exc), "details": details}
                    )

        if not refreshed:
            raise BenchmarkRegistryError(
                "Benchmark refresh produced no valid categories; previous registry was preserved",
                502,
                {"failures": failures},
            )

        valid_days = max(1, min(int(config.get("valid_days", 8)), 31))
        previous_valid_until = previous.get("valid_until")
        merged_categories = {}
        for name, result in previous.get("categories", {}).items():
            if isinstance(result, dict):
                migrated = dict(result)
                migrated.setdefault("valid_until", previous_valid_until)
                merged_categories[name] = migrated
            else:
                merged_categories[name] = result
        merged_categories.update(refreshed)
        category_expiries: list[datetime] = []
        for result in merged_categories.values():
            if not isinstance(result, dict):
                continue
            expiry = result.get("valid_until")
            if isinstance(expiry, str):
                try:
                    category_expiries.append(
                        datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                    )
                except ValueError:
                    pass
        earliest_expiry = (
            min(category_expiries)
            if category_expiries
            else now + timedelta(days=valid_days)
        )
        stale_categories = [
            name
            for name, result in merged_categories.items()
            if not isinstance(result, dict)
            or _is_stale(_category_valid_until(result, previous), now)
        ]
        value = {
            "schema_version": 2,
            "status": "current" if not failures and not stale_categories else "partial",
            "updated_at": isoformat(now),
            "valid_until": isoformat(earliest_expiry),
            "registry_hash": None,
            "selection_policy": "public_web_benchmark_only",
            "categories": merged_categories,
            "task_aliases": config.get("task_aliases", {}),
            "failures": failures,
        }
        value["registry_hash"] = registry_hash(value)

        history_dir = registry_path.parent / "benchmark_history"
        history_dir.mkdir(parents=True, exist_ok=True)
        history_path = history_dir / (now.strftime("%Y%m%dT%H%M%SZ") + ".json")
        _atomic_write_json(history_path, value)
        _atomic_write_json(registry_path, value)
        return {
            "ok": True,
            "skipped": False,
            "status": value["status"],
            "updated_at": value["updated_at"],
            "valid_until": value["valid_until"],
            "registry_hash": value["registry_hash"],
            "refreshed_categories": sorted(refreshed),
            "preserved_category_count": len(merged_categories) - len(refreshed),
            "failures": failures,
            "registry_path": str(registry_path),
            "history_path": str(history_path),
        }
    finally:
        _refresh_lock.release()


def registry_view(category: str | None = None, path: Path = REGISTRY_PATH) -> dict[str, Any]:
    registry = load_registry(path)
    if not category:
        return registry
    aliases = {
        normalize_category(str(key)): normalize_category(str(value))
        for key, value in registry.get("task_aliases", {}).items()
    }
    requested_key = normalize_category(category)
    canonical = aliases.get(requested_key, requested_key)
    value = registry.get("categories", {}).get(canonical)
    if value is None:
        raise BenchmarkRegistryError(
            "Unknown benchmark category",
            404,
            {"requested": category, "available": sorted(registry.get("categories", {}))},
        )
    category_valid_until = _category_valid_until(value, registry)
    return {
        "requested_category": category,
        "category": canonical,
        "registry_hash": registry.get("registry_hash"),
        "updated_at": registry.get("updated_at"),
        "valid_until": category_valid_until,
        "stale": _is_stale(category_valid_until),
        "result": value,
    }


def select_benchmark_model(category: str, path: Path = REGISTRY_PATH) -> dict[str, Any]:
    view = registry_view(category, path)
    if view["stale"]:
        raise BenchmarkRegistryError(
            "Benchmark evidence for this category is stale; refresh the registry before selection",
            503,
            {"category": view["category"], "valid_until": view["valid_until"]},
        )
    result = view["result"]
    quality_error = _result_quality_error(view["category"], result)
    if quality_error:
        raise BenchmarkRegistryError(
            "Benchmark evidence for this category does not pass current quality gates",
            422,
            {"category": view["category"], "reason": quality_error},
        )
    selected = result.get("selected_model")
    if not isinstance(selected, dict) or not selected.get("id"):
        raise BenchmarkRegistryError("No OpenRouter model is selected for this category", 404)
    return {
        "requested_category": category,
        "category": view["category"],
        "model": selected,
        "benchmark_name": result.get("benchmark_name"),
        "benchmark_date": result.get("benchmark_date"),
        "score": result.get("selected_score"),
        "score_text": result.get("selected_score_text"),
        "source_url": result.get("selection_source_url"),
        "selection_policy": result.get("selection_policy"),
        "registry_hash": view.get("registry_hash"),
        "updated_at": view.get("updated_at"),
        "valid_until": view.get("valid_until"),
        "stale": False,
    }
