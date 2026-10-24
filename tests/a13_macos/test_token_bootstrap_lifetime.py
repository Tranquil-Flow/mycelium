# SPDX-License-Identifier: AGPL-3.0-only
"""Control-only bootstrap must not consume the later model worker's budget.

Construct the real runtime/pin channel without starting a native worker. Signed
membership comes from the isolated loopback fixture; only monotonic process time
is advanced so no wall-clock sleep or model is needed. Physical startup remains
separate evidence.
"""
import json
import time
from types import SimpleNamespace

import pytest

from mycelium_a13_macos import token_policy
from mycelium_a13_macos.worker import WorkerLimits
from mycelium_node.token_provisioning import load_host_trust, sign_route_policy
from mycelium_router.token_path_authority import TokenPathAuthorityError
from test_managed_runtime import runtime, _world, RUN_ID, DEPLOYMENT_ID


def _limits(seconds):
    return WorkerLimits(max_rss_bytes=2_000_000_000, max_disk_bytes=2_000_000,
                        max_wall_seconds=seconds, max_commands=16)


def _admit(target, world, now):
    """Use the actual private pin through the unchanged signed-policy consumer."""
    client = world['target_client']
    member = world['target'].managed_control_snapshot()
    local = token_policy.member_pin(client, member)
    graph = SimpleNamespace(deployment_id=DEPLOYMENT_ID,
        manifest_digest='sha256:'+'a'*64,
        stages=[SimpleNamespace(placements=[SimpleNamespace(node_id=member['node_id'])])])
    readers = [load_host_trust(target.worker.token_policy_trust_file,
        run_id=RUN_ID, deployment_id=DEPLOYMENT_ID, node_id=member['node_id'],
        current=target._membership_current, clock=lambda: now,
        unix_ms=lambda: int(time.time()*1000)) for _ in range(2)]
    envelope = sign_route_policy(signer=world['owner_signer'], run_id=RUN_ID,
        deployment_id=DEPLOYMENT_ID, model_digest=graph.manifest_digest,
        requester_id='owner', members={member['node_id']: local}, max_new_tokens=48,
        stop_token_ids=(), expires_at_unix_ms=int((time.time()+120)*1000))
    peers = lambda: {member['node_id']: {k: local[k] for k in ('node_id','endpoint_id','generation')}}
    return [trust.admit(envelope, graph=graph, peers=peers) for trust in readers]


def test_expired_control_bootstrap_does_not_poison_new_work_lifetime(tmp_path, monkeypatch):
    with _world(tmp_path/'world') as world:
        client = world['target_client']
        client.trusted_owner_endpoint_id = world['owner_signer'].endpoint_id
        current = world['target'].managed_control_snapshot
        origin = time.monotonic()
        bootstrap = runtime(tmp_path/'bootstrap', world, current_member=current,
                            limits=_limits(300), authorize_load=None)
        path = bootstrap.worker.token_policy_trust_file
        before = path.read_bytes() if path is not None else None
        root = bootstrap.root
        bootstrap.close()
        world['target'].renew()  # Accept a real signed renewal, never forge a host lease.
        work = None
        try:
            # Bootstrap is closed. This is a distinct, explicitly load-authorized
            # runtime, not an install refresh or a replacement of a live worker.
            with monkeypatch.context() as clock:
                clock.setattr(time, 'monotonic', lambda: origin+400)
                work = runtime(tmp_path/'work', world, state_root=root,
                    current_member=current, limits=_limits(1800), authorize_load=lambda intent: True)
            assert work.worker.token_policy_trust_file is not None
            admitted = _admit(work, world, origin+400)
            assert len(admitted) == 2  # Independent host/native readers of the same channel.
            for installed in admitted:
                assert installed.current()
                assert installed.deadline_monotonic_s > origin+400
            # A separate work lifetime must not rewrite or revive bootstrap state.
            if path is not None:
                assert path.read_bytes() == before
        finally:
            if work is not None:
                work.close()


