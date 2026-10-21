# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV10: a member's signed withdrawal over the member channel (``Leave smoothly``) and the owner's record of it.

Two notices travel on the same signed managed-control plane as the state report (``member_state``); the owner verifies
them against the CURRENT member record, so a revoked member or an old generation is refused before this module sees it:

``planning``  Leave smoothly has started: the member names the requests still running on it (bounded, ids only).  The
              owner answers, per request, what it did: ``a7_kv_standby`` (the existing A7 fenced KV-standby cutover),
              ``a6_replay`` (the existing A6 full-context replay), ``not_in_flight`` (not running through that member,
              or already finished) or ``loss_detection`` (nothing it could do early; the member keeps draining).
``final``     the member is about to stop: ``drained`` (its work finished) or ``forced`` (the stall/cap bound lapsed and
              running work was cut off).  The owner records ``left_smoothly`` and, because the notice is signed by
              the member, revokes that generation itself (self-revoke) so the Mac drops out of the roster at once.

``Leave now`` sends nothing.  A member the owner can no longer reach and that never withdrew is shown as
``left_suddenly`` after ``LOST_AFTER_SECONDS`` without any signed contact; the label comes only from these records and
from observed silence - the owner never calls a vanished Mac "smooth", and never guesses after its own restart (no
contact seen yet = no verdict).  Final records are persisted (bounded); planning and contact are in memory only.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from .managed_control import (_BINDING_FIELDS, _REQUEST_COMMON_FIELDS, _RESPONSE_COMMON_FIELDS, MAX_CLOCK_SKEW_SECONDS,
                              ManagedControlError, _binding_from_member)

NOTICE = 'mycelium.v2_member_withdrawal_notice.v1'
REQUEST = 'mycelium.v2_member_withdrawal.v1'
RESPONSE = 'mycelium.v2_member_withdrawal_ack.v1'
PATH_WITHDRAW = '/managed-control/member-state/withdraw'
DISCOVERY = 'demo-run-discovery'

PHASES = frozenset({'planning', 'final'})
OUTCOMES = frozenset({'drained', 'forced'})
DECISIONS = frozenset({'a7_kv_standby', 'a6_replay', 'not_in_flight', 'loss_detection'})
#: Decisions after which the owner has the request under control (the member need not finish it).
HANDLED = frozenset({'a7_kv_standby', 'a6_replay', 'not_in_flight'})

MAX_IN_FLIGHT = 16
MAX_RECORDS = 64
MAX_NOTICE_AGE_SECONDS = 120.0
MAX_FUTURE_SECONDS = MAX_CLOCK_SKEW_SECONDS
LOST_AFTER_SECONDS = 60.0            # a member that is silent this long without a withdrawal "left suddenly"
MAX_NOTICE_BYTES = 4096
_REQUEST_ID = re.compile(r'[A-Za-z0-9:_.\-]{1,128}\Z')

_REQ_FIELDS = frozenset(_REQUEST_COMMON_FIELDS | _BINDING_FIELDS | {'notice'})
_RESP_FIELDS = frozenset(_RESPONSE_COMMON_FIELDS | _BINDING_FIELDS | {'ack'})
_NOTICE_KEYS = frozenset({'protocol', 'phase', 'issued_at', 'generation', 'in_flight', 'outcome'})


def valid_request_id(value: Any) -> bool:
    return isinstance(value, str) and _REQUEST_ID.match(value) is not None


def _invalid() -> ValueError:
    return ValueError('member_withdrawal_invalid')


def validate_notice(notice: Any) -> dict[str, Any]:
    """Closed member withdrawal notice; raises ``ValueError('member_withdrawal_invalid')``."""
    try:
        raw = json.dumps(notice, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError):
        raise _invalid() from None
    ok = (isinstance(notice, dict) and len(raw) <= MAX_NOTICE_BYTES and set(notice) == _NOTICE_KEYS
          and notice['protocol'] == NOTICE and notice['phase'] in PHASES
          and type(notice['issued_at']) in (int, float) and math.isfinite(notice['issued_at'])
          and type(notice['generation']) is int and 0 <= notice['generation'] <= 2 ** 50)
    if not ok:
        raise _invalid()
    ids = notice['in_flight']
    if not (isinstance(ids, list) and len(ids) <= MAX_IN_FLIGHT and all(valid_request_id(i) for i in ids)
            and len(set(ids)) == len(ids)):
        raise _invalid()
    if notice['phase'] == 'final' and notice['outcome'] not in OUTCOMES:
        raise _invalid()
    if notice['phase'] == 'planning' and notice['outcome'] is not None:
        raise _invalid()
    return json.loads(raw)


