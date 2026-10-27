# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded public numbers, never request content or node identity.

TTFT starts at app admission; decode excludes the first token and includes owner
stream delivery overhead. Only completed app-chat requests contribute. Counts
reset with the app; coverage says how much of 24h was actually observed.
"""
from collections import deque
import math
import re
import threading
import time

from mycelium_swarm_directory.stats_docs import MAX_STAGES

REGISTRY_PATH = '/__mycelium/live-status'
_LOOPBACK = re.compile(r'http://127\.0\.0\.1:[1-9][0-9]{0,4}\Z')


def quantiles(values):
    values = sorted(values)
    n = len(values)
    return {'p50': values[math.ceil(n*.5)-1] if n else None,
            'p90': values[math.ceil(n*.9)-1] if n else None, 'n': n}


def milliseconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 86400:
        return None
    return round(value * 1000)


class PerformanceWindow:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.window_started = self.started
        self.context = None
        self._lock = threading.Lock()
        self.samples = deque(maxlen=50)
        # The chat admission cap is far below this; still bounded independently.
        self.completed = deque(maxlen=100000)

    def select_context(self, context):
        with self._lock:
            if context != self.context:
                self.samples.clear()
                self.context = context
                self.window_started = self.clock()

    def record(self, *, ttft_ms, decode_millitokens_s, request_ms, context=None):
        values = (ttft_ms, decode_millitokens_s, request_ms)
        if any(v is not None and (type(v) is not int or not 0 <= v <= 86400000) for v in values):
            return
        with self._lock:
            now = self.clock()
            if len(self.completed) == self.completed.maxlen:
                self.started = max(self.started, self.completed[0])
            self.completed.append(now)
            if context == self.context:
                self.samples.append((now, values))

    def snapshot(self):
        with self._lock:
            now = self.clock()
            while self.completed and self.completed[0] <= now - 86400:
                self.completed.popleft()
            while self.samples and self.samples[0][0] <= now - 3600:
                self.samples.popleft()
            elapsed = max(0, int(now-self.started))
            return {'window_seconds':3600, 'window_limit':50, 'observed_seconds':min(3600, max(0,int(now-self.window_started))),
                    'coverage_seconds':min(86400, elapsed), 'requests_24h':len(self.completed),
                    **{name: quantiles([values[i] for _,values in self.samples if values[i] is not None])
                       for i,name in enumerate(('ttft_ms','decode_millitokens_s','request_ms'))}}


def stage_projection(run, runtime, *, now):
    """A10 decode stage duration on ONE node clock; never cross-clock hop subtraction."""
    participants = run.get('participants') or []
    stages = []
    for participant in participants[:MAX_STAGES]:
        layers = participant.get('layers')
        if (not isinstance(layers, (list, tuple)) or len(layers) != 2
                or any(type(v) is not int for v in layers) or not 0 <= layers[0] < layers[1] <= 10000):
            return []
        stages.append({'index':len(stages), 'layer_start':layers[0], 'layer_end':layers[1],
                       'compute_ms':quantiles([])})
    if len(participants) > MAX_STAGES:
        return []  # no silently truncated topology
    stages.sort(key=lambda s:s['layer_start'])
    for i,stage in enumerate(stages):
        stage['index'] = i
        if i and stage['layer_start'] != stages[i-1]['layer_end']:
            return []
    durations = {i:{} for i in range(len(stages))}
    # A10 already has a bounded node-signed publication validated by the registry.
    for peer in runtime.get('peers', [])[:64]:
        publication = peer.get('a10_publication')
        if not isinstance(publication, dict):
            continue
        clock = publication.get('clock', {})
        wall, mono = clock.get('wall_anchor'), clock.get('monotonic_anchor')
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in (wall,mono)):
            continue
        for interval in publication.get('intervals', [])[-512:]:
            index = interval.get('stage_index')
            if (type(index) is not int or index not in durations or interval.get('phase') != 'decode'
                    or interval.get('hop_kind') != 'compute' or interval.get('runtime_batch_ref')):
                continue  # batched operations are not single-token compute measurements
            start, end = interval.get('started_at'), interval.get('finished_at')
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in (start,end)):
                continue
            duration = milliseconds(end-start)
            if duration is None or not 0 <= now - (wall + end - mono) < 3600:
                continue
            # Map stage index to the peer's actual layer placement, not participant order alone.
            if not any(p.get('start_layer') == stages[index]['layer_start']
                       and p.get('end_layer_exclusive') == stages[index]['layer_end']
                       for p in peer.get('placements', [])):
                continue
            ref = interval.get('interval_ref')
            if isinstance(ref, str) and len(ref) <= 200:
                durations[index][ref] = duration
    for i,stage in enumerate(stages):
        stage['compute_ms'] = quantiles(list(durations[i].values())[-512:])
    return stages


class AppStats:
    def __init__(self, *, clock=time.time, reader=None):
        self.clock, self.reader = clock, reader
        self.empty = PerformanceWindow()
        self.run_id = None
        self.serving_since = None

    def snapshot(self, app, status):
        now = int(self.clock())
        run = app.owner_run.status() if getattr(app, 'owner_run', None) else {}
        run = run or {}
        run_id = run.get('run_id')
        if status != 'serving' or run_id != self.run_id:
            self.serving_since = None
        self.run_id = run_id
        if status == 'serving' and self.serving_since is None:
            self.serving_since = now
        runtime = {}
        origin = run.get('registry_origin')
        if status == 'serving' and isinstance(origin, str) and _LOOPBACK.fullmatch(origin):
            try:
                if self.reader:
                    runtime = self.reader(origin, REGISTRY_PATH)
                else:
                    from .chat import RegistryClient
                    runtime = RegistryClient(origin, timeout=1.5).read(REGISTRY_PATH)
                if not isinstance(runtime, dict) or runtime.get('simulated') is not False:
                    runtime = {}
            except Exception:
                runtime = {}
        mode = 'unknown'
        if runtime.get('route_alive') is True and type(runtime.get('token_path_enabled')) is bool:
            mode = 'fast' if runtime['token_path_enabled'] else 'normal'
        window = getattr(getattr(app, 'chat', None), 'performance', self.empty)
        window.select_context(run_id)
        stages = stage_projection(run, runtime, now=now)
        # Runtime indices refer to the same ordered layer chain; discard owner-local
        # control probes rather than calling a local mailbox a network hop.
        participants = sorted(run.get('participants') or [], key=lambda p:(p.get('layers') or [0])[0])
        hops = [dict(h) for h in runtime.get('swarm_hops', [])[:MAX_STAGES]
                if stages and type(h.get('to_stage')) is int and 0 <= h['to_stage'] < len(stages)
                and not (h.get('kind') == 'owner_control_rtt_probe'
                         and participants[h['to_stage']].get('label') == 'This Mac (owner)')]
        return {**window.snapshot(), 'serving_since':self.serving_since,
                'uptime_seconds':None if self.serving_since is None else max(0, now-self.serving_since),
                'mode':mode, 'stages':stages,
                'hops':hops, 'stage_window_seconds':3600}
