# SPDX-License-Identifier: AGPL-3.0-only
"""Target-local canonical artifact verification from an acquired pinned source.

No tensor runtime, quantization, model execution, network fallback or readiness
promotion occurs here. The application must independently authorize the exact
assignment/member/source intent before calling this metadata-and-file consumer.
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from .serving import REGISTERED_MODELS, PreparedSource, node_runtime


class AssignmentProvisioningError(ValueError):
    pass


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value,allow_nan=False))


def _digest(value):
    from mycelium_qualification.evidence import canonical_json_bytes
    return 'sha256:'+hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def provision_selected_assignment(*, prepared:PreparedSource, assignment:Mapping[str,Any],
        expected_assignment_digest:str, member_binding:Mapping[str,Any],
        current_member:Callable, authorize:Callable, max_artifact_bytes:int,
        runtime_backend:str='mlx'):
    """Verify one locally acquired assignment and produce native pack/reports.

    The expected digest and member binding come from admitted owner intent, not
    a digest field inside an untrusted assignment. Cache paths are target-owned:
    a remotely selected cache root cannot cause directory creation by the native
    provisioner. This is an internal API, not a grant or a public HTTP handler.
    """
    from layer_assignment import validate_assignment_identity
    import model_manifest as mm
    from .managed_control import _binding_from_member
    try:
        if (not isinstance(prepared,PreparedSource) or prepared.model not in REGISTERED_MODELS
            or type(max_artifact_bytes) is not int or max_artifact_bytes<=0
            or not callable(current_member) or not callable(authorize)):
            raise ValueError()
        assignment=_copy(dict(assignment)); manifest=_copy(dict(prepared.manifest))
        bound=_copy(dict(member_binding))
        if _digest(assignment)!=expected_assignment_digest: raise ValueError()
        validate_assignment_identity(assignment)
        if (not mm.verify_manifest_digest(manifest)
            or mm.manifest_digest_ref(manifest)!=prepared.manifest_digest
            or assignment['manifest_digest']!=prepared.manifest_digest
            or assignment['model_id']!=prepared.model.model_id
            or manifest['model_id']!=prepared.model.model_id
            or manifest['resolved_commit']!=prepared.model.resolved_commit
            or assignment['resolved_commit']!=prepared.model.resolved_commit
            or any(assignment['runtime'].get(key)!=value for key,value in
                node_runtime(prepared.model,runtime_backend).items())):
            raise ValueError()
        source=Path(prepared.source_root)
        if (not source.is_absolute() or source.resolve(strict=True)!=source
            or source.is_symlink() or not source.is_dir()
            or assignment['artifact_cache_root']!=str(source)):
            raise ValueError()
        sizes=[row['size_bytes'] for row in assignment['files']]
        if any(type(n) is not int or n<0 for n in sizes) or sum(sizes)>max_artifact_bytes:
            raise ValueError()
        deployment=assignment['deployment_id']
        binding=_binding_from_member(bound,'assignment-provisioning',deployment)
        if assignment['node_id']!=binding['target_node_id']: raise ValueError()
        intent={'operation':'verify_assignment_artifacts','assignment_digest':expected_assignment_digest,
            'manifest_digest':prepared.manifest_digest,'source_root':str(source),
            'member_binding':binding,'artifact_bytes':sum(sizes),'network_download_bytes':0,
            'model_load_authorized':False}
    except Exception:
        raise AssignmentProvisioningError('assignment_provisioning_admission_invalid') from None

    def admit():
        try:
            current=current_member()
            lease=current.get('lease_expires_at')
            if (type(lease) not in (int,float) or not math.isfinite(lease) or time.time()>=lease
                or _binding_from_member(current,'assignment-provisioning',deployment)!=binding
                or authorize(_copy(intent)) is not True):
                raise ValueError()
        except Exception:
            raise AssignmentProvisioningError('assignment_provisioning_not_authorized') from None

    admit()
    # Lazy imports keep ordinary application startup free of builder/runtime work.
    from mycelium_qualification.physical_deployment import _IdentityAwareLocalFetcher
    from weight_provisioning import provision_assignment, artifact_report_errors
    from stage_pack import compile_stage_pack, verify_stage_pack, artifact_report_for_loader
    try:
        fetcher=_IdentityAwareLocalFetcher(source,model_id=prepared.model.model_id,
            resolved_commit=prepared.model.resolved_commit)
        def guarded_fetch(*args,**kwargs):
            admit()
            return fetcher(*args,**kwargs)
        report=provision_assignment(assignment,fetch_file=guarded_fetch,local_files_only=True)
        if artifact_report_errors(assignment,report) or report['network_download_bytes']!=0:
            raise ValueError()
        pack=compile_stage_pack(assignment,manifest,report)
        verification=verify_stage_pack(pack,assignment=assignment,manifest=manifest)
        loader=artifact_report_for_loader(pack,verification,assignment=assignment,manifest=manifest)
    except AssignmentProvisioningError: raise
    except Exception:
        raise AssignmentProvisioningError('assignment_artifact_verification_failed') from None
    admit()  # Source verification is not permission to publish after revocation.
    return {'state':'artifacts_verified_not_loaded','route_ready':False,
        'assignment_digest':expected_assignment_digest,'artifact_report':report,
        'stage_pack':pack,'stage_pack_verification':verification,'loader_report':loader}