def _age_text(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f'{seconds} s ago'
    if seconds < 3600:
        return f'{seconds // 60} min ago'
    return f'{seconds // 3600} h ago'


def _write_private(path: Path, document: Any) -> None:
    temp = path.with_name(path.name + f'.{os.getpid()}.{threading.get_ident()}.tmp')
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(json.dumps(document, sort_keys=True, separators=(',', ':')))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


# -- owner side ---------------------------------------------------------------------------------------------------------

class MemberWithdrawalChannel:
    """Owner side: the signed route, the record of who left how, and the view built from it."""

    def __init__(self, mailbox: Any, *, clock: Callable[[], float] = time.time,
                 store_path: Path | str | None = None) -> None:
        self.mailbox = mailbox
        self._clock = clock
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}
        self._contact: dict[str, float] = {}
        self._planner: Callable[[str, int, list[str]], Mapping[str, Any]] | None = None
        self._on_final: Callable[[str, int], Any] | None = None
        self._store = None if store_path is None else Path(store_path)
        self._load()

    def install(self) -> 'MemberWithdrawalChannel':
        routes = dict(getattr(self.mailbox, 'route_extensions', None) or {})
        routes[PATH_WITHDRAW] = self._withdraw
        self.mailbox.route_extensions = routes
        return self

    def set_planner(self, planner: Callable[[str, int, list[str]], Mapping[str, Any]] | None) -> None:
        """``planner(node_id, generation, request_ids) -> {request_id: decision}`` (the A7/A6 choice, see leave_handoff)."""
        self._planner = planner

    def set_on_final(self, callback: Callable[[str, int], Any] | None) -> None:
        """``callback(node_id, generation)`` runs once per signed final: the owner's self-revoke."""
        self._on_final = callback

    # -- persistence (final records only) ---------------------------------------------------------------------------------
    def _load(self) -> None:
        if self._store is None:
            return
        try:
            value = json.loads(self._store.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(value, dict):
            return
        for node, row in list(value.items())[:MAX_RECORDS]:
            if (isinstance(node, str) and isinstance(row, dict) and type(row.get('generation')) is int
                    and type(row.get('final_at')) in (int, float) and type(row.get('drained')) is bool):
                self._records[node] = {'generation': row['generation'], 'phase': 'final', 'final_at': float(row['final_at']),
                                       'drained': row['drained'], 'revoked': row.get('revoked') is True,
                                       'planning_at': None, 'in_flight': [], 'handoff': {}}

    def _save_locked(self) -> None:
        if self._store is None:
            return
        finals = {n: {'generation': r['generation'], 'final_at': r['final_at'], 'drained': r['drained'],
                      'revoked': r['revoked']} for n, r in self._records.items() if r['phase'] == 'final'}
        try:
            _write_private(self._store, finals)
        except OSError:
            pass   # the in-memory record still stands; a persistence failure never fails the withdrawal

    # -- store ----------------------------------------------------------------------------------------------------------------
    def observe_contact(self, node_id: str) -> None:
        with self._lock:
            if node_id not in self._contact and len(self._contact) >= 4 * MAX_RECORDS:
                del self._contact[min(self._contact, key=lambda key: self._contact[key])]
            self._contact[node_id] = self._clock()

    def last_contact(self, node_id: str) -> float | None:
        with self._lock:
            return self._contact.get(node_id)

    def records(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._records)

    def forget_all(self) -> None:
        with self._lock:
            self._records.clear()
            self._save_locked()

    def _put_locked(self, node_id: str, record: dict[str, Any]) -> None:
        if node_id not in self._records and len(self._records) >= MAX_RECORDS:
            oldest = min(self._records, key=lambda key: self._records[key].get('final_at')
                         or self._records[key].get('planning_at') or 0.0)
            del self._records[oldest]
        self._records[node_id] = record

    def record_final(self, node_id: str, generation: int, *, drained: bool, revoked: bool) -> None:
        with self._lock:
            self._put_locked(node_id, {'generation': generation, 'phase': 'final', 'final_at': self._clock(),
                                       'drained': drained, 'revoked': revoked, 'planning_at': None,
                                       'in_flight': [], 'handoff': {}})
            self._save_locked()

    # -- view ------------------------------------------------------------------------------------------------------------------
    def view(self, node_id: str, generation: int | None = None, lifecycle_state: str | None = None) -> dict[str, Any] | None:
        """What the owner may say about how one member left, or ``None`` (nothing known = nothing claimed)."""
        now = self._clock()
        with self._lock:
            record = copy.deepcopy(self._records.get(node_id))
            contact = self._contact.get(node_id)
        if record is not None and generation is not None and not record['generation'] <= generation <= record['generation'] + 1:
            record = None          # a later membership (a re-join) supersedes it
        if record is not None and record['phase'] == 'final':
            age = max(0.0, now - record['final_at'])
            cut = '' if record['drained'] else ' (work still running was cut off after the wait limit)'
            return {'kind': 'left_smoothly', 'signed': True, 'drained': record['drained'], 'generation': record['generation'],
                    'at': record['final_at'], 'age_seconds': round(age, 1), 'revoked': record['revoked'],
                    'handoff': None,
                    'line': f'Left smoothly - signed withdrawal received {_age_text(age)}{cut}'}
        silent = contact is not None and now - contact > LOST_AFTER_SECONDS
        contact = 0.0 if contact is None else contact
        if record is not None:         # planning only
            if not silent:
                return {'kind': 'leaving', 'signed': True, 'drained': None, 'generation': record['generation'],
                        'at': record['planning_at'], 'age_seconds': round(max(0.0, now - (record['planning_at'] or now)), 1),
                        'revoked': False, 'handoff': dict(record['handoff']),
                        'line': 'Leaving smoothly - the swarm is planning for it'}
            return self._suddenly(now, contact, record['generation'], planned=True)
        if silent and lifecycle_state not in {'STOPPING', 'STOPPED'}:
            return self._suddenly(now, contact, generation, planned=False)
        return None

    @staticmethod
    def _suddenly(now: float, contact: float, generation: int | None, *, planned: bool) -> dict[str, Any]:
        age = max(0.0, now - contact)
        why = 'no final withdrawal after a planned leave' if planned else 'no withdrawal received'
        return {'kind': 'left_suddenly', 'signed': False, 'drained': None, 'generation': generation, 'at': None,
                'age_seconds': round(age, 1), 'revoked': False, 'handoff': None,
                'last_contact_age_seconds': round(age, 1),
                'line': f'Left suddenly - {why}; last heard {_age_text(age)}'}

    # -- route -----------------------------------------------------------------------------------------------------------------
    def _withdraw(self, envelope: Any) -> dict[str, Any]:
        mailbox = self.mailbox
        statement, member = mailbox._verify_target(envelope, protocol=REQUEST, fields=_REQ_FIELDS)
        if statement['run_id'] != DISCOVERY or statement['deployment_id'] != DISCOVERY:
            raise ManagedControlError('managed_control_run_binding_mismatch')
        try:
            notice = validate_notice(statement['notice'])
        except ValueError:
            raise ManagedControlError('member_withdrawal_invalid') from None
        if notice['generation'] != statement['membership_generation'] or notice['generation'] != member['generation']:
            raise ManagedControlError('member_withdrawal_generation_mismatch')
        age = self._clock() - notice['issued_at']
        if age > MAX_NOTICE_AGE_SECONDS:
            raise ManagedControlError('member_withdrawal_stale')
        if age < -MAX_FUTURE_SECONDS:
            raise ManagedControlError('member_withdrawal_from_future')
        node = statement['target_node_id']
        self.observe_contact(node)
        ack = self._planning(node, notice) if notice['phase'] == 'planning' else self._final(node, notice)
        return mailbox._response(RESPONSE, statement['request_id'], {
            **{key: statement[key] for key in _BINDING_FIELDS}, 'ack': ack})

    def _planning(self, node: str, notice: Mapping[str, Any]) -> dict[str, Any]:
        generation, ids = notice['generation'], list(notice['in_flight'])
        with self._lock:
            held = self._records.get(node)
            if held is not None and held['generation'] == generation and held['phase'] == 'planning' \
                    and held['in_flight'] == ids:
                return {'accepted': True, 'phase': 'planning', 'recorded': 'leaving', 'revoked': False,
                        'handoff': dict(held['handoff'])}
        decisions = self._decide(node, generation, ids)
        with self._lock:
            self._put_locked(node, {'generation': generation, 'phase': 'planning', 'planning_at': self._clock(),
                                    'final_at': None, 'drained': None, 'revoked': False, 'in_flight': ids,
                                    'handoff': dict(decisions)})
        return {'accepted': True, 'phase': 'planning', 'recorded': 'leaving', 'revoked': False, 'handoff': decisions}

    def _decide(self, node: str, generation: int, ids: list[str]) -> dict[str, Any]:
        answers: Mapping[str, Any] = {}
        planner = self._planner
        if planner is not None and ids:
            try:
                answers = planner(node, generation, list(ids)) or {}
            except Exception:
                answers = {}
        return {rid: (answers.get(rid) if answers.get(rid) in DECISIONS else 'loss_detection') for rid in ids}

    def _final(self, node: str, notice: Mapping[str, Any]) -> dict[str, Any]:
        generation = notice['generation']
        revoked = False
        callback = self._on_final
        if callback is not None:
            try:
                callback(node, generation)
                revoked = True
            except Exception:
                revoked = False
        self.record_final(node, generation, drained=notice['outcome'] == 'drained', revoked=revoked)
        return {'accepted': True, 'phase': 'final', 'recorded': 'left_smoothly', 'revoked': revoked, 'handoff': {}}


# -- member side ------------------------------------------------------------------------------------------------------------

class MemberWithdrawalClient:
    """Member side: sign and post one notice on the existing managed-control client."""

    def __init__(self, peer: Any, *, _test_ca_certificate=None) -> None:
        self.peer = peer
        self._ca = _test_ca_certificate

    def _statement(self, client: Any, member: Mapping[str, Any], notice: Mapping[str, Any]) -> tuple[dict, str]:
        now = client._clock()
        request_id = uuid.uuid4().hex
        binding = _binding_from_member(member, DISCOVERY, DISCOVERY)
        return ({**binding, 'protocol': REQUEST, 'swarm_id': client.swarm_id, 'request_id': request_id,
                 'sequence': 1, 'nonce': uuid.uuid4().hex, 'issued_at': now, 'expires_at': now + 30,
                 'notice': dict(notice)}, request_id)

    def envelope(self, notice: Mapping[str, Any]) -> dict[str, Any]:
        """The signed request (also what the tests tamper with and replay)."""
        client = self.peer.staging_control_client(_test_ca_certificate=self._ca)
        member = self.peer.staging_control_snapshot()
        statement, _ = self._statement(client, member, notice)
        return client._signed_outbound(statement)

    def send(self, notice: Mapping[str, Any]) -> dict[str, Any]:
        value = validate_notice(dict(notice))
        client = self.peer.staging_control_client(_test_ca_certificate=self._ca)
        member = self.peer.staging_control_snapshot()
        statement, request_id = self._statement(client, member, value)
        response = client._request(PATH_WITHDRAW, client._signed_outbound(statement))
        result = client._verified_response(response, protocol=RESPONSE, fields=_RESP_FIELDS, request_id=request_id,
                                           target=member, run_id=DISCOVERY, deployment_id=DISCOVERY)
        ack = result['ack']
        if not (isinstance(ack, dict) and ack.get('accepted') is True and ack.get('phase') == value['phase']
                and isinstance(ack.get('handoff'), dict)
                and all(k in value['in_flight'] and v in DECISIONS for k, v in ack['handoff'].items())):
            raise ManagedControlError('member_withdrawal_ack_invalid')
        return ack
