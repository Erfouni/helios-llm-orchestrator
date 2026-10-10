#!/usr/bin/env python3
"""Web-only benchmark registry for Helios.

This module never benchmarks models itself. It reads approved official data
files or cited public web evidence, validates comparable dated results, and
atomically publishes a versioned local registry. Selection checks the exact
evaluated configuration against live OpenRouter capabilities.
"""

from __future__ import annotations

try:
    from agent.model_parameters import validate_parameters, output_limit, PARAMETERS
except ModuleNotFoundError:
    from model_parameters import validate_parameters, output_limit, PARAMETERS

import csv
import hashlib
import json
import math
import os
import re
import threading
from collections import Counter
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt


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
_GATEWAY_REASONING_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


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


def _lock_file(descriptor: int) -> None:
    """Take an exclusive, non-blocking OS lock; the OS drops it if the process dies."""
    os.lseek(descriptor, 0, os.SEEK_SET)
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)


def _unlock_file(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    else:
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)


@contextmanager
def _refresh_guard(registry_path: Path) -> Iterator[None]:
    """Allow one refresh at a time across threads and processes.

    The scheduled refresh runs scripts/refresh-benchmarks.py in its own process
    while the agent can be refreshing for an MCP caller. Both would pay for the
    same searches and write the same temporary file.
    """
    if not _refresh_lock.acquire(blocking=False):
        raise BenchmarkRegistryError("A benchmark refresh is already running", 409)
    try:
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            str(registry_path.with_name(registry_path.name + ".lock")),
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        try:
            try:
                _lock_file(descriptor)
            except OSError as exc:
                raise BenchmarkRegistryError(
                    "A benchmark refresh is already running", 409
                ) from exc
            try:
                yield
            finally:
                _unlock_file(descriptor)
        finally:
            os.close(descriptor)
    finally:
        _refresh_lock.release()


def _is_stale(valid_until: Any, now: datetime | None = None) -> bool:
    if not isinstance(valid_until, str):
        return True
    try:
        return datetime.fromisoformat(valid_until.replace("Z", "+00:00")) <= (now or utc_now())
    except (ValueError, TypeError):
        return True


def normalize_category(value: str) -> str:
    return "_".join(re.findall(r"[a-z0-9]+", str(value).strip().lower()))


def _category_valid_until(result: dict[str, Any], registry: dict[str, Any]) -> Any:
    return result.get("valid_until") or registry.get("valid_until")


def _enabled_categories(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(category): spec
        for category, spec in config.get("categories", {}).items()
        if isinstance(spec, dict) and spec.get("enabled", True)
    }


def _evidence_date(value: Any) -> date | None:
    """Parse an ISO date/timestamp; a month means its first day, conservatively."""
    value = str(value or "").strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}", value):
            return date.fromisoformat(value + "-01")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        if re.match(r"\d{4}-\d{2}-\d{2}T", value):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    return None


def _minimum_models(config: dict[str, Any]) -> int:
    return max(3, min(int(config.get("min_ranked_models", 3)), 5))


def _max_evidence_age(config: dict[str, Any]) -> int:
    return max(1, min(int(config.get("max_evidence_age_days", 180)), 730))


