# SPDX-License-Identifier: AGPL-3.0-only
"""Consent follows actual staged documents, not filename-shaped claims."""
import json
from pathlib import Path
from test_work_authorization import (_consent,_assignment,_manifest,_offer,_limits,_digest,_graph,_envelope,_configure_payload)
from mycelium_a13_macos.work_authorization import authorize_work_consent

def test_compiler_deployment_subdirectory_is_admitted(tmp_path):
    root=(tmp_path/'stage');root.mkdir();root=root.resolve();(root/'control').mkdir()
    manifest=_manifest();assignment=_assignment(root,manifest,artifact_cache_root=str(root/'deployment'))
    (root/'control/execution-graph.json').write_text(json.dumps(_graph(assignment['manifest_digest'])))
    grant=authorize_work_consent(offer=_offer(),acknowledged=True,limits=_limits(),staged_root=root,
        assignment=assignment,expected_assignment_digest=_digest(assignment),manifest=manifest)
    assert grant.staged_root==str(root)

def test_missing_actual_assignment_cannot_be_authorized(tmp_path):
    grant,root,manifest,assignment=_consent(tmp_path)
    (root/'assignment.json').unlink(missing_ok=True)
    assert grant.authorize_load(_envelope('configure',_configure_payload(assignment['manifest_digest']))) is False

def test_alternate_assignment_contents_cannot_be_authorized(tmp_path):
    grant,root,manifest,assignment=_consent(tmp_path)
    altered={**assignment,'deployment_epoch':2}
    (root/'assignment.json').write_text(json.dumps(altered))
    assert grant.authorize_load(_envelope('configure',_configure_payload(assignment['manifest_digest']))) is False

def test_graph_content_is_pinned_not_just_model_labels(tmp_path):
    grant,root,manifest,assignment=_consent(tmp_path)
    payload=_configure_payload(assignment['manifest_digest']);payload['graph']['deployment_epoch']=2
    assert grant.authorize_load(_envelope('configure',payload)) is False


def test_actual_manifest_must_match_consent(tmp_path):
    grant,root,manifest,assignment=_consent(tmp_path)
    (root/'manifest.json').write_text(json.dumps({**manifest,'model_id':'unapproved'}))
    payload=_configure_payload(assignment['manifest_digest']);del payload['artifact_report_file']
    payload.update(manifest_file='manifest.json',stage_pack_file='pack.json')
    assert grant.authorize_load(_envelope('configure',payload)) is False


def test_assignment_symlink_cannot_escape_staging(tmp_path):
    grant,root,manifest,assignment=_consent(tmp_path)
    other=tmp_path/'outside.json';other.write_text(json.dumps(assignment))
    (root/'assignment.json').unlink();(root/'assignment.json').symlink_to(other)
    assert grant.authorize_load(_envelope('configure',_configure_payload(assignment['manifest_digest']))) is False
