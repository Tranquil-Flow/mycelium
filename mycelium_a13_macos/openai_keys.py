# SPDX-License-Identifier: AGPL-3.0-or-later
"""Owner-issued API keys for the private-swarm OpenAI-compatible API (PV22).

``KeyStore`` keeps, in one owner-only directory, ``keys.json`` (id, label, display prefix, SHA-256 of the secret, quota,
timestamps) and ``usage.json`` (per-key fixed-window counters).  The secret is returned once by ``create`` and never
stored.  Secrets are 256-bit random, so an unsalted SHA-256 is a sufficient verifier.

Quotas are per key and per fixed window (``window_seconds``, anchored at the key's first use in the window):
``requests_per_window`` and ``tokens_per_window`` (generated tokens).  ``admit`` reserves the request's maximum output,
``settle`` charges what was actually produced, ``release`` refunds a request that never ran.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable

PROTOCOL = 'mycelium.openai_api_keys.v1'
KEY_PREFIX = 'myc-sk-'
DISPLAY_PREFIX_CHARS = len(KEY_PREFIX) + 4
MAX_KEYS = 32
MAX_LABEL = 64
MAX_QUOTA = 10 ** 10
DEFAULT_WINDOW_S = 3600
MIN_WINDOW_S = 60
MAX_WINDOW_S = 30 * 86400
_MAX_FILE = 262144


class KeyStoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class QuotaExceeded(RuntimeError):
    """``kind`` is ``requests`` or ``tokens``; ``retry_after_s`` is None when waiting cannot help."""

    def __init__(self, kind: str, retry_after_s: int | None) -> None:
        self.kind, self.retry_after_s = kind, retry_after_s
        super().__init__(f'quota_{kind}_exceeded')


class Reservation:
    __slots__ = ('key_id', 'tokens', 'window_start')

    def __init__(self, key_id: str, tokens: int, window_start: float) -> None:
        self.key_id, self.tokens, self.window_start = key_id, tokens, window_start


def _int(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode('utf-8')).hexdigest()


class KeyStore:
    def __init__(self, root: Any, *, clock: Callable[[], float] = time.time) -> None:
        from .app import DemoError, private_dir
        self._clock = clock
        try:
            self.root = private_dir(Path(root))
        except DemoError:
            raise KeyStoreError('key_store_unsafe') from None
        self._lock = threading.RLock()
        self._keys: list[dict[str, Any]] = []
        self._usage: dict[str, dict[str, Any]] = {}
        document = self.read_json('keys.json')
        if document is not None:
            keys = document.get('keys') if document.get('protocol') == PROTOCOL else None
            if not isinstance(keys, list) or not all(self._record_valid(k) for k in keys):
                raise KeyStoreError('key_store_corrupt')
            self._keys = keys
        usage = self.read_json('usage.json')
        if usage is not None:
            counters = usage.get('usage')
            if not isinstance(counters, dict):
                raise KeyStoreError('key_store_corrupt')
            self._usage = {k: v for k, v in counters.items() if self._usage_valid(v)}

    # -- files ---------------------------------------------------------------
    def read_json(self, name: str) -> dict[str, Any] | None:
        path = self.root / name
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError:
            raise KeyStoreError('key_store_unsafe') from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > _MAX_FILE):
                raise KeyStoreError('key_store_unsafe')
            raw = os.read(fd, _MAX_FILE + 1)
        finally:
            os.close(fd)
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeError):
            raise KeyStoreError('key_store_corrupt') from None
        if not isinstance(document, dict):
            raise KeyStoreError('key_store_corrupt')
        return document

    def write_json(self, name: str, document: dict[str, Any]) -> None:
        from .app import private_write
        private_write(self.root / name, json.dumps(document, sort_keys=True, separators=(',', ':')).encode())

    def _save_keys(self) -> None:
        self.write_json('keys.json', {'protocol': PROTOCOL, 'keys': self._keys})

    def _save_usage(self) -> None:
        self.write_json('usage.json', {'protocol': PROTOCOL, 'usage': self._usage})

    @staticmethod
    def _quota_valid(quota: Any) -> bool:
        return (isinstance(quota, dict) and set(quota) == {'requests_per_window', 'tokens_per_window', 'window_seconds'}
                and (quota['requests_per_window'] is None or _int(quota['requests_per_window'], 1, MAX_QUOTA))
                and (quota['tokens_per_window'] is None or _int(quota['tokens_per_window'], 1, MAX_QUOTA))
                and _int(quota['window_seconds'], MIN_WINDOW_S, MAX_WINDOW_S))

    @classmethod
    def _record_valid(cls, record: Any) -> bool:
        return (isinstance(record, dict)
                and set(record) == {'id', 'label', 'prefix', 'hash', 'created_at', 'revoked_at', 'quota'}
                and isinstance(record['id'], str) and record['id'].startswith('key_')
                and isinstance(record['label'], str) and isinstance(record['prefix'], str)
                and isinstance(record['hash'], str) and len(record['hash']) == 64
                and type(record['created_at']) in (int, float)
                and (record['revoked_at'] is None or type(record['revoked_at']) in (int, float))
                and cls._quota_valid(record['quota']))

    @staticmethod
    def _usage_valid(value: Any) -> bool:
        return (isinstance(value, dict) and set(value) == {'window_start', 'requests', 'tokens'}
                and type(value['window_start']) in (int, float) and _int(value['requests'], 0, 10 ** 12)
                and _int(value['tokens'], 0, 10 ** 15))

    # -- public API ----------------------------------------------------------
    @staticmethod
    def _label(label: Any) -> str:
        if (not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL
                or any(ord(c) < 0x20 or ord(c) == 0x7F for c in label)):
            raise KeyStoreError('key_label_invalid')
        return label.strip()

    def _public(self, record: dict[str, Any]) -> dict[str, Any]:
        usage = self._current_usage(record)
        return {'id': record['id'], 'label': record['label'], 'prefix': record['prefix'],
                'created_at': record['created_at'], 'revoked_at': record['revoked_at'],
                'quota': dict(record['quota']),
                'usage': {'window_start': usage['window_start'], 'requests': usage['requests'],
                          'tokens': usage['tokens'],
                          'window_ends': usage['window_start'] + record['quota']['window_seconds']
                          if usage['window_start'] else None}}

    def _current_usage(self, record: dict[str, Any]) -> dict[str, Any]:
        """The counters of the key's live window (a read; an elapsed window reads as empty)."""
        usage = self._usage.get(record['id'])
        if usage is None or self._clock() >= usage['window_start'] + record['quota']['window_seconds']:
            return {'window_start': 0, 'requests': 0, 'tokens': 0}
        return usage

    def create(self, *, label: Any, requests_per_window: Any = None, tokens_per_window: Any = None,
               window_seconds: Any = DEFAULT_WINDOW_S) -> tuple[dict[str, Any], str]:
        label = self._label(label)
        quota = {'requests_per_window': requests_per_window, 'tokens_per_window': tokens_per_window,
                 'window_seconds': window_seconds}
        if not self._quota_valid(quota):
            raise KeyStoreError('key_quota_invalid')
        with self._lock:
            if len(self._keys) >= MAX_KEYS:
                raise KeyStoreError('key_limit_reached')
            secret = KEY_PREFIX + secrets.token_urlsafe(32)
            record = {'id': 'key_' + secrets.token_hex(6), 'label': label, 'prefix': secret[:DISPLAY_PREFIX_CHARS],
                      'hash': _digest(secret), 'created_at': self._clock(), 'revoked_at': None, 'quota': quota}
            self._keys.append(record)
            self._save_keys()
            return self._public(record), secret

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._public(record) for record in self._keys]

    def _find(self, key_id: Any) -> dict[str, Any]:
        for record in self._keys:
            if record['id'] == key_id:
                return record
        raise KeyStoreError('key_unknown')

    def revoke(self, key_id: Any) -> dict[str, Any]:
        with self._lock:
            record = self._find(key_id)
            if record['revoked_at'] is None:
                record['revoked_at'] = self._clock()
                self._save_keys()
            return self._public(record)

    def authenticate(self, secret: Any) -> dict[str, Any] | None:
        if not isinstance(secret, str) or not secret.startswith(KEY_PREFIX) or len(secret) > 256:
            return None
        candidate = _digest(secret)
        with self._lock:
            found = None
            for record in self._keys:  # no early exit: every stored hash is compared
                if hmac.compare_digest(record['hash'], candidate) and record['revoked_at'] is None:
                    found = record
            return None if found is None else self._public(found)

    def admit(self, key_id: Any, tokens: int) -> Reservation:
        with self._lock:
            record = self._find(key_id)
            if record['revoked_at'] is not None:
                raise KeyStoreError('key_revoked')
            quota, now = record['quota'], self._clock()
            window = quota['window_seconds']
            usage = self._usage.get(record['id'])
            if usage is None or now >= usage['window_start'] + window:
                usage = {'window_start': now, 'requests': 0, 'tokens': 0}
            retry = max(1, math.ceil(usage['window_start'] + window - now))
            limit = quota['requests_per_window']
            if limit is not None and usage['requests'] + 1 > limit:
                raise QuotaExceeded('requests', retry)
            limit = quota['tokens_per_window']
            if limit is not None:
                if tokens > limit:
                    raise QuotaExceeded('tokens', None)
                if usage['tokens'] + tokens > limit:
                    raise QuotaExceeded('tokens', retry)
            usage['requests'] += 1
            usage['tokens'] += tokens
            self._usage[record['id']] = usage
            self._save_usage()
            return Reservation(record['id'], tokens, usage['window_start'])

    def _in_window(self, reservation: Reservation) -> dict[str, Any] | None:
        usage = self._usage.get(reservation.key_id)
        if usage is not None and usage['window_start'] == reservation.window_start:
            return usage
        return None

    def settle(self, reservation: Reservation, actual_tokens: int) -> None:
        """Charge the tokens actually produced in place of the reserved maximum."""
        with self._lock:
            usage = self._in_window(reservation)
            if usage is not None:
                usage['tokens'] = max(0, usage['tokens'] - reservation.tokens + max(0, int(actual_tokens)))
                self._save_usage()

    def release(self, reservation: Reservation) -> None:
        """Refund a request that never reached the route."""
        with self._lock:
            usage = self._in_window(reservation)
            if usage is not None:
                usage['requests'] = max(0, usage['requests'] - 1)
                usage['tokens'] = max(0, usage['tokens'] - reservation.tokens)
                self._save_usage()
