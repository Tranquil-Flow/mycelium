# SPDX-License-Identifier: AGPL-3.0-only
"""Actual TLS/native hello with synthetic metadata; no tensors are loaded."""
import io,json,tarfile
from pathlib import Path
import pytest
import model_manifest as mm
from mycelium_a13_macos.target_app import TargetApplication
from mycelium_a13_macos.work_acceptance import preview_work,accept_work
from test_control_pump import _world
from test_work_authorization import _manifest,_assignment,_graph,_limits,DEPLOYMENT_ID
from test_worker import POLICY,SAFE


def arrange(w,*,assignment_name=None,duplicate=False):
    app=TargetApplication(w['target'].state_root);app.peer=w['target']
    run='acceptance-test';node=w['member']['node_id']
    stage=app.peer.state_root/'managed'/run/'mycelium-stage'
    manifest=_manifest();assignment=_assignment(stage,manifest,node_id=node)
    documents={(assignment_name or f'control/{node}-assignment.json'):assignment,'control/model-manifest.json':manifest,
               'control/execution-graph.json':_graph(mm.manifest_digest_ref(manifest))}
    if duplicate:documents['control/duplicate-assignment.json']=assignment
    output=io.BytesIO()
    with tarfile.open(fileobj=output,mode='w') as tar:
        for path,doc in sorted(documents.items()):
            data=json.dumps(doc).encode();info=tarfile.TarInfo(path);info.size=len(data);tar.addfile(info,io.BytesIO(data))
    offer=w['authority'].control_mailbox.publish_offer(target=w['member'],run_id=run,deployment_id=DEPLOYMENT_ID,archive=output.getvalue())
    options={'limits':_limits(max_disk_bytes=1_000_000),'resource_policy':POLICY,'authorize_stage':lambda intent:True,
             '_test_ca_certificate':w['authority'].seed_server.material.ca_certificate,'_test_observer':lambda:dict(SAFE)}
    return app,offer,options


def test_preview_then_exact_accept_starts_native_but_does_not_load(tmp_path):
    with _world(tmp_path/'world') as w:
        app,offer,options=arrange(w)
        preview=preview_work(app,offer,stage_acknowledged=True,**options)
        assert not preview['route_ready'] and app._runtime is None
        try:
            result=accept_work(app,preview,work_acknowledged=True,**options)
            assert result['worker_running'] and result['model_load_authorized'] is False
            # Accepted work: the load authorizer is attached; the load itself waits
            # for the owner's configure after activation (demo 'targets' reads this).
            assert result['state']=='running' and result['work_admitted'] is True
            assert app.status()['native_control']['work_admitted'] is True
        finally:app.stop_control()


def test_refused_or_changed_consent_never_creates_runtime(tmp_path):
    with _world(tmp_path/'world') as w:
        app,offer,options=arrange(w)
        with pytest.raises(ValueError):preview_work(app,offer,stage_acknowledged=False,**options)
        assert not (app.peer.state_root/'managed'/offer['run_id']).exists()
        p=preview_work(app,offer,stage_acknowledged=True,**options)
        with pytest.raises(ValueError):accept_work(app,p,work_acknowledged=False,**options)
        with pytest.raises(ValueError):accept_work(app,{**p,'consent_digest':'sha256:'+'0'*64},work_acknowledged=True,**options)
        assert app._runtime is None


def test_preview_uses_the_graph_digest_pinned_by_the_grant(tmp_path,monkeypatch):
    import mycelium_a13_macos.work_acceptance as bridge
    from mycelium_a13_macos.work_authorization import _staged_document,_digest
    with _world(tmp_path/'world') as w:
        p,o,kw=arrange(w)
        original=bridge.authorize_work_consent
        pinned=[]
        def race(**args):
            grant=original(**args)
            graph=Path(grant.staged_root)/'control/execution-graph.json'
            pinned.append(_digest(_staged_document(Path(grant.staged_root),'control/execution-graph.json')))
            graph.write_text(json.dumps({'changed_after_grant':True}))
            return grant
        monkeypatch.setattr(bridge,'authorize_work_consent',race)
        preview=bridge.preview_work(p,o,stage_acknowledged=True,**kw)
        assert preview['graph_digest']==pinned[0]
        assert preview['manifest_digest'].startswith('sha256:')


def test_canonical_compiler_assignment_filename_is_supported(tmp_path):
    with _world(tmp_path/'world') as w:
        app,offer,options=arrange(w,assignment_name='control/node-0-assignment.json')
        preview=preview_work(app,offer,stage_acknowledged=True,**options)
        try:
            result=accept_work(app,preview,work_acknowledged=True,**options)
            assert result['worker_running'] and not result['model_load_authorized']
        finally:app.stop_control()


def test_ambiguous_assignment_is_rejected_before_runtime(tmp_path):
    with _world(tmp_path/'world') as w:
        app,offer,options=arrange(w,duplicate=True)
        with pytest.raises(ValueError,match='work_assignment_selection_invalid'):
            preview_work(app,offer,stage_acknowledged=True,**options)
        assert app._runtime is None
