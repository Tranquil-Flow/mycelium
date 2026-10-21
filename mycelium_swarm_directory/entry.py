# SPDX-License-Identifier: AGPL-3.0-only
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
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import docs, hosted_defaults
from .directory import PRIVACY_NOTE
from .ratelimit import DailyBudget, WindowLimiter
from .wire import Verifier, WireError, fingerprint, parse_json_object

PREFIX = '/swarm-entry/v1/'
KILLED = 'entry_killed_by_owner'
ROUTES = frozenset({'state', 'open-grant', 'send', 'events', 'kill', 'cancel'})
CANCELLED_BY_REQUESTER = 'cancelled_by_requester'
MAX_REMEMBERED_REQUESTS = 512


@dataclass(frozen=True)
class EntryLimits:
    skew: int = 120
    max_body: int = 16384
    max_new_tokens: int = docs.MAX_NEW_TOKENS
    per_key_per_minute: int = 6
    per_ip_per_minute: int = 12
    events_per_ip_per_minute: int = 240
    cancels_per_ip_per_minute: int = 60
    open_grants_per_ip_per_hour: int = 6
    open_grants_per_ip_per_day: int = hosted_defaults.OPEN_GRANTS_PER_IP_PER_DAY
    max_nonces: int = 50000
    # Swarm-wide budget per UTC day over all keys and addresses (open swarms); None = unlimited.
    swarm_daily_requests: int | None = hosted_defaults.SWARM_DAILY_REQUESTS
    swarm_daily_tokens: int | None = hosted_defaults.SWARM_DAILY_TOKENS
    # Per-address share of the same day (open swarms; the address is the relay's normalised client key).
    ip_daily_requests: int = hosted_defaults.IP_DAILY_REQUESTS
    ip_daily_tokens: int = hosted_defaults.IP_DAILY_TOKENS


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
            'grant TEXT NOT NULL, expires_at INTEGER NOT NULL, PRIMARY KEY (swarm_id, grantee));'
            'CREATE TABLE IF NOT EXISTS swarm_usage (swarm_id TEXT NOT NULL, day INTEGER NOT NULL, '
            'requests INTEGER NOT NULL, tokens INTEGER NOT NULL, PRIMARY KEY (swarm_id, day));'
            'CREATE TABLE IF NOT EXISTS kill_switch (swarm_id TEXT PRIMARY KEY, engaged INTEGER NOT NULL, '
            'seq INTEGER NOT NULL, at INTEGER NOT NULL);')

    def charge(self, grant: dict[str, Any] | None, wanted_tokens: int,
               swarm: tuple[str, int, int | None, int | None] | None = None) -> tuple[int, dict[str, int]]:
        """Atomically take one request and up to ``wanted_tokens``; returns (tokens, left).

        ``swarm`` = (swarm_id, utc_day, max_requests, max_tokens) also charges the swarm-wide daily
        budget in the same transaction (None = unlimited); nothing is charged when either side refuses.
        ``grant=None`` (the relay's shared chat page, which has no grant) charges only the swarm-wide budget.
        """
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                row = None if grant is None else self._db.execute(
                    'SELECT requests, tokens FROM usage WHERE grant_id=?', (grant['grant_id'],)).fetchone()
                requests, tokens = row if row else (0, 0)
                tokens_left = wanted_tokens if grant is None else grant['max_tokens'] - tokens
                if grant is not None and (requests >= grant['max_requests'] or tokens_left < 1):
                    raise WireError('entry_quota_exhausted', 429)
                taken = min(wanted_tokens, tokens_left)
                swarm_left: dict[str, int] = {}
                if swarm is not None:
                    swarm_id, day, max_requests, max_tokens = swarm
                    used_r, used_t = self._db.execute(
                        'SELECT requests, tokens FROM swarm_usage WHERE swarm_id=? AND day=?',
                        (swarm_id, day)).fetchone() or (0, 0)
                    if (max_requests is not None and used_r >= max_requests) or \
                            (max_tokens is not None and used_t >= max_tokens):
                        raise WireError('entry_swarm_daily_cap', 429)
                    if max_tokens is not None:
                        taken = min(taken, max_tokens - used_t)
                        swarm_left['swarm_tokens_left'] = max_tokens - used_t - taken
                    if max_requests is not None:
                        swarm_left['swarm_requests_left'] = max_requests - used_r - 1
                    self._db.execute('INSERT OR REPLACE INTO swarm_usage (swarm_id, day, requests, tokens) '
                                     'VALUES (?, ?, ?, ?)', (swarm_id, day, used_r + 1, used_t + taken))
                    self._db.execute('DELETE FROM swarm_usage WHERE swarm_id=? AND day<?', (swarm_id, day - 7))
                if grant is not None:
                    self._db.execute('INSERT OR REPLACE INTO usage (grant_id, requests, tokens) VALUES (?, ?, ?)',
                                     (grant['grant_id'], requests + 1, tokens + taken))
                self._db.execute('COMMIT')
            except BaseException:
                self._db.execute('ROLLBACK')
                raise
        if grant is None:
            return taken, swarm_left
        return taken, {'requests_left': grant['max_requests'] - requests - 1, 'tokens_left': tokens_left - taken,
                       **swarm_left}

    def refund(self, grant_id: str | None, tokens: int, swarm: tuple[str, int] | None = None) -> None:
        with self._lock:
            if grant_id is not None:
                self._db.execute('UPDATE usage SET requests=MAX(requests-1, 0), tokens=MAX(tokens-?, 0) '
                                 'WHERE grant_id=?', (tokens, grant_id))
            if swarm is not None:
                self._db.execute('UPDATE swarm_usage SET requests=MAX(requests-1, 0), tokens=MAX(tokens-?, 0) '
                                 'WHERE swarm_id=? AND day=?', (tokens, swarm[0], swarm[1]))

    def swarm_usage(self, swarm_id: str, day: int) -> tuple[int, int]:
        with self._lock:
            row = self._db.execute('SELECT requests, tokens FROM swarm_usage WHERE swarm_id=? AND day=?',
                                   (swarm_id, day)).fetchone()
        return row if row else (0, 0)

    def kill_state(self, swarm_id: str) -> tuple[bool, int]:
        with self._lock:
            row = self._db.execute('SELECT engaged, seq FROM kill_switch WHERE swarm_id=?', (swarm_id,)).fetchone()
        return (bool(row[0]), row[1]) if row else (False, 0)

    def set_kill(self, swarm_id: str, engaged: bool, seq: int, at: int) -> bool:
        """Store the owner's switch; False (nothing changed) unless ``seq`` is newer than the stored one."""
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                row = self._db.execute('SELECT seq FROM kill_switch WHERE swarm_id=?', (swarm_id,)).fetchone()
                fresh = row is None or seq > row[0]
                if fresh:
                    self._db.execute('INSERT OR REPLACE INTO kill_switch (swarm_id, engaged, seq, at) '
                                     'VALUES (?, ?, ?, ?)', (swarm_id, int(engaged), seq, at))
                self._db.execute('COMMIT')
            except BaseException:
                self._db.execute('ROLLBACK')
                raise
        return fresh

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
    grant_id: str | None
    key: str
    client_key: str
    max_new_tokens: int
    clamped: bool
    left: dict
    swarm: tuple[str, int] | None = None
    daily: tuple[str, int, int] | None = None  # (address key, utc day, tokens reserved) of the per-address budget


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
        self._open_ip_day = WindowLimiter(limits.open_grants_per_ip_per_day, 86400)
        self._ip_day = DailyBudget(limits.ip_daily_requests, limits.ip_daily_tokens)
        self._cancel_ip = WindowLimiter(limits.cancels_per_ip_per_minute)
        self._nonces: dict[str, int] = {}
        self._cut: OrderedDict[str, None] = OrderedDict()  # requests ended by the kill switch
        self._requesters: OrderedDict[str, str] = OrderedDict()  # request id -> key that signed the prompt
        self._lock = threading.Lock()
        #: Called (best effort, never raises into the kill) when the owner engages the kill switch; the gateway
        #: points it at the chat backend so the running generation is cancelled, not only hidden.
        self.on_kill: Callable[[], None] | None = None
        self.limits = limits

    @property
    def limits(self) -> EntryLimits:
        return self._limits

    @limits.setter
    def limits(self, value: EntryLimits) -> None:
        self._limits = value  # counters survive a limit change
        self._ip.limit, self._key.limit = value.per_ip_per_minute, value.per_key_per_minute
        self._events.limit, self._open_ip.limit = value.events_per_ip_per_minute, value.open_grants_per_ip_per_hour
        self._cancel_ip.limit = value.cancels_per_ip_per_minute
        self._open_ip_day.limit = value.open_grants_per_ip_per_day
        self._ip_day.requests, self._ip_day.tokens = value.ip_daily_requests, value.ip_daily_tokens

    def current(self) -> EntryPolicy:
        policy = self.policy()
        if policy is None:
            raise WireError('entry_not_published', 503)
        return policy

    def _signed(self, raw: bytes, protocol: str, client: str, policy: EntryPolicy, now: int, *,
                ip: WindowLimiter | None = None, key_limit: bool = True) -> dict[str, Any]:
        if not (ip or self._ip).allow(client, now):
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
        if key_limit and not self._key.allow(document['key'], now):
            raise WireError('entry_rate_limited_key', 429)
        return document

    # -- the requester's own stop ---------------------------------------------------------
    def remember(self, request_id: Any, key: str) -> None:
        """Note which key signed the prompt behind ``request_id`` (in memory: a request does not outlive the app)."""
        if not isinstance(request_id, str):
            return
        with self._lock:
            self._requesters[request_id] = key
            while len(self._requesters) > MAX_REMEMBERED_REQUESTS:
                self._requesters.popitem(last=False)

    def apply_cancel(self, raw: bytes, client: str) -> str:
        """Verify a requester-signed cancel and return the request id it may stop.

        Only the key that signed the prompt may stop it (not the owner: that is the kill switch).  No grant and
        no send budget is needed to stop your own prompt; the cancel has its own per-address budget.
        """
        policy = self.current()
        now = int(self.clock())
        document = self._signed(raw, docs.ENTRY_CANCEL, client, policy, now, ip=self._cancel_ip, key_limit=False)
        with self._lock:
            requester = self._requesters.get(document['request_id'])
        if requester is None:
            raise WireError('entry_request_unknown', 404)
        if requester != document['key']:
            raise WireError('entry_cancel_not_requester', 403)
        return document['request_id']

    # -- swarm-wide budget and owner kill switch --------------------------------------
    def _cap(self, policy: EntryPolicy, now: int) -> tuple[str, int, int | None, int | None] | None:
        limits = self.limits
        if policy.access_mode not in hosted_defaults.SWARM_CAP_ACCESS_MODES or \
                (limits.swarm_daily_requests is None and limits.swarm_daily_tokens is None):
            return None
        return policy.swarm_id, now // 86400, limits.swarm_daily_requests, limits.swarm_daily_tokens

    def killed(self, policy: EntryPolicy) -> bool:
        return self.ledger.kill_state(policy.swarm_id)[0]

    def budget(self, policy: EntryPolicy) -> dict[str, Any] | None:
        now = int(self.clock())
        cap = self._cap(policy, now)
        if cap is None:
            return None
        used_r, used_t = self.ledger.swarm_usage(cap[0], cap[1])
        return {'requests_left': None if cap[2] is None else max(cap[2] - used_r, 0),
                'tokens_left': None if cap[3] is None else max(cap[3] - used_t, 0),
                'resets_at': (cap[1] + 1) * 86400}

    def apply_kill(self, raw: bytes, client: str) -> dict[str, Any]:
        """Apply an owner-signed kill switch document (engage or release); replays are refused."""
        policy = self.current()
        now = int(self.clock())
        if not self._ip.allow(client, now):
            raise WireError('entry_rate_limited_ip', 429)
        document = docs.check(parse_json_object(raw, limit=self.limits.max_body), docs.KILL_SWITCH, self.verifier)
        if document['swarm_id'] != policy.swarm_id:
            raise WireError('entry_swarm_mismatch', 404)
        if document['owner_key'] != policy.owner_key:
            raise WireError('entry_kill_not_owner', 403)
        if abs(document['at'] - now) > self.limits.skew:
            raise WireError('entry_clock_skew', 400)
        if not self.ledger.set_kill(policy.swarm_id, document['engaged'], document['seq'], now):
            raise WireError('entry_kill_seq_stale', 409)
        if document['engaged'] and self.on_kill is not None:
            try:
                self.on_kill()
            except Exception:  # noqa: BLE001 - the kill is already durable; cancelling is best effort
                pass
        return {'engaged': document['engaged'], 'seq': document['seq']}

    def settle_events(self, request_id: Any, page: dict[str, Any], policy: EntryPolicy) -> dict[str, Any]:
        """After a kill, a request still running ends with an explicit terminal (and stays ended)."""
        with self._lock:
            ended = request_id in self._cut
            if not ended and not page.get('done') and self.killed(policy) and isinstance(request_id, str):
                self._cut[request_id] = None
                while len(self._cut) > 512:
                    self._cut.popitem(last=False)
                ended = True
        if not ended and page.get('error') != KILLED:
            return page
        return {**page, 'done': True, 'error': KILLED, 'killed': True}

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
        if self.killed(policy):
            raise WireError('entry_killed_by_owner', 403)
        document = self._signed(raw, docs.ENTRY_SEND, client, policy, now)
        grant = self.check_grant(document['grant'], document['key'], policy, now)
        wanted = min(document['max_new_tokens'], self.limits.max_new_tokens)
        cap = self._cap(policy, now)
        daily = self._reserve_address(cap, client, now, wanted)
        try:
            taken, left = self.ledger.charge(grant, daily[2] if daily else wanted, cap)
        except BaseException:
            self._release_address(daily, 0)
            raise
        self._release_address(daily, taken)
        return Admission(grant_id=grant['grant_id'], key=document['key'],
                         client_key='swarm:' + fingerprint(document['key']), max_new_tokens=taken,
                         clamped=taken != document['max_new_tokens'], left=left,
                         swarm=None if cap is None else (cap[0], cap[1]),
                         daily=None if daily is None else (daily[0], daily[1], taken)), document['prompt']

    # -- per-address daily budget (open swarms) -------------------------------------------
    def _reserve_address(self, cap: Any, client: str, now: int, wanted: int) -> tuple[str, int, int] | None:
        if cap is None:  # not an open swarm: its grants bound the use
            return None
        day = now // 86400
        taken = self._ip_day.reserve(client, day, wanted)
        if taken is None:
            raise WireError('entry_ip_daily_cap', 429)
        return client, day, taken

    def _release_address(self, daily: tuple[str, int, int] | None, used_tokens: int) -> None:
        """Give back what the swarm-wide charge did not use (all of it when ``used_tokens`` is 0 after a refusal)."""
        if daily is None:
            return
        client, day, reserved = daily
        if used_tokens == 0:
            self._ip_day.release(client, day, requests=1, tokens=reserved)
        elif used_tokens < reserved:
            self._ip_day.release(client, day, requests=0, tokens=reserved - used_tokens)

    def ip_budget(self, policy: EntryPolicy, client: str) -> dict[str, Any] | None:
        now = int(self.clock())
        if self._cap(policy, now) is None:
            return None
        requests_left, tokens_left = self._ip_day.left(client, now // 86400)
        return {'requests_left': requests_left, 'tokens_left': tokens_left, 'resets_at': (now // 86400 + 1) * 86400}

    def refund(self, admission: Admission) -> None:
        self.ledger.refund(admission.grant_id, admission.max_new_tokens, admission.swarm)
        if admission.daily is not None:
            self._ip_day.release(admission.daily[0], admission.daily[1], requests=1, tokens=admission.daily[2])

    def admit_relay_chat(self, client: str, wanted_tokens: int) -> Admission | None:
        """Gate the owner relay's shared ``/chat/api/send`` with the entry's own checks (None = nothing published).

        While a swarm is published its entry is the only public door: ``/chat`` honours the same owner kill
        switch and the same swarm-wide daily budget, and is refused outright unless the swarm is open (an
        invite-only or request-only swarm admits grant holders through ``/swarm-entry/v1/send`` alone).
        """
        policy = self.policy()
        if policy is None:
            return None
        now = int(self.clock())
        if self.killed(policy):
            raise WireError('entry_killed_by_owner', 403)
        if policy.access_mode != 'open':
            raise WireError('entry_chat_requires_entry', 403)
        wanted = min(wanted_tokens, self.limits.max_new_tokens)
        cap = self._cap(policy, now)
        daily = self._reserve_address(cap, client, now, wanted)
        try:
            taken, left = self.ledger.charge(None, daily[2] if daily else wanted, cap)
        except BaseException:
            self._release_address(daily, 0)
            raise
        self._release_address(daily, taken)
        return Admission(grant_id=None, key=client, client_key=client, max_new_tokens=taken,
                         clamped=taken != wanted_tokens, left=left, swarm=None if cap is None else (cap[0], cap[1]),
                         daily=None if daily is None else (daily[0], daily[1], taken))

    def open_grant(self, raw: bytes, client: str) -> dict[str, Any]:
        policy = self.current()
        now = int(self.clock())
        if policy.access_mode != 'open' or not policy.open_quota:
            raise WireError('entry_swarm_not_open', 403)
        if self.killed(policy):
            raise WireError('entry_killed_by_owner', 403)
        document = self._signed(raw, docs.OPEN_GRANT_REQUEST, client, policy, now)
        existing = self.ledger.open_grant(policy.swarm_id, document['key'], now)
        if existing is not None:
            return existing
        if not self._open_ip.allow(client, now):
            raise WireError('entry_open_grants_rate_limited', 429)
        if not self._open_ip_day.allow(client, now):
            self._open_ip.refund(client)
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

    def __init__(self, guard: EntryGuard, chat: Callable[[], Any],
                 live: Callable[[], dict[str, Any]] | None = None) -> None:
        self.guard = guard
        self.chat = chat
        self.live = live
        guard.on_kill = lambda: self._cancel_backend(None)

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
                    'killed': self.guard.killed(policy), 'swarm_budget': self.guard.budget(policy),
                    **({'ip_budget': ip_budget} if (ip_budget := self.guard.ip_budget(policy, client)) else {}),
                    'live': self._live(), 'privacy': PRIVACY_NOTE}
        if route == 'kill':
            return self.guard.apply_kill(body, client)
        if route == 'open-grant':
            return {'grant': self.guard.open_grant(body, client)}
        if route == 'cancel':
            request_id = self.guard.apply_cancel(body, client)
            cancel = getattr(self.chat(), 'cancel', None)
            if not callable(cancel):
                raise WireError('entry_cancel_unsupported', 501)
            return {'cancelled': bool(self._chat_call(lambda: cancel(request_id, CANCELLED_BY_REQUESTER))),
                    'request_id': request_id}
        if route == 'events':
            self.guard.current()
            if not self.guard.allow_events(client):
                raise WireError('entry_rate_limited_ip', 429)
            document = parse_json_object(body, limit=1024)
            page = self._chat_call(lambda: self.chat().events(document))
            settled = self.guard.settle_events(document.get('request_id'), page, self.guard.current())
            if settled.get('killed') and not page.get('done'):
                self._cancel_backend(document.get('request_id'))  # the bulk cancel missed it (or failed): retry here
            return settled
        admission, prompt = self.guard.admit(body, client)
        try:
            accepted = self._chat_call(lambda: self.chat().send(
                admission.client_key, {'prompt': prompt, 'max_new_tokens': admission.max_new_tokens}))
        except BaseException:
            self.guard.refund(admission)
            raise
        self.guard.remember(accepted.get('request_id'), admission.key)
        return {**accepted, 'clamped': bool(accepted.get('clamped')) or admission.clamped, 'quota': admission.left}

    def _live(self) -> dict[str, Any]:
        if self.live is None:
            return {'available': False, 'reason': 'live_source_not_wired'}
        try:
            return self.live()
        except Exception:  # noqa: BLE001 - the state route must never fail because a live source did
            return {'available': False, 'reason': 'live_source_failed'}

    def _cancel_backend(self, request_id: Any) -> None:
        """Ask the chat backend to stop one request (or all in flight); a backend without cancel is left alone."""
        chat = self.chat()
        if request_id is None:
            cancel_all = getattr(chat, 'cancel_all', None)
            if callable(cancel_all):
                cancel_all(KILLED)
            return
        cancel = getattr(chat, 'cancel', None)
        if callable(cancel) and isinstance(request_id, str):
            try:
                cancel(request_id, KILLED)
            except Exception:  # noqa: BLE001
                pass

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
