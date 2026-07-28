#!/usr/bin/env python3
"""Pure deterministic compile of physical inference run-plans for the current controller.

The compiled run-plan is designed to be accepted by
QualificationController._validate_run_plan and to supply the exact configure
shape expected by PhysicalNodeService._configure (stage-pack variant).

This module reads NO files, launches NO processes, stages NO bytes, and
invents NO identities.  It is entirely fail-closed.
"""

from __future__ import annotations

import copy
import math
import re
from pathlib import PurePosixPath
from typing import Any

_RUN_PLAN_PROTOCOL = "mycelium.controller_run_plan.v1"
_EXECUTION_GRAPH_PROTOCOL = "mycelium.execution_graph.v1"
_DEVICE_STATE_PROTOCOL = "mycelium.device_state.v1"
_MAX_DECODE_COUNT = 127

# Must exactly match controller's _SEGMENT_RE:
#   re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Must exactly match controller's _DIGEST_RE:
#   re.compile(r"^sha256:[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# Bounds for iterative traversal to prevent hangs/RecursionError
_MAX_TRAVERSAL_DEPTH = 64
_MAX_TRAVERSAL_ITEMS = 10_000


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _is_non_empty_segment(value: Any) -> bool:
    """Must pass the controller's exact _segment() grammar and length.

    Equivalent to controller _SEGMENT_RE.fullmatch() returning a match:
    ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$
    """
    return isinstance(value, str) and _SEGMENT_RE.fullmatch(value) is not None


def _has_control_char(value: str) -> bool:
    """True if value contains any C0 control, DEL, or non-printable Unicode cc."""
    for ch in value:
        cp = ord(ch)
        # C0 controls (0x00-0x1F) or DEL (0x7F), plus Unicode Cc category
        if cp < 0x20 or cp == 0x7F:
            return True
        # Also reject Unicode Cc category (U+0080-U+009F)
        if 0x80 <= cp <= 0x9F:
            return True
    return False


def _validate_bundle_path(value: str, *, field: str) -> None:
    """Ensure relative, canonical, traversal-free bundle file path."""
    _require(
        isinstance(value, str) and bool(value) and len(value) <= 1024,
        f"{field} must be a non-empty string",
    )
    _require(
        not _has_control_char(value),
        f"{field} must not contain control characters",
    )
    path = PurePosixPath(value)
    _require(
        not path.is_absolute(),
        f"{field} must be relative",
    )
    _require(
        str(path) == value,
        f"{field} must be canonical (no trailing slashes, no dot segments)",
    )
    _require(
        0 < len(path.parts) <= 16,
        f"{field} must have 1-16 path components",
    )
    _require(
        all(part not in {"", ".", ".."} for part in path.parts),
        f"{field} must not contain traversal or empty segments",
    )


def _validate_controller_path(value: str, *, field: str, require_mycelium: bool = False) -> None:
    """Ensure absolute, canonical controller-side path (socket_root, etc.)."""
    _require(
        isinstance(value, str) and len(value) <= 1024,
        f"{field} must be a string",
    )
    _require(
        not _has_control_char(value),
        f"{field} must not contain control characters",
    )
    path = PurePosixPath(value)
    _require(
        path.is_absolute(),
        f"{field} must be absolute",
    )
    _require(
        str(path) == value,
        f"{field} must be canonical",
    )
    _require(
        len(path.parts) >= 3,
        f"{field} must anchor at least 3 levels deep",
    )
    _require(
        all(part not in {"", ".", ".."} for part in path.parts),
        f"{field} must not contain traversal segments",
    )
    if require_mycelium:
        _require(
            any(part.startswith("mycelium") for part in path.parts),
            f"{field} must contain a mycelium marker directory",
        )


