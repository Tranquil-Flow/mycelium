# SPDX-License-Identifier: AGPL-3.0-only
"""Closed read-only route view of public product facts; never a route authority.

A8's activation summary identifies a remote endpoint, not a membership edge or
request phase. Keep that observation separate from selected logical links. In
particular, never join those namespaces by display name or borrow its transport
class for every selected link. Reviewed coarse-region records are reduced for
publication; no region or overlay authority exists.
"""
from __future__ import annotations

import hashlib
import json
import re

from .coarse_region import (
    DISTANCE_CLASSES,
    REGIONS,
    REGION_BLOCKERS,
    REGION_SOURCES,
    ROW_FIELDS,
    project_coarse_regions,
)

PROTOCOL = 'mycelium.route_explorer.v1'
MAX_NODES = 4096
MAX_SEGMENTS = 4096
DIMS = {
    'edge_kind': ('logical_execution', 'physical_membership', 'transport_observation'),
    'phase': ('prefill_forward', 'decode_forward', 'decode_closure', 'control_probe', 'unknown'),
    'path_status': ('proposed', 'selected', 'observed', 'failed', 'recovered', 'historical', 'unknown'),
    'role': ('primary', 'alternative', 'replica', 'recovery_predecessor', 'recovery_successor', 'draft', 'unknown'),
    'transport': ('direct', 'relay', 'unknown'),
    'freshness': ('current', 'stale', 'unknown'),
}
_HASH = re.compile(r'sha256:[0-9a-f]{64}\Z')
_FIELDS = {'protocol', 'generation', 'source_mode', 'source_digest', 'nodes', 'segments', 'overlays', 'digest'}
_OPTIONAL_FIELDS = _FIELDS | {'regions'}
_SEGMENT_FIELDS = {'id', 'source', 'destination', 'binding', 'connection_generation', 'issued_at', 'expires_at', 'metrics', 'authority_digest', 'provenance', 'history', *DIMS}
_METRICS = {'rtt_ms', 'warm_rtt_ms', 'jitter_ms', 'goodput_bytes_per_second', 'loss_ratio', 'sample_count', 'measured_zero'}
_BINDING = {'deployment', 'request', 'attempt', 'track', 'path_generation'}


def digest(value):
    return 'sha256:' + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _integer(v):
    return type(v) is int and 0 <= v <= 9007199254740991


def _hash(v):
    return isinstance(v, str) and _HASH.fullmatch(v) is not None


def _validate_regions(regions, nodes, ids):
    def require(ok):
        if not ok:
            raise ValueError('route_explorer_invalid')
    require(type(regions) is list and len(regions) <= MAX_NODES)
    require(len(regions) == len(nodes))
    seen = set()
    for row in regions:
        require(type(row) is dict and set(row) == ROW_FIELDS)
        require(_hash(row['node']) and row['node'] in ids and row['node'] not in seen)
        require(row['region'] in REGIONS and row['region_source'] in REGION_SOURCES)
        require(row['region_blocker'] in (None, *REGION_BLOCKERS))
        require(row['distance_class'] in (None, *DISTANCE_CLASSES))
        seen.add(row['node'])
    require([row['node'] for row in regions] == sorted(row['node'] for row in regions))
    by_node = {row['node']: row for row in regions}
    require(set(by_node) == ids)
    for node in nodes:
        row = by_node[node['id']]
        require(row['region'] == node['region'] and row['region_source'] == node['region_source'])


