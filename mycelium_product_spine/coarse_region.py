# SPDX-License-Identifier: AGPL-3.0-only
"""Closed coarse-region vocabulary, evidence record, and read-only reduction.

A14b publishes reviewed coarse-region statements for the route-explorer
projection. Nothing here locates a peer: the vocabulary is a closed macro-region
set, a record is a reviewed claim by its authority, and the reduction is a pure
function that never derives a region from network identity, timing, or a
default. Rows without an accepted authority publish `unknown`. This module
creates no authority and holds no signing material.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

PROTOCOL = 'mycelium.coarse_region.v1'
EVIDENCE_PROTOCOL = 'mycelium.coarse_region_evidence.v1'
REGIONS = ('africa', 'americas_north', 'americas_south', 'asia', 'europe', 'oceania', 'polar', 'unknown')
UNKNOWN = 'unknown'
SOURCE_KINDS = ('owner_declared', 'reviewed_relay_metadata', 'latency_distance_class')
REGION_BLOCKERS = ('missing', 'expired', 'conflicting', 'unauthorized')
DISTANCE_CLASSES = ('near', 'mid', 'far', 'unknown')
REGION_SOURCES = ('unavailable', *SOURCE_KINDS)
DISPLAY_CELLS = {code: f'cell_{code}' for code in REGIONS if code != UNKNOWN}
ROW_FIELDS = {'node', 'region', 'region_source', 'region_blocker', 'distance_class'}
_ALIASES = {
    'africa': 'africa',
    'americas_north': 'americas_north',
    'north_america': 'americas_north',
    'central_america': 'americas_north',
    'caribbean': 'americas_north',
    'americas_south': 'americas_south',
    'south_america': 'americas_south',
    'asia': 'asia',
    'europe': 'europe',
    'oceania': 'oceania',
    'polar': 'polar',
    'arctic': 'polar',
    'antarctic': 'polar',
    'antarctica': 'polar',
    'unknown': 'unknown',
}
_UNCERTAINTY = {
    'owner_declared': 'declared_unverified',
    'reviewed_relay_metadata': 'reviewed',
    'latency_distance_class': 'model_estimate',
}
_SUBJECT_KINDS = ('member', 'relay')
# One binding only: a member record may bind a member node, a relay record may
# bind an endpoint node. A relay statement never locates a member.
_SUBJECT_KIND_FOR_NODE = {'member': 'member', 'endpoint': 'relay'}
_EVIDENCE_FIELDS = {
    'protocol', 'subject_ref', 'subject_kind', 'source_kind', 'region', 'distance_class',
    'vocabulary_version', 'model_version', 'uncertainty', 'issued_at_unix_ms',
    'expires_at_unix_ms', 'authority_digest', 'digest',
}
_HASH = re.compile(r'sha256:[0-9a-f]{64}\Z')
_MODEL_VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')


def digest(value):
    """The repository canonical digest rule, identical to `route_explorer.digest`."""
    return 'sha256:' + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def normalize_region(value):
    """Closed alias table, never a parser: anything unmapped or non-string stays `unknown`."""
    if not isinstance(value, str):
        return UNKNOWN
    return _ALIASES.get(value, UNKNOWN)


def _integer(value):
    return type(value) is int and 0 <= value <= 9007199254740991


def _hash(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def validate_coarse_region_evidence(record):
    """Validate and detach one closed reviewed `mycelium.coarse_region_evidence.v1` record."""
    def require(ok):
        if not ok:
            raise ValueError('coarse_region_evidence_invalid')
    require(isinstance(record, Mapping) and set(record) == _EVIDENCE_FIELDS)
    require(record['protocol'] == EVIDENCE_PROTOCOL and record['vocabulary_version'] == PROTOCOL)
    require(_hash(record['subject_ref']) and record['subject_kind'] in _SUBJECT_KINDS)
    require(record['source_kind'] in SOURCE_KINDS and record['region'] in REGIONS)
    require(record['uncertainty'] == _UNCERTAINTY[record['source_kind']])
    require(_hash(record['authority_digest']))
    require(_integer(record['issued_at_unix_ms']) and _integer(record['expires_at_unix_ms']))
    require(record['expires_at_unix_ms'] >= record['issued_at_unix_ms'])
    if record['source_kind'] == 'latency_distance_class':
        # A latency class is not geography: no code, an explicit class, and a model.
        require(record['region'] == UNKNOWN and record['distance_class'] in DISTANCE_CLASSES)
        require(isinstance(record['model_version'], str) and _MODEL_VERSION.fullmatch(record['model_version']) is not None)
    else:
        require(record['distance_class'] is None and record['model_version'] is None)
    require(record['digest'] == digest({key: value for key, value in record.items() if key != 'digest'}))
    return json.loads(json.dumps(record, sort_keys=True, separators=(',', ':'), allow_nan=False))


def compatibility_fixtures():
    """A synthetic, unverified unknown claim; never evidence of a peer's location."""
    record = dict(
        protocol=EVIDENCE_PROTOCOL,
        subject_ref='sha256:' + 'a' * 64,
        subject_kind='member',
        source_kind='owner_declared',
        region=UNKNOWN,
        distance_class=None,
        vocabulary_version=PROTOCOL,
        model_version=None,
        uncertainty='declared_unverified',
        issued_at_unix_ms=0,
        expires_at_unix_ms=0,
        authority_digest='sha256:' + 'b' * 64,
    )
    record['digest'] = digest(record)
    return {'coarse-region-evidence-v1.json': validate_coarse_region_evidence(record)}


