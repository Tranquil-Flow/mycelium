# SPDX-License-Identifier: AGPL-3.0-only
"""Owner cancellation retires admitted successor transport subjects, not unrelated requests."""
import pytest

from dataclasses import replace

from mycelium_router.contracts import HopHeader, PathBuildState, PathCancellation, PathHop
from mycelium_router.wire import ProgressivePrefillContext, decode_frame, encode_progressive_prefill
from test_router_iroh_transport import _Hub, _transport, graph_fixture, request_fixture


def _register(transport, path_id, attempt=0, request_id=None):
    graph = graph_fixture()
    entry_node = graph.stages[0].placements[0].node_id
    graph = replace(graph, stages=tuple(replace(stage, placements=tuple(
        replace(placement, node_id='local-node') if placement.node_id == entry_node else placement
        for placement in stage.placements)) for stage in graph.stages))
    request = request_fixture()
    if request_id is not None:
        request = replace(request, request_id=request_id)
    first = graph.stages[0].placements[0]
    build = PathBuildState(request=request, graph=graph, path_id=path_id, path_attempt=attempt,
        ordered_hops=(PathHop(graph.stages[0].stage_id, first.placement_id, 'reservation-'+path_id,
                             30.0, graph.deployment_epoch),))
    header = HopHeader(request_id=request.request_id, path_id=path_id, path_attempt=attempt,
        phase='PREFILL', token_index=-1, hop_index=0, source_placement_id='',
        destination_placement_id=first.placement_id, topology_version=graph.topology_version,
        idempotency_key=f'{request.request_id}:{path_id}:{attempt}:PREFILL:-1:0')
    frame = encode_progressive_prefill(header, ProgressivePrefillContext(
        graph=graph, request=request, build=build, payload=b'activation'))
    transport._dispatch(decode_frame(frame), source_node_id='node-b')
    return PathCancellation(request.request_id, path_id, attempt, graph.topology_version)


class _Router:
    def __init__(self):
        self.paths = set()
        self.cancelled = []

    def receive_progressive_prefill(self, header, context, **kwargs):
        self.paths.add(header.path_id)

    def apply_controlled_path_cancellation(self, cancellation):
        self.cancelled.append(cancellation)
        self.paths.discard(cancellation.path_id)
        return True


def test_owner_cleanup_retires_successor_without_touching_other_request():
    transport = _transport(_Hub())
    router = _Router()
    transport.bind_router(router)
    transport.start()
    try:
        original = _register(transport, 'old-path')
        successor = _register(transport, 'successor-path', 1)
        other = _register(transport, 'unrelated-path', request_id='unrelated-request')
        assert transport.apply_controlled_path_cancellation(original, entry_cancelled=False)
        assert router.paths == {other.path_id}
        assert {(c.path_id, c.path_attempt) for c in router.cancelled} == {
            (original.path_id, 0), (successor.path_id, 1)}
        assert transport.cancellation_cleanup_complete(original.request_id, original.path_id, 0)
        assert other.path_id in transport._path_graphs
        # A queued recovery frame cannot resurrect a successor after owner fencing.
        _register(transport, 'late-successor', 2)
        assert 'late-successor' not in router.paths
        assert 'late-successor' not in transport._path_graphs
    finally:
        transport.close()


def test_retiring_old_path_preserves_the_live_successor_entry():
    transport = _transport(_Hub())
    transport.bind_router(_Router())
    old = _register(transport, 'old-path')
    successor = _register(transport, 'successor-path', 1)
    transport._forget_path(old.request_id, old.path_id)
    assert old.request_id in transport._entry_nodes
    assert successor.path_id in transport._path_graphs
    transport._forget_path(successor.request_id, successor.path_id)
    assert old.request_id not in transport._entry_nodes


def test_cleanup_receipt_cannot_ignore_successor_forward_after_retirement():
    transport = _transport(_Hub())
    router = _Router()
    transport.bind_router(router)
    transport.start()
    try:
        old = _register(transport, 'old-path')
        successor = _register(transport, 'successor-path', 1)
        assert transport.apply_controlled_path_cancellation(old, entry_cancelled=False)
        # An already executing forward still owns its scope until its finally.
        scope = (old.request_id, successor.path_id, successor.path_attempt)
        with transport._state_lock:
            transport._forward_scopes[scope] = 1
        assert not transport.cancellation_cleanup_complete(old.request_id, old.path_id, 0)
        with transport._state_lock:
            transport._forward_scopes.pop(scope)
        assert transport.cancellation_cleanup_complete(old.request_id, old.path_id, 0)
    finally:
        transport.close()


def test_exact_terminal_receipt_is_not_blocked_by_retired_sibling_path():
    transport = _transport(_Hub())
    transport.bind_router(_Router())
    old = _register(transport, 'old-path')
    successor = _register(transport, 'successor-path', 1)
    # A failed peer can leave old-path cancellation delivery pending while the
    # all-local successor finishes. Exact legacy cleanup is not an owner-wide
    # receipt; it must neither hide old-path state nor claim the successor busy.
    transport._forget_path(successor.request_id, successor.path_id)
    assert not transport.cancellation_cleanup_complete(old.request_id, old.path_id, 0)
    assert transport.cancellation_cleanup_complete(successor.request_id, successor.path_id, 1)


def test_failed_successor_release_remains_a_blocker_and_is_retried():
    class FailingRouter(_Router):
        fail = True

        def apply_controlled_path_cancellation(self, cancellation):
            if cancellation.path_id == 'successor-path' and self.fail:
                raise RuntimeError('successor resource release failed')
            return super().apply_controlled_path_cancellation(cancellation)

    transport = _transport(_Hub())
    router = FailingRouter()
    transport.bind_router(router)
    transport.start()
    try:
        old = _register(transport, 'old-path')
        _register(transport, 'successor-path', 1)
        with pytest.raises(RuntimeError, match='successor resource release failed'):
            transport.apply_controlled_path_cancellation(old, entry_cancelled=False)
        assert not transport.cancellation_cleanup_complete(old.request_id, old.path_id, 0)
        router.fail = False
        assert transport.apply_controlled_path_cancellation(old, entry_cancelled=False)
        assert router.paths == set()
        assert transport.cancellation_cleanup_complete(old.request_id, old.path_id, 0)
    finally:
        transport.close()


def test_a7_successor_completion_retires_remote_relay_records(tmp_path):
    from tests.a7_acceptance.test_entry_cutover import _Harness
    harness = _Harness(tmp_path)
    harness.run('request:terminal-successor-cleanup', kill_after=3)
    record = harness.entry.entry.get_request('request:terminal-successor-cleanup')
    assert record.status == 'COMPLETED'
    assert record.kv_successor_cutover['outcome'] == 'fenced_kv_successor'
    assert all(record.manifest.path_id not in router.relay._paths
               for router in harness.routers.values())
