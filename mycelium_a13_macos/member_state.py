# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV1/PV11: signed member state reports, the owner's store and view, and the member-side reporter.

Every joined Mac sends the owner a *state report* over the same signed managed-control plane the demo-run exchange uses
(``run_exchange``): the member signs the request with its membership key (the mailbox verifies it against the CURRENT
member record, so a revoked member or an old generation is refused before this module sees it).  The report is closed
and bounded - codes and counters only, never an application name:

``generation``  the membership generation the report speaks for (must equal the signed binding),
``sharing``     Adaptive sharing ``normal | reduced | paused`` + a closed reason code + whether the automatic controller is on,
``worker``      ``running | idle | warming``,
``model``       the pinned snapshot identity and its verification state, or ``None``,
``caps``        the standing storage / memory caps, or ``None``,
``service_mode`` the Interactive/Batch mode this Mac applies (``None`` until the modes code is on this branch),
``issued_at``   the member's clock (bounded skew is enforced by the owner).

The owner keeps the latest report per member **in memory only**.  After the owner restarts there is nothing to assume:
the view says ``unknown`` and the announcement each member already reads every 2 s carries
``member_state_requested`` so the member answers at once; nothing is read from disk or guessed.  The same
announcement carries ``service_mode`` (the owner's mode) so a later merge of ``lane/rel2-modes`` can hand the mode to
members without a new channel.
"""
from __future__ import annotations

import copy
import json
import math
import re
import threading
import time
import uuid
from typing import Any, Callable, Mapping

from .managed_control import (_BINDING_FIELDS, _REQUEST_COMMON_FIELDS, _RESPONSE_COMMON_FIELDS, MAX_CLOCK_SKEW_SECONDS,
                              ManagedControlError, _binding_from_member)

STATE_REPORT = 'mycelium.v2_member_state_report.v1'
REQUEST = 'mycelium.v2_member_state_request.v1'
RESPONSE = 'mycelium.v2_member_state_ack.v1'
PATH_STATE = '/managed-control/member-state/report'
DISCOVERY = 'demo-run-discovery'

PURPOSES = frozenset({'reconnect', 'periodic', 'change', 'requested'})
SHARING_STATES = frozenset({'normal', 'reduced', 'paused'})
#: ``adaptive_host.REASON_CATEGORIES`` plus the documented fail-closed codes; never an app name.
REASONS = frozenset({'none', 'owner_game_mode', 'owner_stop', 'host_memory_pressure', 'host_cpu_saturation',
                     'adaptive_signal_unknown', 'adaptive_controller_stale', 'adaptive_policy_invalid'})
WORKER_STATES = frozenset({'running', 'idle', 'warming'})
VERIFICATIONS = frozenset({'verified', 'downloading', 'unverified', 'mismatch'})
SERVICE_MODES = frozenset({'interactive', 'batch'})

REPORT_INTERVAL_SECONDS = 30.0
MIN_CHANGE_GAP_SECONDS = 10.0
FRESH_SECONDS = 3 * REPORT_INTERVAL_SECONDS     # a view older than this is stale
MAX_REPORT_AGE_SECONDS = 120.0                  # a report older than this when it arrives is refused
MAX_FUTURE_SECONDS = MAX_CLOCK_SKEW_SECONDS     # the documented managed-control skew bound
MAX_MEMBERS = 64
MAX_REPORT_BYTES = 4096
_RETRY_FIRST_SECONDS, _RETRY_MAX_SECONDS = 2.0, 30.0
_CODE = re.compile(r'[a-z][a-z0-9_]{0,95}\Z')

_REQ_FIELDS = frozenset(_REQUEST_COMMON_FIELDS | _BINDING_FIELDS | {'report'})
_RESP_FIELDS = frozenset(_RESPONSE_COMMON_FIELDS | _BINDING_FIELDS | {'ack'})
_REPORT_KEYS = frozenset({'protocol', 'purpose', 'issued_at', 'generation', 'sharing', 'worker', 'model', 'caps',
                          'service_mode'})
_FACT_KEYS = _REPORT_KEYS - {'protocol', 'purpose', 'issued_at'}
_SHARING_KEYS = frozenset({'state', 'reason', 'adaptive_enabled'})
_MODEL_KEYS = frozenset({'model_id', 'resolved_commit', 'verification'})
_CAP_KEYS = frozenset({'storage_bytes', 'max_memory_bytes'})

#: What the owner shows for a member's reason code (the member's own wording would say "you").
REASON_TEXT = {'owner_game_mode': 'paused on that Mac', 'owner_stop': 'stopped on that Mac',
               'host_memory_pressure': 'memory is low', 'host_cpu_saturation': 'the CPU is busy',
               'adaptive_signal_unknown': 'host signal unavailable', 'adaptive_controller_stale': 'host signal stale',
               'adaptive_policy_invalid': 'sharing policy unreadable'}


def _invalid() -> ValueError:
    return ValueError('member_state_report_invalid')


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= 2 ** 50


def validate_state_report(report: Any) -> dict[str, Any]:
    """Closed member state report; raises ``ValueError('member_state_report_invalid')``."""
    try:
        raw = json.dumps(report, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError):
        raise _invalid() from None
    ok = (isinstance(report, dict) and len(raw) <= MAX_REPORT_BYTES and set(report) == _REPORT_KEYS
          and report['protocol'] == STATE_REPORT and report['purpose'] in PURPOSES
          and _finite(report['issued_at']) and _count(report['generation']))
    if not ok:
        raise _invalid()
    sharing, worker, model, caps = report['sharing'], report['worker'], report['model'], report['caps']
    if not (isinstance(sharing, dict) and set(sharing) == _SHARING_KEYS and sharing['state'] in SHARING_STATES
            and sharing['reason'] in REASONS and type(sharing['adaptive_enabled']) is bool):
        raise _invalid()
    if not (isinstance(worker, dict) and set(worker) == {'state'} and worker['state'] in WORKER_STATES):
        raise _invalid()
    if model is not None and not (
            isinstance(model, dict) and set(model) == _MODEL_KEYS and model['verification'] in VERIFICATIONS
            and all(isinstance(model[k], str) and 0 < len(model[k]) <= 128 for k in ('model_id', 'resolved_commit'))):
        raise _invalid()
    if caps is not None and not (isinstance(caps, dict) and set(caps) == _CAP_KEYS
                                 and all(_count(caps[k]) for k in _CAP_KEYS)):
        raise _invalid()
    if report['service_mode'] is not None and report['service_mode'] not in SERVICE_MODES:
        raise _invalid()
    return json.loads(raw)


# -- owner side -----------------------------------------------------------------------------------------------------

def _age_text(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f'{seconds} s ago'
    if seconds < 3600:
        return f'{seconds // 60} min ago'
    return f'{seconds // 3600} h ago'


def _label(report: Mapping[str, Any]) -> str:
    state = report['sharing']['state']
    if state == 'paused':
        return 'Paused sharing'
    if state == 'reduced':
        return 'Sharing reduced'
    return 'Sharing'


def reported_view(report: Mapping[str, Any], generation: int, age: float, received_at: float | None, *,
                  source: str = 'report') -> dict[str, Any]:
    """The owner-visible view of one held report; ``age`` is on the owner's clock."""
    fresh = age <= FRESH_SECONDS
    line = (f'{_label(report)} - reported {_age_text(age)}' if fresh
            else f'Stale - last report {_age_text(age)} ({_label(report)})')
    return {'state': 'reported' if fresh else 'stale', 'age_seconds': round(age, 1),
            'since': None if fresh else received_at, 'generation': generation, 'source': source,
            'sharing': report['sharing'], 'worker': report['worker'], 'model': report['model'],
            'caps': report['caps'], 'service_mode': report['service_mode'], 'purpose': report['purpose'],
            'reason_text': REASON_TEXT.get(report['sharing']['reason']) if report['sharing']['state'] != 'normal' else None,
            'line': line}


def unknown_view(generation: int | None, since: float | None, line: str) -> dict[str, Any]:
    return {'state': 'unknown', 'age_seconds': None, 'since': since, 'generation': generation, 'source': 'none',
            'sharing': None, 'worker': None, 'model': None, 'caps': None, 'service_mode': None, 'purpose': None,
            'reason_text': None, 'line': line}


class MemberStateChannel:
    """Owner side: the signed route, the latest report per member, and the view built from them."""

    def __init__(self, mailbox: Any, *, clock: Callable[[], float] = time.time) -> None:
        self.mailbox = mailbox
        self._clock = clock
        self._lock = threading.Lock()
        self._latest: dict[str, dict[str, Any]] = {}
        self._service_mode: Callable[[], Any] | None = None
        #: When this view started: after a restart "unknown since" is this moment, never a remembered state.
        self.started_at = clock()

    def install(self) -> 'MemberStateChannel':
        routes = dict(getattr(self.mailbox, 'route_extensions', None) or {})
        routes[PATH_STATE] = self._report
        self.mailbox.route_extensions = routes
        return self

    # -- store ------------------------------------------------------------------------------------------------------
    def accept(self, node_id: str, generation: int, report: Mapping[str, Any], *, received_at: float | None = None) -> bool:
        """Keep ``report`` when it is not older than the one held (latest wins; a replay never overwrites)."""
        now = self._clock() if received_at is None else received_at
        with self._lock:
            held = self._latest.get(node_id)
            if held is not None and held['generation'] == generation and report['issued_at'] < held['report']['issued_at']:
                return False
            if node_id not in self._latest and len(self._latest) >= MAX_MEMBERS:
                oldest = min(self._latest, key=lambda key: self._latest[key]['received_at'])
                del self._latest[oldest]
            self._latest[node_id] = {'generation': generation, 'report': copy.deepcopy(dict(report)), 'received_at': now}
            return True

    def latest(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._latest)

    def forget(self, node_id: str) -> None:
        with self._lock:
            self._latest.pop(node_id, None)

    def is_fresh(self, node_id: str) -> bool:
        with self._lock:
            held = self._latest.get(node_id)
        return held is not None and self._clock() - held['received_at'] <= FRESH_SECONDS

    # -- view -------------------------------------------------------------------------------------------------------
    def view(self, node_id: str, generation: int | None = None) -> dict[str, Any]:
        """What the owner may say about one member now: reported / stale / unknown, with the age on the OWNER's clock."""
        now = self._clock()
        with self._lock:
            held = copy.deepcopy(self._latest.get(node_id))
        if held is None or (generation is not None and held['generation'] != generation):
            return unknown_view(generation, self.started_at,
                                f'Unknown - no report received yet (this view started {_age_text(now - self.started_at)})')
        return reported_view(held['report'], held['generation'], max(0.0, now - held['received_at']), held['received_at'])

    def set_service_mode_provider(self, provider: Callable[[], Any] | None) -> None:
        self._service_mode = provider

    def owner_service_mode(self) -> str | None:
        provider = self._service_mode
        if provider is None:
            return None
        try:
            value = provider()
        except Exception:
            return None
        return value if value in SERVICE_MODES else None

    # -- route ------------------------------------------------------------------------------------------------------
    def _report(self, envelope: Any) -> dict[str, Any]:
        mailbox = self.mailbox
        statement, member = mailbox._verify_target(envelope, protocol=REQUEST, fields=_REQ_FIELDS)
        if statement['run_id'] != DISCOVERY or statement['deployment_id'] != DISCOVERY:
            raise ManagedControlError('managed_control_run_binding_mismatch')
        try:
            report = validate_state_report(statement['report'])
        except ValueError:
            raise ManagedControlError('member_state_report_invalid') from None
        if report['generation'] != statement['membership_generation'] or report['generation'] != member['generation']:
            raise ManagedControlError('member_state_generation_mismatch')
        age = self._clock() - report['issued_at']
        if age > MAX_REPORT_AGE_SECONDS:
            raise ManagedControlError('member_state_stale')
        if age < -MAX_FUTURE_SECONDS:
            raise ManagedControlError('member_state_from_future')
        self.accept(statement['target_node_id'], report['generation'], report)
        return mailbox._response(RESPONSE, statement['request_id'], {
            **{key: statement[key] for key in _BINDING_FIELDS},
            'ack': {'accepted': True, 'owner_service_mode': self.owner_service_mode()}})


# -- member side ------------------------------------------------------------------------------------------------------

class MemberStateClient:
    """Member side: sign and post one state report on the existing managed-control client."""

    def __init__(self, peer: Any, *, _test_ca_certificate=None) -> None:
        self.peer = peer
        self._ca = _test_ca_certificate

    def _statement(self, client: Any, member: Mapping[str, Any], report: Mapping[str, Any]) -> tuple[dict, str]:
        now = client._clock()
        request_id = uuid.uuid4().hex
        binding = _binding_from_member(member, DISCOVERY, DISCOVERY)
        return ({**binding, 'protocol': REQUEST, 'swarm_id': client.swarm_id, 'request_id': request_id,
                 'sequence': 1, 'nonce': uuid.uuid4().hex, 'issued_at': now, 'expires_at': now + 30,
                 'report': dict(report)}, request_id)

    def envelope(self, report: Mapping[str, Any]) -> dict[str, Any]:
        """The signed request (also what the tests tamper with)."""
        client = self.peer.staging_control_client(_test_ca_certificate=self._ca)
        member = self.peer.staging_control_snapshot()
        statement, _ = self._statement(client, member, report)
        return client._signed_outbound(statement)

    def report(self, report: Mapping[str, Any]) -> dict[str, Any]:
        value = validate_state_report(dict(report))
        client = self.peer.staging_control_client(_test_ca_certificate=self._ca)
        member = self.peer.staging_control_snapshot()
        statement, request_id = self._statement(client, member, value)
        response = client._request(PATH_STATE, client._signed_outbound(statement))
        result = client._verified_response(response, protocol=RESPONSE, fields=_RESP_FIELDS, request_id=request_id,
                                           target=member, run_id=DISCOVERY, deployment_id=DISCOVERY)
        ack = result['ack']
        if not (isinstance(ack, dict) and ack.get('accepted') is True):
            raise ManagedControlError('member_state_ack_invalid')
        return ack


def _error_code(error: BaseException) -> str:
    code = getattr(error, 'code', None)
    if type(error).__module__.startswith('mycelium_') and isinstance(code, str) and _CODE.fullmatch(code):
        return code
    return 'member_state_send_failed'


class MemberStateReporter:
    """Decides WHEN to send: on (re)connect, when the owner asks, on a change (debounced), and periodically.

    ``facts()`` returns the report body (see ``collect_facts``); ``sink(report)`` delivers it and returns the ack.
    A failed send backs off (2 s doubling to 30 s) and the next success is labelled ``reconnect``.
    """

    def __init__(self, *, facts: Callable[[], Mapping[str, Any]], sink: Callable[[Mapping[str, Any]], Any],
                 clock: Callable[[], float] = time.time, interval: float = REPORT_INTERVAL_SECONDS,
                 min_change_gap: float = MIN_CHANGE_GAP_SECONDS,
                 on_owner_service_mode: Callable[[str | None], Any] | None = None) -> None:
        self._facts, self._sink, self._clock = facts, sink, clock
        self._interval, self._gap = float(interval), float(min_change_gap)
        self._on_mode = on_owner_service_mode
        self._lock = threading.Lock()
        self._last_sent_at: float | None = None
        self._last_facts: dict[str, Any] | None = None
        self._last_generation: Any = None
        self._reconnect = True
        self._retry_at = 0.0
        self._retry_delay = 0.0
        self._sent = 0
        self.last_purpose: str | None = None
        self.last_error: str | None = None
        self.owner_service_mode: str | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {'last_sent_at': self._last_sent_at, 'last_purpose': self.last_purpose,
                    'last_error': self.last_error, 'sent': self._sent}

    def _fail(self, now: float, code: str) -> None:
        self.last_error = code
        self._retry_delay = min(_RETRY_MAX_SECONDS, self._retry_delay * 2) if self._retry_delay else _RETRY_FIRST_SECONDS
        self._retry_at = now + self._retry_delay
        self._reconnect = True

    def tick(self, *, requested: bool = False) -> str | None:
        """One scheduling step; returns the purpose of the report just delivered, else ``None``."""
        with self._lock:
            now = self._clock()
            if now < self._retry_at:
                return None
            try:
                facts = copy.deepcopy(dict(self._facts()))
            except Exception:
                self._fail(now, 'member_state_facts_unavailable')
                return None
            generation = facts.get('generation')
            if generation is None:
                self.last_error = 'membership_required'
                return None
            if self._last_sent_at is None or self._reconnect or generation != self._last_generation:
                purpose = 'reconnect'
            elif requested:
                purpose = 'requested'
            elif facts != self._last_facts and now - self._last_sent_at >= self._gap:
                purpose = 'change'
            elif now - self._last_sent_at >= self._interval:
                purpose = 'periodic'
            else:
                return None
            report = {'protocol': STATE_REPORT, 'purpose': purpose, 'issued_at': now, **facts}
            try:
                validate_state_report(report)
            except ValueError:
                self._fail(now, 'member_state_report_invalid')
                return None
            try:
                ack = self._sink(report)
            except Exception as error:
                self._fail(now, _error_code(error))
                return None
            self._last_sent_at, self._last_facts, self._last_generation = now, facts, generation
            self._reconnect, self._retry_delay, self._retry_at = False, 0.0, 0.0
            self.last_purpose, self.last_error = purpose, None
            self._sent += 1
            mode = ack.get('owner_service_mode') if isinstance(ack, Mapping) else None
            self.owner_service_mode = mode if mode in SERVICE_MODES else None
        if self._on_mode is not None:
            try:
                self._on_mode(self.owner_service_mode)
            except Exception:
                pass
        return purpose


# -- facts from the app -----------------------------------------------------------------------------------------------

_WARMING = frozenset({'preparing', 'bootstrap_running', 'downloading_model', 'downloading_slice', 'model_ready',
                      'slice_ready', 'cleaning_up'})
_RUNNING = frozenset({'work_running', 'serving'})
_ACQUIRING_DONE = frozenset({'idle', 'closed', 'cancelled', 'failed', 'prepared'})


def _member(app: Any) -> Any | None:
    authority = getattr(app, 'authority', None)
    member = getattr(authority, 'member', None) if getattr(app, 'installation_role', None) == 'target' else None
    if member is None:
        member = getattr(getattr(app, 'local_contributor', None), 'member', None)
    return member


def _sharing(app: Any) -> dict[str, Any]:
    from .adaptive_owner import adaptive_status
    try:
        status = adaptive_status(app)
    except Exception:
        return {'state': 'paused', 'reason': 'adaptive_policy_invalid', 'adaptive_enabled': False}
    if status.get('state') == 'error':
        return {'state': 'paused', 'reason': 'adaptive_policy_invalid', 'adaptive_enabled': False}
    if not status.get('enabled'):
        return {'state': 'normal', 'reason': 'none', 'adaptive_enabled': False}
    mode = status.get('mode')
    state = 'reduced' if mode == 'reduced' else 'paused' if mode in {'drain', 'withdraw'} or status.get('stopped') else 'normal'
    reason = status.get('reason') if status.get('reason') in REASONS else 'none'
    if state == 'paused' and reason == 'none' and status.get('stopped'):
        reason = 'owner_stop'
    return {'state': state, 'reason': reason, 'adaptive_enabled': bool(status.get('adaptive_sharing'))}


def _worker(app: Any) -> dict[str, str]:
    agent = getattr(app, 'demo_target', None)
    state = getattr(agent, 'state', None)
    if state in _RUNNING:
        return {'state': 'running'}
    if state in _WARMING:
        return {'state': 'warming'}
    try:
        from .sharing_state import sharing_state
        shared = sharing_state(app)['state']
    except Exception:
        shared = 'idle'
    return {'state': 'running' if shared == 'sharing' else 'warming' if shared == 'rejoining' else 'idle'}


def _model(app: Any) -> dict[str, str] | None:
    acquisition = getattr(app, 'acquisition', None)
    if acquisition is None:
        return None
    try:
        state = acquisition.status().get('state')
        if state == 'prepared':
            identity = acquisition.prepared_source().model
            model_id, commit = identity.model_id, identity.resolved_commit
            verification = 'verified'
        elif state in {'idle', 'closed', 'cancelled'}:
            return None
        else:
            from .run_model import app_run_model
            identity = app_run_model(app.root)[2]
            model_id, commit = identity.model_id, identity.resolved_commit
            verification = 'unverified' if state == 'failed' else 'downloading'
        agent = getattr(app, 'demo_target', None)
        if getattr(agent, 'state', None) == 'error' and getattr(agent, 'detail', None) == 'demo_model_mismatch':
            verification = 'mismatch'
        return {'model_id': str(model_id)[:128], 'resolved_commit': str(commit)[:128], 'verification': verification}
    except Exception:
        return None


def _caps(app: Any) -> dict[str, int] | None:
    try:
        from .demo_target import StandingConsentGate
        caps = StandingConsentGate().member_caps(app)
        return {'storage_bytes': int(caps['storage_bytes']), 'max_memory_bytes': int(caps['max_memory_bytes'])}
    except Exception:
        return None


def _applied_service_mode(app: Any) -> str | None:
    """The mode this Mac applies; ``None`` until ``lane/rel2-modes`` (mycelium_router.service_modes) is merged."""
    try:
        from mycelium_router import service_modes as sm
        mode = sm.read_service_mode(app.root)
    except Exception:
        return None
    return mode if mode in SERVICE_MODES else None


def collect_facts(app: Any) -> dict[str, Any]:
    """The report body for this Mac; ``generation`` is ``None`` while it is not a member."""
    member = _member(app)
    return {'generation': None if member is None else int(member.generation), 'sharing': _sharing(app),
            'worker': _worker(app), 'model': _model(app), 'caps': _caps(app),
            'service_mode': _applied_service_mode(app)}


def apply_owner_service_mode(app: Any, mode: str | None) -> bool:
    """Adopt the owner's service mode on this Mac; a no-op (``False``) until the modes code is on this branch."""
    if mode not in SERVICE_MODES:
        return False
    try:
        from mycelium_router import service_modes as sm
    except ImportError:
        return False
    sm.write_service_mode(app.root, mode)
    return True


def app_tick(app: Any) -> str | None:
    """Joined Mac: drive this app's reporter one step (called from the standing loop; never raises)."""
    try:
        if getattr(app, 'installation_role', None) != 'target' or getattr(app.authority, 'peer', None) is None:
            return None
        if getattr(app, '_left', False) or getattr(app, '_leave_fence', False):
            return None   # leaving or left (PV10): the owner has the signed withdrawal, or sees silence; no more reports
        reporter = getattr(app, 'member_state_reporter', None)
        if reporter is None:
            client = MemberStateClient(app.authority.peer)
            reporter = app.member_state_reporter = MemberStateReporter(
                facts=lambda: collect_facts(app), sink=client.report,
                on_owner_service_mode=lambda mode: apply_owner_service_mode(app, mode))
        agent = getattr(app, 'demo_target', None)
        announcement = getattr(agent, 'announcement', None) or {}
        return reporter.tick(requested=announcement.get('member_state_requested') is True)
    except Exception:
        return None


def reporter_status(app: Any) -> dict[str, Any] | None:
    reporter = getattr(app, 'member_state_reporter', None)
    return None if reporter is None else reporter.status()


def owner_member_view(app: Any, member: Mapping[str, Any]) -> dict[str, Any]:
    """``/api/status`` ``authority.members[].member_state``: what the owner knows of one member's A16 state.

    The owner's own contributor is read from this process; every other member only from its signed reports (or
    ``unknown``) - never assumed.  Never raises.
    """
    node, generation = member.get('node_id'), member.get('generation')
    try:
        local = getattr(app, 'local_contributor', None)
        local_member = getattr(local, 'member', None)
        if local_member is not None and getattr(local_member, 'node_id', None) == node:
            facts = collect_facts(app)
            report = validate_state_report({'protocol': STATE_REPORT, 'purpose': 'periodic', 'issued_at': time.time(),
                                            **{**facts, 'generation': generation}})
            return reported_view(report, generation, 0, None, source='local')
    except Exception:
        pass
    channel = getattr(getattr(app, 'run_exchange', None), 'member_states', None)
    try:
        if channel is not None:
            return channel.view(node, generation)
    except Exception:
        pass
    return unknown_view(generation, None, 'Unknown - no report received yet')
