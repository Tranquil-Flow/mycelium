# SPDX-License-Identifier: AGPL-3.0-only
"""Real app workflow/registry composition, injected route refusal, no native work.

Hold cleanup to prove startup failure is visible before teardown or the serving
wait budget. The refusal is explicit; cleanup completion is a separate fact.
"""
from types import SimpleNamespace
import threading

import pytest

from mycelium_a13_macos import serving, workflow as wfmod
from mycelium_a13_macos.demo_chain import AppDemoChain
from mycelium_a13_macos.owner_run import OwnerRunError
from mycelium_live import supervisor as sup
from physical_inference_qualification import ControllerError


def test_token_install_refusal_reaches_app_before_held_cleanup(tmp_path, monkeypatch):
    from tests.live.test_registry_unavailable_diagnostic import _deployment
    # Only metadata/admission fixtures; all external-effect entrypoints are
    # fenced before running the actual workflow/RegistryLaunch/registry chain.
    monkeypatch.setattr(serving, '_validate_execution_approval', lambda a, p: a)
    monkeypatch.setattr(serving, '_plan_digest', lambda p: 'digest')
    monkeypatch.setattr(serving, '_validate_operator_plan', lambda *a: None)
    monkeypatch.setattr(serving, '_canonical_host_ids', lambda p: ())
    monkeypatch.setattr(serving, '_bind_existing_seed', lambda *a: (None, ('seed', 'binding', 1)))
    monkeypatch.setattr(serving, '_validate_dynamic_port', lambda p: None)
    monkeypatch.setattr(serving, '_assert_loopback_port_free', lambda p: None)
    monkeypatch.setattr(wfmod, 'publish_controller_offers', lambda *a, **k: ({}, object()))
    cleanup_entered, cleanup_release = threading.Event(), threading.Event()
    installs, errors = [], []
    deployment = _deployment()
    class Route:
        execution_graph = SimpleNamespace()
        def open(self):
            return SimpleNamespace(deployment_id=deployment.deployment_id,
                model_id=deployment.model_id, resolved_commit=deployment.model_revision)
        def set_deployment_qualification(self, *a, **k): pass
        def set_stop_token_ids(self, *a): pass
        def qualify_token_path(self):
            installs.append('install_token_path')
            raise ControllerError('node_command_refused', remote_code='token_path_disabled')
        def close(self):
            cleanup_entered.set()
            assert cleanup_release.wait(5), 'test cleanup barrier not released'
    monkeypatch.setattr(sup, 'native_deployment_projection', lambda p: deployment)
    monkeypatch.setattr(sup.PhysicalLiveRoute, 'from_operator_plan', lambda *a, **k: Route())
    monkeypatch.setattr(sup, '_validate_route_identity', lambda *a: None)
    monkeypatch.setattr(sup, '_qualify_open_route', lambda *a, **k: object())
    monkeypatch.setattr(sup, '_deployment_from_plan', lambda p: tmp_path)
    monkeypatch.setattr(sup, 'prompt_codec_from_deployment', lambda p: SimpleNamespace(stop_token_ids=()))
    class EndFixture(RuntimeError): pass
    def no_server(*a, **k): raise EndFixture('fixture_no_network_or_registry')
    monkeypatch.setattr(sup, 'LiveDeploymentRegistry', no_server)
    monkeypatch.setattr(sup, 'create_server', no_server)
    launch = serving.RegistryLaunch(prepared=None, operator_plan=tmp_path/'inert-plan',
        private_state_root=tmp_path, registry_state=tmp_path, seed_state_root=tmp_path,
        product_state_root=tmp_path, operator_plan_digest='digest', physical_host_ids=(),
        seed_binding=('seed','binding',1), _runner=sup.run_registry_server)
    controller = SimpleNamespace(_run_plan={'run_id':'run','deployment_id':deployment.deployment_id},
                                 execute=lambda phase: None)
    wf = wfmod.ManagedServingWorkflow(authority=None, controller=controller, launch=launch)
    wf.bind_progress_identity(run_id='run', deployment_id=deployment.deployment_id, epoch=2)
    approval = SimpleNamespace(operator_plan_digest='digest', physical_host_ids=())
    def run():
        try: wf.run(approval=approval, port=59261)
        except Exception as exc: errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert cleanup_entered.wait(3), [type(e).__name__ for e in errors]
        assert installs == ['install_token_path']
        assert wf.status()['state'] == 'failed', 'refusal hidden behind route cleanup'
        assert wf.status()['error_code'] == 'token_path_disabled'
        app = SimpleNamespace(_chain_workflow=wf, _chain_epoch=2,
            _chain_compile_state={'state':'compiled','epoch':2},
            run_exchange=SimpleNamespace(reports=lambda: {}))
        ctx = SimpleNamespace(run_id='run', deployment_id=deployment.deployment_id,
            state={'participants':[], 'compile_state':{'epoch':2}}, result={},
            progress=lambda text: None, wait_until=lambda predicate, **kw: predicate())
        with pytest.raises(OwnerRunError, match='token_path_disabled'):
            AppDemoChain(app)._step_serving(ctx)
    finally:
        cleanup_release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert len(errors) == 1
    # A later cleanup/registry failure cannot overwrite the original verdict.
    assert wf.status()['state'] == 'failed'
    assert wf.status()['error_code'] == 'token_path_disabled'
