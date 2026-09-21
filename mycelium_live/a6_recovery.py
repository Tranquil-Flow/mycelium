# SPDX-License-Identifier: AGPL-3.0-or-later
"""Qualifier-owned A6 compatibility and physical successor challenge.

This module is used by the ordinary physical server.  It never consumes a
browser-authored success flag: eligibility is derived from an exact physical
startup challenge on the already-reserved successor placements.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import json
from pathlib import Path
import time
from typing import Any
import uuid

from mycelium_a6_m16 import recovery_successor_binding
from mycelium_a6_recovery import (
    ReplayCheckpoint,
    build_recovery_qualification,
    compatibility_matches,
    recovery_request_binding,
)
from mycelium_router.serialization import execution_graph_to_dict


_TOKENIZER_FILES = (
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
    "vocab.json",
    "merges.txt",
)


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def tokenizer_artifact_digest(deployment_dir: Path) -> str:
    """Bind every tokenizer artifact name, byte length, and exact SHA-256."""

    root = Path(deployment_dir)
    records: list[dict[str, object]] = []
    for name in _TOKENIZER_FILES:
        path = root / name
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("a6_tokenizer_artifact_unsafe")
        records.append(
            {"name": name, "size": path.stat().st_size, "digest": _file_digest(path)}
        )
    if not records or not any(
        record["name"] in {"tokenizer.json", "vocab.json"} for record in records
    ):
        raise ValueError("a6_tokenizer_artifact_unavailable")
    return _digest(records)


def _configuration(deployment_dir: Path) -> Mapping[str, Any]:
    path = Path(deployment_dir) / "config.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("a6_model_configuration_unsafe")
    try:
        document = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("a6_model_configuration_invalid") from exc
    if not isinstance(document, Mapping):
        raise ValueError("a6_model_configuration_invalid")
    return document


def _graph_digest(graph: Any) -> str:
    try:
        return _digest(execution_graph_to_dict(graph))
    except (AttributeError, TypeError, ValueError):
        # Narrow fallback supports deterministic probes with reduced graph
        # doubles. Production ExecutionGraph objects always take the exact
        # serialization path above.
        return _digest(
            {
                "deployment_id": graph.deployment_id,
                "deployment_epoch": graph.deployment_epoch,
                "topology_version": graph.topology_version,
                "model_id": graph.model_id,
                "resolved_commit": graph.resolved_commit,
                "manifest_digest": graph.manifest_digest,
            }
        )


def _compatibility(
    *,
    graph: Any,
    checkpoint: ReplayCheckpoint,
    deployment_dir: Path,
    tokenizer_digest: str,
    parity_policy_digest: str,
    workload_envelope_digest: str,
) -> dict[str, Any]:
    config = _configuration(deployment_dir)
    tokenizer_inventory = tokenizer_artifact_digest(deployment_dir)
    special_policy = {
        key: config.get(key)
        for key in (
            "bos_token_id",
            "eos_token_id",
            "pad_token_id",
            "decoder_start_token_id",
            "tie_word_embeddings",
        )
    }
    context_policy = {
        key: config.get(key)
        for key in (
            "max_position_embeddings",
            "sliding_window",
            "use_sliding_window",
            "rope_scaling",
        )
    }
    return {
        "model_id": graph.model_id,
        "model_revision": graph.resolved_commit,
        "representation_digest": checkpoint.representation_digest,
        "manifest_digest": graph.manifest_digest,
        "tokenizer_digest": tokenizer_digest,
        "vocabulary_digest": tokenizer_inventory,
        "special_token_policy_digest": _digest(special_policy),
        "architecture_digest": _digest(config),
        "context_policy_digest": _digest(context_policy),
        "output_semantics_digest": _digest(
            {
                "decode": "gateway_contiguous_token_positions",
                "replay_prefix_publication": "suppressed",
                "sampling": "request_generation_settings_bound",
            }
        ),
        "generation_settings_digest": checkpoint.generation_settings_digest,
        "qualification_parity_policy_digest": parity_policy_digest,
        "workload_envelope_digest": workload_envelope_digest,
    }


class LiveA6RecoveryQualifier:
    """Freshly qualify one exact immutable successor reservation."""

    def __init__(
        self,
        *,
        graph: Any,
        deployment_dir: Path,
        tokenizer_digest: str,
        route: Any | None = None,
        physical_challenge: Callable[..., Mapping[str, Any]] | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if route is None and physical_challenge is None:
            raise ValueError("a6_physical_challenge_source_required")
        if not isinstance(tokenizer_digest, str) or not tokenizer_digest.startswith("sha256:"):
            raise ValueError("a6_tokenizer_digest_invalid")
        self._graph = graph
        self._deployment_dir = Path(deployment_dir)
        self._tokenizer_digest = tokenizer_digest
        self._route = route
        self._physical_challenge = physical_challenge or self._run_physical_challenge
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1_000))

    def __call__(
        self,
        request_id: str,
        checkpoint: ReplayCheckpoint,
        successor_identity: Mapping[str, Any],
        successor_manifest: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if request_id != checkpoint.request_id:
            raise ValueError("successor_identity_mismatch")
        expected = {
            "request_id": request_id,
            "request_attempt": checkpoint.source_attempt + 1,
            "path_id": successor_manifest.path_id,
            "path_attempt": successor_manifest.path_attempt,
            "path_manifest_digest": successor_manifest.manifest_digest,
            "deployment_id": checkpoint.deployment_id,
            "deployment_epoch": checkpoint.deployment_epoch,
            "topology_generation": checkpoint.topology_generation,
        }
        if any(successor_identity.get(key) != value for key, value in expected.items()):
            raise ValueError("successor_identity_mismatch")
        if (
            successor_manifest.request_id != request_id
            or successor_manifest.deployment_id != checkpoint.deployment_id
            or successor_manifest.deployment_epoch != checkpoint.deployment_epoch
            or successor_manifest.topology_version != checkpoint.topology_generation
            or not successor_manifest.ordered_hops
        ):
            raise ValueError("successor_identity_mismatch")
        if (
            self._graph.deployment_id != checkpoint.deployment_id
            or self._graph.deployment_epoch != checkpoint.deployment_epoch
            or self._graph.topology_version != checkpoint.topology_generation
            or self._graph.model_id != checkpoint.model_id
            or self._graph.manifest_digest != checkpoint.representation_digest
            or checkpoint.tokenizer_digest != self._tokenizer_digest
        ):
            raise ValueError("source_compatibility_binding_mismatch")

        challenge = self._physical_challenge(
            request_id=request_id,
            placement_ids=tuple(
                hop.placement_id for hop in successor_manifest.ordered_hops
            ),
            absolute_deadline_unix_ms=checkpoint.expiry_unix_ms,
        )
        if set(challenge) != {
            "passed",
            "parity_policy_digest",
            "workload_envelope_digest",
        } or not isinstance(challenge["passed"], bool):
            raise ValueError("a6_physical_challenge_result_invalid")
        for key in ("parity_policy_digest", "workload_envelope_digest"):
            value = challenge[key]
            if (
                not isinstance(value, str)
                or len(value) != 71
                or not value.startswith("sha256:")
            ):
                raise ValueError("a6_physical_challenge_result_invalid")

        source = _compatibility(
            graph=self._graph,
            checkpoint=checkpoint,
            deployment_dir=self._deployment_dir,
            tokenizer_digest=self._tokenizer_digest,
            parity_policy_digest=str(challenge["parity_policy_digest"]),
            workload_envelope_digest=str(challenge["workload_envelope_digest"]),
        )
        successor = dict(source)
        passed = bool(challenge["passed"]) and compatibility_matches(source, successor)
        now = self._clock_ms()
        expiry = min(checkpoint.expiry_unix_ms, now + 30_000)
        if expiry <= now:
            raise ValueError("recovery_qualification_expired")
        binding = {
            "deployment_id": self._graph.deployment_id,
            "deployment_epoch": self._graph.deployment_epoch,
            "topology_generation": self._graph.topology_version,
            "model_id": self._graph.model_id,
            "model_revision": self._graph.resolved_commit,
            "representation_digest": self._graph.manifest_digest,
            "graph_digest": _graph_digest(self._graph),
        }
        qualification = build_recovery_qualification(
            binding,
            qualification_id=f"a6-successor-{uuid.uuid4().hex}",
            request_binding=recovery_request_binding(checkpoint),
            successor_binding=recovery_successor_binding(
                successor_identity,
                successor_manifest,
            ),
            compatibility=successor,
            compatible=passed,
            issued_at_unix_ms=now,
            expiry_unix_ms=expiry,
            reason_codes=() if passed else ("successor_physical_challenge_failed",),
            physical_challenge="passed" if passed else "failed",
        )
        return source, qualification

    def _run_physical_challenge(
        self,
        *,
        request_id: str,
        placement_ids: tuple[str, ...],
        absolute_deadline_unix_ms: int,
    ) -> Mapping[str, Any]:
        route = self._route
        assert route is not None
        prompt, expected = route.startup_challenge
        policy_digest = _digest(
            {
                "challenge": "exact_startup_token_parity",
                "expected_output_digest": _digest(list(expected)),
                "placement_count": len(placement_ids),
            }
        )
        workload_digest = _digest(
            {
                "prompt_token_count": len(prompt),
                "maximum_new_tokens": len(expected),
                "absolute_deadline_unix_ms": absolute_deadline_unix_ms,
            }
        )
        emitted: list[int] = []

        class Sink:
            def emit(self, token_index: int, token_id: int) -> None:
                if token_index != len(emitted) or type(token_id) is not int:
                    raise RuntimeError("a6_physical_challenge_noncontiguous")
                emitted.append(token_id)

        probe_id = f"a6-qual-{hashlib.sha256(request_id.encode()).hexdigest()[:16]}-{uuid.uuid4().hex[:8]}"
        passed = False
        try:
            if self._clock_ms() >= absolute_deadline_unix_ms:
                return {
                    "passed": False,
                    "parity_policy_digest": policy_digest,
                    "workload_envelope_digest": workload_digest,
                }
            result = route.infer(
                prompt,
                max_new_tokens=len(expected),
                request_id=probe_id,
                sink=Sink(),
                selected_placement_ids=placement_ids,
            )
            passed = tuple(result.token_ids) == tuple(expected) and tuple(emitted) == tuple(
                expected
            )
        except Exception:
            passed = False
        finally:
            try:
                route.release_request(probe_id)
            except Exception:
                passed = False
        return {
            "passed": passed,
            "parity_policy_digest": policy_digest,
            "workload_envelope_digest": workload_digest,
        }


__all__ = ["LiveA6RecoveryQualifier", "tokenizer_artifact_digest"]