def _numeric_score(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _model_group(item: dict[str, Any]) -> str:
    if isinstance(item.get("model_group"), str) and item["model_group"].strip():
        return _normalize(item["model_group"])
    name = _identity_label(str(item.get("model_name", "")))
    name = re.sub(r"(?:[-\s]+20\d{6})?[-\s]+(?:xhigh|high|medium|low)(?:[-\s]+20\d{6})?$", "", name, flags=re.I)
    return _normalize(re.sub(r"[-\s]+20\d{6}$", "", name.strip()))


def _rank(value: Any, fallback: int) -> int:
    # The search model may write "1st" or "#2" instead of a number.
    match = None if isinstance(value, bool) else re.match(r"\s*#?(\d+)", str(value))
    return int(match[1]) if match else fallback


def _result_quality_error(
    category: str, result: dict[str, Any], config: dict[str, Any] | None = None,
    *, today: date | None = None,
) -> str | None:
    config = config or load_config()
    minimum = _minimum_models(config)
    blocked = {str(item).lower() for item in config.get("exclude_domains", [])}
    spec = config.get("categories", {}).get(category, {})
    allowed = {
        str(item).lower()
        for item in spec.get("allowed_domains", [])
        if isinstance(item, str)
    }
    if not allowed:
        return "category primary-source allowlist is missing"
    ranking = result.get("ranking")
    if not isinstance(ranking, list):
        return "ranking is missing"
    names: set[str] = set()
    sources: set[tuple[str, str]] = set()
    comparable: dict[str, set[str]] = {"dataset_id": set(), "score_metric": set()}
    for item in ranking:
        if not isinstance(item, dict):
            return "ranking contains an invalid row"
        name = _model_group(item)
        url = item.get("source_url")
        if not name or not isinstance(url, str):
            return "ranking contains an unnamed or uncited model"
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            return "ranking source is not an approved HTTP URL"
        hostname = (parsed.hostname or "").lower()
        if any(hostname == domain or hostname.endswith("." + domain) for domain in blocked):
            return "ranking uses a blocked or aggregator source"
        if allowed and not any(
            hostname == domain or hostname.endswith("." + domain) for domain in allowed
        ):
            return "ranking source is not on the category primary-source allowlist"
        if not _numeric_score(item.get("score")):
            return "ranking score must be a finite numeric value"
        sources.add(_citation_key(url))
        for key in comparable:
            if item.get(key) is not None:
                comparable[key].add(str(item[key]))
        names.add(name)
    if len(names) < minimum:
        return f"ranking contains fewer than {minimum} distinct cited base model groups"
    if any(len(values) > 1 for values in comparable.values()):
        return "ranking scores use different datasets or metrics and are not comparable"
    if len(sources) > 1 and not all(
        item.get("dataset_id") and item.get("score_metric") for item in ranking
    ):
        return "ranking combines different source pages without a shared dataset and score metric"
    evidence_date = _evidence_date(result.get("benchmark_date"))
    if evidence_date is None:
        return "benchmark evidence date is missing or invalid"
    age = ((today or utc_now().date()) - evidence_date).days
    if age < 0:
        return "benchmark evidence date is in the future"
    if age > _max_evidence_age(config):
        return "benchmark evidence is older than the configured maximum age"
    generated_at = result.get("source_generated_at")
    if isinstance(generated_at, str) and "T" in generated_at:
        try:
            generated_time = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            if generated_time.tzinfo is None or generated_time > utc_now():
                return "source generation timestamp is unzoned or in the future"
        except ValueError:
            return "source generation timestamp is invalid"
    return None


def registry_status(
    path: Path = REGISTRY_PATH, config_path: Path = CONFIG_PATH
) -> dict[str, Any]:
    registry = load_registry(path)
    valid_until = registry.get("valid_until")
    now = utc_now()
    try:
        config = load_config(config_path)
    except BenchmarkRegistryError:
        config = None
    categories = registry.get("categories", {})
    missing_categories: list[str] = []
    if config is not None:
        # A category removed from or disabled in the config is never refreshed
        # again, so its evidence must not keep the whole registry stale.
        enabled = _enabled_categories(config)
        categories = {name: result for name, result in categories.items() if name in enabled}
        missing_categories = sorted(set(enabled) - set(categories))
    low_quality_categories = sorted(
        category
        for category, result in categories.items()
        if not isinstance(result, dict)
        or (
            config is not None
            and _result_quality_error(category, result, config) is not None
        )
    )
    stale_categories = sorted(
        category
        for category, result in categories.items()
        if not isinstance(result, dict)
        or _is_stale(_category_valid_until(result, registry), now)
    )
    stale = (
        not categories
        or bool(stale_categories)
        or bool(low_quality_categories)
    )
    return {
        "status": registry.get("status", "unknown"),
        "updated_at": registry.get("updated_at"),
        "valid_until": valid_until,
        "stale": stale,
        "registry_hash": registry.get("registry_hash"),
        "category_count": len(categories),
        "stale_categories": stale_categories,
        "low_quality_categories": low_quality_categories,
        "missing_categories": missing_categories,
        "failure_count": len(registry.get("failures", [])),
    }


def _normalize(value: str) -> str:
    value = value.lower().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", value))


_IGNORED_TOKENS = {
    "ai",
    "model",
    "latest",
    "instruct",
    "it",
    "chat",
    "anthropic",
    "google",
    "openai",
    "moonshotai",
    "z",
}
# Words leaderboards add to say how a model was run ("GPT-5 (high)", "(FC)").
# They do not name the model, but in an OpenRouter slug some of them do name a
# different one (qwen3-max, gpt-5-codex), so sibling checks keep them.
_RUN_ANNOTATION_TOKENS = {
    "max",
    "high",
    "xhigh",
    "medium",
    "low",
    "reasoning",
    "grounding",
    "codex",
    "harness",
    "fc",
    "prompt",
}


# A ranked name may also carry these without naming a different model: run
# descriptions, "thinking", "Non-reasoning", and a vendor next to the model.
_DROPPABLE_TOKENS = _IGNORED_TOKENS | (_RUN_ANNOTATION_TOKENS - {"max", "reasoning", "codex"}) | {
    "thinking",
    "non",
    "alibaba",
    "meta",
    "xai",
    "microsoft",
    "amazon",
}


def _identity_label(name: str) -> str:
    def annotation(match: re.Match[str]) -> str:
        value = match.group(0)[1:-1]
        # Parentheses can describe an actual sibling, not just run settings.
        if _normalize(value) in {"mini", "nano", "pro", "turbo", "flash", "lite", "image", "audio", "video", "codex", "omni"}:
            return " " + value + " "
        return " "
    return re.sub(r"\([^)]*\)|\[[^\]]*\]", annotation, name)


def _token_list(value: str) -> list[str]:
    result: list[str] = []
    for token in _normalize(value).split():
        if token in _IGNORED_TOKENS:
            continue
        # Leaderboards often append evaluation/release dates that are absent from
        # the canonical API model slug. Treat YYYYMMDD as metadata, not identity.
        if re.fullmatch(r"20\d{6}", token):
            continue
        result.append(token)
    return result


def _is_number(token: str) -> bool:
    return any(ch.isdigit() for ch in token)


def _is_snapshot_token(token: str) -> bool:
    # 0905, 2507, 20250514: a dated release of the same model.
    return token.isdigit() and len(token) >= 4


def resolve_available_model(name: str, models: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Conservatively map a public leaderboard name to an OpenRouter model.

    A sibling must never stand in for the ranked model. Every word and version
    number of the ranked name, apart from parenthesized notes and run
    descriptions, must be in the candidate, and the candidate may add nothing
    but a dated snapshot suffix. So "GPT-5 (high)" is gpt-5, never gpt-5.5,
    gpt-5-nano or gpt-5-image, and a name with no such model stays unmatched.
    Version numbers are counted, not just looked up, because 5.5 is not 5.
    """
    requested = _normalize(name)
    if not requested:
        return None
    every = Counter(_token_list(name))
    core = [
        token
        for token in _token_list(_identity_label(name))
        if token not in _DROPPABLE_TOKENS
    ]
    if not core:
        return None
    core_words = {token for token in core if not _is_number(token)}
    core_numbers = Counter(token for token in core if _is_number(token))
    specialized_markers = {"image", "audio", "video", "embedding", "customtools", "batch"}
    exact: list[dict[str, Any]] = []
    scored: list[tuple[int, int, int, dict[str, Any]]] = []

    for item in models:
        model_id = str(item.get("id", ""))
        model_name = str(item.get("name", ""))
        # OpenRouter ids are "provider/slug" and names "Provider: Name", so a
        # bare leaderboard name only ever equals the part without the provider.
        slug = model_id.split("/", 1)[-1]
        short_name = model_name.split(": ", 1)[-1]
        haystack = _normalize(model_id + " " + model_name)
        candidate_all_tokens = set(haystack.split())
        requested_all_tokens = set(requested.split())
        if any(
            marker in candidate_all_tokens and marker not in requested_all_tokens
            for marker in specialized_markers
        ):
            continue
        if requested in {_normalize(value) for value in (model_id, model_name, slug, short_name)}:
            exact.append(item)
            continue
        if not core_words <= set(_token_list(model_id + " " + model_name)):
            continue
        best: tuple[int, int] | None = None
        for label in (slug, short_name):
            tokens = Counter(_token_list(label))
            if core_numbers - Counter({t: n for t, n in tokens.items() if _is_number(t)}):
                continue
            extras = tokens - every
            if not all(_is_snapshot_token(token) for token in extras):
                continue
            # Prefer the candidate that carries more of the ranked name, so
            # "Sonar Reasoning (high)" picks sonar-reasoning over sonar.
            key = (sum((tokens & every).values()), -sum(extras.values()))
            if best is None or key > best:
                best = key
        if best is not None:
            scored.append((*best, int(item.get("created") or 0), item))

    candidates = exact
    if not candidates and scored:
        scored.sort(key=lambda row: row[:3], reverse=True)
        candidates = [scored[0][3]]
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


def _citation_key(value: str) -> tuple[str, str]:
    parsed = urllib.parse.urlsplit(value.strip())
    hostname = (parsed.hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"
    return hostname, path


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
        key = _citation_key(canonical)
        if key not in {_citation_key(item) for item in citations}:
            return None
    return canonical


def _build_prompt(
    category: str,
    spec: dict[str, Any],
    today: str,
    min_ranked_models: int,
    max_evidence_age_days: int,
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
      "model_group": "base model identity without run settings",
      "dataset_id": "exact comparable benchmark split",
      "score_metric": "exact score metric and unit",
      "evaluation_settings": {{}},
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
- Prefer the actual leaderboard/results page over a blog post, news story, or summary.
- Return between {min_ranked_models} and five distinct models from one comparable
  leaderboard and preserve the source's ranking. Use one leaderboard URL for all rows
  whenever that page contains the compared models.
- Every ranked item must contain a source URL that was actually returned and cited by web search.
- Use the latest edition. Results published more than {max_evidence_age_days} days
  before today are rejected; report the publication or last-update date.
- Do not infer, average, estimate, or fabricate a score.
- Preserve the exact model variant, run annotations, and evaluated parameter values
  in model_name and evaluation_settings. Do not replace an effort such as Max with High.
- If reliable comparable results are unavailable, return an empty ranking.
"""



def _parse_numeric_score(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    text = str(value if value is not None else "").strip().replace(",", "")
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*%?", text):
        return None
    number = float(text.rstrip("% "))
    return number if math.isfinite(number) else None


def _evaluation_settings(item: dict[str, Any]) -> dict[str, Any]:
    """Retain exact run settings, including notes we cannot safely translate."""
    settings = item.get("evaluation_settings")
    name = str(item.get("model_name", ""))
    notes = re.findall(r"\(([^)]*)\)|\[([^\]]*)\]", name)
    annotations = [left or right for left, right in notes]
    suffix = re.search(r"[-\s](xhigh|high|medium|low)(?:[-\s]20\d{6})?$", re.sub(r"\([^)]*\)|\[[^\]]*\]", "", name).strip(), re.I)
    if suffix:
        annotations.append(suffix.group(1))
    result: dict[str, Any] = {}
    unmapped: list[str] = []
    for annotation in annotations:
        effort = re.fullmatch(r"\s*(?:reasoning\s+)?(max|xhigh|high|medium|low)(?:\s+effort)?\s*", annotation, re.I)
        if effort:
            value = effort.group(1).lower()
            if "reasoning_effort" in result and result["reasoning_effort"] != value:
                unmapped.append(annotation)
            result["reasoning_effort"] = value
        elif annotation.strip().lower() not in {"fc", "prompt", "mini", "nano", "pro", "turbo", "flash", "lite", "image", "audio", "video", "codex", "omni"}:
            unmapped.append(annotation)
    if isinstance(settings, dict):
        for key, value in settings.items():
            if key == "unmapped_settings":
                unmapped.extend(str(part) for part in value) if isinstance(value, list) else unmapped.append(str(value))
            elif key in result and result[key] != value:
                unmapped.append("structured settings conflict with the evaluated variant label")
            else:
                result[key] = value
    elif "evaluation_settings" in item:
        unmapped.append("evaluation settings are not a valid object")
    if unmapped:
        result["unmapped_settings"] = list(dict.fromkeys(unmapped))
    return result


def _finalize_result(
    category: str, spec: dict[str, Any], config: dict[str, Any],
    ranking: list[dict[str, Any]], benchmark_date: Any, *,
    benchmark_name: str | None = None, today: date | None = None,
    **metadata: Any,
) -> dict[str, Any]:
    """All adapters publish through the same source/date/score/model-group gate."""
    ranking = sorted(ranking, key=lambda row: _rank(row.get("rank"), 999999))
    # Keep the best reported configuration of each base model, not five runs
    # of the same model masquerading as a comparable multi-model leaderboard.
    distinct: dict[str, dict[str, Any]] = {}
    for row in ranking:
        group = _model_group(row)
        item = dict(row, model_group=row.get("model_group") or group, evaluation_settings=_evaluation_settings(row))
        distinct.setdefault(group, item)
    ranking = list(distinct.values())[:5]
    result = {
        "category": category,
        "benchmark_name": str(benchmark_name or spec.get("benchmark_name", category))[:300],
        "benchmark_date": benchmark_date,
        "ranking": ranking,
        **metadata,
    }
    validation_config = dict(config, categories={**config.get("categories", {}), category: spec})
    error = _result_quality_error(category, result, validation_config, today=today)
    if error:
        raise BenchmarkRegistryError(
            "Public evidence did not meet the comparable-model quality gate",
            422,
            {"category": category, "reason": error, "benchmark_date": benchmark_date,
             "validated_model_count": len(ranking), "minimum_required": _minimum_models(config),
             "max_evidence_age_days": _max_evidence_age(config)},
        )
    selected = next((item for item in ranking if item.get("available_on_openrouter")), None)
    top = ranking[0]
    refreshed_at = utc_now()
    validity = refreshed_at + timedelta(days=max(1, min(int(config.get("valid_days", 8)), 31)))
    evidence_day = _evidence_date(benchmark_date)
    # Re-fetching old evidence cannot extend it beyond the evidence age gate.
    evidence_expiry = datetime.combine(evidence_day + timedelta(days=_max_evidence_age(config) + 1), datetime.min.time(), timezone.utc)
    result.update({
        "selected_model": selected["openrouter_model"] if selected else None,
        "selected_rank": selected["rank"] if selected else None,
        "selected_score": selected["score"] if selected else None,
        "selected_score_text": selected.get("score_text") if selected else None,
        "selected_evaluation_settings": selected["evaluation_settings"] if selected else None,
        "selection_source_url": (selected or top)["source_url"],
        "top_public_model": top["model_name"],
        "openrouter_match_available": selected is not None,
        "evidence_model_count": len(ranking),
        "quality_gate": {"passed": True, "minimum_ranked_models": _minimum_models(config),
                         "primary_source_required": True, "allowed_domains": spec.get("allowed_domains", [])},
        "selection_policy": ("highest_cited_public_rank_available_on_openrouter" if selected
                             else "fresh_public_benchmark_no_openrouter_match"),
        "refreshed_at": isoformat(refreshed_at),
        "valid_until": isoformat(min(validity, evidence_expiry)),
        **metadata,
    })
    return result


def _approved_source_url(value: str, allowed_domains: set[str]) -> str:
    canonical = _canonical_url(value)
    parsed = urllib.parse.urlsplit(canonical)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not any(hostname == domain or hostname.endswith("." + domain) for domain in allowed_domains):
        raise BenchmarkRegistryError(
            "Official source adapter URL is outside the category allowlist",
            500,
            {"url": canonical, "allowed_domains": sorted(allowed_domains)},
        )
    return canonical


def _fetch_text(url: str, timeout: int = 45) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Helios-Benchmark-Refresh/2.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise BenchmarkRegistryError(
            "Could not fetch approved official benchmark source",
            502,
            {"url": url},
        ) from exc


def _refresh_official_csv_category(
    category: str,
    spec: dict[str, Any],
    config: dict[str, Any],
    models: list[dict[str, Any]],
) -> dict[str, Any]:
    allowed_domains = {
        str(item).lower() for item in spec.get("allowed_domains", []) if isinstance(item, str)
    }
    if not allowed_domains:
        raise BenchmarkRegistryError("Official CSV adapter requires allowed_domains", 500)
    data_url = _approved_source_url(str(spec.get("data_url", "")), allowed_domains)
    source_url = _approved_source_url(str(spec.get("source_url", data_url)), allowed_domains)
    text = _fetch_text(data_url)
    reader = csv.DictReader(text.splitlines())
    rank_column = str(spec.get("rank_column", "Rank"))
    model_column = str(spec.get("model_column", "Model"))
    score_column = str(spec.get("score_column", "Score"))
    ranking: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, row in enumerate(reader, start=1):
        model_name = str(row.get(model_column, "")).strip()
        key = _normalize(model_name)
        if not model_name or not key or key in seen:
            continue
        seen.add(key)
        try:
            rank = int(str(row.get(rank_column, position)).strip())
        except ValueError:
            rank = position
        score_text = str(row.get(score_column, "")).strip()
        available = resolve_available_model(model_name, models)
        ranking.append(
            {
                "rank": rank,
                "model_name": model_name,
                "score": _parse_numeric_score(score_text),
                "score_text": score_text[:200],
                "source_url": source_url,
                "source_title": str(spec.get("benchmark_name", category))[:300],
                "openrouter_model": available,
                "available_on_openrouter": available is not None,
            }
        )
    ranking.sort(key=lambda item: item["rank"])
    benchmark_date = None
    date_regex = spec.get("date_regex")
    if isinstance(date_regex, str) and date_regex:
        page_text = _fetch_text(source_url)
        match = re.search(date_regex, page_text, flags=re.IGNORECASE)
        if match:
            benchmark_date = match.group(1)
    return _finalize_result(
        category, spec, config, ranking, benchmark_date,
        notes=str(spec.get("notes", "Fetched directly from an approved official leaderboard data file."))[:1000],
        evidence_method="approved_official_csv", data_source_url=data_url,
        search_model_used=None, usage={},
    )


def _refresh_arc_agi_category(
    category: str, spec: dict[str, Any], config: dict[str, Any], models: list[dict[str, Any]],
) -> dict[str, Any]:
    """Read ARC Prize's official leaderboard, never infer a table using an LLM."""
    allowed = {str(domain).lower() for domain in spec.get("allowed_domains", [])}
    data_url = _approved_source_url(str(spec.get("data_url", "")), allowed)
    source_url = _approved_source_url(str(spec.get("source_url", data_url)), allowed)
    try:
        payload = json.loads(_fetch_text(data_url))
    except (TypeError, json.JSONDecodeError) as exc:
        raise BenchmarkRegistryError("Official ARC-AGI source returned invalid JSON", 502) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("evaluations"), list):
        raise BenchmarkRegistryError("Official ARC-AGI JSON schema is invalid", 422)
    dataset = str(spec.get("dataset_id", "v2_Semi_Private"))
    ranking: list[dict[str, Any]] = []
    for raw in payload["evaluations"]:
        if not isinstance(raw, dict) or raw.get("datasetId") != dataset or raw.get("display") is not True:
            continue
        name = str(raw.get("modelDisplayName", "")).strip()
        if (str(raw.get("modelType", "")).lower() != "cot"
                or re.search(r"\b(?:human|custom|estimate[d]?|preview)\b", name, re.I)
                or raw.get("estimated") or raw.get("isEstimate") or raw.get("preview")):
            continue
        release = _evidence_date(raw.get("modelReleaseDate"))
        if release is not None and release > utc_now().date():
            continue
        score = raw.get("score")
        if not name or not raw.get("modelGroup") or not _numeric_score(score) or not 0 <= score <= 1:
            raise BenchmarkRegistryError("Official ARC-AGI evaluation has invalid model identity or score", 422)
        results_url = urllib.parse.urljoin(source_url, str(raw.get("resultsUrl", "")))
        results_url = _approved_source_url(results_url, allowed)
        available = resolve_available_model(name, models)
        ranking.append({
            "model_name": name, "model_group": str(raw["modelGroup"]),
            "model_id": raw.get("modelId"), "model_type": raw.get("modelType"),
            "provider_id": raw.get("providerId"), "model_release_date": raw.get("modelReleaseDate"),
            "dataset_id": dataset, "score_metric": "accuracy_percent",
            "score": round(float(score) * 100, 10), "score_text": f"{float(score) * 100:g}%",
            "cost_per_task": raw.get("costPerTask"), "results_url": results_url,
            "source_url": source_url, "source_title": str(spec.get("benchmark_name", "ARC-AGI-2")),
            "openrouter_model": available, "available_on_openrouter": available is not None,
        })
    ranking.sort(key=lambda item: (-item["score"], item["model_group"], item["model_name"]))
    for position, row in enumerate(ranking, start=1):
        row["rank"] = position
    generated_at = payload.get("generatedAt")
    evidence_date = _evidence_date(generated_at)
    return _finalize_result(
        category, spec, config, ranking, evidence_date.isoformat() if evidence_date else None,
        notes=str(spec.get("notes", "Official ARC-AGI-2 semi-private CoT evaluations; best configuration per model group."))[:1000],
        evidence_method="approved_official_arc_agi_json", data_source_url=data_url,
        source_generated_at=generated_at, source_version=payload.get("version"),
        search_model_used=None, usage={},
    )


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
    max_age_days = _max_evidence_age(config)

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
                    _minimum_models(config),
                    max_age_days,
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
                "rank": _rank(raw.get("rank"), position),
                "model_name": model_name,
                "score": raw.get("score"),
                "score_text": str(raw.get("score_text", ""))[:200],
                "source_url": source_url,
                "source_title": str(raw.get("source_title", ""))[:300],
                "openrouter_model": available,
                "available_on_openrouter": available is not None,
                **{key: raw[key] for key in ("model_group", "dataset_id", "score_metric", "evaluation_settings") if key in raw},
            }
        )
    ranking.sort(key=lambda item: item["rank"])
    return _finalize_result(
        category, spec, config, ranking, extracted.get("benchmark_date"),
        benchmark_name=str(extracted.get("benchmark_name", "")), today=date.fromisoformat(today),
        notes=str(extracted.get("notes", ""))[:1000],
        evidence_method="cited_web_extraction", search_model_used=response.get("model"),
        usage=response.get("usage", {}),
    )


