# SPDX-License-Identifier: AGPL-3.0-only
"""A11 node wiring: the staged draft binding of one entry node.

A node runs speculative rounds only when its own artifact root stages an A11 draft
binding (``a11/<node_id>/node.json`` from a transfer bundle, or ``a11/node.json`` for
a single node; both is ambiguous), exactly like the A7 node configuration.  The
binding names the exact draft artifact the owner authorised -- model id, resolved
commit and the SHA-256 of every weight file -- under the node's artifact root.
Discovery of a local model grants nothing: a missing or mismatched binding leaves
the node target-only.

The node measures its own warm target-only baseline: the per-token wall time of
ordinary target-only decode rounds on the exact route it serves.  Speculation is
not admitted until enough rounds were observed (``minimum_baseline_samples``),
unless the binding freezes an operator-measured baseline.

Claim boundary: configuration and wiring only.  Nothing here qualifies a draft,
advertises a capability or claims benefit.
"""

from __future__ import annotations

import collections
import re
import statistics
import threading
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

PROTOCOL = "mycelium.a11_node_draft.v1"
NODE_CONFIG_PATH = "a11/node.json"
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_TOP_FIELDS = frozenset({"protocol", "deployment_id", "node_id", "draft", "policy"})
_DRAFT_FIELDS = frozenset({"model_dir", "model_id", "resolved_commit", "expected_sha256", "quantize_bits"})
_POLICY_FIELDS = frozenset({"width", "warm_target_tpot_ms", "minimum_baseline_samples"})
BASELINE_WINDOW = 256


class A11WiringError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> None:
    raise A11WiringError(code)


def node_config_path_for(node_id: str) -> str:
    if not isinstance(node_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", node_id):
        _fail("invalid_a11_node_id")
    return f"a11/{node_id}/node.json"


def _relative(value: Any) -> str:
    if not isinstance(value, str) or not value:
        _fail("invalid_a11_model_dir")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        _fail("invalid_a11_model_dir")
    return value


def validate_node_config(document: Any, *, deployment_id: str, node_id: str) -> dict[str, Any]:
    if not isinstance(document, Mapping) or set(document) != _TOP_FIELDS:
        _fail("invalid_a11_node_config")
    if document["protocol"] != PROTOCOL:
        _fail("invalid_a11_node_config")
    if document["deployment_id"] != deployment_id or document["node_id"] != node_id:
        _fail("a11_node_config_binding_mismatch")
    draft = document["draft"]
    if not isinstance(draft, Mapping) or set(draft) != _DRAFT_FIELDS:
        _fail("invalid_a11_draft_binding")
    _relative(draft["model_dir"])
    if not isinstance(draft["model_id"], str) or not draft["model_id"]:
        _fail("invalid_a11_draft_binding")
    if not isinstance(draft["resolved_commit"], str) or not _HEX40.fullmatch(draft["resolved_commit"]):
        _fail("invalid_a11_draft_binding")
    digests = draft["expected_sha256"]
    if (
        not isinstance(digests, Mapping)
        or not digests
        or any(not isinstance(k, str) or "/" in k or not isinstance(v, str) or not _HEX64.fullmatch(v) for k, v in digests.items())
    ):
        _fail("invalid_a11_draft_binding")
    if draft["quantize_bits"] not in (None, 4, 8):
        _fail("invalid_a11_draft_binding")
    policy = document["policy"]
    if not isinstance(policy, Mapping) or set(policy) != _POLICY_FIELDS:
        _fail("invalid_a11_policy")
    width = policy["width"]
    if isinstance(width, bool) or not isinstance(width, int) or not 1 <= width <= 16:
        _fail("invalid_a11_policy")
    warm = policy["warm_target_tpot_ms"]
    if warm is not None:
        if (
            not isinstance(warm, Mapping)
            or set(warm) != {"p50", "p95"}
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < float(v) < 1e7 for v in warm.values())
            or float(warm["p50"]) > float(warm["p95"])
        ):
            _fail("invalid_a11_policy")
    samples = policy["minimum_baseline_samples"]
    if isinstance(samples, bool) or not isinstance(samples, int) or not 1 <= samples <= BASELINE_WINDOW:
        _fail("invalid_a11_policy")
    return {
        "protocol": PROTOCOL,
        "deployment_id": deployment_id,
        "node_id": node_id,
        "draft": {**dict(draft), "expected_sha256": dict(digests)},
        "policy": {**dict(policy), "warm_target_tpot_ms": None if warm is None else dict(warm)},
    }