def _validate_endpoint_secret_file(value: str) -> None:
    """Endpoint secret file must anchor deep enough and contain identities."""
    _require(
        isinstance(value, str) and len(value) <= 1024,
        "endpoint_secret_file must be a string",
    )
    _require(
        not _has_control_char(value),
        "endpoint_secret_file must not contain control characters",
    )
    path = PurePosixPath(value)
    _require(
        path.is_absolute(),
        "endpoint_secret_file must be absolute",
    )
    _require(
        str(path) == value,
        "endpoint_secret_file must be canonical",
    )
    _require(
        len(path.parts) >= 5,
        "endpoint_secret_file must anchor at least 5 levels deep",
    )
    _require(
        all(part not in {"", ".", ".."} for part in path.parts),
        "endpoint_secret_file must not contain traversal segments",
    )
    path_pairs = set(zip(path.parts, path.parts[1:]))
    _require(
        ("mycelium", "identities") in path_pairs,
        "endpoint_secret_file must contain mycelium/identities ancestry",
    )


def _reject_readiness(doc: Any, *, path: str = "document") -> None:
    """Fail-closed if route_ready or release_ready are truthy anywhere.

    Uses bounded iterative traversal to prevent RecursionError/hangs on
    deeply nested, oversized, or cyclic structures.
    """
    # Stack: (current_value, current_path, depth)
    stack: list[tuple[Any, str, int]] = [(doc, path, 0)]
    visited_ids: set[int] = set()
    item_count = 0

    while stack:
        value, current_path, depth = stack.pop()
        item_count += 1

        if depth > _MAX_TRAVERSAL_DEPTH:
            raise ValueError(
                f"{current_path}: structure exceeds maximum depth {_MAX_TRAVERSAL_DEPTH}"
            )
        if item_count > _MAX_TRAVERSAL_ITEMS:
            raise ValueError(
                f"{current_path}: structure exceeds maximum item count {_MAX_TRAVERSAL_ITEMS}"
            )

        if isinstance(value, dict):
            # Detect cyclic references via id()
            obj_id = id(value)
            if obj_id in visited_ids:
                raise ValueError(f"{current_path}: cyclic reference detected")
            visited_ids.add(obj_id)

            for key, sub_value in value.items():
                if key in {"route_ready", "release_ready"}:
                    if sub_value:
                        raise ValueError(
                            f"{current_path}.{key} must be false; never claim readiness"
                        )
                stack.append((sub_value, f"{current_path}.{key}", depth + 1))
        elif isinstance(value, (list, tuple)):
            obj_id = id(value)
            if obj_id in visited_ids:
                raise ValueError(f"{current_path}: cyclic reference detected")
            visited_ids.add(obj_id)

            for index, item in enumerate(value):
                stack.append((item, f"{current_path}[{index}]", depth + 1))
        # Scalars: nothing to reject


def _reject_endpoint_credentials(configure: dict[str, Any]) -> None:
    """Reject credential keys and URLs with userinfo/query/fragment.

    Recursively traverses all nested runtime endpoint objects, bounded.
    Also scans endpoint URL strings for userinfo/query/fragment.
    """
    _CREDENTIAL_KEYS = frozenset({
        "credential", "credentials", "query", "fragment",
        "password", "token", "secret", "api_key",
    })
    # Stack: (current_value, current_path, depth)
    stack: list[tuple[Any, str, int]] = [(configure, "configure", 0)]
    visited_ids: set[int] = set()
    item_count = 0

    while stack:
        value, current_path, depth = stack.pop()
        item_count += 1

        if depth > _MAX_TRAVERSAL_DEPTH:
            raise ValueError(f"{current_path}: exceeds max depth")
        if item_count > _MAX_TRAVERSAL_ITEMS:
            raise ValueError(f"{current_path}: exceeds max items")

        if isinstance(value, dict):
            obj_id = id(value)
            if obj_id in visited_ids:
                raise ValueError(f"{current_path}: cyclic reference")
            visited_ids.add(obj_id)

            for key, sub_value in value.items():
                if key in _CREDENTIAL_KEYS:
                    raise ValueError(
                        f"Placement endpoint must not contain {key}"
                    )
                # Check URL strings that may carry userinfo/query/fragment
                if isinstance(sub_value, str):
                    _reject_url_components(sub_value, f"{current_path}.{key}")
                stack.append((sub_value, f"{current_path}.{key}", depth + 1))
        elif isinstance(value, (list, tuple)):
            obj_id = id(value)
            if obj_id in visited_ids:
                raise ValueError(f"{current_path}: cyclic reference")
            visited_ids.add(obj_id)

            for idx, item in enumerate(value):
                if isinstance(item, str):
                    _reject_url_components(item, f"{current_path}[{idx}]")
                stack.append((item, f"{current_path}[{idx}]", depth + 1))
        elif isinstance(value, str):
            _reject_url_components(value, current_path)


