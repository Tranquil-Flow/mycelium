# SPDX-License-Identifier: AGPL-3.0-only
"""App lifecycle controls reach the real registry launch API; inert runner only."""
import socket
import threading
import pytest
from test_serving import seed, private_fixture_identity, _prepared, _operator_plan, _approval, _free_dynamic_port
from mycelium_a13_macos.serving import prepare_registry_launch, ServingPreparationError


def test_launch_forwards_app_stop_and_started_callback(tmp_path,seed):
    prepared=_prepared(tmp_path);calls=[]
    launch=prepare_registry_launch(prepared=prepared,operator_plan=_operator_plan(tmp_path,prepared,seed=seed),
        private_state_root=tmp_path/'runtime',seed_state_root=seed._seed_root,
        runner=lambda **kw:calls.append(kw) or 0)
    stop=threading.Event();seen=[]
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    assert launch.run(approval=_approval(launch),port=port,api_only=True,
        stop_event=stop,on_started=seen.append)==0
    assert calls[0]['stop_event'] is stop and calls[0]['on_started']==seen.append
    assert calls[0]['api_only'] is True


def test_invalid_lifecycle_hooks_never_reach_runner(tmp_path,seed):
    prepared=_prepared(tmp_path);calls=[]
    launch=prepare_registry_launch(prepared=prepared,operator_plan=_operator_plan(tmp_path,prepared,seed=seed),
        private_state_root=tmp_path/'runtime',seed_state_root=seed._seed_root,
        runner=lambda **kw:calls.append(kw) or 0)
    for hooks in ({'stop_event':object()},{'on_started':7}):
        with pytest.raises(ServingPreparationError,match='registry_lifecycle_invalid'):
            launch.run(approval=_approval(launch),port=_free_dynamic_port(),api_only=True,**hooks)
    assert calls==[]
