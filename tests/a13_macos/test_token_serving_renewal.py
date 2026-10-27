# SPDX-License-Identifier: AGPL-3.0-only
"""Serving lease renewal: signed membership, independent target/native readers."""
import json
import time
import pytest

from mycelium_a13_macos import token_policy
from mycelium_node.token_provisioning import sign_route_policy
from mycelium_router.token_path_authority import TokenPathAuthorityError
from tests.a13_macos.test_token_pin_lease import lease_world, NOW
from tests.a13_macos.test_token_workflow_owner import configured, world


def test_serving_refreshes_only_lease_and_keeps_both_installed_readers(lease_world):
    w = lease_world
    envelope = sign_route_policy(signer=w.seed, run_id='cold', deployment_id='deployment',
        model_digest=w.graph.manifest_digest, requester_id='owner', members={w.session.node_id:w.pin},
        max_new_tokens=48, stop_token_ids=(), expires_at_unix_ms=int((NOW+1800)*1000))
    installed = [t.admit(envelope, graph=w.graph, peers=w.peers) for t in w.readers]
    registries = [i.participant_policy()._registry for i in installed]
    target = token_policy.TargetTokenPolicy.__new__(token_policy.TargetTokenPolicy)
    import threading
    target._lock = threading.RLock()
    target._refresh = lambda: token_policy.prepare_host_pin(**w.options)
    target.installed = installed[0]
    w.renew()
    w.wall[0] = NOW+400
    assert target.current()
    assert all(i.current() for i in installed), 'installed readers retained the original lease'
    pin = json.loads(w.path.read_bytes())
    assert pin == {**w.original, 'lease_expires_at_unix_ms': int(w.session.lease_expires_at*1000)}
    assert [i.participant_policy()._registry for i in installed] == registries
    # Fresh membership cannot move a process or signed-policy ceiling.
    w.shift[0] = 1801
    assert not installed[1].current()
    with pytest.raises(TokenPathAuthorityError, match='token_path_process_expired'):
        installed[1]._members()


def test_owner_renews_serving_lease_without_reinstall_or_new_registry(world, tmp_path, monkeypatch):
    wall, mono = [time.time()], [time.monotonic()]
    monkeypatch.setattr(time, 'time', lambda: wall[0])
    monkeypatch.setattr(time, 'monotonic', lambda: mono[0])
    owner, route, members, installs = configured(world, tmp_path, monkeypatch)
    owner.install(stop_token_ids=(99,))
    policy = installs[0][1]
    mono[0] += 400; wall[0] += 400
    for member in members.values(): member['lease_expires_at'] = wall[0]+300
    world.control['absolute_deadline_ms'] = int((mono[0]+120)*1000)
    attempt = owner.prepare(command_id='renewed', request=world.request, control=world.control,
        manifest=world.manifest, participating_node_ids={'entry','tail'}, stop_token_ids=(99,))
    assert len(installs) == 2
    assert policy['statement']['expires_at_unix_ms'] > int(wall[0]*1000)
    retained = tuple(owner._owners.values())[0].policy._registry
    attempt.close()
    # An old request is still replay, even after another authenticated renewal.
    # Remain inside the original 120 s request budget minus 30 s skew.
    mono[0] += 50; wall[0] += 50
    for member in members.values(): member['lease_expires_at'] = wall[0]+300
    with pytest.raises((ValueError, RuntimeError), match='replay'):
        owner.prepare(command_id='replay', request=world.request, control=world.control,
            manifest=world.manifest, participating_node_ids={'entry','tail'}, stop_token_ids=(99,))
    assert tuple(owner._owners.values())[0].policy._registry is retained
    mono[0] = owner.deadline
    with pytest.raises((ValueError, RuntimeError), match='token_path_process_expired'):
        owner.install(stop_token_ids=(99,))


def test_owner_expired_lease_has_specific_bounded_code(world, tmp_path, monkeypatch):
    owner, route, members, installs = configured(world, tmp_path, monkeypatch)
    owner.install(stop_token_ids=(99,))
    members['tail']['lease_expires_at'] = time.time()-1
    with pytest.raises((ValueError, RuntimeError), match='token_path_policy_expired'):
        owner.install(stop_token_ids=(99,))
