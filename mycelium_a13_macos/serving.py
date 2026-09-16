"""Small, fail-closed A13 macOS seam into Mycelium's native live stack.

This module deliberately separates source-only preparation from model execution.
``prepare_local_source`` runs the native immutable manifest compiler and nothing
that loads tensors.  ``prepare_registry_launch`` validates an already-built,
original-representation operator plan and returns a launch object whose ``run``
method enters the real physical-route qualification/registry supervisor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
from typing import Any, Callable, Mapping


class ServingPreparationError(ValueError):
    """A bounded A13 serving input failed closed."""


@dataclass(frozen=True)
class ResourceProposal:
    """The exact bounded proposal; these are not observed execution results."""

    network_download_bytes: int
    execution_download_bytes: int
    additional_disk_bytes: int
    max_combined_rss_bytes: int
    max_wall_seconds: int
    run_count: int
    estimated_cost_usd: int = 0


RESOURCE_PROPOSAL = ResourceProposal(
    # Pinned Instruct repository metadata: total 999,604,126 bytes (weights 988,097,824).
    # This remains a proposal: no download is performed by this module.
    network_download_bytes=999_604_126,
    # Both native compiler/provisioner paths are local-files-only.
    execution_download_bytes=0,
    # 8 GiB each for output/temporary closure and combined process RSS.
    additional_disk_bytes=8_589_934_592,
    max_combined_rss_bytes=8_589_934_592,
    max_wall_seconds=1_800,
    run_count=1,
)


@dataclass(frozen=True)
class ModelContract:
    model_id: str
    resolved_commit: str
    source_representation: str
    runtime_backend: str
    runtime_dtype: str
    runtime_quantization: str
    decoding: str = "quantized_greedy_token_id"
    sampling_seed: int = 0

    @property
    def resource_proposal(self) -> ResourceProposal:
        return RESOURCE_PROPOSAL


# Owner addendum: match hosted W6 identity/representation exactly. This is
# the selected contract, not permission to reuse hosted artifacts or evidence.
CANDIDATE_MODEL = ModelContract(
    model_id="Qwen/Qwen2.5-0.5B-Instruct",
    resolved_commit="7ae557604adf67be50417f59c2c2f167def9a775",
    source_representation="bfloat16-safetensors",
    runtime_backend="mlx",
    runtime_dtype="float32",
    runtime_quantization="int8-weight-only",
)

# What the checked-in native end-to-end evidence actually exercised.  It is
# now matches the owner-selected candidate; historic evidence is not fresh demo proof.
EXECUTED_NATIVE_MODEL = ModelContract(
    model_id="Qwen/Qwen2.5-0.5B-Instruct",
    resolved_commit="7ae557604adf67be50417f59c2c2f167def9a775",
    source_representation="bfloat16-safetensors",
    runtime_backend="mlx",
    runtime_dtype="float32",
    runtime_quantization="int8-weight-only",
)

_PROTECTED_PORTS = frozenset({8791, 8876, 8877, 4350, 4351, 4352})
_MIN_DYNAMIC_PORT = 49_152
_MAX_JSON_BYTES = 16 * 1024 * 1024

Compiler = Callable[[Any], Mapping[str, Any]]
RegistryRunner = Callable[..., int]


@dataclass(frozen=True)
class PreparedSource:
    """Native compiler output only; never a load or route-readiness claim."""

    model: ModelContract
    source_root: Path
    source_bytes: int
    manifest: Mapping[str, Any]
    manifest_digest: str
    resource_proposal: ResourceProposal = RESOURCE_PROPOSAL
    chain: tuple[str, ...] = (
        "LocalModelSource",
        "compile_local_model_manifest",
        "compile_model_manifest",
    )


@dataclass(frozen=True)
class ExecutionApproval:
    """Caller-carried record of the exact separately granted execution scope.

    This is a local API guard, not Mycelium owner authority and not evidence that
    the person operating the Mac granted approval.  The caller must construct it
    only after obtaining that approval.
    """

    model_id: str
    resolved_commit: str
    source_representation: str
    runtime_dtype: str
    runtime_quantization: str
    max_execution_download_bytes: int
    max_additional_disk_bytes: int
    max_combined_rss_bytes: int
    max_wall_seconds: int
    run_count: int
    operator_plan_digest: str
    physical_host_ids: tuple[str, ...]


@dataclass(frozen=True)
class RegistryLaunch:
    """Validated call into the real qualification + registry supervisor."""

    prepared: PreparedSource
    operator_plan: Path
    private_state_root: Path
    registry_state: Path
    seed_state_root: Path
    product_state_root: Path
    operator_plan_digest: str
    physical_host_ids: tuple[str, ...]
    _runner: RegistryRunner = field(repr=False, compare=False)
    seed_binding: tuple[str, str, int]
    session_factory: Any = field(default=None, repr=False, compare=False)
    chain: tuple[str, ...] = (
        "PhysicalLiveRoute.from_operator_plan",
        "PhysicalLiveRoute.open",
        "_qualify_open_route",
        "PhysicalLiveRoute.set_deployment_qualification",
        "LiveDeploymentRegistry",
        "build_registry_stack",
        "run_registry_server",
    )

    def run(self, *, approval: ExecutionApproval | None, port: int, api_only: bool = False,
            stop_event=None, on_started=None) -> int:
        """Execute the real native startup chain and block in its supervisor.

        Calling this method loads/probes the model.  Source preparation never
        calls it.  The native supervisor owns startup cleanup and signal drain.
        ``api_only=True`` serves the existing request/status/stream routes
        without any static UI build; static routes then fail closed.
        """

        import threading
        if ((stop_event is not None and not isinstance(stop_event,threading.Event))
                or (on_started is not None and not callable(on_started))):
            raise ServingPreparationError("registry_lifecycle_invalid")
        if type(api_only) is not bool:
            raise ServingPreparationError("api_only_invalid")
        approval = _validate_execution_approval(approval, self.prepared)
        if (
            approval.operator_plan_digest != self.operator_plan_digest
            or approval.physical_host_ids != self.physical_host_ids
        ):
            raise ServingPreparationError("model_execution_topology_mismatch")
        if _plan_digest(self.operator_plan) != self.operator_plan_digest:
            raise ServingPreparationError("operator_plan_changed")
        # Preparation is never a permanent trust grant.
        _validate_operator_plan(self.operator_plan, self.prepared)
        if _canonical_host_ids(self.operator_plan) != self.physical_host_ids:
            raise ServingPreparationError("operator_plan_changed")
        _, binding = _bind_existing_seed(self.operator_plan, self.seed_state_root)
        if binding != self.seed_binding:
            raise ServingPreparationError("operator_plan_seed_binding_changed")
        if _plan_digest(self.operator_plan) != self.operator_plan_digest:
            raise ServingPreparationError("operator_plan_changed")
        _validate_dynamic_port(port)
        _assert_loopback_port_free(port)
        return self._runner(
            operator_plans=(self.operator_plan,),
            host="127.0.0.1",
            port=port,
            registry_state=self.registry_state,
            seed_state_root=self.seed_state_root,
            product_state_root=self.product_state_root,
            governance_root=None,  # Installed app is not a source-control checkout.
            **({"api_only": api_only} if api_only else {}),
            **({"stop_event": stop_event} if stop_event is not None else {}),
            **({"on_started": on_started} if on_started is not None else {}),
            **({"session_factory": self.session_factory} if self.session_factory is not None else {}),
        )


def _read_json_object(path: Path, *, reason: str) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _MAX_JSON_BYTES
        ):
            raise ServingPreparationError(reason)
        value = json.loads(path.read_text(encoding="utf-8"))
    except ServingPreparationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ServingPreparationError(reason) from exc
    if not isinstance(value, dict):
        raise ServingPreparationError(reason)
    return value


def _snapshot_identity(source_root: Path, model: ModelContract) -> None:
    if (
        source_root.name != model.resolved_commit
        or source_root.parent.name != "snapshots"
        or source_root.parent.parent.name
        != "models--" + model.model_id.replace("/", "--")
    ):
        raise ServingPreparationError("source_revision_mismatch")


def _weight_bytes(source_root: Path) -> int:
    weights = tuple(source_root.glob("*.safetensors"))
    if not weights:
        raise ServingPreparationError("source_safetensors_missing")
    seen: set[tuple[int, int]] = set()
    total = 0
    for path in weights:
        try:
            resolved = path.resolve(strict=True)
            metadata = resolved.stat()
        except OSError as exc:
            raise ServingPreparationError("source_safetensors_invalid") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise ServingPreparationError("source_safetensors_invalid")
        inode = (metadata.st_dev, metadata.st_ino)
        if inode not in seen:
            total += metadata.st_size
            seen.add(inode)
    return total


def _native_compiler(source: Any) -> Mapping[str, Any]:
    from mycelium_qualification.physical_deployment import compile_local_model_manifest

    return compile_local_model_manifest(source)


def _local_model_source(source_root: Path, model: ModelContract) -> Any:
    from mycelium_qualification.physical_deployment import LocalModelSource

    return LocalModelSource(
        root=source_root,
        model_id=model.model_id,
        requested_revision="main",
        resolved_commit=model.resolved_commit,
    )


def prepare_local_source(
    source_root: str | Path,
    *,
    model: ModelContract = CANDIDATE_MODEL,
    compiler: Compiler | None = None,
) -> PreparedSource:
    """Hash and compile one pinned local HF snapshot; never load tensors.

    The default compiler is the production ``compile_local_model_manifest``.
    ``compiler`` exists only so deterministic unit tests can stop before reading
    a real model-sized Safetensors file.
    """

    candidate = Path(source_root).expanduser()
    if candidate.is_symlink():
        raise ServingPreparationError("source_root_symlink")
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise ServingPreparationError("source_root_missing") from exc
    if not root.is_dir():
        raise ServingPreparationError("source_root_not_directory")
    _snapshot_identity(root, model)
    config = _read_json_object(root / "config.json", reason="source_config_invalid")
    if (
        config.get("model_type") != "qwen2"
        or config.get("torch_dtype") not in {"bfloat16", "bf16"}
    ):
        raise ServingPreparationError("source_architecture_mismatch")
    source_bytes = _weight_bytes(root)
    source = _local_model_source(root, model)
    try:
        manifest = dict((compiler or _native_compiler)(source))
    except ServingPreparationError:
        raise
    except Exception as exc:
        raise ServingPreparationError("native_manifest_compile_failed") from exc
    digest = manifest.get("manifest_digest")
    manifest_digest = (
        "sha256:" + str(digest.get("value"))
        if isinstance(digest, Mapping) and digest.get("algorithm") == "sha256"
        else ""
    )
    if (
        manifest.get("model_id") != model.model_id
        or manifest.get("resolved_commit") != model.resolved_commit
        or manifest.get("runtime_model", {}).get("architecture") != "qwen2"
        or len(manifest_digest) != 71
    ):
        raise ServingPreparationError("native_manifest_identity_mismatch")
    return PreparedSource(
        model=model,
        source_root=root,
        source_bytes=source_bytes,
        manifest=manifest,
        manifest_digest=manifest_digest,
    )


def _resolve_bundle_file(bundle: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ServingPreparationError("operator_plan_assignment_invalid")
    try:
        path = (bundle / relative).resolve(strict=True)
        path.relative_to(bundle)
    except (OSError, ValueError) as exc:
        raise ServingPreparationError("operator_plan_assignment_invalid") from exc
    return path


def _validate_operator_plan(plan_path: Path, prepared: PreparedSource) -> None:
    plan = _read_json_object(plan_path, reason="operator_plan_invalid")
    controller = plan.get("controller")
    run_plan = controller.get("run_plan") if isinstance(controller, Mapping) else None
    nodes = run_plan.get("nodes") if isinstance(run_plan, Mapping) else None
    source_root = controller.get("source_root") if isinstance(controller, Mapping) else None
    if (
        not isinstance(nodes, list)
        or len(nodes) < 2
        or not isinstance(source_root, str)
    ):
        raise ServingPreparationError("operator_plan_not_physical_multi_process")
    bundle_candidate = Path(source_root).expanduser()
    if bundle_candidate.is_symlink():
        raise ServingPreparationError("operator_plan_bundle_invalid")
    try:
        bundle = bundle_candidate.resolve(strict=True)
    except OSError as exc:
        raise ServingPreparationError("operator_plan_bundle_invalid") from exc
    node_ids: set[str] = set()
    graph_identity: tuple[object, object, object] | None = None
    for node in nodes:
        configure = node.get("configure") if isinstance(node, Mapping) else None
        node_id = node.get("node_id") if isinstance(node, Mapping) else None
        graph = configure.get("graph") if isinstance(configure, Mapping) else None
        assignment_name = (
            configure.get("assignment_file") if isinstance(configure, Mapping) else None
        )
        if not isinstance(node_id, str) or not node_id or not isinstance(graph, Mapping):
            raise ServingPreparationError("operator_plan_not_physical_multi_process")
        node_ids.add(node_id)
        identity = (
            graph.get("model_id"),
            graph.get("resolved_commit"),
            graph.get("manifest_digest"),
        )
        if graph_identity is None:
            graph_identity = identity
        elif graph_identity != identity:
            raise ServingPreparationError("operator_plan_graph_identity_mismatch")
        assignment = _read_json_object(
            _resolve_bundle_file(bundle, assignment_name),
            reason="operator_plan_assignment_invalid",
        )
        runtime = assignment.get("runtime")
        if (
            assignment.get("model_id") != prepared.model.model_id
            or assignment.get("resolved_commit") != prepared.model.resolved_commit
            or assignment.get("manifest_digest") != prepared.manifest_digest
            or assignment.get("node_id") != node_id
            or not isinstance(runtime, Mapping)
            or runtime.get("backend") != prepared.model.runtime_backend
            or runtime.get("dtype") != prepared.model.runtime_dtype
            or runtime.get("quantization") != prepared.model.runtime_quantization
        ):
            raise ServingPreparationError("operator_plan_representation_mismatch")
    if len(node_ids) != len(nodes):
        raise ServingPreparationError("operator_plan_duplicate_node")
    expected = (
        prepared.model.model_id,
        prepared.model.resolved_commit,
        prepared.manifest_digest,
    )
    if graph_identity != expected:
        raise ServingPreparationError("operator_plan_graph_identity_mismatch")
    request = run_plan.get("request") if isinstance(run_plan, Mapping) else None
    if (
        not isinstance(request, Mapping)
        or type(request.get("sampling_seed")) is not int
        or request["sampling_seed"] != prepared.model.sampling_seed
        or prepared.model.decoding != "quantized_greedy_token_id"
    ):
        raise ServingPreparationError("operator_plan_decoding_mismatch")


def _private_directory(path: Path) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ServingPreparationError("private_state_root_unsafe")
    try:
        candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(candidate, 0o700)
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise ServingPreparationError("private_state_root_unsafe") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ServingPreparationError("private_state_root_unsafe")
    return resolved


def _native_registry_runner(**kwargs: Any) -> int:
    from mycelium_live.supervisor import run_registry_server

    return run_registry_server(**kwargs)


def _plan_digest(path: Path) -> str:
    """Bind approval to exact plan bytes, including member/host/transport scope."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= _MAX_JSON_BYTES:
                raise ServingPreparationError("operator_plan_invalid")
            raw = handle.read(_MAX_JSON_BYTES + 1)
        if len(raw) > _MAX_JSON_BYTES:
            raise ServingPreparationError("operator_plan_invalid")
    except OSError as exc:
        raise ServingPreparationError("operator_plan_invalid") from exc
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical_host_ids(path: Path) -> tuple[str, ...]:
    """Validate native schema and A13 two-host intent, not physical evidence.

    Native startup must still prove distinct physical hosts and the complete
    signed graph/assignment/qualification chain.
    """
    from mycelium_physical_runner.config import load_operator_plan
    from mycelium_physical_runner.errors import RunnerError

    try:
        config = load_operator_plan(path)
    except (RunnerError, ValueError, TypeError) as exc:
        raise ServingPreparationError("operator_plan_canonical_invalid") from exc
    peers = config.controller["peers"]
    hosts = [peer.get("host_id") for peer in peers]
    node_ids = {node["node_id"] for node in config.controller["run_plan"]["nodes"]}
    if (
        len(peers) != 2
        or any(not isinstance(host, str) or not host.strip() for host in hosts)
        or len(set(hosts)) != 2
        or {peer["node_id"] for peer in peers} != node_ids
    ):
        raise ServingPreparationError("operator_plan_distinct_hosts_required")
    return tuple(sorted(hosts))


