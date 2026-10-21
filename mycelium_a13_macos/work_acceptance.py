# SPDX-License-Identifier: AGPL-3.0-only
"""Target-local preview/accept bridge for the existing exact work authorizer."""
import json
from .target_app import TargetApplication
from .worker import WorkerLimits
from .managed_offers import MAX_ARCHIVE
from .work_authorization import authorize_work_consent,_staged_document,_digest


def _selected_assignment(root,offer):
    from itertools import islice
    paths=list(islice((root/'control').glob('*-assignment.json'),17))
    if not 0<len(paths)<=16:raise ValueError('work_assignment_selection_invalid')
    matches=[]
    for path in sorted(paths):
        document=_staged_document(root,str(path.relative_to(root)))
        if (document.get('node_id')==offer['target_node_id']
            and document.get('deployment_id')==offer['deployment_id']):matches.append(document)
    if len(matches)!=1:raise ValueError('work_assignment_selection_invalid')
    return matches[0]


def _prepare(app,offer,*,stage_acknowledged,limits,resource_policy,authorize_stage,
             _test_ca_certificate=None,_test_observer=None):
    if (not isinstance(app,TargetApplication) or stage_acknowledged is not True
        or not isinstance(limits,WorkerLimits) or not callable(authorize_stage)):
        raise ValueError('work_stage_consent_required')
    pending=app.pending_stage(_test_ca_certificate=_test_ca_certificate)
    if pending!=offer:raise ValueError('work_offer_changed')
    app.stage_offer(offer,max_bytes=min(MAX_ARCHIVE,limits.max_disk_bytes),max_disk_bytes=limits.max_disk_bytes,
        authorize=authorize_stage,_test_ca_certificate=_test_ca_certificate)
    root=app.peer.state_root/'managed'/offer['run_id']/'mycelium-stage'
    node=offer['target_node_id']
    assignment=_selected_assignment(root,offer)
    manifest=_staged_document(root,'control/model-manifest.json')
    from .managed_classes import managed_runtime_backend
    grant=authorize_work_consent(runtime_backend=managed_runtime_backend(app.peer.PEER_CLASS),
        offer=offer,acknowledged=True,limits=limits,staged_root=root,
        assignment=assignment,expected_assignment_digest=_digest(assignment),manifest=manifest)
    # This is only a proposed grant until the exact digest is acknowledged.
    facts={'protocol':'mycelium.a13.work_preview.v1','offer_id':offer['offer_id'],
        'archive_digest':offer['archive_digest'],'assignment_digest':_digest(assignment),
        'manifest_digest':grant.manifest_digest,'graph_digest':grant.graph_digest,
        'node_id':node,'run_id':offer['run_id'],'deployment_id':offer['deployment_id'],
        'model_id':grant.model_id,'resolved_commit':grant.resolved_commit,
        'limits':vars(limits),'resource_policy':dict(resource_policy),'route_ready':False}
    return {**facts,'consent_digest':_digest(facts)},grant


def preview_work(app,offer,**kwargs):
    preview,_=_prepare(app,offer,**kwargs)
    return preview


def accept_work(app,preview,*,work_acknowledged,limits,resource_policy,authorize_stage,
                _test_ca_certificate=None,_test_observer=None):
    if work_acknowledged is not True or not isinstance(preview,dict):
        raise ValueError('work_consent_required')
    offer=app.pending_stage(_test_ca_certificate=_test_ca_certificate)
    if offer is None:raise ValueError('work_offer_missing')
    current,grant=_prepare(app,offer,stage_acknowledged=True,limits=limits,
        resource_policy=resource_policy,authorize_stage=authorize_stage,
        _test_ca_certificate=_test_ca_certificate,_test_observer=_test_observer)
    if current!=preview:raise ValueError('work_preview_changed')
    return app.start_work(offer,acknowledged=True,resource_policy=resource_policy,limits=limits,
        authorize_load=grant.authorize_load,_test_ca_certificate=_test_ca_certificate,
        _test_observer=_test_observer)