def _needs_refresh(
    category: str, registry: dict[str, Any], config: dict[str, Any], now: datetime
) -> bool:
    """True when a category's evidence is missing, expiring, or below the quality gates.

    Evidence lasts valid_days (8) and the refresh runs weekly, so a category
    that is still valid for a day at one run would otherwise expire before the
    next and stay stale for most of a week. Renew it refresh_ahead_days early.
    """
    result = registry.get("categories", {}).get(category)
    ahead = timedelta(days=max(0, min(int(config.get("refresh_ahead_days", 2)), 7)))
    return (
        not isinstance(result, dict)
        or _is_stale(_category_valid_until(result, registry), now + ahead)
        or _result_quality_error(category, result, config) is not None
    )


def refresh_registry(
    openrouter_request: Callable[..., dict[str, Any]],
    models: list[dict[str, Any]],
    *,
    only_if_stale: bool = False,
    config_path: Path = CONFIG_PATH,
    registry_path: Path = REGISTRY_PATH,
) -> dict[str, Any]:
    with _refresh_guard(registry_path):
        config = load_config(config_path)
        previous = load_registry(registry_path)
        now = utc_now()
        today = now.date().isoformat()
        enabled = _enabled_categories(config)
        if only_if_stale:
            # Only pay for the categories that need it; fresh evidence is kept.
            targets = [
                category
                for category in enabled
                if _needs_refresh(category, previous, config, now)
            ]
            if not targets:
                return {
                    "ok": True,
                    "skipped": True,
                    **registry_status(registry_path, config_path),
                }
        else:
            targets = list(enabled)
        max_parallel = max(1, min(int(config.get("max_parallel", 3)), 6))
        refreshed: dict[str, Any] = {}
        failures: list[dict[str, Any]] = []
        def submit_refresh(executor: ThreadPoolExecutor, category: str):
            spec = enabled[category]
            if spec.get("source_adapter") == "arc_agi_json":
                return executor.submit(
                    _refresh_arc_agi_category, category, spec, config, models
                )
            if spec.get("source_adapter") == "official_csv":
                return executor.submit(
                    _refresh_official_csv_category, category, spec, config, models
                )
            return executor.submit(
                _refresh_category, category, spec, config, openrouter_request, models, today
            )

        with ThreadPoolExecutor(max_workers=max_parallel) as executor:
            futures = {submit_refresh(executor, category): category for category in targets}
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
            if not failures and not targets:
                return {
                    "ok": True,
                    "skipped": True,
                    **registry_status(registry_path, config_path),
                }
            raise BenchmarkRegistryError(
                "Benchmark refresh produced no valid categories; previous registry was preserved",
                502,
                {"failures": failures, "attempted_categories": sorted(targets)},
            )

        valid_days = max(1, min(int(config.get("valid_days", 8)), 31))
        previous_valid_until = previous.get("valid_until")
        merged_categories = {}
        dropped_categories = sorted(set(previous.get("categories", {})) - set(enabled))
        for name, result in previous.get("categories", {}).items():
            if name not in enabled:
                continue
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
            "attempted_categories": sorted(targets),
            "refreshed_categories": sorted(refreshed),
            "preserved_category_count": len(merged_categories) - len(refreshed),
            "dropped_categories": dropped_categories,
            "failures": failures,
            "registry_path": str(registry_path),
            "history_path": str(history_path),
        }


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


