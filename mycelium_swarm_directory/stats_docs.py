# SPDX-License-Identifier: AGPL-3.0-only
"""Optional, independently signed numeric telemetry; heartbeat v1 stays unchanged.

All durations are integer milliseconds, throughput is milli-tokens/second.
Unknown measurements are null with n=0, never fabricated zeroes. Future schemas
use a new protocol/endpoint; no extension bag bypasses closed validation.
"""
from . import docs
from .wire import WireError, canonical, exact_keys, ident, integer, key_bytes, verify_document

PROTOCOL = 'mycelium.swarm.stats.v1'
MAX_BYTES = 8192
MAX_STAGES = 32
KEYS = frozenset({'protocol', 'swarm_id', 'signer_key', 'at', 'values', 'sig'})
VALUE_KEYS = frozenset({'window_seconds', 'window_limit', 'observed_seconds', 'requests_24h',
    'coverage_seconds', 'serving_since', 'uptime_seconds', 'mode', 'ttft_ms',
    'decode_millitokens_s', 'request_ms', 'stages', 'hops', 'stage_window_seconds'})


def _int(value, low=0, high=86400000):
    return integer(value, low=low, high=high, code='stats_number_invalid')


def _fields(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise WireError('stats_fields_invalid')


def _metric(value, max_n):
    _fields(value, ('p50', 'p90', 'n'))
    _int(value['n'], high=max_n)
    if value['n'] == 0:
        if value['p50'] is not None or value['p90'] is not None:
            raise WireError('stats_sample_invalid')
    else:
        _int(value['p50']); _int(value['p90'])
        if value['p90'] < value['p50']:
            raise WireError('stats_percentile_invalid')


def check_values(value):
    _fields(value, VALUE_KEYS)
    _int(value['window_seconds'], 1, 3600)
    _int(value['window_limit'], 1, 50)
    _int(value['observed_seconds'], high=value['window_seconds'])
    _int(value['stage_window_seconds'], 1, 3600)
    _int(value['requests_24h'], high=1000000)
    _int(value['coverage_seconds'], high=86400)
    if value['mode'] not in ('fast', 'normal', 'unknown'):
        raise WireError('stats_mode_invalid')
    if value['serving_since'] is None:
        if value['uptime_seconds'] is not None:
            raise WireError('stats_uptime_invalid')
    else:
        _int(value['serving_since'], high=docs.TIME_MAX)
        _int(value['uptime_seconds'], high=docs.TIME_MAX)
    for name in ('ttft_ms', 'decode_millitokens_s', 'request_ms'):
        _metric(value[name], value['window_limit'])
    stages = value['stages']
    if not isinstance(stages, list) or len(stages) > MAX_STAGES:
        raise WireError('stats_stages_invalid')
    for index, stage in enumerate(stages):
        _fields(stage, ('index', 'layer_start', 'layer_end', 'compute_ms'))
        _int(stage['index'], index, index)
        _int(stage['layer_start'], high=10000)
        _int(stage['layer_end'], stage['layer_start'] + 1, 10000)
        if index and stage['layer_start'] != stages[index-1]['layer_end']:
            raise WireError('stats_layers_invalid')
        _metric(stage['compute_ms'], 512)
    hops = value['hops']
    if not isinstance(hops, list) or len(hops) > len(stages):
        raise WireError('stats_hops_invalid')
    seen = set()
    for hop in hops:
        _fields(hop, ('from_stage', 'to_stage', 'kind', 'rtt_ms', 'samples', 'age_seconds'))
        _int(hop['to_stage'], high=len(stages)-1)
        if len(stages) < 2:
            raise WireError('stats_hops_invalid')
        if hop['kind'] == 'activation_rtt':
            _int(hop['from_stage'], high=len(stages)-2)
            _int(hop['to_stage'], hop['from_stage']+1, hop['from_stage']+1)
        elif hop['kind'] == 'owner_control_rtt_probe':
            if hop['from_stage'] is not None:
                raise WireError('stats_hops_invalid')
        else:
            raise WireError('stats_hops_invalid')
        if hop['to_stage'] in seen:
            raise WireError('stats_hops_invalid')
        seen.add(hop['to_stage'])
        _int(hop['rtt_ms'])
        _int(hop['samples'], 1, 1000000000)
        _int(hop['age_seconds'], high=90)
    return value


def stats(signer, swarm_id, *, at, values):
    check_values(values)
    return docs.sign(signer, {'protocol': PROTOCOL, 'swarm_id': swarm_id,
                             'signer_key': signer.public_key, 'at': at, 'values': values})


def check(document, verifier):
    exact_keys(document, PROTOCOL, KEYS)
    if len(canonical(document)) > MAX_BYTES:
        raise WireError('stats_too_large', 413)
    ident(document['swarm_id'], docs.SWARM_ID, 'swarm_id_invalid')
    key_bytes(document['signer_key'])
    _int(document['at'], high=docs.TIME_MAX)
    check_values(document['values'])
    since = document['values']['serving_since']
    if since is not None and since > document['at']:
        raise WireError('stats_uptime_invalid')
    verify_document(document, document['signer_key'], verifier)
    return document
