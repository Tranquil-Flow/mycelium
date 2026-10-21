# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit, fail-closed work consent for the managed native worker boundary.

This module turns one acknowledged owner/target consent decision into the two
inputs ``TargetApplication.start_work`` requires: a bounded ``WorkerLimits``
instance and an exact ``authorize_load`` callable.  It is a local consent
gate, not Mycelium owner authority and not evidence that the person operating
the Mac approved anything; the caller must construct it only after obtaining
that approval.

Design invariants:

* Fail closed.  Any malformed input, unknown field, digest mismatch, path
  escape, or internal exception denies authorization.
* Pins come from the run's registered model contract (``serving.
  REGISTERED_MODELS``) and its own resource proposal (``ModelContract.
  resource_proposal``, model_resources.py; the 7B candidate's is the measured
  ``RESOURCE_PROPOSAL``); nothing is re-hardcoded here.
* ``authorize_load`` never grants on command name alone: every exact-grant
  command's payload is checked against the consent pins (graph identity,
  staged file shape, run/deployment binding, typed limits semantics).
* The staged artifact cache root is the offer-bound staged deployment path
  (``<state_root>/managed/<run_id>/mycelium-stage/deployment``), NOT the HF source root.
* Native configure validation stays authoritative: this gate only admits
  fully pinned commands to the native boundary; it never substitutes for the
  native node's own validation and never performs or fakes a model load.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
import time
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping

from .serving import (
    CANDIDATE_MODEL,
    RESOURCE_PROPOSAL,
    ServingPreparationError,
    node_runtime,
    registered_model_contract,
)


class WorkAuthorizationError(ValueError):
    """A bounded work-consent input failed closed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# Native grammar is shared; the authorization policy admits only these five operations.
from .worker import _NATIVE_PROTOCOL, _OPERATION_PAYLOAD_FIELDS
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_EXECUTION_GRAPH_PROTOCOL = "mycelium.execution_graph.v1"
_EXACT_GRANT_COMMANDS = frozenset({"configure", "start", "infer_start", "infer_decode", "rotate"})
_CONFIGURE_FILE_FIELDS = (
    "assignment_file",
    "artifact_report_file",
    "manifest_file",
    "stage_pack_file",
    "pixel_stage_pack_file",
)
_ENVELOPE_FIELDS = frozenset(
    {"protocol", "command_id", "run_id", "deployment_id", "command", "payload"}
)


@dataclass(frozen=True)
class WorkAuthorization:
    """One admitted consent decision bound to exact execution pins."""

    limits: Any
    offer_id: str
    run_id: str
    deployment_id: str
    node_id: str
    model_id: str
    resolved_commit: str
    manifest_digest: str
    assignment_id: str
    staged_root: str
    graph_digest: str
    authorize_load: Callable[[Mapping[str, Any]], bool] = field(
        compare=False, repr=False
    )

    def pinned(self) -> dict[str, Any]:
        """Readable summary of the exact pins this consent admitted."""
        return {
            "offer_id": self.offer_id,
            "run_id": self.run_id,
            "deployment_id": self.deployment_id,
            "node_id": self.node_id,
            "model_id": self.model_id,
            "resolved_commit": self.resolved_commit,
            "manifest_digest": self.manifest_digest,
            "assignment_id": self.assignment_id,
            "staged_root": self.staged_root,
            "graph_digest": self.graph_digest,
            "grant_commands": tuple(sorted(_EXACT_GRANT_COMMANDS)),
        }


def _require(condition: bool, code: str) -> None:
    if condition is not True:
        raise WorkAuthorizationError(code)


def _segment(value: Any) -> bool:
    return isinstance(value, str) and _SEGMENT.fullmatch(value) is not None


def _copy(value: Any) -> Any:
    import json

    return json.loads(json.dumps(value, allow_nan=False))


def _digest(value: Any) -> str:
    import hashlib

    from mycelium_qualification.evidence import canonical_json_bytes

    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _safe_relative_path(value: Any) -> bool:
    """Staged-file shape: relative POSIX path without traversal, mirroring
    the native node's own ``_safe_document`` admission rules."""
    if not isinstance(value, str) or not 0 < len(value) <= 1024:
        return False
    relative = PurePosixPath(value)
    return (
        not relative.is_absolute()
        and str(relative) == value
        and 0 < len(relative.parts) <= 16
        and all(part not in {"", ".", ".."} for part in relative.parts)
    )