def validate_route_explorer(value):
    def require(ok):
        if not ok:
            raise ValueError('route_explorer_invalid')
    require(type(value) is dict and set(value) in (_FIELDS, _OPTIONAL_FIELDS))
    require(value['protocol'] == PROTOCOL and _integer(value['generation']))
    require(value['source_mode'] in ('live', 'fixture', 'replay', 'degraded'))
    require(value['overlays'] == 'unavailable' and _hash(value['source_digest']))
    nodes, segments = value['nodes'], value['segments']
    require(type(nodes) is list and len(nodes) <= MAX_NODES)
    require(type(segments) is list and len(segments) <= MAX_SEGMENTS)
    ids = set()
    for n in nodes:
        require(type(n) is dict and set(n) == {'id', 'kind', 'region', 'region_source'})
        require(_hash(n['id']) and n['id'] not in ids)
        require(n['kind'] in ('member', 'endpoint'))
        require(n['region'] in REGIONS and n['region_source'] in REGION_SOURCES)
        ids.add(n['id'])
    if 'regions' in value:
        _validate_regions(value['regions'], nodes, ids)
    segment_ids = set()
    for s in segments:
        require(type(s) is dict and set(s) == _SEGMENT_FIELDS)
        require(_hash(s['id']) and s['id'] not in segment_ids and _hash(s['authority_digest']))
        segment_ids.add(s['id'])
        require(all(s[k] in allowed for k, allowed in DIMS.items()))
        require(s['source'] is None or (_hash(s['source']) and s['source'] in ids))
        require(s['destination'] is None or (_hash(s['destination']) and s['destination'] in ids))
        require(s['provenance'] == value['source_mode'])
        require(type(s['history']) is bool)
        require(all(s[k] is None or _integer(s[k]) for k in ('connection_generation', 'issued_at', 'expires_at')))
        require(s['issued_at'] is None or s['expires_at'] is None or s['expires_at'] >= s['issued_at'])
        b = s['binding']
        require(type(b) is dict and set(b) == _BINDING)
        require(all(b[k] is None or _hash(b[k]) for k in ('deployment', 'request', 'attempt', 'track')))
        require(b['path_generation'] is None or _integer(b['path_generation']))
        m = s['metrics']
        require(type(m) is dict and set(m) == _METRICS)
        for k in _METRICS - {'measured_zero'}:
            require(m[k] is None or (type(m[k]) in (float, int) and 0 <= m[k] <= 9007199254740991))
        require(type(m['measured_zero']) is bool)
        require(m['sample_count'] is None or _integer(m['sample_count']))
        require(m['loss_ratio'] is None or m['loss_ratio'] <= 1)
        require(m['loss_ratio'] != 0 or (m['measured_zero'] and (m['sample_count'] or 0) > 0))
        if s['transport'] != 'unknown':
            require(s['edge_kind'] == 'transport_observation' and s['destination'] is not None)
            require(s['connection_generation'] is not None and s['issued_at'] is not None and s['expires_at'] is not None)
        else:
            require(all(m[k] is None for k in _METRICS - {'measured_zero'}) and not m['measured_zero'])
    require(nodes == sorted(nodes, key=lambda n: n['id']))
    require(segments == sorted(segments, key=lambda s: (s['issued_at'] or 0, s['id'])))
    require(value['digest'] == digest({k: v for k, v in value.items() if k != 'digest'}))
    return json.loads(json.dumps(value, allow_nan=False))


