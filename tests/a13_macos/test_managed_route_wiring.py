# SPDX-License-Identifier: AGPL-3.0-only
"""Production factory threading with inert admission seams; no model execution."""
import json
from types import SimpleNamespace
import pytest
import mycelium_live.route as routes
import mycelium_live.supervisor as supervisor
from test_control_pump import _world
from test_control_session import owner, session


@pytest.mark.parametrize('token_enabled',[False,True])
def test_plan_factory_reaches_controller_and_route_without_process_arguments(tmp_path, monkeypatch, token_enabled):
    monkeypatch.setenv('MYCELIUM_TOKEN_PATH','on' if token_enabled else 'off')
    captured = {}
    authority = SimpleNamespace(signer=object(), seed_node_id='seed', members={}, close=lambda: None)
    monkeypatch.setattr(routes, '_load_live_seed_authority', lambda **kw: authority)
    monkeypatch.setattr(routes, '_refresh_membership_snapshot', lambda snapshot, **kw: snapshot)
    class Controller:
        def __init__(self, **kw): captured['controller'] = kw
        def _validate_physical_distinctness(self): captured['distinctness_checked'] = True
        def _validate_membership(self): return {}
        def _validate_run_plan(self): return {'nodes': []}
    class Route(routes.PhysicalLiveRoute):
        def __init__(self, **kw): captured['route'] = kw
    monkeypatch.setattr(routes, 'QualificationController', Controller)
    path = tmp_path/'plan.json'
    path.write_text(json.dumps({'controller': {'mode':'physical', 'peers':[], 'source_root':str(tmp_path),
        'transfer_manifest':{}, 'membership_snapshot':{}, 'run_plan':{'nodes':[]}}}))
    factory = lambda **kw: None
    token_owner=SimpleNamespace(enabled=True)
    if token_enabled:factory.token_path_owner_factory=lambda:token_owner
    Route.from_operator_plan(path, seed_state_root=tmp_path/'seed', session_factory=factory)
    assert captured['route']['token_path_owner'] is (token_owner if token_enabled else None)
    assert captured['controller']['session_factory'] is factory
    assert captured['route']['session_factory'] is factory
    assert captured['distinctness_checked'] is True


def test_managed_node_command_never_reads_process_launch_fields():
    route = object.__new__(routes.PhysicalLiveRoute)
    route._peers = {'member': SimpleNamespace(process_transport='managed_https')}
    route._plans_by_node = {'member': {}}
    route._session_factory = lambda **kw: None
    assert route._node_command('member') == ()


def test_managed_node_without_provider_raises_explicit_controller_error():
    from physical_inference_qualification import ControllerError
    route = object.__new__(routes.PhysicalLiveRoute)
    route._peers = {'member': SimpleNamespace(process_transport='managed_https')}
    route._plans_by_node = {'member': {}}
    route._session_factory = routes._ConcurrentNodeSession
    with pytest.raises(ControllerError, match='managed_session_provider_required'):
        route._node_command('member')


def test_qualified_runtime_passes_provider_before_open(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, 'native_deployment_projection', lambda path: object())
    seen = []
    factory = lambda **kw: None
    def reject(path, **kw):
        seen.append(kw)
        raise RuntimeError('inert_factory_boundary')
    monkeypatch.setattr(supervisor.PhysicalLiveRoute, 'from_operator_plan', reject)
    with pytest.raises(RuntimeError, match='inert_factory_boundary'):
        supervisor._qualified_runtime(tmp_path/'plan.json', seed_state_root=tmp_path/'seed', session_factory=factory)
    assert seen[0]['session_factory'] is factory


def test_managed_session_exposes_no_fabricated_remote_exit_or_stderr(tmp_path):
    with _world(tmp_path/'world') as world:
        sender = owner(tmp_path/'owner', world)
        try:
            target = session(sender, world)
            assert target.returncode is None
            assert target.stderr == b''
            # Local owner closure isn't an authenticated native process exit.
            sender.close()
            assert target.returncode is None
        finally:
            sender.close()