def _staged_document(root, relative):
    """Read bounded actual metadata without following a substituted path."""
    import json, os, stat
    if not _safe_relative_path(relative):raise ValueError('work_document_invalid')
    path=root.joinpath(*PurePosixPath(relative).parts)
    if path.resolve(strict=True)!=path:raise ValueError('work_document_invalid')
    import mycelium_host_platform
    if mycelium_host_platform.is_win32():
        # No O_NOFOLLOW on Windows (the Windows managed host reuses this
        # workflow): open by handle, refusing every reparse point on the way.
        try:
            stream=mycelium_host_platform.open_beneath(root,PurePosixPath(relative).parts)
        except mycelium_host_platform.HostPlatformError as exc:
            raise ValueError('work_document_invalid') from exc
    else:
        stream=os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('work_document_invalid')
            result[key]=value
        return result
    with stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=2*1024*1024:
            raise ValueError('work_document_invalid')
        value=json.loads(stream.read(2*1024*1024+1),object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('work_document_invalid')))
    if not isinstance(value,dict):raise ValueError('work_document_invalid')
    return value


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _proposal_ceiling(manifest: Any):
    """The run's model-level proposal (upper bound for any member of any split).

    Unregistered or malformed manifests fall back to the candidate's proposal;
    they are refused below as ``work_manifest_invalid``.
    """
    try:
        return registered_model_contract(manifest.get("model_id"), manifest.get("resolved_commit")).resource_proposal
    except Exception:
        return RESOURCE_PROPOSAL


