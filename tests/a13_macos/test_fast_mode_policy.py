# SPDX-License-Identifier: AGPL-3.0-only
"""Joined native workers follow signed owner policy, not a local switch."""
import pytest
from tests.live.test_token_path_admission import world, payload
from tests.live.test_token_path_participants import bind
from tests.live.test_token_path_provisioning import provisioned


@pytest.mark.parametrize('local_flag', ['off', 'on'])
def test_signed_owner_policy_controls_joined_worker(world, monkeypatch, local_flag):
    monkeypatch.setenv('MYCELIUM_TOKEN_PATH', local_flag)
    s = provisioned(world)
    s.n._token_route_trust = s.trust
    s.n._token_peers = lambda: s.peers
    result = s.n.dispatch(dict(protocol='mycelium.physical_node_control.v1', run_id='desk',
        deployment_id='deployment', command_id='install', command='install_token_path',
        payload={'policy': s.envelope}))
    assert result['observation']['event'] == 'token_path_installed'
    s.n._bind_request_control(bind(world))
    assert s.n._infer_start(payload(world))['observation']['details']['status'] == 'COMPLETED'
    assert world.prefill == 1 and world.rounds == 5