def _reject_url_components(value: str, path: str) -> None:
    """Reject URL-like strings that contain userinfo, query, or fragment."""
    # Quick length/complexity gate to avoid long URI parsing
    if len(value) > 2048:
        return  # Not a plausible endpoint URL; defer to other validators

    # Check for userinfo (credentials@host)
    if "@" in value:
        # Only flag if it looks URL-like (has scheme or //)
        if "://" in value or "//" in value:
            raise ValueError(
                f"{path} must not contain userinfo (credentials@host)"
            )

    # Check for query string
    if "?" in value:
        raise ValueError(f"{path} must not contain query string")

    # Check for fragment
    if "#" in value:
        raise ValueError(f"{path} must not contain fragment")


def _request_fields() -> set[str]:
    return {
        "request_id",
        "prompt_token_ids",
        "max_new_tokens",
        "expected_new_tokens",
        "qos_class",
        "admitted_at",
        "target_ttft_ms",
        "target_tpot_ms",
        "target_tokens_per_second",
        "sampling_seed",
        "generation_config_digest",
    }


def _validate_request(request: dict[str, Any]) -> dict[str, Any]:
    """Validate request has exact fields expected by controller."""
    expected = _request_fields()
    _require(
        isinstance(request, dict) and set(request) == expected,
        "request must contain exactly the expected fields",
    )
    _require(
        _is_non_empty_segment(request.get("request_id")),
        "request.request_id must be a non-empty segment",
    )
    prompt = request.get("prompt_token_ids")
    _require(
        isinstance(prompt, list)
        and len(prompt) > 0
        and len(prompt) <= 1024
        and all(isinstance(t, int) and not isinstance(t, bool) and t >= 0 for t in prompt),
        "request.prompt_token_ids must be non-empty list of non-negative ints",
    )
    max_new = request.get("max_new_tokens")
    _require(
        isinstance(max_new, int) and not isinstance(max_new, bool) and 1 <= max_new <= 32768,
        "request.max_new_tokens must be a bounded integer",
    )
    expected_new = request.get("expected_new_tokens")
    _require(
        isinstance(expected_new, int) and not isinstance(expected_new, bool) and 1 <= expected_new <= 32768,
        "request.expected_new_tokens must be a bounded integer",
    )
    _require(
        _is_non_empty_segment(request.get("qos_class")),
        "request.qos_class must be a non-empty segment",
    )
    admitted_at = request.get("admitted_at")
    _require(
        isinstance(admitted_at, (int, float))
        and not isinstance(admitted_at, bool)
        and math.isfinite(admitted_at),
        "request.admitted_at must be finite numeric",
    )
    for field in ("target_ttft_ms", "target_tpot_ms", "target_tokens_per_second"):
        val = request.get(field)
        _require(
            isinstance(val, (int, float))
            and not isinstance(val, bool)
            and val > 0
            and math.isfinite(val),
            f"request.{field} must be positive finite numeric",
        )
    _require(
        isinstance(request.get("sampling_seed"), int)
        and not isinstance(request.get("sampling_seed"), bool),
        "request.sampling_seed must be an integer",
    )
    digest = request.get("generation_config_digest")
    _require(
        isinstance(digest, str)
        and _DIGEST_RE.fullmatch(digest) is not None,
        "request.generation_config_digest must be a sha256 digest (sha256:<64-char-hex>)",
    )
    return dict(request)


