# SPDX-License-Identifier: AGPL-3.0-only
"""Canonical no-argv factory over target-owned real native model-free runtime."""
import pytest
from test_control_pump import _world
from test_control_session import owner
from test_managed_runtime import runtime as _runtime
from test_managed_control import RUN_ID,DEPLOYMENT_ID


def factory(world,managed):
    from mycelium_a13_macos.session_factory import ManagedSessionFactory
    member=world['authority'].coordinator.member(world['member']['node_id'])
    return ManagedSessionFactory(owner=managed,run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,
        members=[member])


@pytest.mark.parametrize('requested_timeout',[3,930])
def test_factory_matches_canonical_session_protocol_without_remote_argv(tmp_path,requested_timeout):
    with _world(tmp_path/'world') as world:
        runtime=_runtime(tmp_path,world);runtime.start()
        managed=None
        try:
            managed=owner(tmp_path/'owner',world);sessions=factory(world,managed)
            session=sessions(argv=(),node_id=world['member']['node_id'],
                run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,timeout_seconds=requested_timeout)
            assert session.timeout_seconds==min(requested_timeout,240)
            assert session.returncode is None and session.stderr==b''
            result=session.send(command_id='factory-native-hello',command='hello',payload={})
            assert result['ok'] is True and result['node_id']==world['member']['node_id']
            assert result['route_ready'] is False
            assert result['result']['protocol']=='mycelium.physical_node_control.v1'
            session.close();assert session.returncode==0 and session.stderr==b''
        finally:
            runtime.close()
            if managed is not None:managed.close()


@pytest.mark.parametrize('change',['argv','node','run','deployment','stale'])
def test_factory_rejects_scope_mismatch_before_enqueuing(tmp_path,change):
    with _world(tmp_path/'world') as world:
        managed=owner(tmp_path/'owner',world);sessions=factory(world,managed)
        fields=dict(argv=(),node_id=world['member']['node_id'],run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,timeout_seconds=3)
        if change=='argv':fields['argv']=('arbitrary-executable',)
        elif change=='node':fields['node_id']='unbound-node'
        elif change=='run':fields['run_id']='other-run'
        elif change=='deployment':fields['deployment_id']='other-deployment'
        else:
            m=world['member']
            world['authority'].revoke(m['node_id'],m['generation'])
        try:
            from mycelium_a13_macos.session_factory import ManagedSessionFactoryError
            with pytest.raises(ManagedSessionFactoryError):sessions(**fields)
            assert managed._state['commands']=={}
        finally:managed.close()