def _selection_requirements(requirements: dict[str, Any] | None) -> dict[str, Any]:
    if requirements is not None and not isinstance(requirements, dict):
        raise BenchmarkRegistryError("Selection requirements must be an object")
    value = dict(requirements or {})
    unknown = set(value) - {"input_modalities", "output_modalities", "min_context_length", "input_tokens", "max_tokens", "max_cost_usd", "requested_parameters", "reasoning_effort"}
    if unknown:
        raise BenchmarkRegistryError("Unknown selection requirements", details={"unknown": sorted(unknown)})
    for key in ("input_modalities", "output_modalities"):
        value.setdefault(key, ["text"])
        if not isinstance(value[key], list) or not value[key] or any(not isinstance(item, str) or not item for item in value[key]):
            raise BenchmarkRegistryError(f"{key} must be a nonempty list of modalities")
    for key in ("min_context_length", "input_tokens", "max_tokens"):
        if key in value and (not isinstance(value[key], int) or isinstance(value[key], bool) or value[key] < (1 if key == "max_tokens" else 0)):
            raise BenchmarkRegistryError(f"{key} must be a nonnegative integer" if key != "max_tokens" else "max_tokens must be a positive integer")
    if "max_cost_usd" in value and (not _numeric_score(value["max_cost_usd"]) or value["max_cost_usd"] < 0):
        raise BenchmarkRegistryError("max_cost_usd must be a finite nonnegative number")
    parameters = value.setdefault("requested_parameters", {})
    if not isinstance(parameters, (dict, list)) or any(not isinstance(key, str) for key in parameters):
        raise BenchmarkRegistryError("requested_parameters must be a list of names or an object")
    if "reasoning_effort" in value and value["reasoning_effort"] not in _GATEWAY_REASONING_EFFORTS:
        raise BenchmarkRegistryError("reasoning_effort is not a supported gateway effort")
    return value


