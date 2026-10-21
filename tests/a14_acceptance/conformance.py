# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic authority inputs only; production projectors/validators do the work."""
from pathlib import Path
import json
from mycelium_product_spine import ProductProjector
from mycelium_product_spine.projector import _unknown_internet_native

ROOT = Path(__file__).resolve().parents[2]


def inputs(now=1000000, member_count=2):
    route = json.loads((ROOT / 'contracts/compatibility-fixtures/live-route-status-v1.json').read_text())
    member_ids = sorted({s['node_id'] for s in route['stages']})
    if not 2 <= member_count <= 4095:
        raise ValueError('conformance_member_count_invalid')
    member_ids += [f'conformance-member-{i:04d}' for i in range(member_count-len(member_ids))]
    members = [dict(node_id=n, generation=1, incarnation='conformance', lease_expires_at=(now+600000)/1000,
                    last_liveness_at=now/1000, lifecycle_state='RUNNING', peer_class='mac_mlx_iroh',
                    runtime_capability=dict(runtime_backend='mlx', transport='iroh', activation_protocol='mycelium.router_wire.v1'),
                    activation_eligible=False, endpoint_id='RAW-ENDPOINT-CANARY', endpoint_addrs=['192.0.2.23']) for n in member_ids]
    internet = _unknown_internet_native(now)
    history = []
    for generation, transport in [(1, 'direct'), (2, 'relay')]:
        a = json.loads(json.dumps(internet['activation_observation']))
        a.update(observation_id=f'conformance-{generation}', connection_generation=generation,
                 path_class=transport, path_source='bound_live_connection', endpoint_pseudonym='sha256:'+'a'*64,
                 observed_at_unix_ms=now-2000+generation, evidence_lifetime_until_unix_ms=now+600000,
                 freshness='current', metrics=dict(rtt_ms=4, warm_rtt_ms=2, jitter_ms=1,
                 goodput_bytes_per_second=1000, loss_ratio=0, sample_count=2, measured_zero=True))
        history.append(a)
    internet.update(activation_history=history, activation_observation=history[-1])
    return members, route, internet


class ConformanceProjector(ProductProjector):
    """Only fixes the evidence class. No alternate projection/rendering engine."""
    def project(self, **kwargs):
        kwargs['source_mode'] = 'fixture'
        return super().project(**kwargs)


def conformance_snapshot(now=1000000):
    members, route, internet = inputs(now)
    return ConformanceProjector(pseudonym_salt=b'c'*32).project(members=members,
        route_status=route, qualification=None, internet_native=internet, now_unix_ms=now)
