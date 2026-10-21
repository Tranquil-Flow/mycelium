# SPDX-License-Identifier: AGPL-3.0-only
"""Application acquisition and managed staging share a target-owned cache."""
from mycelium_a13_macos.app import DemoApp
from test_app import login, request
from test_app_authority import CONSENT


def test_target_role_switch_and_restart_use_the_managed_artifact_cache(tmp_path):
    root=tmp_path/'application'
    app=DemoApp(root,check_platform=False).start()
    try:
        previous=app.acquisition
        # The owner cache is per run model (model-source-<commit12> for any model
        # but the old 7B candidate); the demo default is now a Qwen3 (run_model.py).
        assert previous.state_root==root/('model-source-'+app.run_model_contract().resolved_commit[:12])
        retained=previous.state_root/'retained-owner-file'
        retained.write_text('preserve prior source state')
        assert request(app,'/api/join/request','POST',{'consent':CONSENT},login(app))[0]==200
        # Per run model inside the confined 'serving' root (see the switch test).
        expected=(app.authority.peer.state_root/'serving'
                  /('model-source-'+app.run_model_contract().resolved_commit[:12]))
        assert app.acquisition.state_root==expected
        assert app.acquisition is not previous
        assert previous._closed
        assert app.acquisition.status()['state']=='idle'
        assert retained.read_text()=='preserve prior source state'
        # A restart must choose the same cache, not the former owner cache.
        app.close()
        app=DemoApp(root,check_platform=False).start()
        assert app.acquisition.state_root==expected
        # Acquisition status is durable; reopening its lock is not new consent
        # to download. The existing closed status remains until start(consent).
        assert app.acquisition.status()['state']=='closed'
        assert app.acquisition._closed is False
    finally:
        app.close()


def test_target_switching_run_model_gets_its_own_cache_inside_serving(tmp_path):
    # Two-Mac run 7: a joined Mac adopting the owner's model reopened the same
    # 'serving' cache whose status was stamped with the previous model and
    # failed acquisition_state_corrupt.  Each run model keeps its own cache,
    # still inside the staging consumer's confined 'serving' root.
    root=tmp_path/'application'
    app=DemoApp(root,check_platform=False).start()
    try:
        session=login(app)
        assert request(app,'/api/join/request','POST',{'consent':CONSENT},session)[0]==200
        serving=app.authority.peer.state_root/'serving'
        before=app.acquisition
        assert request(app,'/api/run-model','POST',{'choice':'qwen3-0.6b'},session)[0]==200
        assert before._closed and app.acquisition is not before
        status=app.acquisition.status()
        assert status['state'] in ('idle','closed')
        assert status['model']['model_id']=='Qwen/Qwen3-0.6B'
        assert app.acquisition.state_root.parent==serving
        assert app.acquisition.state_root!=before.state_root
        # Switching back reopens the first model's cache without corruption.
        assert request(app,'/api/run-model','POST',{'choice':'qwen3-8b'},session)[0]==200
        assert app.acquisition.status()['model']['model_id']=='Qwen/Qwen3-8B'
    finally:
        app.close()
