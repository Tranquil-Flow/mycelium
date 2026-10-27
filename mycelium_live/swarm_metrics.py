# SPDX-License-Identifier: AGPL-3.0-only
"""Numbers-only hop projection: real activation RTT, else verified idle control probes.

No probe is scheduled here. Reuse existing keepalives, never add traffic while
inference is busy. Cross-host compute timestamps are not network measurements.
"""
import math
import time
from mycelium_router.rtt_measurement import LOCAL_HOST_KEY
from mycelium_topology_evidence import validate_transport_path_observation


def project_hops(graph, snapshots, probe_table, *, now_unix_ms=None, health=None):
    now = int(time.time()*1000) if now_unix_ms is None else now_unix_ms
    stages = list(graph.stages)
    if len(stages) < 2 or len(stages) > 32:
        return []
    result = []
    measured_destinations = set()
    for index in range(len(stages)-1):
        # Replicated stages cannot be labeled as one uniquely measured Mac.
        if len(stages[index].placements) != 1 or len(stages[index+1].placements) != 1:
            continue
        src = stages[index].placements[0].node_id
        dst = stages[index+1].placements[0].node_id
        candidates = []
        for observer, remote in ((src,dst), (dst,src)):
            observations = []
            for source in (snapshots, health or {}):
                transport = source.get(observer, {}).get('details', {}).get('transport', {})
                observations.extend(transport.get('transport_path_observations', [])[:256])
            for raw in observations:
                try:
                    item = validate_transport_path_observation(raw, now_unix_ms=now, require_resolved=True)
                except (ValueError, TypeError):
                    continue
                if (item['local_node_id'] == observer and item['remote_node_id'] == remote
                        and 0 <= now-item['measured_at_unix_ms'] <= 90000
                        and item['warm_rtt_ms'] <= 86400000 and item['sample_count'] <= 1000000000):
                    candidates.append(item)
        if candidates:
            latest = max(candidates, key=lambda v:v['measured_at_unix_ms'])
            result.append({'from_stage':index,'to_stage':index+1,'kind':'activation_rtt',
                'rtt_ms':round(latest['warm_rtt_ms']), 'samples':latest['sample_count'],
                'age_seconds':max(0,(now-latest['measured_at_unix_ms'])//1000)})
            measured_destinations.add(index+1)
    if probe_table is not None:
        for index, stage in enumerate(stages):
            if index in measured_destinations or len(stage.placements) != 1:
                continue
            sample = probe_table.rtt_ms(LOCAL_HOST_KEY, stage.placements[0].node_id)
            if (sample.is_measured and sample.latest_ms is not None and sample.age_seconds is not None
                    and math.isfinite(sample.latest_ms) and 0 <= sample.latest_ms <= 86400000
                    and 0 <= sample.age_seconds <= 90):
                result.append({'from_stage':None,'to_stage':index,'kind':'owner_control_rtt_probe',
                    'rtt_ms':round(sample.latest_ms), 'samples':sample.sample_count,
                    'age_seconds':int(sample.age_seconds)})
    return result
