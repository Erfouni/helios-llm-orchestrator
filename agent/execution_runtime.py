"""Outbound capacity, billing observations and conservative text reservations.

No prompts, provider credentials or response text belong in this ledger.
OS locks bound actual requests across the gateway and refresh processes and
are released by the kernel if a process dies.
"""
from __future__ import annotations

import contextvars
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager, closing
from pathlib import Path

execution_context = contextvars.ContextVar('helios_execution', default={})


class CapacityError(Exception):
    status = 429
    code = 'concurrency_limit'
    retryable = True
    details = None


def _finite_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def quote_text_request(model: dict, messages: list, max_tokens: int) -> dict:
    """Reserve UTF-8 byte upper bound plus framing and bounded completion.

This deliberately overestimates ordinary text tokenization. Dynamic routers,
non-text payloads and unpriced extra modalities cannot enter a budgeted task.
Provider price caps accompany the request; observed overages block the project.
"""
    pricing = model.get('pricing') or {}
    rates = {name: _finite_number(pricing.get(name)) for name in ('prompt', 'completion')}
    rates['request'] = _finite_number(pricing.get('request', 0))
    if any(value is None for value in rates.values()):
        raise ValueError('Model pricing is unavailable; cannot reserve a project budget')
    if any(not isinstance(m.get('content'), str) for m in messages):
        raise ValueError('Budgeted tasks support text messages only')
    input_tokens = sum(len(m['content'].encode('utf-8')) + 64 for m in messages) + 256
    context = model.get('context_length')
    if not isinstance(context, int) or input_tokens + max_tokens > context:
        raise ValueError('Conservative input and output reservation exceeds model context')
    output_limit = (model.get('top_provider') or {}).get('max_completion_tokens')
    if output_limit and max_tokens > output_limit:
        raise ValueError('max_tokens exceeds model output limit')
    architecture = model.get('architecture') or {}
    if 'text' not in architecture.get('input_modalities', []) or 'text' not in architecture.get('output_modalities', []):
        raise ValueError('Model does not expose text input and output capabilities')
    if model.get('id', '').startswith('openrouter/'):
        raise ValueError('Dynamic routers cannot supply a fixed budget reservation')
    for name in ('image', 'audio', 'web_search', 'internal_reasoning'):
        extra = _finite_number(pricing.get(name, 0))
        if extra is None or extra > 0:
            # No separately billed modality or hidden reasoning allowance in V2.
            if name == 'internal_reasoning':
                raise ValueError('Separately billed reasoning requires an explicit budget adapter')
    cost = input_tokens * rates['prompt'] + max_tokens * rates['completion'] + rates['request']
    return {
        'reservation_tokens': input_tokens + max_tokens,
        'reservation_cost_usd': math.ceil(cost * 1e10) / 1e10,
        'input_tokens_upper_bound': input_tokens,
        'provider': {'max_price': {'prompt': rates['prompt'] * 1_000_000,
                                   'completion': rates['completion'] * 1_000_000,
                                   'request': rates['request']},
                     'require_parameters': True, 'allow_fallbacks': False},
    }


class CallRecord:
    def __init__(self, lock_fd=None):
        self.response = None
        self.lock_fd = lock_fd

    def result(self, response: dict):
        self.response = response


class ProviderRuntime:
    def __init__(self, database_path: Path, max_concurrency: int = 4):
        self.database_path = Path(database_path).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_concurrency = max_concurrency
        self.lock_root = self.database_path.parent / 'provider-slots'
        self.lock_root.mkdir(mode=0o750, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS provider_calls (
                id TEXT PRIMARY KEY, provider TEXT NOT NULL, operation TEXT NOT NULL,
                model TEXT, project_id TEXT, execution_id TEXT, status TEXT NOT NULL,
                billing_status TEXT NOT NULL DEFAULT 'unknown', cost_usd REAL,
                total_tokens INTEGER, credits REAL, error_type TEXT, pid INTEGER,
                created_at REAL NOT NULL, finished_at REAL)''')

    def _connect(self):
        db = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def _acquire_slot(self):
        for number in range(self.max_concurrency):
            handle = (self.lock_root / f'{number}.lock').open('a+b')
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    if not handle.read(1):
                        handle.write(b'0'); handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except (BlockingIOError, OSError):
                handle.close()
        raise CapacityError('Helios outbound capacity is full; retry later')

    @contextmanager
    def admission(self):
        slot = self._acquire_slot()
        context = execution_context.get()
        token = execution_context.set({**context, '_admission': slot})
        try:
            yield
        finally:
            execution_context.reset(token)
            slot.close()

    @contextmanager
    def call(self, provider: str, operation: str, *, model: str | None = None):
        context = execution_context.get()
        slot = context.get('_admission') or self._acquire_slot()
        owns_slot = '_admission' not in context
        call_id = str(uuid.uuid4())
        record = CallRecord(slot.fileno())
        error_type = None
        try:
            with closing(self._connect()) as db:
                db.execute('''INSERT INTO provider_calls
                    (id,provider,operation,model,project_id,execution_id,status,pid,created_at)
                    VALUES (?,?,?,?,?,?,'running',?,?)''',
                    (call_id, provider, operation, model, context.get('project_id'),
                     context.get('execution_id'), os.getpid(), time.time()))
            try:
                yield record
            except BaseException as exc:
                error_type = type(exc).__name__
                raise
            finally:
                response = record.response or {}
                usage = response.get('usage') or {}
                if not isinstance(usage, dict):
                    usage = {}
                cost = _finite_number(usage.get('cost'))
                tokens = _finite_number(usage.get('total_tokens'))
                credits = _finite_number(usage.get('credits', response.get('credits_used')))
                with closing(self._connect()) as db:
                    db.execute('''UPDATE provider_calls SET status=?, billing_status=?,
                        cost_usd=?,total_tokens=?,credits=?,error_type=?,finished_at=? WHERE id=?''',
                        ('failed' if error_type else 'succeeded', 'known' if cost is not None else 'unknown',
                         cost, int(tokens) if tokens is not None else None, credits,
                         error_type, time.time(), call_id))
        finally:
            if owns_slot:
                slot.close()

    def usage(self, limit: int = 200):
        with closing(self._connect()) as db:
            calls = [dict(row) for row in db.execute('SELECT * FROM provider_calls ORDER BY created_at DESC LIMIT ?', (limit,))]
            totals = db.execute('''SELECT COUNT(*) AS call_count, COALESCE(SUM(cost_usd),0) AS known_cost_usd,
                SUM(CASE WHEN billing_status='unknown' THEN 1 ELSE 0 END) AS unknown_cost_calls
                FROM provider_calls''').fetchone()
        return {**dict(totals), 'calls': calls, 'scope': 'all gateway paid calls; includes V2 executions'}
