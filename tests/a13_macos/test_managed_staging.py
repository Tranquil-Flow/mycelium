# SPDX-License-Identifier: AGPL-3.0-only
"""Target-owned canonical staging; tiny fixtures, no model or remote hosts."""
import hashlib
import io
import json
import tarfile
from pathlib import Path
import pytest
from mycelium_a13_macos.managed_staging import TargetStaging, StagingError
from physical_inference_qualification import QualificationController, CommandCapture
from test_managed_transport import peer


def bundle(name='physical_inference_node.py', content=b'# fixture bytes, never executed\n'):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as tar:
        item = tarfile.TarInfo(name); item.size = len(content)
        tar.addfile(item, io.BytesIO(content))
    data = stream.getvalue()
    return data, 'sha256:'+hashlib.sha256(data).hexdigest()


def stager(tmp_path, **kw):
    root = tmp_path/'mycelium-target'
    return TargetStaging(root, node_id='managed-node', max_archive_bytes=100_000,
        max_disk_bytes=200_000, current_member=lambda: True, authorize=lambda intent: True, **kw)


def test_real_canonical_stage_ack_and_marker_consumed_then_cleanup(tmp_path):
    stage = stager(tmp_path)
    archive,digest = bundle()
    ack = stage.prepare(archive=archive, archive_digest=digest)
    target = peer(staging_root=str(stage.staging_root))
    controller = QualificationController(mode='dry-run', peers=(target,), source_root=tmp_path,
        transfer_manifest={}, membership_snapshot={}, now=1.0)
    raw = (json.dumps(ack, sort_keys=True, separators=(',',':'))+'\n').encode()
    assert controller._parse_stage_ack(CommandCapture(argv=(), returncode=0, stdout=raw, stderr=b''),
        peer=target, archive_digest=digest, archive_size=len(archive)) == ack
    assert (stage.staging_root/'physical_inference_node.py').read_bytes().startswith(b'# fixture')
    assert stage.prepare(archive=archive, archive_digest=digest) == ack
    removed = stage.cleanup(archive_digest=digest)
    assert removed['removed'] is True
    assert not stage.staging_root.exists()
    assert stage.cleanup(archive_digest=digest)['removed'] is False


def test_digest_rejection_preserves_previous_stage(tmp_path):
    stage=stager(tmp_path); data,digest=bundle()
    stage.prepare(archive=data, archive_digest=digest)
    with pytest.raises(StagingError,match='stage_archive_digest_mismatch'):
        stage.prepare(archive=data+b'x',archive_digest=digest)
    assert (stage.staging_root/'physical_inference_node.py').is_file()
    with pytest.raises(StagingError,match='stage_cleanup_binding_mismatch'):
        stage.cleanup(archive_digest='sha256:'+'0'*64)
    assert stage.staging_root.is_dir()


@pytest.mark.parametrize('name',['../escaped','/absolute','.mycelium-stage.json','x/../../escaped'])
def test_unsafe_archive_rejected_without_target_write(tmp_path,name):
    stage=stager(tmp_path); data,digest=bundle(name)
    with pytest.raises(StagingError): stage.prepare(archive=data, archive_digest=digest)
    assert not stage.staging_root.exists()


def test_member_and_approval_required_before_any_staging(tmp_path):
    data,digest=bundle()
    for suffix, member, grant in [('revoked',False,True),('unapproved',True,False)]:
        stage=TargetStaging(tmp_path/('mycelium-'+suffix), node_id='managed-node',
            max_archive_bytes=100_000,max_disk_bytes=200_000,
            current_member=lambda:member,authorize=lambda intent:grant)
        with pytest.raises(StagingError): stage.prepare(archive=data,archive_digest=digest)
        assert not stage.staging_root.exists()


def test_preposition_source_is_local_approved_cache_only(tmp_path):
    cache=tmp_path/'cache'; cache.mkdir(); (cache/'tensor').write_bytes(b'not a model')
    data,digest=bundle(); stage=stager(tmp_path,artifact_cache_root=cache)
    record={'content_digest':'sha256:'+hashlib.sha256(b'not a model').hexdigest(),
        'destination_path':'deployment/tensor','source_path':str(cache/'tensor'),'size_bytes':11}
    doc={'protocol':'mycelium.controller_prepositioned_member_artifacts.v1','files':[record]}
    ack=stage.prepare(archive=data,archive_digest=digest,preposition=doc)
    assert (stage.staging_root/'deployment/tensor').read_bytes()==b'not a model'
    stage.cleanup(archive_digest=digest,preposition_digest=ack['preposition_digest'])
    record['source_path']=str(tmp_path/'outside')
    with pytest.raises(StagingError,match='stage_preposition_invalid'):
        stage.prepare(archive=data,archive_digest=digest,preposition=doc)
    assert not stage.staging_root.exists()
