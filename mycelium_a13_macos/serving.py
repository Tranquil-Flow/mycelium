# SPDX-License-Identifier: AGPL-3.0-only
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


#: Owner decision D-1 as RELAXED on 2026-10-13 for the coworking demo.
D1_MODEL_BYTES_RULE = (
    "D-1 (relaxed 2026-10-13): for a Qwen3 demo model whose pinned bf16 source "
    "weights total more than SLICE_FETCH_MIN_SOURCE_BYTES and at most "
    "SLICE_FETCH_MAX_SOURCE_BYTES, each member Mac fetches "
    "ONLY its assigned layer slice of the pinned revision (ranged HTTPS), every "
    "byte verified against the owner-compiled slice plan digests; never the whole "
    "model. Each member's slice download and disk are bounded by its share in the "
    "per-model, per-split resource proposal (model_resources.py); a model without a "
    "registered resource derivation is never slice-fetched. A smaller Qwen3 model "
    "goes as a whole snapshot to each Mac, verified against its pinned digests "
    "(slicing it saves almost nothing). Other models (the Qwen2.5 7B candidate) "
    "keep the original D-1 rule: weight bytes are pre-staged and never fetched by "
    "the app."
)
#: Slice-fetch window on the bf16 SOURCE bytes: Qwen3 8B..14B slice (Qwen3-14B
#: ~29.5 GB bf16 source, ~14.8 GB int8 at runtime); 32B does not fit; 0.6B
#: (~1.5 GB) goes whole.
SLICE_FETCH_MIN_SOURCE_BYTES = 4_000_000_000
SLICE_FETCH_MAX_SOURCE_BYTES = 32_000_000_000
SLICE_FETCH_ARCHITECTURES = frozenset({"qwen3"})


def slice_fetch_permitted(source_weight_bytes: object, *, architecture: object,
                          model: object = None) -> bool:
    """Whether the relaxed D-1 slice rule applies to this model.

    With ``model`` the contract must also have a registered per-model resource
    derivation (``model_resources.MODEL_SHAPES``): that is what bounds each
    member's slice download and disk.
    """

    if model is not None:
        from .model_resources import model_shape
        if model_shape(model) is None:
            return False
    return (
        architecture in SLICE_FETCH_ARCHITECTURES
        and type(source_weight_bytes) is int
        and SLICE_FETCH_MIN_SOURCE_BYTES < source_weight_bytes <= SLICE_FETCH_MAX_SOURCE_BYTES
    )