def project_coarse_regions(nodes: Sequence[Mapping[str, Any]], evidence: Sequence[Mapping[str, Any]], *, now_unix_ms: int, authorized_authorities: Sequence[str]) -> list[dict[str, Any]]:
    """Pure, deterministic per-node reduction with the frozen precedence.

    One row per node, sorted by `node`; rows carry vocabulary codes only. A
    lapsed geographic statement is never replaced by a latency class, and an
    unknown peer is never dropped.
    """
    if not _integer(now_unix_ms):
        raise ValueError('coarse_region_input_invalid')
    accepted = set(authorized_authorities)
    records = list(evidence)
    rows = []
    for node in sorted(nodes, key=lambda item: item['id']):
        node_id = node['id']
        subject_kind = _SUBJECT_KIND_FOR_NODE.get(node['kind'])
        candidates = [record for record in records if record['subject_ref'] == node_id and record['subject_kind'] == subject_kind]
        authorized = [record for record in candidates if record['authority_digest'] in accepted]
        geographic = [record for record in authorized if record['source_kind'] != 'latency_distance_class']
        current_geographic = [record for record in geographic if now_unix_ms <= record['expires_at_unix_ms']]
        if not candidates:
            row = (UNKNOWN, 'unavailable', 'missing', None)
        elif not authorized:
            row = (UNKNOWN, 'unavailable', 'unauthorized', None)
        elif geographic and not current_geographic:
            row = (UNKNOWN, 'unavailable', 'expired', None)
        elif current_geographic:
            codes = {record['region'] for record in current_geographic}
            if len(codes) != 1:
                row = (UNKNOWN, 'unavailable', 'conflicting', None)
            else:
                source = 'owner_declared' if any(record['source_kind'] == 'owner_declared' for record in current_geographic) else 'reviewed_relay_metadata'
                row = (next(iter(codes)), source, None, None)
        else:
            current_latency = [
                record for record in authorized
                if record['source_kind'] == 'latency_distance_class' and now_unix_ms <= record['expires_at_unix_ms']
            ]
            if not current_latency:
                row = (UNKNOWN, 'unavailable', 'expired', None)
            else:
                classes = {record['distance_class'] for record in current_latency}
                if len(classes) != 1:
                    row = (UNKNOWN, 'unavailable', 'conflicting', None)
                else:
                    row = (UNKNOWN, 'latency_distance_class', None, next(iter(classes)))
        rows.append({'node': node_id, 'region': row[0], 'region_source': row[1], 'region_blocker': row[2], 'distance_class': row[3]})
    return rows