def _nonnegative_price(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _candidate_eligibility(
    model: dict[str, Any], row: dict[str, Any], requirements: dict[str, Any],
) -> tuple[str | None, dict[str, Any], float | None]:
    settings = _evaluation_settings(row)
    if settings.get("unmapped_settings"):
        return "evaluated configuration contains settings that cannot be reproduced", {}, None
    requested = requirements["requested_parameters"]
    parameters = dict(requested) if isinstance(requested, dict) else {}
    if "reasoning_effort" in requirements:
        if "reasoning_effort" in parameters and parameters["reasoning_effort"] != requirements["reasoning_effort"]:
            return "requested reasoning settings conflict", {}, None
        parameters["reasoning_effort"] = requirements["reasoning_effort"]
    for key, evaluated in settings.items():
        if key in parameters and parameters[key] != evaluated:
            return f"requested {key} differs from the evaluated configuration", {}, None
        parameters[key] = evaluated
    output_tokens = parameters.get("max_tokens", requirements.get("max_tokens", 0))
    if (not isinstance(output_tokens, int) or isinstance(output_tokens, bool) or output_tokens < 0
            or "max_tokens" in parameters and "max_tokens" in requirements and output_tokens != requirements["max_tokens"]):
        return "requested output limit differs from the evaluated or requested parameter configuration", {}, None
    # An effort in the nested provider form is equivalent only when it agrees.
    nested_reasoning = parameters.get("reasoning")
    if isinstance(nested_reasoning, dict) and "reasoning_effort" in settings:
        if nested_reasoning.get("enabled") is False or "max_tokens" in nested_reasoning:
            return "requested reasoning settings differ from the evaluated configuration", {}, None
    if isinstance(nested_reasoning, dict) and "effort" in nested_reasoning:
        effort = nested_reasoning["effort"]
        if "reasoning_effort" in parameters and parameters["reasoning_effort"] != effort:
            return "requested reasoning settings differ from the evaluated configuration", {}, None
        parameters["reasoning_effort"] = effort
    if isinstance(nested_reasoning, dict) and set(nested_reasoning) == {'effort'}:
        parameters.pop('reasoning', None)
    if output_tokens:
        parameters['max_tokens'] = output_tokens
    try:
        parameters = validate_parameters(parameters)
        if output_tokens:
            validate_parameters({'max_tokens': output_tokens})
    except ValueError as exc:
        return str(exc), {}, None
    supported = model.get("supported_parameters") or []
    parameter_names = set(requested) | set(parameters)
    parameter_names.discard("reasoning_effort")
    if "reasoning_effort" in parameters or "reasoning_effort" in requested:
        parameter_names.add("reasoning")
        reasoning = model.get("reasoning")
        if not isinstance(reasoning, dict) or "supported_efforts" not in reasoning:
            return "catalog does not expose support for the exact reasoning effort", {}, None
        efforts = reasoning["supported_efforts"]
        if "reasoning_effort" in parameters and (parameters["reasoning_effort"] not in _GATEWAY_REASONING_EFFORTS
                or efforts is not None and (not isinstance(efforts, list) or parameters["reasoning_effort"] not in efforts)):
            return "catalog does not support the exact evaluated or requested reasoning effort", {}, None
    missing = sorted(parameter_names - set(supported))
    if missing:
        return "catalog lacks requested parameters: " + ", ".join(missing), {}, None
    architecture = model.get("architecture") or {}
    for key in ("input_modalities", "output_modalities"):
        if not set(requirements[key]) <= set(architecture.get(key) or []):
            return "catalog does not support required " + key, {}, None
    context = model.get("context_length")
    needed = max(requirements.get("min_context_length", 0), requirements.get("input_tokens", 0) + output_tokens, 1)
    if not isinstance(context, int) or isinstance(context, bool) or context < needed:
        return "catalog context window is unknown or too small", {}, None
    provider = model.get("top_provider") or {}
    completion_limit = provider.get("max_completion_tokens")
    if isinstance(completion_limit, (int, float)) and output_tokens > completion_limit:
        return "requested output exceeds catalog completion limit", {}, None
    pricing = model.get("pricing") or {}
    prompt_price, completion_price = (_nonnegative_price(pricing.get(key)) for key in ("prompt", "completion"))
    request_price = _nonnegative_price(pricing.get("request", 0))
    if prompt_price is None or completion_price is None or request_price is None:
        return "catalog token or request pricing is unknown or invalid", {}, None
    estimate = None
    if "input_tokens" in requirements and output_tokens:
        estimate = requirements["input_tokens"] * prompt_price + output_tokens * completion_price + request_price
    if "max_cost_usd" in requirements:
        if estimate is None:
            return "cost bound requires input_tokens and max_tokens", {}, None
        extra_fees = ("image", "audio", "input_audio", "output_audio", "web_search", "internal_reasoning")
        if any(key in pricing and _nonnegative_price(pricing[key]) != 0 for key in extra_fees):
            return "catalog has additional fees that cannot be bounded from supplied requirements", {}, None
        if estimate > requirements["max_cost_usd"]:
            return "estimated maximum token cost exceeds max_cost_usd", {}, estimate
    return None, parameters, estimate


def select_benchmark_model(
    category: str, path: Path = REGISTRY_PATH, *,
    catalog: list[dict[str, Any]] | None = None,
    requirements: dict[str, Any] | None = None,
    config_path: Path = CONFIG_PATH,
) -> dict[str, Any]:
    view = registry_view(category, path)
    if view["stale"]:
        raise BenchmarkRegistryError(
            "Benchmark evidence for this category is stale; refresh the registry before selection",
            503,
            {"category": view["category"], "valid_until": view["valid_until"]},
        )
    result = view["result"]
    quality_error = _result_quality_error(view["category"], result, load_config(config_path))
    if quality_error:
        raise BenchmarkRegistryError(
            "Benchmark evidence for this category does not pass current quality gates",
            422,
            {"category": view["category"], "reason": quality_error},
        )
    normalized_requirements = _selection_requirements(requirements)
    selected = result.get("selected_model")
    selected_row: dict[str, Any] = {}
    rejected: list[dict[str, Any]] = []
    parameters: dict[str, Any] = {}
    estimate = None
    if catalog is not None:
        if not isinstance(catalog, list) or any(not isinstance(item, dict) for item in catalog):
            raise BenchmarkRegistryError("Live model catalog must be a list of models")
        selected = None
        for row in sorted(result["ranking"], key=lambda item: _rank(item.get("rank"), 999999)):
            available = resolve_available_model(row["model_name"], catalog)
            candidate = next((item for item in catalog if available and item.get("id") == available["id"]), None)
            reason = "exact ranked model is unavailable in the live catalog"
            if candidate is not None:
                reason, candidate_parameters, candidate_estimate = _candidate_eligibility(candidate, row, normalized_requirements)
                if reason is None:
                    selected, selected_row = available, row
                    parameters, estimate = candidate_parameters, candidate_estimate
                    break
            rejected.append({"model_name": row["model_name"], "reason": reason})
        if selected is None:
            raise BenchmarkRegistryError("No ranked model meets the requested capabilities and evaluated settings", 422,
                                         {"category": view["category"], "rejected_candidates": rejected})
    else:
        if requirements:
            raise BenchmarkRegistryError("A live model catalog is required to verify selection requirements", 422)
        if not isinstance(selected, dict) or not selected.get("id"):
            raise BenchmarkRegistryError("No OpenRouter model is selected for this category", 404)
        selected_row = next((row for row in result["ranking"]
                             if isinstance(row.get("openrouter_model"), dict) and row["openrouter_model"].get("id") == selected["id"]), {})
        if not selected_row:
            selected_row = next((row for row in result["ranking"] if resolve_available_model(row["model_name"], [selected])), {})
        if not selected_row:
            raise BenchmarkRegistryError("Stored selected model cannot be bound to a cited ranking row; select with a live catalog", 422)
        settings = _evaluation_settings(selected_row) or result.get("selected_evaluation_settings") or {}
        if settings:
            raise BenchmarkRegistryError("A live model catalog is required to verify the exact evaluated settings", 422)
    return {
        "requested_category": category,
        "category": view["category"],
        "model": selected,
        "evaluated_model_name": selected_row.get("model_name"),
        "benchmark_name": result.get("benchmark_name"),
        "benchmark_date": result.get("benchmark_date"),
        "selected_rank": selected_row.get("rank", result.get("selected_rank")),
        "score": selected_row.get("score", result.get("selected_score")),
        "score_text": selected_row.get("score_text", result.get("selected_score_text")),
        "source_url": selected_row.get("source_url", result.get("selection_source_url")),
        "evaluation_settings": _evaluation_settings(selected_row),
        "execution_parameters": parameters,
        "eligibility": {"checked": catalog is not None, "requirements": normalized_requirements,
                        "estimated_max_cost_usd": estimate},
        "rejected_candidates": rejected,
        "selection_policy": ("highest_cited_public_rank_eligible_in_live_catalog" if catalog is not None else result.get("selection_policy")),
        "registry_hash": view.get("registry_hash"),
        "updated_at": view.get("updated_at"),
        "valid_until": view.get("valid_until"),
        "stale": False,
    }