#: The Qwen2.5-7B candidate's measured proposal (2026-09-16/19).  No longer the
#: one global bound: every contract derives its own via ``ModelContract.
#: resource_proposal`` / ``model_resources`` (per model, and per compiled split
#: for a run).  ``model_resources.MEASURED_ANCHORS`` reproduces these exact
#: numbers for the 7B contract; tests pin them.
RESOURCE_PROPOSAL = ResourceProposal(
    # D-1 (owner, 2026-09-16; relaxed 2026-10-13 for Qwen3 demo models, see
    # D1_MODEL_BYTES_RULE): for THIS Qwen2.5 7B proposal model bytes are still
    # provisioned from the LAN seed / retained state and are NEVER fetched by
    # the app (not a slice-fetch architecture).  Qwen3 demo
    # members instead fetch only their own digest-pinned slice
    # (mycelium_a13_macos/slice_acquisition.py), budgeted by the slice plan's
    # per-member ``download_bytes``, never by this whole-model allowance.
    # The network allowance below is therefore only the pinned 7B snapshot's
    # non-weight metadata/text assets,
    # 11,535,382 B = .gitattributes 1,519 + LICENSE 11,343 + README.md 6,240 +
    # config.json 663 + generation_config.json 243 + merges.txt 1,671,839 +
    # model.safetensors.index.json 27,752 + tokenizer.json 7,031,645 +
    # tokenizer_config.json 7,305 + vocab.json 2,776,833.  The 15,231,271,888 B
    # of bf16 safetensors shards must already be staged: with this budget a
    # missing shard refuses (download_limit_exceeded) before any byte is
    # requested, instead of silently downloading it (ERRATA-1).  Derivation:
    # runs/wp8-02-g1/g2-derivation.json.
    network_download_bytes=11_535_382,
    # Both native compiler/provisioner paths are local-files-only.
    execution_download_bytes=0,
    # Balanced-split additional disk.  Two binding quantities:
    #   laptop storage grant: its seeded 7B source snapshot 15,242,807,270 B
    #     (+ its [0,14)+input_embedding stage copy 7,810,167,792 B on disk);
    #   m4pro worker stage: its [14,28)+final_norm+lm_head stage copy
    #     11,285,830,448 B + non-weight deployment assets (~11,505,918 B proxy)
    #     = 11,297,336,366 B.
    # The runner derives each worker's disk limit as this value / 2, so the
    # constant carries 10% headroom over the m4pro worker need:
    #   2 x ceil(1.10 x 11,297,336,366) = 24,854,140,006 B.
    # (The components-only 23,052,975,062 B left the runner-derived m4pro limit
    # only ~229 MB / 2.0% slack - receipt section 8; raised 2026-09-16 before the
    # first 7B round.  Covering file sets measured on the real 7B shard index;
    # the same numbers Tranche B derived for the app route's balanced split.)
    additional_disk_bytes=24_854_140_006,
    # Load-peak bound.  The guard measures the worker process GROUP (app + worker),
    # not the worker alone, so the bound must cover the group peak.
    # Original derivation (worker-only, the modelling error): heaviest host stage
    #   load peak 4,692,854,784 B ([14,28)+final_norm+lm_head = 3,811,704,832 B
    #   resident weights + 881,149,952 B streaming transient) + 512 MiB workspace
    #   = 5,229,725,696 B; x2 (the runner divides this proposal by two for the
    #   worker limits) = 10,459,451,392 B; +10% = 11,505,396,531 B.
    # MEASURED 2026-09-16 by the guard's own self-report (rounds 66r1/67r1): the
    #   group peaked at 5,885,624,320 B against the runner-derived per-node limit
    #   of 5,752,698,265 B -- over by only 132,926,055 B.  So the worker-only
    #   estimate was short by ~625 MB, which is the app process's own resident
    #   footprint during the load peak: real, bounded, and previously unmodelled.
    # Rebuilt from the measurement: 2 x ceil(1.15 x 5,885,624,320) = 13,536,935,936 B.
    # MEASURED AGAIN 2026-09-19 (round p76r1, preview.76) by the same self-report, on the
    #   owner node: observed_group_rss_bytes 7,371,915,264 against the then-current
    #   per-node half of 6,768,467,968 -- over by 603,447,296 B.  That trip is why the
    #   run-level `configure` could not be answered, the pump honestly acked
    #   reconciliation_required, and the deployment was marked unavailable (ERRATA-17).
    #   A 7B int8 model's resident weights alone are ~7 GB, so the 09-16 derivation was
    #   sized for a lighter tranche rather than exposing a leak.
    # Rebuilt from the newer measurement by the same recipe:
    #   2 x ceil(1.15 x 7,371,915,264) = 16,955,405,108 B -- 8.48 GB per node, which the
    #   16 GiB laptop can afford and the 48 GiB owner node comfortably so.
    max_combined_rss_bytes=16_955_405_108,
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
        """This contract's model-level proposal (an upper bound for any member).

        Derived by ``model_resources`` from the registered model shape; the 7B
        candidate returns its measured ``RESOURCE_PROPOSAL`` exactly.  A run
        replaces it with the per-split proposal once the split is compiled.
        """
        from .model_resources import model_resource_proposal, model_shape
        if model_shape(self) is None:
            return RESOURCE_PROPOSAL
        return model_resource_proposal(self)

    @property
    def architecture(self) -> str:
        """The manifest runtime architecture this exact revision must compile to.

        A property, not a field: ``asdict(model)`` is the model identity carried
        in acquisition/status documents and must stay unchanged.
        """

        return _MODEL_ARCHITECTURES.get(self.model_id, "qwen2")


# Owner-directed useful-model composition (wp8-02 G1): the A13 app is now a
# 7B vehicle.  Every field is locally evidenced: the pinned HF-cache snapshot
# models--Qwen--Qwen2.5-7B-Instruct/snapshots/a09a35458c...(14 files, 15,242,807,270 B,
# 28 decoder layers), the WP7-1/A3 records, and the Tranche B route derivation.
CANDIDATE_MODEL = ModelContract(
    model_id="Qwen/Qwen2.5-7B-Instruct",
    resolved_commit="a09a35458c702b33eeacc393d103063234e8bc28",
    source_representation="bfloat16-safetensors",
    runtime_backend="mlx",
    runtime_dtype="float32",
    runtime_quantization="int8-weight-only",
)

# What the checked-in native end-to-end evidence actually exercised: the wp8-01
# two-Mac round ran the 0.5B contract.  It no longer matches the selected
# candidate — wp8-02's fresh 7B run is what will replace this record; historic
# evidence is not fresh demo proof.
EXECUTED_NATIVE_MODEL = ModelContract(
    model_id="Qwen/Qwen2.5-0.5B-Instruct",
    resolved_commit="7ae557604adf67be50417f59c2c2f167def9a775",
    source_representation="bfloat16-safetensors",
    runtime_backend="mlx",
    runtime_dtype="float32",
    runtime_quantization="int8-weight-only",
)

# A9 two-host M4 Pro + Windows route (A9 spec section 3.4): the exact 0.5B
# revision in int8-weight-only.  ``runtime_backend`` is only the default; each
# node runs the stage runtime of its managed peer class (mlx on macOS, numpy
# on Windows), carried per node by ``node_runtime_backends``.
A9_ROUTE_MODEL = ModelContract(
    model_id="Qwen/Qwen2.5-0.5B-Instruct",
    resolved_commit="7ae557604adf67be50417f59c2c2f167def9a775",
    source_representation="bfloat16-safetensors",
    runtime_backend="mlx",
    runtime_dtype="float32",
    runtime_quantization="int8-weight-only",
)

# Qwen3 dense stepping stone toward the qwen3_5 hybrid models: the pinned
# Qwen/Qwen3-0.6B revision (28 layers, hidden 1024, 16 query x 128 = 2048-wide
# attention, 8 KV heads, tied embeddings; one 1,503,300,328 B bf16
# model.safetensors).  The small registered Qwen3 for tests and bring-up: a
# two-stage MLX route over this exact revision reproduces a single-process NumPy
# reference token for token (float32 and int8-weight-only).  Opt-in only
# (``MYCELIUM_A13_RUN_MODEL=qwen3-0.6b``); never the default.  Its prompt codec
# closes an empty think block (thinking off).  Its resource proposal is derived
# per model and per split (model_resources.py).
QWEN3_0_6B_MODEL = ModelContract(
    model_id="Qwen/Qwen3-0.6B",
    resolved_commit="c1899de289a04d12100db370d81485cdf75e47ca",
    source_representation="bfloat16-safetensors",
    runtime_backend="mlx",
    runtime_dtype="float32",
    runtime_quantization="int8-weight-only",
)

# Coworking-demo Qwen3 dense sizes, registered the same way as 0.6B: the exact
# HF revision (main on 2026-10-13, read from the HF API), bf16 source, MLX,
# float32 activations with int8 rowwise weights (the qualified Qwen mode).
# Qwen3-8B: 36 layers, hidden 4096, 32 q x 128 / 8 KV heads, 5 bf16 shards,
# snapshot 16,397,461,266 B.  Qwen3-14B: 40 layers, hidden 5120, 40 q x 128 /
# 8 KV heads, 8 bf16 shards, snapshot 29,552,613,776 B.  Both have square
# attention.  Their resource proposals are derived per model and per compiled
# split (model_resources.py), not from the 7B RESOURCE_PROPOSAL.
QWEN3_8B_MODEL = ModelContract(
    model_id="Qwen/Qwen3-8B",
    resolved_commit="b968826d9c46dd6066d109eabc6255188de91218",
    source_representation="bfloat16-safetensors",
    runtime_backend="mlx",
    runtime_dtype="float32",
    runtime_quantization="int8-weight-only",
)
QWEN3_14B_MODEL = ModelContract(
    model_id="Qwen/Qwen3-14B",
    resolved_commit="40c069824f4251a91eefaf281ebe4c544efd3e18",
    source_representation="bfloat16-safetensors",
    runtime_backend="mlx",
    runtime_dtype="float32",
    runtime_quantization="int8-weight-only",
)

#: Every file of the pinned Qwen3-14B revision: (size bytes, sha256).  The
#: safetensors digests are the HF LFS sha256 of revision 40c06982...; the small
#: files were checked against their HF git blob ids and hashed locally.  Every
#: entry was verified once against the local snapshot (2026-10-13, nice -n 10;
#: .gitattributes and README.md, which the lead's download skipped, were fetched
#: at the pinned revision).  Total 29,552,613,776 B.
QWEN3_14B_SNAPSHOT_FILES: dict[str, tuple[int, str]] = {
    '.gitattributes': (1570, 'sha256:34448b82c17d60fec9b65b1f093c115ddbaadc04beb1b0140b6bfed2e012a930'),
    'LICENSE': (11343, 'sha256:832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e'),
    'README.md': (16670, 'sha256:7ef503e68996a4e32007fedc7d8e198846e9b1f36620d38a79b89934100b1250'),
    'config.json': (728, 'sha256:e73c3664ca09b10a673fef0c22e8a6b456201d49bd4713c9691f775720e8857a'),
    'generation_config.json': (239, 'sha256:2325da0f15bb848e018c5ae071b7943332e9f871d6b60e2ed22ca97d4cb993d2'),
    'merges.txt': (1671853, 'sha256:8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5'),
    'model-00001-of-00008.safetensors': (3841788544, 'sha256:e942bdbdf08857d16a8fef7d1dae9fceabeb4e84def6043485fe2f6f085dab0e'),
    'model-00002-of-00008.safetensors': (3963750816, 'sha256:f7c9c6eee628f5ad831d2d1d292e120505e5fcadeb38f88b4d3c4cb86306ccf9'),
    'model-00003-of-00008.safetensors': (3963750880, 'sha256:dfb8c5df9404b41ad6ae74e8b6b367135f017b4467b884cf71b17c71954f18a9'),
    'model-00004-of-00008.safetensors': (3963750880, 'sha256:eab286fec759e3e59ab228621aefa0fef14ed56039e06f959e67257d5af7604d'),
    'model-00005-of-00008.safetensors': (3963750880, 'sha256:97f0dc2992e59da95c466eff6f4fd0c8335843bbc36ed5c913a6f5150748c0e6'),
    'model-00006-of-00008.safetensors': (3963750880, 'sha256:9e8e76a013cd5e253865b792991e0b410f869b136b3c500079b531b09198e99e'),
    'model-00007-of-00008.safetensors': (3963750880, 'sha256:0aee70ee6e91dc00d818804fb47f124d13ee4ad5b4a64553e09dbf9391cd5750'),
    'model-00008-of-00008.safetensors': (1912371880, 'sha256:0d6b92296e326d39bbbaeb32c3ec454ac606da843d4c8ffa8edf010b62b8c9e0'),
    'model.safetensors.index.json': (36514, 'sha256:62d7ad35757bae5e7baa452cb1483178b7daa50e869e923226b8da10871f7ebc'),
    'tokenizer.json': (11422654, 'sha256:aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4'),
    'tokenizer_config.json': (9732, 'sha256:d5d09f07b48c3086c508b30d1c9114bd1189145b74e982a265350c923acd8101'),
    'vocab.json': (2776833, 'sha256:ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910'),
}

_MODEL_ARCHITECTURES = {
    QWEN3_0_6B_MODEL.model_id: "qwen3",
    QWEN3_8B_MODEL.model_id: "qwen3",
    QWEN3_14B_MODEL.model_id: "qwen3",
}

#: Every model a run may pin.  A run selects exactly one; nothing else loads.
REGISTERED_MODELS: tuple[ModelContract, ...] = (
    CANDIDATE_MODEL,
    A9_ROUTE_MODEL,
    QWEN3_0_6B_MODEL,
    QWEN3_8B_MODEL,
    QWEN3_14B_MODEL,
)
#: Stage runtimes a node may run for a registered model.
STAGE_RUNTIME_BACKENDS = frozenset({"mlx", "numpy"})


RUN_MODEL_ENVIRONMENT = "MYCELIUM_A13_RUN_MODEL"
_RUN_MODEL_NAMES = {
    "candidate": CANDIDATE_MODEL,
    "a9-route": A9_ROUTE_MODEL,
    "qwen3-0.6b": QWEN3_0_6B_MODEL,
    "qwen3-8b": QWEN3_8B_MODEL,
    "qwen3-14b": QWEN3_14B_MODEL,
}


def selected_run_model(environ: Mapping[str, str] | None = None) -> ModelContract:
    """The model this installation's runs pin (default: the A13 candidate).

    ``MYCELIUM_A13_RUN_MODEL=a9-route`` selects the A9 two-host route model.
    Any other value is refused, never silently defaulted.
    """

    import os

    value = (os.environ if environ is None else environ).get(RUN_MODEL_ENVIRONMENT, "candidate")
    if value not in _RUN_MODEL_NAMES:
        raise ServingPreparationError("run_model_selection_invalid")
    return _RUN_MODEL_NAMES[value]


def registered_model_contract(model_id: object, resolved_commit: object) -> ModelContract:
    """The registered contract for this exact model revision, or refuse."""

    for model in REGISTERED_MODELS:
        if model.model_id == model_id and model.resolved_commit == resolved_commit:
            return model
    raise ServingPreparationError("model_contract_unregistered")


def node_runtime(model: ModelContract, backend: str) -> dict[str, str]:
    """The exact assignment runtime of one node for a registered model."""

    if model not in REGISTERED_MODELS or backend not in STAGE_RUNTIME_BACKENDS:
        raise ServingPreparationError("node_runtime_invalid")
    return {
        "backend": backend,
        "dtype": model.runtime_dtype,
        "quantization": model.runtime_quantization,
    }


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
    external_session_factory: Any = field(default=None, repr=False, compare=False)
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
            stop_event=None, on_started=None, on_challenge_event=None, on_startup_failure=None) -> int:
        """Execute the real native startup chain and block in its supervisor.

        Calling this method loads/probes the model.  Source preparation never
        calls it.  The native supervisor owns startup cleanup and signal drain.
        ``api_only=True`` serves the existing request/status/stream routes
        without any static UI build; static routes then fail closed.
        """

        import threading
        if ((stop_event is not None and not isinstance(stop_event,threading.Event))
                or (on_started is not None and not callable(on_started))
                or (on_challenge_event is not None and not callable(on_challenge_event))
                or (on_startup_failure is not None and not callable(on_startup_failure))):
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
            **({"on_challenge_event": on_challenge_event} if on_challenge_event is not None else {}),
            **({"on_startup_failure": on_startup_failure} if on_startup_failure is not None else {}),
            **({"session_factory": self.session_factory} if self.session_factory is not None else {}),
            **({"external_session_factory": self.external_session_factory} if self.external_session_factory is not None else {}),
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
        config.get("model_type") != model.architecture
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
        or manifest.get("runtime_model", {}).get("architecture") != model.architecture
        or len(manifest_digest) != 71
    ):
        raise ServingPreparationError("native_manifest_identity_mismatch")
    return PreparedSource(
        model=model,
        source_root=root,
        source_bytes=source_bytes,
        manifest=manifest,
        manifest_digest=manifest_digest,
        resource_proposal=model.resource_proposal,
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


def _validate_operator_plan(
    plan_path: Path,
    prepared: PreparedSource,
    node_runtime_backends: Mapping[str, str] | None = None,
) -> None:
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
        backend = (
            prepared.model.runtime_backend
            if node_runtime_backends is None
            else node_runtime_backends.get(node_id)
        )
        try:
            expected_runtime = node_runtime(prepared.model, backend)  # type: ignore[arg-type]
        except ServingPreparationError:
            raise ServingPreparationError("operator_plan_representation_mismatch") from None
        if (
            assignment.get("model_id") != prepared.model.model_id
            or assignment.get("resolved_commit") != prepared.model.resolved_commit
            or assignment.get("manifest_digest") != prepared.manifest_digest
            or assignment.get("node_id") != node_id
            or not isinstance(runtime, Mapping)
            or {key: runtime.get(key) for key in expected_runtime} != expected_runtime
        ):
            raise ServingPreparationError("operator_plan_representation_mismatch")
    if len(node_ids) != len(nodes):
        raise ServingPreparationError("operator_plan_duplicate_node")
    if node_runtime_backends is not None and set(node_runtime_backends) != node_ids:
        raise ServingPreparationError("operator_plan_representation_mismatch")
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
    external_session_factory=None,
    node_runtime_backends: Mapping[str, str] | None = None,
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
    if prepared.model not in REGISTERED_MODELS:
        raise ServingPreparationError("model_contract_unregistered")
    _validate_operator_plan(plan, prepared, node_runtime_backends)
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
        external_session_factory=external_session_factory,
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
    "A9_ROUTE_MODEL",
    "CANDIDATE_MODEL",
    "QWEN3_0_6B_MODEL",
    "REGISTERED_MODELS",
    "STAGE_RUNTIME_BACKENDS",
    "node_runtime",
    "registered_model_contract",
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
