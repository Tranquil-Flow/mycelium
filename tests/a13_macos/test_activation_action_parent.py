# SPDX-License-Identifier: AGPL-3.0-only
"""Production action dispatcher scheduling gate; not native qualification."""
import threading
from types import SimpleNamespace as NS
import pytest
from mycelium_a13_macos.app import DemoApp,DemoError


@pytest.mark.parametrize('case',['accepted','no-consent','changed-proposal','wrong-phase','target'])
def test_explicit_activation_action_is_owner_bound(case):
    gate=threading.Event();proposal={'fixture':'exact'}
    app=NS(guard=threading.RLock(),closed=threading.Event(),installation_role='owner',running=True,
        local_contributor=None,authority=None,_chain_activation_event=gate,
        _chain_compile_state={'state':'compiled','execution_proposal':proposal},
        _chain_workflow=NS(status=lambda:{'state':'starting' if case=='wrong-phase' else 'awaiting_activation'}))
    body={'execution_proposal':proposal,'activation_acknowledged':True}
    if case=='no-consent':body['activation_acknowledged']=False
    if case=='changed-proposal':body['execution_proposal']={}
    if case=='target':app.installation_role='target'
    if case=='accepted':
        result=DemoApp.action(app,'/api/serving/activate',body)
        assert result=={'state':'activation_requested','route_ready':False}
        assert gate.is_set()
    else:
        with pytest.raises(DemoError if case=='target' else ValueError):DemoApp.action(app,'/api/serving/activate',body)
        assert not gate.is_set()