def _bind_existing_seed(
    plan: Path, seed_state_root: str | Path | None
) -> tuple[Path, tuple[str, str, int]]:
    """Use the native load-only authority validator; never create seed state."""
    from mycelium_live.route import LiveSeedStateError, _load_live_seed_authority

    if seed_state_root is None:
        raise ServingPreparationError("existing_seed_required")
    payload = _read_json_object(plan, reason="operator_plan_invalid")
    snapshot = payload["controller"]["membership_snapshot"]
    try:
        authority = _load_live_seed_authority(
            seed_state_root=Path(seed_state_root).expanduser(),
            plan_membership_snapshot=snapshot,
        )
    except LiveSeedStateError as exc:
        raise ServingPreparationError("operator_plan_seed_binding_invalid") from exc
    try:
        return authority.state_root.path, (
            authority.swarm_id,
            authority.signer.verification_key_digest,
            authority.authority_generation,
        )
    finally:
        authority.close()


def prepare_registry_launch(
    *,
    prepared: PreparedSource,
    operator_plan: str | Path,
    private_state_root: str | Path,
    seed_state_root: str | Path | None = None,
    runner: RegistryRunner | None = None,
    session_factory=None,
) -> RegistryLaunch:
    """Validate a built route and compose the real single-deployment registry.

    No route is opened here.  The returned ``RegistryLaunch.run`` is the explicit
    model-executing boundary.
    """

    plan_candidate = Path(operator_plan).expanduser()
    if plan_candidate.is_symlink():
        raise ServingPreparationError("operator_plan_invalid")
    try:
        plan = plan_candidate.resolve(strict=True)
    except OSError as exc:
        raise ServingPreparationError("operator_plan_invalid") from exc
    _validate_operator_plan(plan, prepared)
    plan_digest = _plan_digest(plan)
    host_ids = _canonical_host_ids(plan)
    seed, seed_binding = _bind_existing_seed(plan, seed_state_root)
    if _plan_digest(plan) != plan_digest:
        raise ServingPreparationError("operator_plan_changed")
    root = _private_directory(Path(private_state_root))

    product = _private_directory(root / "product")
    registry_state = root / "registry-state.json"
    if registry_state.is_symlink() or (
        registry_state.exists() and not registry_state.is_file()
    ):
        raise ServingPreparationError("registry_state_unsafe")
    return RegistryLaunch(
        prepared=prepared,
        operator_plan=plan,
        private_state_root=root,
        registry_state=registry_state.resolve(),
        seed_state_root=seed,
        product_state_root=product,
        operator_plan_digest=plan_digest,
        physical_host_ids=host_ids,
        seed_binding=seed_binding,
        _runner=runner or _native_registry_runner,
        session_factory=session_factory,
    )


