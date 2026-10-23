# SPDX-License-Identifier: AGPL-3.0-only
"""Signed startup refusal through real CPU children, Iroh and managed TLS.

Compute/load/host metadata are the existing desk fixtures. This characterizes
inherited fail-closed startup, not a physical launch or A6/A7 recovery proof.
No signature verifier or qualification/install result is replaced.
"""
import base64
from copy import deepcopy

import pytest

import token_startup_desk
from tests.a13_macos import test_token_two_process_join as joined
from tests.live.test_token_path_admission import world


class _StartupRefused(Exception):
    """End the composed fixture before its happy-path chat assertions."""


@pytest.mark.parametrize('refused_index', [0, 1])
def test_signed_startup_refusal_closes_route_without_grant(
        tmp_path, world, monkeypatch, refused_index):
    original = token_startup_desk.qualify_startup
    observed = {}
    target_refusals = []
    from mycelium_a13_macos.token_policy import TargetTokenPolicy
    before = TargetTokenPolicy.before

    def observed_before(self, command, payload):
        try:
            return before(self, command, payload)
        except Exception as error:
            if command == 'install_token_path':
                target_refusals.append(getattr(error, 'code', None))
            raise

    monkeypatch.setattr(TargetTokenPolicy, 'before', observed_before)

    def qualify_and_refuse(**kwargs):
        route, owner = kwargs['route'], kwargs['owner']
        sessions = kwargs['sessions']
        installs, successful, grants = [], [], []
        refused_node = sorted(sessions)[refused_index]
        for node, session in sessions.items():
            send = session.send

            def checked_send(*, command_id, command, payload, node=node, send=send):
                if command == 'install_token_path':
                    installs.append(node)
                    if node == refused_node:
                        payload = deepcopy(payload)
                        signature = payload['policy']['signature']
                        raw = bytearray(base64.b64decode(signature['signature'], validate=True))
                        assert len(raw) == 64  # Valid grammar; only the signed bytes are wrong.
                        raw[0] ^= 1
                        signature['signature'] = base64.b64encode(raw).decode('ascii')
                result = send(command_id=command_id, command=command, payload=payload)
                if command == 'install_token_path' and result.get('ok') is True:
                    successful.append(node)
                return result

            monkeypatch.setattr(session, 'send', checked_send)

        def no_grant(**kwargs):
            grants.append(kwargs['command_id'])
            raise AssertionError('startup must not create request authority')

        monkeypatch.setattr(owner, 'prepare', no_grant)
        with pytest.raises(Exception) as failure:
            original(**kwargs)
        error = failure.value
        code = getattr(error, 'remote_code', None) or getattr(error, 'code', None) or str(error)
        observed.update(code=code, installs=installs, successful=successful,
                        grants=grants, commands=list(kwargs['inference_commands']))
        managed = route._peers[refused_node].process_transport == 'managed_https'
        # The target rejects before dispatch and requires reconciliation. A local
        # native rejection instead retains its exact native code. Neither is ready.
        expected_code = ('managed_control_reconciliation_required' if managed
                         else 'token_path_policy_signature')
        assert code == expected_code, observed
        assert target_refusals == (['token_path_policy_signature'] if managed else [])
        assert installs == sorted(sessions)[:refused_index + 1], observed
        assert successful == sorted(sessions)[:refused_index], observed
        assert not grants and not owner._owners, observed
        assert owner._install_started and owner._policy_members is None
        assert not owner.current() and route._closed and not route._open
        # The exact legacy qualification challenge ran; no product request did.
        assert observed['commands'] == ['infer_start', 'infer_decode'], observed
        # A partial install is not permission to retry, even with a repaired wire.
        with pytest.raises(RuntimeError, match='token_path_owner_route_unavailable'):
            owner.install(stop_token_ids=())
        assert installs == sorted(sessions)[:refused_index + 1]
        raise _StartupRefused()

    monkeypatch.setattr(token_startup_desk, 'qualify_startup', qualify_and_refuse)
    with pytest.raises(_StartupRefused):
        joined.test_owner_native_two_process_route(
            tmp_path, world, monkeypatch, True, 'signed', 'chat', startup=True)
    assert observed['installs'] and not observed['grants']
