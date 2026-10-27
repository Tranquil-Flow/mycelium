# SPDX-License-Identifier: AGPL-3.0-only
"""Managed observation over real local TLS/native hello, not two-host proof."""
import pytest
from types import SimpleNamespace
from datetime import datetime,timezone
from mycelium_a13_macos.app import DemoApp
from test_app_authority import CONSENT
from test_worker import SAFE


def test_application_bootstrap_observes_native_hello_without_tensor_authority(native_sidecar_required, tmp_path,monkeypatch):
    app=DemoApp(tmp_path/'owner',check_platform=False).start()
    try:
        app.action('/api/create',{'consent':CONSENT});app.action('/api/local-contributor/start',{})
        app._policy_observer=lambda:dict(SAFE)
        source=app.acquisition.state_root/'fixture';source.mkdir()  # per-model owner cache (run_model.py)
        monkeypatch.setattr(app.acquisition,'prepared_source',lambda:SimpleNamespace(source_root=source,manifest_digest='sha256:'+'1'*64))
        run='observe-qwen-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        card=app.action('/api/installation/descriptor',{'run_id':run,'deployment_id':'observe-native','disclosure_acknowledged':True})
        result=app.action('/api/serving/observe/prepare',{'run_id':run,'deployment_id':'observe-native','descriptors':[card],'bootstrap_acknowledged':True})
        assert result['state']=='awaiting_bootstrap_workers' and result['route_ready'] is False
        offer=app.action('/api/staging/check',{})['offer']
        intent={'offer_id':offer['offer_id'],'archive_digest':offer['archive_digest']}
        app.action('/api/staging/accept',{**intent,'stage_acknowledged':True})
        started=app.action('/api/control/start',{**intent,'control_acknowledged':True})
        assert started['model_load_authorized'] is False
        observations=app.action('/api/serving/observe/collect',{'observe_acknowledged':True})
        assert observations['state']=='observed' and observations['route_ready'] is False
        assert len(observations['observations'])==1
        value=observations['observations'][0]
        assert value['host_id'] and value['boot_id']
        app.action('/api/control/stop',{})
        cleanup=app.action('/api/staging/cleanup/check',{})['offer']
        assert cleanup['offer_id']==offer['offer_id']
        assert app.action('/api/staging/cleanup/accept',{**intent,'cleanup_acknowledged':True})['state']=='cleaned'
        receipts=app._observation_context.retire_bootstrap()
        assert receipts[offer['target_node_id']]['cleanup_ack']['removed'] is True
        assert app._observation_context.retire_bootstrap()==receipts
        assert app.authority.control_mailbox.pending_offer(app.authority.coordinator.member(offer['target_node_id'])) is None
        with pytest.raises(ValueError,match='two_distinct_hosts_required'):
            app.action('/api/serving/compile',{'model_preparation_acknowledged':True})
    finally:app.close()