def _validate_execution_approval(
    approval: ExecutionApproval | None, prepared: PreparedSource
) -> ExecutionApproval:
    proposal = prepared.resource_proposal
    if not isinstance(approval, ExecutionApproval):
        raise ServingPreparationError("model_execution_approval_required")
    if (
        approval.model_id != prepared.model.model_id
        or approval.resolved_commit != prepared.model.resolved_commit
        or approval.source_representation != prepared.model.source_representation
        or approval.runtime_dtype != prepared.model.runtime_dtype
        or approval.runtime_quantization != prepared.model.runtime_quantization
        or approval.max_execution_download_bytes != proposal.execution_download_bytes
        or approval.max_additional_disk_bytes < proposal.additional_disk_bytes
        or approval.max_combined_rss_bytes < proposal.max_combined_rss_bytes
        or approval.max_wall_seconds < proposal.max_wall_seconds
        or approval.run_count != proposal.run_count
    ):
        raise ServingPreparationError("model_execution_approval_mismatch")
    return approval


def _validate_dynamic_port(port: int) -> None:
    if type(port) is not int or port in _PROTECTED_PORTS:
        raise ServingPreparationError("port_not_available_for_a13")
    if not _MIN_DYNAMIC_PORT <= port <= 65_535:
        raise ServingPreparationError("dynamic_loopback_port_required")


def _assert_loopback_port_free(port: int) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", port))
    except OSError as exc:
        raise ServingPreparationError("dynamic_loopback_port_unavailable") from exc


__all__ = [
    "CANDIDATE_MODEL",
    "EXECUTED_NATIVE_MODEL",
    "ExecutionApproval",
    "ModelContract",
    "PreparedSource",
    "RESOURCE_PROPOSAL",
    "RegistryLaunch",
    "ResourceProposal",
    "ServingPreparationError",
    "prepare_local_source",
    "prepare_registry_launch",
]