def authorize_work_consent(
    *,
    offer: Mapping[str, Any],
    acknowledged: Any,
    limits: Any,
    staged_root: Any,
    assignment: Mapping[str, Any],
    expected_assignment_digest: str,
    manifest: Mapping[str, Any],
    runtime_backend: str = CANDIDATE_MODEL.runtime_backend,
) -> WorkAuthorization:
    """Admit one exact work consent and return its bounded authorization.

    All inputs are caller-carried records of separately obtained consent and
    freshly verified artifacts; a failure here is a denial, never a downgrade.
    """
    from pathlib import Path

    from .worker import WorkerLimits

    _require(acknowledged is True, "work_consent_acknowledgement_missing")
    _require(isinstance(limits, WorkerLimits), "work_limits_invalid")
    _require(_mapping(offer), "work_offer_invalid")
    try:
        from .managed_offers import _offer_valid

        offer_document = _offer_valid(_copy(dict(offer)))
        expires_at = offer_document["expires_at"]
        _require(
            type(expires_at) in (int, float)
            and math.isfinite(float(expires_at))
            and time.time() < float(expires_at),
            "work_offer_invalid",
        )
    except WorkAuthorizationError:
        raise
    except Exception as exc:
        raise WorkAuthorizationError("work_offer_invalid") from exc
    proposal = _proposal_ceiling(manifest)
    if (
        limits.max_rss_bytes > proposal.max_combined_rss_bytes
        or limits.max_disk_bytes > proposal.additional_disk_bytes
        or limits.max_wall_seconds > proposal.max_wall_seconds
    ):
        raise WorkAuthorizationError("work_limits_exceed_proposal")
    _require(
        limits.max_disk_bytes >= offer_document["archive_bytes"],
        "work_limits_below_offer_archive",
    )
    candidate = Path(str(staged_root)).expanduser()
    try:
        canonical = (
            candidate.is_absolute()
            and not candidate.is_symlink()
            and candidate.resolve(strict=True) == candidate
            and candidate.is_dir()
        )
    except OSError:
        canonical = False
    _require(canonical is True, "work_staged_root_invalid")
    staged = candidate
    # Manifest: real compiler output, digest-verified, contract-pinned.
    import model_manifest as mm

    manifest_document = _copy(dict(manifest))
    model = CANDIDATE_MODEL
    try:
        # The run's pinned model must be a registered contract; this target
        # runs its own class's stage runtime for it.
        model = registered_model_contract(
            manifest_document.get("model_id"), manifest_document.get("resolved_commit")
        )
        manifest_ok = mm.verify_manifest_digest(manifest_document)
    except (ServingPreparationError, Exception):
        manifest_ok = False
    _require(manifest_ok is True, "work_manifest_invalid")
    manifest_digest = mm.manifest_digest_ref(manifest_document)
    # Assignment: digest-bound, identity-valid, pinned to this offer's staged
    # deployment path and the candidate runtime contract.
    from layer_assignment import validate_assignment_identity

    assignment_document = _copy(dict(assignment))
    _require(
        isinstance(expected_assignment_digest, str)
        and _digest(assignment_document) == expected_assignment_digest,
        "work_assignment_digest_mismatch",
    )
    try:
        validate_assignment_identity(assignment_document)
        runtime = assignment_document.get("runtime")
        from runtime_contracts import validate_normalized_mlx_runtime, validate_normalized_numpy_runtime
        validator={'mlx':validate_normalized_mlx_runtime,'numpy':validate_normalized_numpy_runtime}[runtime_backend]
        expected_runtime=validator({**manifest_document['runtime_model'],**node_runtime(model,runtime_backend)})
        assignment_ok = (
            assignment_document.get("deployment_id")
            == offer_document["deployment_id"]
            and assignment_document.get("node_id")
            == offer_document["target_node_id"]
            and assignment_document.get("model_id") == model.model_id
            and assignment_document.get("resolved_commit")
            == model.resolved_commit
            and assignment_document.get("manifest_digest") == manifest_digest
            and _mapping(runtime)
            and expected_runtime["architecture"] == model.architecture
            and dict(runtime) == expected_runtime
            and assignment_document.get("artifact_cache_root") == str(staged / "deployment")
        )
    except Exception as exc:
        raise WorkAuthorizationError("work_assignment_invalid") from exc
    _require(assignment_ok is True, "work_assignment_invalid")
    run_id = offer_document["run_id"]
    deployment_id = offer_document["deployment_id"]
    node_id = offer_document["target_node_id"]
    model_id = model.model_id
    resolved_commit = model.resolved_commit
    assignment_id = assignment_document["assignment_id"]
    try:
        graph_document=_staged_document(staged,'control/execution-graph.json')
        graph_digest=_digest(graph_document)
    except Exception as exc:
        raise WorkAuthorizationError('work_graph_invalid') from exc

    def _peer_binding(document):
        from mycelium_router.transports.iroh import PeerBinding
        if not _mapping(document) or set(document)!={'node_id','endpoint_id','endpoint_addr','generation'}:
            return None
        if not _mapping(document.get('endpoint_addr')):return None
        peer=PeerBinding(**dict(document))
        approved={p['node_id'] for stage in graph_document['stages'] for p in stage['placements']}
        if peer.node_id==node_id or peer.node_id not in approved:return None
        return peer

    def _evaluate(envelope: Any) -> bool:
        """Exact-grant evaluation; callers only ever see literal True/False."""
        if not _mapping(envelope) or set(envelope) != _ENVELOPE_FIELDS:
            return False
        if (
            envelope.get("protocol") != _NATIVE_PROTOCOL
            or not _segment(envelope.get("command_id"))
            or envelope.get("run_id") != run_id
            or envelope.get("deployment_id") != deployment_id
        ):
            return False
        command = envelope.get("command")
        payload = envelope.get("payload")
        if not isinstance(command, str) or command not in _EXACT_GRANT_COMMANDS:
            return False
        if not _mapping(payload) or frozenset(payload) not in (
            _OPERATION_PAYLOAD_FIELDS[command]
        ):
            return False
        if command == "configure":
            graph = payload.get("graph")
            if not _mapping(graph):
                return False
            if (
                graph.get("protocol") != _EXECUTION_GRAPH_PROTOCOL
                or graph.get("deployment_id") != deployment_id
                or graph.get("model_id") != model_id
                or graph.get("resolved_commit") != resolved_commit
                or graph.get("manifest_digest") != manifest_digest
            ):
                return False
            if not all(
                _safe_relative_path(payload.get(name))
                for name in _CONFIGURE_FILE_FIELDS
                if name in payload
            ):
                return False
            if _digest(graph)!=graph_digest:
                return False
            if _digest(_staged_document(staged,payload['assignment_file']))!=expected_assignment_digest:
                return False
            if 'manifest_file' in payload and _digest(_staged_document(staged,payload['manifest_file']))!=_digest(manifest_document):
                return False
            if not _positive_int(payload.get("load_generation")):
                return False
            if not _mapping(payload.get("device_states")):
                return False
            return True
        if command == "start":
            peer=_peer_binding(payload.get('peer'))
            extra=payload.get('peers',[])
            if peer is None or not isinstance(extra,list):return False
            peers=[peer,*[_peer_binding(item) for item in extra]]
            if any(item is None for item in peers):return False
            if len({item.node_id for item in peers})!=len(peers):return False
            if len({item.endpoint_id for item in peers})!=len(peers):return False
            return (_positive_int(payload.get('local_generation')) and
                    payload['local_generation']==offer_document['membership_generation'])
        if command == "infer_start":
            request = payload.get("request")
            if not (_mapping(request) or (isinstance(request, str) and request)):
                return False
            if "excluded_placement_ids" in payload and not isinstance(
                payload.get("excluded_placement_ids"), list
            ):
                return False
            if "control" in payload and not _mapping(payload.get("control")):
                return False
            if "path_manifest" in payload and not _mapping(
                payload.get("path_manifest")
            ):
                return False
            return True
        if command == "infer_decode":
            if not _segment(payload.get("request_id")):
                return False
            if not _positive_int(payload.get("count")):
                return False
            if "control" in payload and not _mapping(payload.get("control")):
                return False
            return True
        # command == "rotate"
        return _peer_binding(payload.get("peer")) is not None

    def authorize_load(envelope: Mapping[str, Any]) -> bool:
        try:
            return _evaluate(envelope) is True
        except Exception:
            return False

    return WorkAuthorization(
        limits=limits,
        offer_id=offer_document["offer_id"],
        run_id=run_id,
        deployment_id=deployment_id,
        node_id=node_id,
        model_id=model_id,
        resolved_commit=resolved_commit,
        manifest_digest=manifest_digest,
        assignment_id=assignment_id,
        staged_root=str(staged),
        graph_digest=graph_digest,
        authorize_load=authorize_load,
    )