def project_route_explorer(snapshot, *, legacy_history=False):
    """Called only after the ordinary product validator reduces source authority.

    ``legacy_history`` reproduces the rule used before per-endpoint transition
    history (only the last observation was current). It exists solely so stored
    state can prove what an older build published before it is re-projected.
    """
    material = {k: v for k, v in snapshot.items() if k != 'route_explorer'}
    mode = snapshot['publication']['source_mode']
    evidence = snapshot.get('coarse_region_evidence')
    nodes, segments = {}, []
    def node(public_id, kind):
        ref = digest([kind, public_id])
        nodes[ref] = {'id': ref, 'kind': kind, 'region': 'unknown', 'region_source': 'unavailable'}
        return ref
    members = {e['entity_id']: node(e['entity_id'], 'member') for e in snapshot['entities'] if e['kind'] == 'device'}
    empty_metrics = {k: False if k == 'measured_zero' else None for k in _METRICS}
    def segment(authority, **values):
        row = dict(source=None, destination=None,
                   binding=dict(deployment=None, request=None, attempt=None, track=None, path_generation=None),
                   connection_generation=None, issued_at=None, expires_at=None,
                   metrics=dict(empty_metrics), authority_digest=digest(authority),
                   provenance=mode, history=False, edge_kind='logical_execution', phase='unknown',
                   path_status='unknown', role='unknown', transport='unknown', freshness='unknown')
        row.update(values)
        # Identity binds immutable evidence, NOT publication time or display freshness.
        row['id'] = digest([row['authority_digest'], row['edge_kind']])
        segments.append(row)
    for e in snapshot['entities']:
        if e['kind'] != 'directed_link':
            continue
        a, b = e['attributes'], e['binding']
        src, dst = members.get(a['src_device_id']), members.get(a['dst_device_id'])
        if src is None or dst is None:
            continue
        segment({'binding': b, 'attributes': a}, source=src, destination=dst,
                path_status='selected',
                binding=dict(deployment=None if b['deployment_id'] is None else digest(b['deployment_id']),
                             request=None, attempt=None, track=None, path_generation=b['route_generation']))
    history = snapshot['internet_native']['activation_history']
    # An observation is historical once a later observation exists for the same
    # endpoint: a direct/relay transition adds a segment and never rewrites the
    # earlier one, and an unrelated unknown projection supersedes nothing.
    latest = {}
    for i, a in enumerate(history):
        if a['endpoint_pseudonym'] is not None:
            latest[a['endpoint_pseudonym']] = i
    for i, a in enumerate(history):
        if a['endpoint_pseudonym'] is None:
            continue
        resolved = a['path_source'] == 'bound_live_connection' and a['path_class'] != 'unknown'
        immutable = {k: v for k, v in a.items() if k != 'freshness'}
        segment(immutable, destination=node(a['endpoint_pseudonym'], 'endpoint'),
                edge_kind='transport_observation', path_status='observed' if resolved else 'unknown',
                transport=a['path_class'] if resolved else 'unknown',
                freshness=a['freshness'],
                history=i != (len(history) - 1 if legacy_history else latest[a['endpoint_pseudonym']]),
                connection_generation=a['connection_generation'], issued_at=a['observed_at_unix_ms'],
                expires_at=a['evidence_lifetime_until_unix_ms'], metrics=a['metrics'] if resolved else dict(empty_metrics))
    # An identical evidence record is one immutable segment, never duplicate traffic.
    segments = list({s['id']: s for s in segments}.values())
    ordered = sorted(nodes.values(), key=lambda n: n['id'])
    if evidence:
        # The published evidence set supplies the accepted authority digests; the
        # `regions` key is present exactly when at least one reviewed record is
        # published, and a snapshot without evidence keeps the A14a projection.
        rows = project_coarse_regions(
            ordered, evidence,
            now_unix_ms=snapshot['publication']['published_at_unix_ms'],
            authorized_authorities=frozenset(record['authority_digest'] for record in evidence))
        by_node = {row['node']: row for row in rows}
        for item in ordered:
            row = by_node[item['id']]
            item['region'] = row['region']
            item['region_source'] = row['region_source']
    else:
        rows = None
    result = dict(protocol=PROTOCOL, generation=snapshot['publication']['generation'], source_mode=mode,
                  source_digest=digest(material), nodes=ordered,
                  segments=sorted(segments, key=lambda s: (s['issued_at'] or 0, s['id'])), overlays='unavailable')
    if rows is not None:
        result['regions'] = rows
    result['digest'] = digest(result)
    return validate_route_explorer(result)


def compatibility_fixtures():
    value = dict(protocol=PROTOCOL, generation=1, source_mode='fixture', source_digest=digest({}), nodes=[], segments=[], overlays='unavailable')
    value['digest'] = digest(value)
    return {'route-explorer-v1.json': validate_route_explorer(value)}
