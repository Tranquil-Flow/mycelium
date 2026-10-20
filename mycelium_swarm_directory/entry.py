# SPDX-License-Identifier: AGPL-3.0-or-later
"""Swarm-entry enforcement: who may send a prompt into this swarm.

The directory is only a phonebook; *this* is the access check.  It runs on the
swarm's entry node (the A13 owner app, behind its public relay) in front of the
existing chat service, and admits a web request only when **all** hold:

1. the request is signed by the requester's Ed25519 key (fresh ``at``, unused
   nonce) -- a stolen grant is useless without the grantee's private key;
2. it carries a grant signed by the swarm owner's key (or, for an ``open``
   swarm only, by the owner-listed entry key that auto-issues open grants),
   for this swarm, for this requester key, inside its validity window, and not
   revoked locally;
3. the grant's request/token quota is not exhausted (charged atomically in a
   durable sqlite ledger *before* the prompt reaches the backend, refunded if
   the backend refuses);
4. per-IP (cheap, before any signature work) and per-key rate limits pass.

``max_new_tokens`` is clamped to ``min(request, MAX_NEW_TOKENS, tokens left)``.
Per-swarm concurrency is the chat service's single-flight rule (``chat_busy``).
The policy (owner key, access mode, open quota) comes from an owner-signed
swarm record the entry verified itself -- never from the directory's word.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import docs
from .directory import PRIVACY_NOTE
from .ratelimit import WindowLimiter
from .wire import Verifier, WireError, fingerprint, parse_json_object

PREFIX = '/swarm-entry/v1/'
ROUTES = frozenset({'state', 'open-grant', 'send', 'events'})


@dataclass(frozen=True)
class EntryLimits:
    skew: int = 120
    max_body: int = 16384
    max_new_tokens: int = docs.MAX_NEW_TOKENS
    per_key_per_minute: int = 6
    per_ip_per_minute: int = 12
    events_per_ip_per_minute: int = 240
    open_grants_per_ip_per_hour: int = 6
    max_nonces: int = 50000


@dataclass(frozen=True)
class EntryPolicy:
    swarm_id: str
    owner_key: str
    entry_key: str
    access_mode: str
    open_quota: dict | None = field(default=None, hash=False, compare=False)
    name: str = ''
    model: str = ''

    @classmethod
    def from_record(cls, record: Any, verifier: Verifier, *, entry_key: str,
                    allow_private: bool = False) -> 'EntryPolicy':
        """Verify an owner-signed record and require that it names this entry."""
        record = docs.check_record(record, verifier, allow_loopback=True, allow_private=allow_private)
        entry = record['entry']
        if entry is None or entry['key'] != entry_key:
            raise WireError('entry_not_attached', 409)
        return cls(swarm_id=record['swarm_id'], owner_key=record['owner_key'], entry_key=entry_key,
                   access_mode=record['access_mode'], open_quota=record['open_quota'], name=record['name'],
                   model=record['model'])


class UsageLedger:
    """Durable per-grant usage, local revocations and issued open grants."""

    def __init__(self, path: Path | str) -> None:
        path = Path(path)
        if str(path) != ':memory:' and not path.exists():
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600))
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.executescript(
            'CREATE TABLE IF NOT EXISTS usage (grant_id TEXT PRIMARY KEY, requests INTEGER NOT NULL, '
            'tokens INTEGER NOT NULL);'
            'CREATE TABLE IF NOT EXISTS revoked (grant_id TEXT PRIMARY KEY, at INTEGER NOT NULL);'
            'CREATE TABLE IF NOT EXISTS open_grants (swarm_id TEXT NOT NULL, grantee TEXT NOT NULL, '
            'grant TEXT NOT NULL, expires_at INTEGER NOT NULL, PRIMARY KEY (swarm_id, grantee));')

    def charge(self, grant: dict[str, Any], wanted_tokens: int) -> tuple[int, dict[str, int]]:
        """Atomically take one request and up to ``wanted_tokens``; returns (tokens, left)."""
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                row = self._db.execute('SELECT requests, tokens FROM usage WHERE grant_id=?',
                                       (grant['grant_id'],)).fetchone()
                requests, tokens = row if row else (0, 0)
                tokens_left = grant['max_tokens'] - tokens
                if requests >= grant['max_requests'] or tokens_left < 1:
                    raise WireError('entry_quota_exhausted', 429)
                taken = min(wanted_tokens, tokens_left)
                self._db.execute('INSERT OR REPLACE INTO usage (grant_id, requests, tokens) VALUES (?, ?, ?)',
                                 (grant['grant_id'], requests + 1, tokens + taken))
                self._db.execute('COMMIT')
            except BaseException:
                self._db.execute('ROLLBACK')
                raise
        return taken, {'requests_left': grant['max_requests'] - requests - 1, 'tokens_left': tokens_left - taken}

    def refund(self, grant_id: str, tokens: int) -> None:
        with self._lock:
            self._db.execute('UPDATE usage SET requests=MAX(requests-1, 0), tokens=MAX(tokens-?, 0) '
                             'WHERE grant_id=?', (tokens, grant_id))

    def usage(self, grant_id: str) -> tuple[int, int]:
        with self._lock:
            row = self._db.execute('SELECT requests, tokens FROM usage WHERE grant_id=?', (grant_id,)).fetchone()
        return row if row else (0, 0)

    def revoke(self, grant_id: str, at: int = 0) -> None:
        with self._lock:
            self._db.execute('INSERT OR REPLACE INTO revoked (grant_id, at) VALUES (?, ?)', (grant_id, at))

    def revoked(self, grant_id: str) -> bool:
        with self._lock:
            return self._db.execute('SELECT 1 FROM revoked WHERE grant_id=?', (grant_id,)).fetchone() is not None

    def open_grant(self, swarm_id: str, grantee: str, now: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute('SELECT grant, expires_at FROM open_grants WHERE swarm_id=? AND grantee=?',
                                   (swarm_id, grantee)).fetchone()
        return json.loads(row[0]) if row and row[1] > now else None

    def put_open_grant(self, grant: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute('INSERT OR REPLACE INTO open_grants (swarm_id, grantee, grant, expires_at) '
                             'VALUES (?, ?, ?, ?)', (grant['swarm_id'], grant['grantee_key'],
                                                     json.dumps(grant, sort_keys=True), grant['expires_at']))

    def close(self) -> None:
        with self._lock:
            self._db.close()


@dataclass(frozen=True)
class Admission:
    grant_id: str
    key: str
    client_key: str
    max_new_tokens: int
    clamped: bool
    left: dict


class EntryGuard:
    def __init__(self, policy: Callable[[], EntryPolicy | None], signer: Any, verifier: Verifier,
                 ledger: UsageLedger, *, clock: Callable[[], float] = time.time,
                 limits: EntryLimits = EntryLimits()) -> None:
        self.policy = policy
        self.signer = signer
        self.verifier = verifier
        self.ledger = ledger
        self.clock = clock
        self._ip = WindowLimiter(limits.per_ip_per_minute)
        self._key = WindowLimiter(limits.per_key_per_minute)
        self._events = WindowLimiter(limits.events_per_ip_per_minute)
        self._open_ip = WindowLimiter(limits.open_grants_per_ip_per_hour, 3600)
        self._nonces: dict[str, int] = {}
        self._lock = threading.Lock()
        self.limits = limits

    @property
    def limits(self) -> EntryLimits:
        return self._limits

    @limits.setter
    def limits(self, value: EntryLimits) -> None:
        self._limits = value  # counters survive a limit change
        self._ip.limit, self._key.limit = value.per_ip_per_minute, value.per_key_per_minute
        self._events.limit, self._open_ip.limit = value.events_per_ip_per_minute, value.open_grants_per_ip_per_hour

    def current(self) -> EntryPolicy:
        policy = self.policy()
        if policy is None:
            raise WireError('entry_not_published', 503)
        return policy

    def _signed(self, raw: bytes, protocol: str, client: str, policy: EntryPolicy, now: int) -> dict[str, Any]:
        if not self._ip.allow(client, now):
            raise WireError('entry_rate_limited_ip', 429)
        document = parse_json_object(raw, limit=self.limits.max_body)
        document = docs.check(document, protocol, self.verifier)
        if document['swarm_id'] != policy.swarm_id:
            raise WireError('entry_swarm_mismatch', 404)
        if abs(document['at'] - now) > self.limits.skew:
            raise WireError('entry_clock_skew', 400)
        with self._lock:
            if document['nonce'] in self._nonces:
                raise WireError('entry_nonce_replayed', 409)
            if len(self._nonces) >= 4096:
                self._nonces = {n: e for n, e in self._nonces.items() if e > now}
                if len(self._nonces) >= self.limits.max_nonces:
                    raise WireError('entry_busy', 503)
            self._nonces[document['nonce']] = now + 2 * self.limits.skew + 1
        if not self._key.allow(document['key'], now):
            raise WireError('entry_rate_limited_key', 429)
        return document

    def check_grant(self, grant: Any, key: str, policy: EntryPolicy, now: int) -> dict[str, Any]:
        try:
            grant = docs.check(grant, docs.GRANT, self.verifier)
        except WireError as error:
            raise WireError('entry_grant_signature_invalid' if error.code == 'signature_invalid'
                            else 'entry_grant_invalid', 403) from None
        issuers = {policy.owner_key} | ({policy.entry_key} if policy.access_mode == 'open' else set())
        if grant['issuer_key'] not in issuers:
            raise WireError('entry_grant_issuer_invalid', 403)
        if grant['swarm_id'] != policy.swarm_id:
            raise WireError('entry_grant_swarm_mismatch', 403)
        if grant['grantee_key'] != key:
            raise WireError('entry_grant_not_for_key', 403)
        if grant['issued_at'] > now + self.limits.skew:
            raise WireError('entry_grant_not_yet_valid', 403)
        if grant['expires_at'] <= now:
            raise WireError('entry_grant_expired', 403)
        if self.ledger.revoked(grant['grant_id']):
            raise WireError('entry_grant_revoked', 403)
        return grant

    def admit(self, raw: bytes, client: str) -> tuple[Admission, str]:
        policy = self.current()
        now = int(self.clock())
        document = self._signed(raw, docs.ENTRY_SEND, client, policy, now)
        grant = self.check_grant(document['grant'], document['key'], policy, now)
        wanted = min(document['max_new_tokens'], self.limits.max_new_tokens)
        taken, left = self.ledger.charge(grant, wanted)
        return Admission(grant_id=grant['grant_id'], key=document['key'],
                         client_key='swarm:' + fingerprint(document['key']), max_new_tokens=taken,
                         clamped=taken != document['max_new_tokens'], left=left), document['prompt']

    def refund(self, admission: Admission) -> None:
        self.ledger.refund(admission.grant_id, admission.max_new_tokens)

    def open_grant(self, raw: bytes, client: str) -> dict[str, Any]:
        policy = self.current()
        now = int(self.clock())
        if policy.access_mode != 'open' or not policy.open_quota:
            raise WireError('entry_swarm_not_open', 403)
        document = self._signed(raw, docs.OPEN_GRANT_REQUEST, client, policy, now)
        existing = self.ledger.open_grant(policy.swarm_id, document['key'], now)
        if existing is not None:
            return existing
        if not self._open_ip.allow(client, now):
            raise WireError('entry_open_grants_rate_limited', 429)
        quota = policy.open_quota
        grant = docs.grant(self.signer, swarm_id=policy.swarm_id, grantee_key=document['key'], issued_at=now,
                           expires_at=now + quota['ttl_seconds'], max_requests=quota['max_requests'],
                           max_tokens=quota['max_tokens'])
        self.ledger.put_open_grant(grant)
        return grant

    def allow_events(self, client: str) -> bool:
        return self._events.allow(client, int(self.clock()))


class EntryGateway:
    """Transport-free ``/swarm-entry/v1/*`` routes in front of a chat service.

    ``chat()`` returns a ``mycelium_a13_macos.chat.ChatService``-shaped object
    (``send(client, body)``, ``events(body)``, ``state()``; errors carry
    ``status``/``code``).  Returns ``(status, json_document)``.
    """

    def __init__(self, guard: EntryGuard, chat: Callable[[], Any]) -> None:
        self.guard = guard
        self.chat = chat

    def handle(self, path: str, body: bytes, client: str) -> tuple[int, dict[str, Any]]:
        try:
            return 200, self._route(path, body, client)
        except WireError as error:
            return error.status, {'error': error.code}

    def _route(self, path, body, client):
        route = path[len(PREFIX):] if path.startswith(PREFIX) else None
        if route not in ROUTES:
            raise WireError('entry_route_unknown', 404)
        if route == 'state':
            policy = self.guard.current()
            state = dict(self.chat().state())
            limits = self.guard.limits
            return {'swarm_id': policy.swarm_id, 'name': policy.name, 'access_mode': policy.access_mode,
                    'owner_fingerprint': fingerprint(policy.owner_key),
                    'entry_fingerprint': fingerprint(policy.entry_key),
                    'available': bool(state.get('available')), 'busy': bool(state.get('busy')),
                    'warming_up': bool(state.get('warming_up')), 'model_id': state.get('model_id'),
                    'served_by': state.get('served_by') or [],
                    'limits': {'max_prompt_chars': docs.MAX_PROMPT_CHARS, 'max_new_tokens': limits.max_new_tokens,
                               'per_key_per_minute': limits.per_key_per_minute,
                               'per_ip_per_minute': limits.per_ip_per_minute},
                    'privacy': PRIVACY_NOTE}
        if route == 'open-grant':
            return {'grant': self.guard.open_grant(body, client)}
        if route == 'events':
            self.guard.current()
            if not self.guard.allow_events(client):
                raise WireError('entry_rate_limited_ip', 429)
            document = parse_json_object(body, limit=1024)
            return self._chat_call(lambda: self.chat().events(document))
        admission, prompt = self.guard.admit(body, client)
        try:
            accepted = self._chat_call(lambda: self.chat().send(
                admission.client_key, {'prompt': prompt, 'max_new_tokens': admission.max_new_tokens}))
        except BaseException:
            self.guard.refund(admission)
            raise
        return {**accepted, 'clamped': bool(accepted.get('clamped')) or admission.clamped, 'quota': admission.left}

    @staticmethod
    def _chat_call(function):
        try:
            return function()
        except WireError:
            raise
        except Exception as error:
            status, code = getattr(error, 'status', None), getattr(error, 'code', None)
            if type(status) is int and isinstance(code, str) and 400 <= status <= 599:
                raise WireError(code, status) from None
            raise WireError('entry_backend_failed', 503) from None