def test_reopening_work_cannot_restart_its_original_process_budget(tmp_path, monkeypatch):
    with _world(tmp_path/'world') as world:
        client = world['target_client']
        client.trusted_owner_endpoint_id = world['owner_signer'].endpoint_id
        current = world['target'].managed_control_snapshot
        origin = time.monotonic()
        work = runtime(tmp_path/'first', world, current_member=current,
                       limits=_limits(60), authorize_load=lambda intent: True)
        root = work.root
        path = work.worker.token_policy_trust_file
        assert path is not None
        before = json.loads(path.read_bytes())
        work.close()
        reopened = None
        try:
            with monkeypatch.context() as clock:
                clock.setattr(time, 'monotonic', lambda: origin+400)
                reopened = runtime(tmp_path/'reopened', world, state_root=root,
                    current_member=current, limits=_limits(1800), authorize_load=lambda intent: True)
            assert reopened.worker.token_policy_trust_file == path
            assert json.loads(path.read_bytes())['process_deadline_monotonic_s'] == before['process_deadline_monotonic_s']
            with pytest.raises(TokenPathAuthorityError, match='token_path_policy_expired'):
                _admit(reopened, world, origin+400)
        finally:
            if reopened is not None:
                reopened.close()


def test_control_bootstrap_neither_creates_nor_refreshes_a_work_pin(tmp_path):
    from mycelium_a13_macos.worker import WorkerError
    with _world(tmp_path/'world') as world:
        world['target_client'].trusted_owner_endpoint_id = world['owner_signer'].endpoint_id
        current = world['target'].managed_control_snapshot
        bootstrap = runtime(tmp_path/'bootstrap', world, current_member=current, limits=_limits(300))
        root = bootstrap.root
        try:
            assert bootstrap.worker.token_policy_trust_file is None
            assert bootstrap.worker._token_policy is None
            assert not (root/'token-host.private.json').exists()
            assert '--token-policy-trust-file' not in bootstrap.worker._native_argv()
            with pytest.raises(WorkerError, match='model_execution_not_authorized'):
                bootstrap.worker._authorize_exact({'command': 'configure', 'payload': {}})
            assert bootstrap.worker.pid is None
        finally:
            bootstrap.close()
        work = runtime(tmp_path/'work', world, state_root=root, current_member=current,
                       limits=_limits(1800), authorize_load=lambda intent: False)
        path = work.worker.token_policy_trust_file
        assert path is not None
        before = path.read_bytes()
        work.close()
        world['target'].renew()
        again = runtime(tmp_path/'again', world, state_root=root, current_member=current, limits=_limits(300))
        try:
            assert again.worker.token_policy_trust_file is None
            assert '--token-policy-trust-file' not in again.worker._native_argv()
            assert path.read_bytes() == before  # Not even the lease may be refreshed by bootstrap.
            assert again.worker.pid is None
        finally:
            again.close()


def test_work_pin_does_not_bypass_a_denying_load_authorizer(tmp_path):
    from mycelium_a13_macos.worker import WorkerError
    with _world(tmp_path/'world') as world:
        world['target_client'].trusted_owner_endpoint_id = world['owner_signer'].endpoint_id
        calls = []
        def deny(intent):
            calls.append(intent)
            return False
        work = runtime(tmp_path/'work', world,
            current_member=world['target'].managed_control_snapshot,
            limits=_limits(1800), authorize_load=deny)
        try:
            assert work.worker.token_policy_trust_file is not None
            assert '--token-policy-trust-file' in work.worker._native_argv()
            assert calls == []  # Construction must not execute the authorizer.
            with pytest.raises(WorkerError, match='model_execution_not_authorized'):
                work.worker._authorize_exact({'command': 'configure', 'payload': {}})
            assert len(calls) == 1
            assert not work.worker.status()['model_load_authorized']
            assert work.worker.pid is None
        finally:
            work.close()
