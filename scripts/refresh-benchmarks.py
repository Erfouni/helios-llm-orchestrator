#!/usr/bin/env python3
"""Refresh Helios's public web benchmark registry from the command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = PROJECT_ROOT / "agent"
sys.path.insert(0, str(AGENT_DIR if AGENT_DIR.exists() else PROJECT_ROOT))

import benchmark_registry  # noqa: E402
import server  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--if-stale", action="store_true")
    args = parser.parse_args()
    try:
        result = benchmark_registry.refresh_registry(
            server.openrouter_request,
            server.get_models(force=True),
            only_if_stale=args.if_stale,
        )
    except Exception as exc:
        details = getattr(exc, "details", None)
        print(json.dumps({"ok": False, "error": str(exc), "details": details}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
