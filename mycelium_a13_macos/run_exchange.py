# SPDX-License-Identifier: AGPL-3.0-only
"""Owner <-> member demo-run exchange on the existing signed managed-control plane.

The owner publishes one *announcement* (phase, run/deployment ids, the pinned
model, the member's bootstrap offer id, its slice plan and work limits); each
member reads it and posts back a *report* (its state, acquisition progress and,
when asked, its signed installation descriptor).  Both directions reuse the
managed-control envelope: the member signs every request with its membership
key (verified by the mailbox against the current member record) and the owner
signs every response with its control key (verified by the member against the
pinned owner key).  The relay forwards ``/managed-control/*`` unchanged, so
this works relay-only with outbound HTTPS from the member.

This replaces, for the coworking demo, the operator script that ferried
installation descriptors to the owner by hand/SSH.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import uuid
from typing import Any, Mapping

from .clock_offset import PeerClockOffsets
from .managed_control import (_BINDING_FIELDS, _REQUEST_COMMON_FIELDS, _RESPONSE_COMMON_FIELDS,
                              ManagedControlError, _binding_from_member)


class DemoExchangeError(ManagedControlError, ValueError):
    """A closed demo-exchange refusal (also a managed-control error code)."""


def _require(condition: object, code: str) -> None:
    if not condition:
        raise DemoExchangeError(code)

READ = 'mycelium.a13_demo_run_read.v1'
REPORT = 'mycelium.a13_demo_run_report.v1'
RESPONSE = 'mycelium.a13_demo_run_state.v1'
ANNOUNCEMENT = 'mycelium.a13_demo_run_announcement.v1'
MEMBER_REPORT = 'mycelium.a13_demo_member_report.v1'
PATH_READ = '/managed-control/demo-run/read'
PATH_REPORT = '/managed-control/demo-run/report'
DISCOVERY = 'demo-run-discovery'
PHASES = frozenset({'idle', 'collect', 'observe', 'compile', 'work', 'serving', 'stopped'})
MEMBER_STATES = frozenset({
    'waiting_for_consent', 'idle', 'joined', 'downloading_model', 'model_ready', 'model_mismatch',
    'reported', 'preparing', 'bootstrap_running', 'cleaning_up', 'downloading_slice',
    'slice_ready', 'work_running', 'serving', 'stopped', 'error'})
_REQ_FIELDS = frozenset(_REQUEST_COMMON_FIELDS | _BINDING_FIELDS)
_REPORT_FIELDS = frozenset(_REQ_FIELDS | {'report'})
_RESP_FIELDS = frozenset(_RESPONSE_COMMON_FIELDS | _BINDING_FIELDS | {'announcement'})
_REPORT_KEYS = frozenset({'protocol', 'run_id', 'deployment_id', 'state', 'detail', 'model',
                          'progress', 'descriptor', 'issued_at'})
#: Optional: the member's own measured round trip of its last exchange (corrects the owner's clock-offset estimate).
_OPTIONAL_REPORT_KEYS = frozenset({'clock_rtt_seconds'})
MAX_CLOCK_RTT_SECONDS = 60.0
_CODE = re.compile(r'[a-z][a-z0-9_]{0,95}\Z')
MAX_REPORT_BYTES = 32 * 1024
MAX_ANNOUNCEMENT_BYTES = 768 * 1024


def _optional_segment(value: Any) -> bool:
    return value is None or (isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', value) is not None)


def validate_report(report: Any) -> dict[str, Any]:
    """Closed member report: codes and counters only, plus an optional signed descriptor."""
    try:
        raw = json.dumps(report, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError):
        raise ValueError('demo_report_invalid') from None
    _require(isinstance(report, dict) and len(raw) <= MAX_REPORT_BYTES and _REPORT_KEYS <= set(report) <= _REPORT_KEYS | _OPTIONAL_REPORT_KEYS
             and report['protocol'] == MEMBER_REPORT, 'demo_report_invalid')
    _require(_optional_segment(report['run_id']) and _optional_segment(report['deployment_id'])
             and report['state'] in MEMBER_STATES
             and (report['detail'] is None or (isinstance(report['detail'], str) and _CODE.fullmatch(report['detail'])))
             and type(report['issued_at']) in (int, float) and math.isfinite(report['issued_at']),
             'demo_report_invalid')
    rtt = report.get('clock_rtt_seconds')
    _require('clock_rtt_seconds' not in report or (type(rtt) in (int, float) and math.isfinite(rtt)
                                                   and 0 <= rtt <= MAX_CLOCK_RTT_SECONDS), 'demo_report_invalid')
    model = report['model']
    _require(model is None or (isinstance(model, dict) and set(model) == {'model_id', 'resolved_commit'}
             and all(isinstance(model[k], str) and len(model[k]) <= 128 for k in model)), 'demo_report_invalid')
    progress = report['progress']
    _require(progress is None or (isinstance(progress, dict) and set(progress) == {'done_bytes', 'total_bytes'}
             and all(type(progress[k]) is int and 0 <= progress[k] <= 2**50 for k in progress)), 'demo_report_invalid')
    descriptor = report['descriptor']
    _require(descriptor is None or (isinstance(descriptor, dict) and len(json.dumps(descriptor)) <= 16384),
             'demo_report_invalid')
    return json.loads(raw)


_NODE_EXTRA = frozenset({'bootstrap_offer_id', 'node_slice_plan', 'work_limits', 'resources'})
_WORK_LIMIT_FIELDS = frozenset({'max_rss_bytes', 'max_disk_bytes', 'max_wall_seconds', 'max_commands'})


def _check_node_extra(extra: Mapping[str, Any]) -> None:
    from .model_resources import PUBLIC_RESOURCE_FIELDS
    if not set(extra) <= _NODE_EXTRA:
        raise ValueError('demo_announcement_invalid')
    limits = extra.get('work_limits')
    if limits is not None and (not isinstance(limits, Mapping) or set(limits) != _WORK_LIMIT_FIELDS
                               or any(type(v) is not int or v <= 0 for v in limits.values())):
        raise ValueError('demo_announcement_invalid')
    resources = extra.get('resources')
    if resources is not None and (not isinstance(resources, Mapping)
                                  or set(resources) != set(PUBLIC_RESOURCE_FIELDS)):
        raise ValueError('demo_announcement_invalid')


class RunExchange:
    """Owner side: mounted on the authority's ManagedProvisioningMailbox."""

    def __init__(self, mailbox: Any, *, clock=time.time) -> None:
        self.mailbox = mailbox
        self._clock = clock
        self._lock = threading.Lock()
        self._announcement: dict[str, Any] = {}
        # Report inbox lifetime is process-local, unlike the persistent seed
        # incarnation. Expose this non-authorizing hint inside signed replies.
        self._owner_incarnation = 'owner-process-' + uuid.uuid4().hex
        self._per_node: dict[str, dict[str, Any]] = {}
        self._reports: dict[str, dict[str, Any]] = {}
        self._issued: dict[str, float] = {}
        #: Each peer's clock offset from the timestamps it signs (V1.1 a); survives ``forget_reports``.
        self.clock_offsets = PeerClockOffsets(clock=clock)
        self._model_source = None
        self.announce(phase='idle')

    def install(self) -> 'RunExchange':
        routes = dict(getattr(self.mailbox, 'route_extensions', None) or {})
        routes[PATH_READ] = self._read
        routes[PATH_REPORT] = self._report
        self.mailbox.route_extensions = routes
        return self

    # -- owner API ------------------------------------------------------------
    def announce(self, *, phase: str, run_id: str | None = None, deployment_id: str | None = None,
                 model: Mapping[str, Any] | None = None, slice_mode: bool = False,
                 manifest_digest: str | None = None, work_limits: Mapping[str, int] | None = None,
                 per_node: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
        if phase not in PHASES:
            raise ValueError('demo_announcement_invalid')
        prepared = self.prepare_announcement(phase=phase, run_id=run_id,
            deployment_id=deployment_id, model=model, slice_mode=slice_mode,
            manifest_digest=manifest_digest, work_limits=work_limits, per_node=per_node)
        self.commit_announcement(prepared)
        return self.announcement()

    def prepare_announcement(self, *, phase: str, run_id: str | None = None,
            deployment_id: str | None = None, model: Mapping[str, Any] | None = None,
            slice_mode: bool = False, manifest_digest: str | None = None,
            work_limits: Mapping[str, int] | None = None,
            per_node: Mapping[str, Mapping[str, Any]] | None = None):
        """Validate and serialize outside a lifecycle lock; no transport on commit."""
        if phase not in PHASES:
            raise ValueError('demo_announcement_invalid')
        document = {'protocol': ANNOUNCEMENT, 'phase': phase, 'run_id': run_id,
                    'deployment_id': deployment_id, 'model': None if model is None else dict(model),
                    'slice_mode': bool(slice_mode), 'manifest_digest': manifest_digest,
                    'work_limits': None if work_limits is None else dict(work_limits),
                    'issued_at': self._clock(),
                    'owner_incarnation': self._owner_incarnation}
        nodes = {node: dict(extra) for node, extra in (per_node or {}).items()}
        for extra in nodes.values():
            _check_node_extra(extra)
        return (json.loads(json.dumps(document, allow_nan=False)),
                json.loads(json.dumps(nodes, allow_nan=False)))

    def commit_announcement(self, prepared, *, idle_refresh: bool = False) -> bool:
        """In-memory swap; an idle refresh is conditional at the swap itself.

        Caller holds app.guard then the current OwnerRun lock for idle refresh.
        No announcement()/provider, serialization, clock or transport here.
        """
        document, nodes = prepared
        with self._lock:
            if idle_refresh:
                if (document['phase'] != 'idle'
                        or self._announcement.get('phase') not in {'idle', 'stopped'}
                        or (self._announcement.get('phase') == 'idle'
                            and self._announcement.get('model') == document['model'])):
                    return False
            self._announcement = document
            self._per_node = nodes
            return True

    def set_model_source_provider(self, provider) -> None:
        """``provider() -> hint | None``: where members may read the pinned bytes.

        Evaluated per read, only while a run is announced; the hint rides inside
        this owner-signed announcement (peer_model_source.validate_hint).
        """
        if provider is not None and not callable(provider):
            raise ValueError('demo_announcement_invalid')
        self._model_source = provider

    def _model_source_hint(self, phase: str) -> dict[str, Any] | None:
        provider = self._model_source
        if provider is None or phase in {'idle', 'stopped'}:
            return None
        try:
            hint = provider()
            return None if hint is None else json.loads(json.dumps(hint, allow_nan=False))
        except Exception:
            return None

    def update_node(self, node_id: str, **extra: Any) -> None:
        _check_node_extra(extra)
        with self._lock:
            self._per_node.setdefault(node_id, {}).update(json.loads(json.dumps(extra)))

    def announcement(self, node_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            value = json.loads(json.dumps(self._announcement))
            extra = self._per_node.get(node_id, {}) if node_id is not None else {}
        value['bootstrap_offer_id'] = extra.get('bootstrap_offer_id')
        value['node_slice_plan'] = extra.get('node_slice_plan')
        # This member's share of the per-split resource proposal: its own worker
        # limits override the run-wide ones, and its share facts let it refuse
        # by name when its standing caps cannot hold it.
        if extra.get('work_limits') is not None:
            value['work_limits'] = extra['work_limits']
        value['member_resources'] = extra.get('resources')
        value['model_source'] = self._model_source_hint(value['phase'])
        return value

    def reports(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._reports))

    def submit_local(self, node_id: str, report: Mapping[str, Any]) -> None:
        """In-process report from the owner's own local contributor."""
        value = validate_report(dict(report))
        with self._lock:
            self._reports[node_id] = {**value, 'received_at': self._clock()}

    def forget_reports(self) -> None:
        with self._lock:
            self._reports.clear(); self._issued.clear()

    # -- mounted routes -------------------------------------------------------
    def _verify(self, envelope: Any, *, protocol: str, fields: frozenset) -> tuple[dict, dict]:
        statement, member = self.mailbox._verify_target(envelope, protocol=protocol, fields=fields)
        _require(statement['run_id'] == DISCOVERY and statement['deployment_id'] == DISCOVERY,
                 'demo_request_invalid')
        return statement, member

    def _answer(self, statement: Mapping[str, Any]) -> dict[str, Any]:
        announcement = self.announcement(statement['target_node_id'])
        _require(len(json.dumps(announcement)) <= MAX_ANNOUNCEMENT_BYTES, 'demo_announcement_too_large')
        return self.mailbox._response(RESPONSE, statement['request_id'], {
            **{key: statement[key] for key in _BINDING_FIELDS}, 'announcement': announcement})

    def _read(self, envelope: Any) -> dict[str, Any]:
        statement, _ = self._verify(envelope, protocol=READ, fields=_REQ_FIELDS)
        self.clock_offsets.observe(statement['target_node_id'], signed_at=statement['issued_at'])
        return self._answer(statement)

    def _report(self, envelope: Any) -> dict[str, Any]:
        statement, _ = self._verify(envelope, protocol=REPORT, fields=_REPORT_FIELDS)
        report = validate_report(statement['report'])
        node = statement['target_node_id']
        self.clock_offsets.observe(node, signed_at=statement['issued_at'],
                                   rtt_seconds=report.get('clock_rtt_seconds'))
        with self._lock:
            # Latest wins; a replayed older report never overwrites a newer one.
            if report['issued_at'] >= self._issued.get(node, float('-inf')):
                self._issued[node] = report['issued_at']
                self._reports[node] = {**report, 'received_at': self._clock()}
        return self._answer(statement)


class RunExchangeClient:
    """Member side: read the owner's announcement and post this member's report."""

    def __init__(self, peer: Any, *, _test_ca_certificate=None) -> None:
        self.peer = peer
        self._ca = _test_ca_certificate
        #: Wall-clock round trip of the last exchange, on this Mac's own clock: told to the owner in the next report.
        self.last_rtt_seconds: float | None = None

    def _call(self, path: str, protocol: str, extra: Mapping[str, Any]) -> dict[str, Any]:
        client = self.peer.staging_control_client(_test_ca_certificate=self._ca)
        member = self.peer.staging_control_snapshot()
        binding = _binding_from_member(member, DISCOVERY, DISCOVERY)
        now = client._clock()
        request_id = uuid.uuid4().hex
        statement = {**binding, 'protocol': protocol, 'swarm_id': client.swarm_id,
                     'request_id': request_id, 'sequence': 1, 'nonce': uuid.uuid4().hex,
                     'issued_at': now, 'expires_at': now + 30, **dict(extra)}
        response = client._request(path, client._signed_outbound(statement))
        self.last_rtt_seconds = max(0.0, client._clock() - now)
        result = client._verified_response(response, protocol=RESPONSE, fields=_RESP_FIELDS,
                                           request_id=request_id, target=member,
                                           run_id=DISCOVERY, deployment_id=DISCOVERY)
        announcement = result['announcement']
        _require(isinstance(announcement, dict) and announcement.get('protocol') == ANNOUNCEMENT
                 and announcement.get('phase') in PHASES
                 and _optional_segment(announcement.get('owner_incarnation')), 'demo_announcement_invalid')
        return announcement

    def read(self) -> dict[str, Any]:
        return self._call(PATH_READ, READ, {})

    def report(self, report: Mapping[str, Any]) -> dict[str, Any]:
        report = dict(report)
        if self.last_rtt_seconds is not None and 'clock_rtt_seconds' not in report \
                and self.last_rtt_seconds <= MAX_CLOCK_RTT_SECONDS:
            report['clock_rtt_seconds'] = round(self.last_rtt_seconds, 4)
        return self._call(PATH_REPORT, REPORT, {'report': validate_report(report)})