class TargetOnlyBaseline:
    """Rolling per-token wall time of ordinary target-only decode rounds."""

    def __init__(self, minimum_samples: int) -> None:
        self._minimum = minimum_samples
        self._samples: collections.deque[float] = collections.deque(maxlen=BASELINE_WINDOW)
        self._lock = threading.Lock()

    def record(self, per_token_ms: float) -> None:
        if per_token_ms > 0:
            with self._lock:
                self._samples.append(float(per_token_ms))

    def measured(self) -> tuple[float, float] | None:
        with self._lock:
            data = sorted(self._samples)
        if len(data) < self._minimum:
            return None
        p95 = data[min(len(data) - 1, int(round(0.95 * (len(data) - 1))))]
        return statistics.median(data), p95

    def count(self) -> int:
        with self._lock:
            return len(self._samples)


class A11NodeWiring:
    """The loaded draft plus the policy; ``attach`` binds a controller to a router."""

    def __init__(self, config: Mapping[str, Any], model: Any) -> None:
        from mycelium_a11_route_speculation import RouteSpeculationController, RouteSpeculationPolicy

        self.config = config
        self.model = model
        warm = config["policy"]["warm_target_tpot_ms"]
        self.baseline = TargetOnlyBaseline(config["policy"]["minimum_baseline_samples"])
        identity = model.identity_digest()
        self.controller = RouteSpeculationController(
            policy=RouteSpeculationPolicy(
                width=config["policy"]["width"],
                warm_target_tpot_p95_ms=None if warm is None else float(warm["p95"]),
                warm_target_tpot_p50_ms=None if warm is None else float(warm["p50"]),
                draft_identity_digest=identity,
            ),
            draft_factory=self._draft,
            runtime_incarnation=identity,
            baseline=self.baseline.measured if warm is None else None,
        )

    def _draft(self, request_id: str) -> Any:
        from mycelium_a11_draft_runtime import MLXDraftRuntime

        return MLXDraftRuntime(self.model, incarnation=self.model.identity_digest(), kv_ref=f"draft-kv:{request_id}")

    def attach(self, router: Any) -> None:
        router.entry.a11_controller = self.controller

    def set_preference(self, request_id: str, preference: str) -> None:
        self.controller.set_preference(request_id, preference)

    def note_decode_round(self, request_id: str, *, elapsed_ms: float, tokens: int) -> None:
        """Feed the warm baseline from ordinary target-only rounds only."""

        admission = self.controller.admission(request_id)
        if tokens == 1 and (admission is None or admission["mode"] == "target_only"):
            self.baseline.record(elapsed_ms)

    def snapshot(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL,
            "draft_identity_digest": self.model.identity_digest(),
            "width": self.config["policy"]["width"],
            "baseline_samples": self.baseline.count(),
            "baseline_measured": self.baseline.measured() is not None,
            "completed": list(self.controller.completed[-8:]),
        }


def load_node_wiring(artifact_root: Path, config: Mapping[str, Any], *, load_model: Any = None) -> A11NodeWiring:
    """Load the exact authorised draft under ``artifact_root`` and build the wiring."""

    root = Path(artifact_root).resolve(strict=True)
    model_dir = (root / config["draft"]["model_dir"]).resolve(strict=True)
    if root not in model_dir.parents or not model_dir.is_dir():
        _fail("invalid_a11_model_dir")
    if load_model is None:
        from mycelium_a11_draft_runtime import DraftRuntimeError, Qwen2DraftModel

        def load_model(path: Path, *, expected_sha256: Mapping[str, str], quantize_bits: int | None) -> Any:
            try:
                return Qwen2DraftModel.from_snapshot(path, expected_sha256=expected_sha256, quantize_bits=quantize_bits)
            except DraftRuntimeError as error:
                raise A11WiringError(error.code) from None

    model = load_model(
        model_dir,
        expected_sha256=config["draft"]["expected_sha256"],
        quantize_bits=config["draft"]["quantize_bits"],
    )
    return A11NodeWiring(config, model)


__all__ = [
    "A11NodeWiring",
    "A11WiringError",
    "NODE_CONFIG_PATH",
    "PROTOCOL",
    "TargetOnlyBaseline",
    "load_node_wiring",
    "node_config_path_for",
    "validate_node_config",
]