def compile_physical_run_plan(
    *,
    run_id: str,
    deployment_id: str,
    entry_node_id: str,
    execution_graph: dict[str, Any],
    device_states: dict[str, Any],
    nodes: list[dict[str, Any]],
    request: dict[str, Any],
    decode_count: int,
    expected_token_ids: list[int],
) -> dict[str, Any]:
    """Compile a pure, fail-closed physical run-plan.

    All inputs are explicit immutable values.  No files are read, no
    processes are launched, no bytes are staged.

    Returns a dict matching the ``mycelium.controller_run_plan.v1`` protocol.
    """
    # ------------------------------------------------------------------
    # 0. Deep-clone all mutable inputs so caller mutation cannot alter result
    # ------------------------------------------------------------------
    execution_graph = copy.deepcopy(execution_graph)
    device_states = copy.deepcopy(device_states)
    request = copy.deepcopy(request)
    nodes = copy.deepcopy(nodes)

    # ------------------------------------------------------------------
    # 1. Basic identity and field validation
    # ------------------------------------------------------------------
    _require(_is_non_empty_segment(run_id), "run_id must be a non-empty segment")
    _require(_is_non_empty_segment(deployment_id), "deployment_id must be a non-empty segment")
    _require(_is_non_empty_segment(entry_node_id), "entry_node_id must be a non-empty segment")

    # 2. Validate execution_graph structure
    _require(
        isinstance(execution_graph, dict)
        and execution_graph.get("protocol") == _EXECUTION_GRAPH_PROTOCOL,
        "execution_graph must be a mycelium.execution_graph.v1 document",
    )
    _require(
        execution_graph.get("deployment_id") == deployment_id,
        "execution_graph.deployment_id must match deployment_id",
    )
    stages = execution_graph.get("stages")
    _require(
        isinstance(stages, list) and len(stages) >= 1,
        "execution_graph must have at least one stage",
    )

    # 3. Validate device_states structure
    _require(
        isinstance(device_states, dict) and bool(device_states),
        "device_states must be a non-empty object",
    )

    # 4. Reject route_ready/release_ready claims in inputs
    _reject_readiness(execution_graph, path="execution_graph")
    _reject_readiness(device_states, path="device_states")
    _reject_readiness(request, path="request")

    # 5. Validate nodes list
    _require(
        isinstance(nodes, list) and len(nodes) >= 1,
        "nodes must be a non-empty list",
    )
    _require(
        len(nodes) <= 256,
        "nodes must not exceed 256 entries",
    )

    collected_ids: list[str] = []
    for idx, node_cfg in enumerate(nodes):
        path_prefix = f"nodes[{idx}]"
        _require(
            isinstance(node_cfg, dict),
            f"{path_prefix} must be a dict",
        )
        node_id = node_cfg.get("node_id")
        _require(
            _is_non_empty_segment(node_id),
            f"{path_prefix}.node_id must be a non-empty segment",
        )
        collected_ids.append(node_id)

        # Bundle paths must be relative and traversal-free
        _validate_bundle_path(node_cfg.get("assignment_file"), field=f"{path_prefix}.assignment_file")
        _validate_bundle_path(node_cfg.get("manifest_file"), field=f"{path_prefix}.manifest_file")
        _validate_bundle_path(node_cfg.get("stage_pack_file"), field=f"{path_prefix}.stage_pack_file")

        # Controller paths must be absolute
        _validate_controller_path(node_cfg.get("socket_root"), field=f"{path_prefix}.socket_root", require_mycelium=True)
        _validate_controller_path(node_cfg.get("sidecar_binary"), field=f"{path_prefix}.sidecar_binary")
        _validate_endpoint_secret_file(node_cfg.get("endpoint_secret_file"))

        # Load generation
        load_gen = node_cfg.get("load_generation")
        _require(
            isinstance(load_gen, int)
            and not isinstance(load_gen, bool)
            and load_gen > 0,
            f"{path_prefix}.load_generation must be a positive integer",
        )

        # Also validate device state exists for this node
        _require(
            node_id in device_states,
            f"device_states missing entry for {node_id}",
        )
        ds = device_states[node_id]
        _require(
            isinstance(ds, dict) and ds.get("protocol") == _DEVICE_STATE_PROTOCOL,
            f"device_states[{node_id}] must be a mycelium.device_state.v1 document",
        )
        _require(
            ds.get("node_id") == node_id,
            f"device_states[{node_id}].node_id must match {node_id}",
        )

    # 6. Exactly two nodes for current controller
    _require(
        len(nodes) == 2,
        "exactly 2 nodes required for current controller",
    )

    # 7. Node IDs must be unique
    _require(
        len(set(collected_ids)) == len(collected_ids),
        "node IDs must be unique",
    )

    # 8. entry_node_id must be one of the nodes
    _require(
        entry_node_id in collected_ids,
        "entry_node_id must be one of the declared nodes",
    )

    # 9. entry_node_id must own the graph's entry stage
    entry_stage_id = execution_graph.get("entry_stage_id")
    _require(
        _is_non_empty_segment(entry_stage_id),
        "execution_graph.entry_stage_id must be set",
    )
    entry_stage = None
    for stage in stages:
        if stage.get("stage_id") == entry_stage_id:
            entry_stage = stage
            break
    _require(
        entry_stage is not None,
        "execution_graph entry stage not found",
    )
    placements = entry_stage.get("placements", [])
    entry_owns = any(
        p.get("node_id") == entry_node_id and p.get("lifecycle_state") == "ACTIVE"
        for p in placements
    )
    _require(
        entry_owns,
        f"entry_node_id {entry_node_id} does not own entry stage {entry_stage_id} (no ACTIVE placement)",
    )

    # 10. Validate request document
    validated_request = _validate_request(request)

    # 11. Validate decode_count and expected_token_ids
    _require(
        isinstance(decode_count, int)
        and not isinstance(decode_count, bool)
        and 1 <= decode_count <= _MAX_DECODE_COUNT,
        f"decode_count must be between 1 and {_MAX_DECODE_COUNT}",
    )
    _require(
        isinstance(expected_token_ids, list)
        and len(expected_token_ids) == decode_count + 1,
        f"expected_token_ids length ({len(expected_token_ids)}) must equal decode_count + 1 ({decode_count + 1})",
    )
    _require(
        all(
            isinstance(tid, int) and not isinstance(tid, bool) and tid >= 0
            for tid in expected_token_ids
        ),
        "expected_token_ids must be non-negative integers",
    )

    # 12. Build node records sorted by node_id
    sorted_nodes = sorted(nodes, key=lambda n: n["node_id"])
    sorted_ids = [n["node_id"] for n in sorted_nodes]
    _require(
        sorted_ids == sorted(collected_ids),
        "internal error: node sorting mismatch",
    )

    # 13. Build configure payloads (stage-pack only)
    node_records: list[dict[str, Any]] = []
    for node_cfg in sorted_nodes:
        node_id = node_cfg["node_id"]
        configure: dict[str, Any] = {
            "assignment_file": node_cfg["assignment_file"],
            "manifest_file": node_cfg["manifest_file"],
            "stage_pack_file": node_cfg["stage_pack_file"],
            "graph": copy.deepcopy(execution_graph),
            "device_states": copy.deepcopy(device_states),
            "load_generation": node_cfg["load_generation"],
        }
        # Reject credentials in endpoints
        _reject_endpoint_credentials(configure)

        node_records.append(
            {
                "node_id": node_id,
                "socket_root": node_cfg["socket_root"],
                "sidecar_binary": node_cfg["sidecar_binary"],
                "endpoint_secret_file": node_cfg["endpoint_secret_file"],
                "configure": configure,
            }
        )

    # 14. Assemble final document (canonical field order via dict literal)
    return {
        "protocol": _RUN_PLAN_PROTOCOL,
        "run_id": run_id,
        "deployment_id": deployment_id,
        "entry_node_id": entry_node_id,
        "nodes": node_records,
        "request": validated_request,
        "decode_count": decode_count,
        "expected_token_ids": list(expected_token_ids),
    }
