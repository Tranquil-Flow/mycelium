# SPDX-License-Identifier: AGPL-3.0-only
"""One placement's side of the A7 cross-host standby parity challenge.

Run on the host that owns the placement (``python -m mycelium_a7_parity_worker``,
locally or over ssh), it verifies and loads exactly that placement's staged
stage pack, checks the load proof against the execution graph, binds the
ordinary runtime port and then answers a small closed JSON-lines protocol on
stdin/stdout:

``{"op": "prefill", "path_id", "payload_b64", "span"}``
    PREFILL at position 0; returns the output payload and token id.
``{"op": "decode", "path_id", "path_attempt", "payload_b64", "position"}``
    One DECODE step; returns the output payload and token id.
``{"op": "export", "path_id"}`` / ``{"op": "import", ...}``
    The exact stage KV checkpoint codec of the runtime port.
``{"op": "identity"}``
    ``port_placement_identity`` plus the host's measured available memory.
``{"op": "close"}``

Raw activations travel between the driver and the workers only; the driver
seals digests.  Every failure answers ``{"ok": false, "code": ...}`` with a
stable code.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PROTOCOL = "mycelium.a7_parity_worker.v1"
REQUEST_ID = "a7-parity-challenge"
_CODE = re.compile(r"[a-z][a-z0-9_:.-]{0,120}\Z")


class WorkerError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _read_json(root: Path, relative: str) -> Any:
    path = (root / relative).resolve()
    if root.resolve() not in path.parents:
        raise WorkerError("parity_worker_path_outside_root")
    return json.loads(path.read_text(encoding="utf-8"))


def available_memory_bytes() -> int | None:
    """Measured reclaimable-available memory (free + inactive + speculative pages), or None."""

    try:
        if platform.system() == "Darwin":
            text = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10, check=True).stdout
            page = int(re.search(r"page size of (\d+) bytes", text).group(1))  # type: ignore[union-attr]
            pages = 0
            for name in ("Pages free", "Pages inactive", "Pages speculative"):
                match = re.search(rf"{name}:\s+(\d+)\.", text)
                if match is None:
                    return None
                pages += int(match.group(1))
            return pages * page
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return None
    return None


class ParityWorker:
    def __init__(self, *, root: Path, graph_file: str, node_id: str, placement_id: str, assignment_file: str, stage_pack_file: str, manifest_file: str, load_generation: int) -> None:
        from mycelium_router.layer_builder import layer_load_proof_digest
        from physical_inference_node import execution_graph_from_document
        from runtime_loader import canonical_json, load_assignment_stage
        from stage_pack import artifact_report_for_loader, verify_stage_pack

        self.graph = execution_graph_from_document(_read_json(root, graph_file))
        self.node_id = node_id
        bound = [
            (stage_index, stage, placement)
            for stage_index, stage in enumerate(self.graph.stages)
            for placement in stage.placements
            if placement.placement_id == placement_id
        ]
        if len(bound) != 1 or bound[0][2].node_id != node_id:
            raise WorkerError("parity_worker_placement_not_local")
        self.stage_index, self.stage, self.placement = bound[0]
        assignment = _read_json(root, assignment_file)
        manifest = _read_json(root, manifest_file)
        pack = _read_json(root, stage_pack_file)
        if assignment.get("assignment_id") != self.placement.assignment_id:
            raise WorkerError("parity_worker_assignment_mismatch")
        try:
            verification = verify_stage_pack(pack, assignment=assignment, manifest=manifest)
            report = artifact_report_for_loader(pack, verification, assignment=assignment, manifest=manifest)
        except (TypeError, ValueError) as exc:
            raise WorkerError("parity_worker_stage_pack_invalid") from exc
        loaded = load_assignment_stage(assignment, report, load_generation=load_generation)
        if layer_load_proof_digest(json.loads(canonical_json(loaded.proof))) != self.placement.load_proof_digest:
            raise WorkerError("parity_worker_load_proof_mismatch")
        backend = self.placement.runtime_backend
        if backend == "mlx":
            from mycelium_router.mlx_runtime import MLXRuntimePort as Port
        elif backend == "numpy":
            from mycelium_router.numpy_runtime import NumpyRuntimePort as Port
        else:
            raise WorkerError("parity_worker_backend_unsupported")
        self.port = Port(node_id, self.graph, {placement_id: loaded}, decode_mode="stage_local_kv")

    def _item(self, *, phase: str, path_id: str, path_attempt: int, payload: bytes, token_index: int, position: int, span: int) -> Any:
        from mycelium_router.contracts import HopWorkItem, RuntimeBatchKey

        graph, placement = self.graph, self.placement
        return HopWorkItem(
            request_id=REQUEST_ID,
            path_id=path_id,
            path_attempt=path_attempt,
            phase=phase,
            token_index=token_index,
            hop_index=self.stage_index,
            placement_id=placement.placement_id,
            qos_class="interactive",
            deficit_ratio=0.0,
            enqueued_at=0.0,
            idempotency_key=f"{path_id}:{phase}:{token_index}:{self.stage_index}",
            payload=payload,
            batch_key=RuntimeBatchKey(
                deployment_id=graph.deployment_id,
                deployment_epoch=graph.deployment_epoch,
                model_commit=graph.resolved_commit,
                manifest_digest=graph.manifest_digest,
                placement_id=placement.placement_id,
                assignment_id=placement.assignment_id,
                stage_signature=placement.stage_signature,
                load_proof_digest=placement.load_proof_digest,
                runtime_backend=placement.runtime_backend,
                phase=phase,
                hidden_size=graph.hidden_size,
                activation_bytes=graph.activation_bytes,
                token_span=span,
            ),
            position=position,
            lease_expires_at=1e15,
        )

    def _execute(self, item: Any) -> dict[str, Any]:
        result = self.port.execute(item)
        if not result.success:
            raise WorkerError("parity_worker_execute_failed:" + str(result.failure_reason or "unknown")[:60])
        payload = result.payload if isinstance(result.payload, (bytes, bytearray)) else b""
        token = result.token_id if type(result.token_id) is int else None
        return {"payload_b64": base64.b64encode(bytes(payload)).decode("ascii"), "token_id": token}

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        if op == "prefill":
            payload = base64.b64decode(request["payload_b64"], validate=True)
            span = int(request["span"])
            return self._execute(self._item(phase="PREFILL", path_id=str(request["path_id"]), path_attempt=0, payload=payload, token_index=-1, position=0, span=span))
        if op == "decode":
            payload = base64.b64decode(request["payload_b64"], validate=True)
            position = int(request["position"])
            return self._execute(
                self._item(phase="DECODE", path_id=str(request["path_id"]), path_attempt=int(request["path_attempt"]), payload=payload, token_index=1, position=position, span=1)
            )
        if op == "export":
            data = self.port.export_kv_checkpoint(str(request["path_id"]))
            return {"checkpoint_b64": base64.b64encode(data).decode("ascii")}
        if op == "import":
            self.port.import_kv_checkpoint(
                base64.b64decode(request["checkpoint_b64"], validate=True),
                request_id=REQUEST_ID,
                path_id=str(request["path_id"]),
                path_attempt=int(request["path_attempt"]),
                placement_id=self.placement.placement_id,
                lease_expires_at=1e15,
                expected_next_position=int(request["expected_next_position"]),
                max_bytes=int(request["max_bytes"]),
            )
            return {"imported": True}
        if op == "identity":
            from mycelium_a7_standby_qualification import port_placement_identity

            return {"identity": port_placement_identity(self.port, self.placement.placement_id), "available_bytes": available_memory_bytes()}
        raise WorkerError("parity_worker_op_invalid")


def _stable(exc: BaseException) -> str:
    for candidate in (getattr(exc, "code", None), str(exc)):
        if isinstance(candidate, str) and _CODE.fullmatch(candidate):
            return candidate
    return "parity_worker_failed:" + type(exc).__name__.lower()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, required=True, help="the placement's staging root")
    parser.add_argument("--graph-file", required=True)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--placement-id", required=True)
    parser.add_argument("--assignment-file", required=True)
    parser.add_argument("--stage-pack-file", required=True)
    parser.add_argument("--manifest-file", required=True)
    parser.add_argument("--load-generation", type=int, required=True)
    args = parser.parse_args(argv)
    out = sys.stdout

    def reply(document: dict[str, Any]) -> None:
        out.write(json.dumps({"protocol": PROTOCOL, **document}, sort_keys=True) + "\n")
        out.flush()

    try:
        worker = ParityWorker(
            root=args.root,
            graph_file=args.graph_file,
            node_id=args.node_id,
            placement_id=args.placement_id,
            assignment_file=args.assignment_file,
            stage_pack_file=args.stage_pack_file,
            manifest_file=args.manifest_file,
            load_generation=args.load_generation,
        )
    except Exception as exc:  # noqa: BLE001 - stable code only
        reply({"ok": False, "code": _stable(exc), "ready": False})
        return 2
    reply({"ok": True, "ready": True, "pid": os.getpid()})
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise WorkerError("parity_worker_request_invalid")
            if request.get("op") == "close":
                reply({"ok": True, "closed": True})
                return 0
            reply({"ok": True, **worker.handle(request)})
        except Exception as exc:  # noqa: BLE001 - stable code only
            reply({"ok": False, "code": _stable(exc)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
