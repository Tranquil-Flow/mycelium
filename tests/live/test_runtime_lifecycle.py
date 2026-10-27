# SPDX-License-Identifier: AGPL-3.0-only
"""Actual emitted desktop lifecycle in desktop-only and mixed topology bundles."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

import pytest
from scripts import build_qwen_live_route as builder
from phone_composition_fixture import (
    elapse_keepalive_interval, fixture_build, members_heartbeat_during_builds)
from test_qwen_route_builder import _topology_node, _write_topology
from physical_inference_qualification import NodeProcessSession

ROOT=Path(__file__).resolve().parents[2]
SIDECAR=ROOT/'native/iroh_transport/target/debug/mycelium-iroh-sidecar'
if not SIDECAR.is_file():
    SIDECAR=ROOT.parent/'native-target/debug/mycelium-iroh-sidecar'


def _evidence_directory(xmlpath, tmp_path, mixed):
    # JUnit is optional in a worktree. When supplied, keep durable evidence
    # beside it, but never collide with another invocation sharing that parent.
    parent = Path(xmlpath).resolve().parent if xmlpath else tmp_path
    root = parent / 'runtime-emitted'
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=('mixed-' if mixed else 'desktop-'), dir=root))


@pytest.mark.parametrize('mixed',[False,True],ids=['desktop-only','mixed-phone'])
def test_emitted_node_full_lifecycle(tmp_path,monkeypatch,record_property,mixed,request):
    if not SIDECAR.is_file():
        pytest.skip('native lifecycle prerequisite absent: run tools/worktree_test_setup.sh INTEGRATION_CHECKOUT')
    assert SIDECAR.is_file(), 'source-aware native fixture build prerequisite'
    original=subprocess.run
    def no_ssh(cmd,**kw):
        if cmd[0]=='ssh':return subprocess.CompletedProcess(cmd,0,json.dumps(['fixture-remote','fixture-boot']),'')
        return original(cmd,**kw)
    monkeypatch.setattr(subprocess,'run',no_ssh)
    fixture=fixture_build(tmp_path,staged_local=True)
    if not mixed:
        nodes=[_topology_node(i,backend='numpy') for i in range(3)]
        nodes[0]['staging_root']=str(tmp_path/'desktop-build/transfer-bundle')
        fixture.args.topology=_write_topology(tmp_path/'desktop-topology.json',nodes)
        fixture.args.output_root=tmp_path/'desktop-build'
    # The mixed bundle is the unpatched production build over a synthetic-signer
    # phone record; this test claims the desktop lifecycle only. It must not
    # depend on host speed: the build runs as if one seed keepalive interval
    # had passed since the members last heartbeated; they heartbeat during the
    # build, as live members do.
    members_heartbeat_during_builds(fixture,monkeypatch)
    elapse_keepalive_interval(fixture)
    report=builder.build(fixture.args)
    plan=json.loads(Path(report['operator_plan']).read_text())
    run=plan['controller']['run_plan'];bundle=fixture.args.output_root/'transfer-bundle'
    assert (bundle/'mycelium-runtime.zip').is_file()
    import shutil
    evidence=_evidence_directory(request.config.getoption('xmlpath'),tmp_path,mixed)
    shutil.copytree(bundle,evidence/'bundle')
    shutil.copyfile(report['operator_plan'],evidence/'operator-plan.json')
    assert len(plan['controller']['transfer_manifest']['files'])<=256
    config=run['nodes'][0]['configure']
    transcript=[]
    # The real sidecar's Unix pathname must remain under macOS's 104-byte cap.
    with tempfile.TemporaryDirectory(prefix='brt-') as ipc:
        argv=(sys.executable,'-I',str(bundle/'physical_inference_node.py'),
            '--run-id',run['run_id'],'--deployment-id',run['deployment_id'],'--node-id','node-0',
            '--artifact-root',str(bundle),'--socket-root',str(Path(ipc)/'run'),
            '--sidecar-binary',str(SIDECAR),'--sidecar-local-only')
        session=NodeProcessSession(argv=argv,node_id='node-0',run_id=run['run_id'],deployment_id=run['deployment_id'],timeout_seconds=20)
        pid=session._process.pid
        def call(cmd,payload):
            r=session.send(command_id='runtime-'+cmd,command=cmd,payload=payload)
            transcript.append(dict(command=cmd,response=r))
            assert r['ok'],(r,session.stderr.decode())
            return r['result']
        try:
            hello=call('hello',{})
            configured=call('configure',config)
            # A real second owned loopback sidecar provides the peer address;
            # there is deliberately no inference or remote peer in this check.
            from physical_inference_node import NativeSidecarProcess
            peer=NativeSidecarProcess(binary=SIDECAR,socket_root=Path(ipc)/'peer',local_only=True,queue_capacity=128,startup_timeout=15)
            try:
                peer_ready=peer.start()
                call('start',dict(peer=dict(node_id='node-1',endpoint_id=peer_ready['endpoint_id'],endpoint_addr=peer_ready['endpoint_addr'],generation=1),local_generation=1))
                snap=call('snapshot',{})
                h=hashlib.sha256(sys.version.encode())
                with zipfile.ZipFile(bundle/'mycelium-runtime.zip') as z:
                    for name in ['physical_inference_node.py','runtime_loader.py','model_adapters.py','numpy_runtime.py','mycelium_router/numpy_runtime.py']:
                        h.update(name.encode());h.update(z.read(name))
                resources=snap['observation']['details']['host_resources']
                assert resources['runtime_build_digest']=='sha256:'+h.hexdigest()
                assert snap['observation']['state']=='RUNNING'
                stopped=call('stop',{})
            finally:peer.close()
        finally:session.close()
        assert session.returncode==0,session.stderr.decode()
        with pytest.raises(ProcessLookupError):os.kill(pid,0)
        assert not (Path(ipc)/'run').exists()
    record_property('emitted_lifecycle',json.dumps(dict(mixed=mixed,pid=pid,returncode=session.returncode,
        sidecar_sha256=hashlib.sha256(SIDECAR.read_bytes()).hexdigest(),transcript=transcript)))
