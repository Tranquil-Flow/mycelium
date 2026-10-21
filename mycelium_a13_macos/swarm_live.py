# SPDX-License-Identifier: AGPL-3.0-only
"""What the swarm entry may say about its live route: real documents in, a bounded public projection out.

Sources (all owned by the owner app, read-only, never forwarded as-is): the owner-run status, the A16
``adaptive_owner.adaptive_status`` of the entry Mac, the registry's ``/api/v1/recovery/status`` runtime document
(``mycelium_a6_recovery``) and ``/api/v1/observatory/snapshot`` transport observations (``routeExplorer/contracts``).
Every section is ``available: true`` with real numbers, or ``available: false`` with a closed reason.  A source that is
missing, off, unreachable or malformed is never turned into a zero or a green light.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable

RECOVERY_PATH = '/api/v1/recovery/status'
OBSERVATORY_PATH = '/api/v1/observatory/snapshot'
CACHE_SECONDS = 3.0
WITHDRAWN_STATES = frozenset({'withdrawn', 'left', 'revoked', 'removed', 'stopped'})
SESSIONS_POLICY = 'one prompt at a time per swarm; others get "busy"'
_REGISTRY = re.compile(r'http://127\.0\.0\.1:[0-9]{1,5}\Z')
_UNSAFE = re.compile(r'[^A-Za-z0-9 ._:()/+,-]')
_RECONCILED = frozenset({'resumed', 'aborted', 'already_terminal'})
_TRANSPORTS = ('direct', 'relay', 'unknown')


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return _UNSAFE.sub('', value)[:limit]


def _unavailable(reason: str) -> dict[str, Any]:
    return {'available': False, 'reason': reason}


def _route(run: Any, sharing_state: Any = None) -> dict[str, Any]:
    if not isinstance(run, dict):
        return {'available': True, 'state': 'offline', 'step': None, 'route_ready': False, 'failed_step': None,
                'error_code': None, 'run': None, 'started_at': None}
    state = run.get('state')
    if sharing_state in ('paused', 'rejoining'):  # never "serving" while this Mac is paused or reconnecting
        shown = sharing_state
    elif state == 'serving' and run.get('route_ready') is True:
        shown = 'serving'
    elif state == 'running':
        shown = 'warming'
    elif state == 'failed':
        shown = 'failed'
    else:
        shown = 'offline'
    started = run.get('started_at')
    return {'available': True, 'state': shown, 'step': _text(run.get('step'), 32),
            'route_ready': run.get('route_ready') is True and shown == 'serving', 'failed_step': _text(run.get('failed_step'), 32),
            'error_code': _text(run.get('error_code'), 64), 'run': _text(run.get('run_id'), 11),
            'started_at': started if type(started) is int else None}


def _helpers(run: Any) -> dict[str, Any]:
    if not isinstance(run, dict):
        return _unavailable('no_run')
    participants = run.get('participants')
    if not isinstance(participants, list):
        return _unavailable('participants_missing')
    rows = []
    for item in participants[:32]:
        if not isinstance(item, dict):
            continue
        state = _text(item.get('state'), 48)
        layers = item.get('layers')
        rows.append({'label': _text(item.get('label'), 48) or 'Mac', 'node': (_text(item.get('node_id'), 12) or ''),
                     'layers': [n for n in layers if type(n) is int][:2] if isinstance(layers, list) else None,
                     'state': state or 'serving', 'detail': _text(item.get('detail'), 120),
                     'withdrawn': (state or '') in WITHDRAWN_STATES})
    return {'available': True, 'count': len(rows), 'withdrawn': sum(1 for row in rows if row['withdrawn']), 'rows': rows}


def _host(adaptive: Any) -> dict[str, Any]:
    if not isinstance(adaptive, dict):
        return _unavailable('adaptive_source_missing')
    if adaptive.get('state') == 'error':
        return _unavailable('adaptive_policy_error')
    if adaptive.get('enabled') is not True:
        return _unavailable('adaptive_off')
    scale = adaptive.get('capacity_scale')
    return {'available': True, 'state': _text(adaptive.get('state'), 24), 'mode': _text(adaptive.get('mode'), 24),
            'reason': _text(adaptive.get('reason'), 64),
            'capacity_scale': scale if type(scale) in (int, float) else None,
            'new_admissions_allowed': adaptive.get('new_admissions_allowed') is True,
            'withdraw_overdue': adaptive.get('withdraw_overdue') is True, 'stopped': adaptive.get('stopped') is True,
            'scope': 'entry_mac',
            # additive booleans: the manual "Pause sharing" and the automatic "Adaptive sharing" setting
            **{key: adaptive[key] is True for key in ('pause_sharing', 'adaptive_sharing') if key in adaptive}}


def _recovery(document: Any) -> dict[str, Any]:
    if document is None:
        return _unavailable('recovery_source_unreachable')
    invalid = _unavailable('recovery_document_invalid')
    if not isinstance(document, dict) or not isinstance(document.get('requests'), list) \
            or len(document['requests']) > 4096 or not isinstance(document.get('breaker'), dict) \
            or document['breaker'].get('state') not in ('closed', 'open'):
        return invalid
    counts = {'recovered': 0, 'aborted': 0, 'recovering': 0, 'already_terminal': 0}
    highest = 0
    for record in document['requests']:
        if not isinstance(record, dict) or type(record.get('recovery_attempt')) is not int \
                or (record.get('terminal') is not None and not isinstance(record['terminal'], str)) \
                or (record.get('reconciled') is not None and record['reconciled'] not in _RECONCILED):
            return invalid
        highest = max(highest, record['recovery_attempt'])
        if record['reconciled'] == 'resumed':
            counts['recovered'] += 1
        elif record['reconciled'] == 'aborted':
            counts['aborted'] += 1
        elif record['reconciled'] == 'already_terminal':
            counts['already_terminal'] += 1
        elif record['terminal'] is None and record['recovery_attempt'] > 0:
            counts['recovering'] += 1
    return {'available': True, 'requests': len(document['requests']), **counts, 'max_attempt': highest,
            'breaker': document['breaker']['state']}


def _transport(document: Any) -> dict[str, Any]:
    if document is None:
        return _unavailable('observatory_unreachable')
    counts = {kind: 0 for kind in _TRANSPORTS}
    stack, seen = [(document, 0)], 0
    while stack and seen < 20000:
        node, depth = stack.pop()
        seen += 1
        if isinstance(node, dict):
            if node.get('edge_kind') == 'transport_observation' and node.get('transport') in counts:
                counts[node['transport']] += 1
            if depth < 8:
                stack.extend((child, depth + 1) for child in node.values() if isinstance(child, (dict, list)))
        elif isinstance(node, list) and depth < 8:
            stack.extend((child, depth + 1) for child in node[:4096] if isinstance(child, (dict, list)))
    if not any(counts.values()):
        return _unavailable('no_transport_observation')
    return {'available': True, **counts}


def project(*, owner_run: Any, adaptive: Any, recovery: Any, observatory: Any, busy: bool,
            sharing: Any = None) -> dict[str, Any]:
    shared = sharing.get('state') if isinstance(sharing, dict) else None
    return {'route': _route(owner_run, shared if shared in ('paused', 'rejoining') else None), 'helpers': _helpers(owner_run), 'host': _host(adaptive),
            'recovery': _recovery(recovery), 'transport': _transport(observatory),
            'sessions': {'available': True, 'max_concurrent': 1, 'busy': bool(busy), 'policy': SESSIONS_POLICY}}


def _default_reader(origin: str) -> Callable[[str], Any]:
    from .chat import RegistryClient
    return RegistryClient(origin, timeout=3.0).read


def live_state(app: Any, *, adaptive: Callable[[], Any] | None = None, read_json: Callable[[str], Any] | None = None,
               reader_factory: Callable[[str], Callable[[str], Any]] = _default_reader,
               clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Project the owner app's current documents; every source failure degrades to an unavailable section."""
    try:
        run = app.owner_run.status()
    except Exception:  # noqa: BLE001
        run = None
    if adaptive is None:
        from .adaptive_owner import adaptive_status
        adaptive = lambda: adaptive_status(app)  # noqa: E731
    try:
        host = adaptive()
    except Exception:  # noqa: BLE001
        host = None
    origin = run.get('registry_origin') if isinstance(run, dict) else None
    cache = app.__dict__.setdefault('_swarm_live_cache', {}) if hasattr(app, '__dict__') else {}

    def read(path: str) -> Any:
        hit = cache.get(path)
        now = clock()
        if hit is not None and now - hit[0] < CACHE_SECONDS:
            return hit[1]
        value = None
        try:
            reader = read_json
            if reader is None and isinstance(origin, str) and _REGISTRY.fullmatch(origin):
                reader = reader_factory(origin)
            value = None if reader is None else reader(path)
        except Exception:  # noqa: BLE001
            value = None
        cache[path] = (now, value)
        return value

    try:
        busy = bool(app.chat.state().get('busy'))
    except Exception:  # noqa: BLE001
        busy = False
    try:
        from .sharing_state import sharing_state
        sharing = sharing_state(app)
    except Exception:  # noqa: BLE001
        sharing = None
    return project(owner_run=run, adaptive=host, recovery=read(RECOVERY_PATH), observatory=read(OBSERVATORY_PATH),
                   busy=busy, sharing=sharing)
