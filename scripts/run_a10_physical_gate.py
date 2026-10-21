#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Run the A10 physical gate: verifier matrix, calibrated benchmark and negatives.

Subcommands (every output is an owner-private file under ``--out``, mode 0700/0600):

``verifier-matrix``
    Loads the exact target from a local model snapshot (``config.json`` plus
    safetensors) into one host's MLX or NumPy stage runtimes, colocating every
    layer on that host, and executes the correctness matrix of specification
    section 8: at least 100 bounded operations over the widths ``one``,
    ``qualified_default`` and ``qualified_maximum`` in at least two prefix-length
    buckets, every transaction mode, plus the stale-prefix, generation-policy,
    position-bound and cancellation negatives.  Each operation is judged against
    the sequential target oracle on the same prefix.  The records are then decided
    by ``qualify_target_verifier``.  This path is a direct runtime call, so its
    records say so (``execution_path = direct_runtime``) and the decider blocks
    qualification until the same matrix runs through the product dispatcher.

``benchmark``
    Runs the pre-registered v2 protocol (``tests/a10_acceptance/
    benchmark_protocol.v2.json``) through the ordinary product gateway: a recorded
    baseline-only calibration walks the descending rate ladder, then one unscored
    warmup per mode, then 16 scheduled baseline/candidate pairs in the registered
    alternating order.  Each window offers at least 60 requests with at least 20
    interactive requests, two prompt-length and two output-length buckets, and
    arrivals continuing while earlier requests decode.  The mode switch is an
    operator command per mode (``--baseline-command`` / ``--candidate-command``)
    because the policy switch belongs to the route, not to this client.  After
    every window the live status is read: a candidate window must show a runtime
    batch in the nodes' A10 publications and a baseline window must show none;
    missing publications invalidate the window (``missing_measurement``), never
    count as batching.  Output parity is judged privately per request shape.
    Before any of that, gate 1's two clean browser sessions run under the
    candidate policy: ``ui/web/scripts/a10-browser-sessions.mjs`` (one engine,
    ``--browser-engine``, default chromium; ``--node`` the Node binary) drives two
    fresh browser profiles through the ordinary operator UI to submit the frozen
    concurrent and late-arrival browser workload (``browser_workload``).  The run
    refuses before any traffic when the harness or engine is unavailable, and
    stops before calibration unless the harness report passes and the route's own
    publications show every browser request executed and one in a true runtime
    batch (``browser-sessions.json``).

``verifier-product``
    Runs the same matrix and negatives through the product dispatcher of a live
    serve: one operation per request to the grant-gated loopback A10 control
    (``--a10-verify-control-port``, grant in ``MYCELIUM_A10_FLEET_GRANT``), which
    the live router port admits exclusively and sends to the target node, whose
    own runtime produces each record (``execution_path = product_dispatcher``).
    The binding digests describe the deployed target and are supplied by the
    operator from its deployment.  Any refused operation stops the run without a
    decision.

``negatives``
    Executes the runtime-batch negatives the product API can drive
    (cancellation isolation, queue overflow, slow consumer) and, under the exact
    fleet grant, the other nine through the opt-in fault hooks of
    ``mycelium_router.a10_fault_hooks`` (timeout, incompatible member, stale prefix,
    partial member failure, shared backend failure, wrong result count, runtime
    exit, shutdown, verifier rollback).  A hook negative is executed on CPU and is
    recorded ``surface = cpu_fault_hook``, ``physical_pending = true``; without the
    grant (or with ``--no-fault-hooks``) it is recorded ``not_executed`` with its
    reason, which blocks qualification.

``qualify``
    Assembles the evidence written by the other subcommands and decides it with
    ``qualify_runtime_batch``; ``gate-decision.json`` binds that sealed bundle to
    gate 1's browser-session evidence and is qualified only when both are.  A
    negative executed only through a CPU fault hook is listed in
    ``physical_negatives_pending`` and blocks the decision
    (``physical_negative_execution_pending:<case>``) unless the owner passes
    ``--accept-cpu-fault-hook-negatives``; the list is never hidden.

No output contains a prompt, decoded text, token IDs, cookies, hostnames,
addresses, private paths or exception strings.
"""

# ruff: noqa: E402 -- direct script execution needs the repository on sys.path.

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
   sys.path.insert(0, str(REPO_ROOT))

from mycelium_router.a10_fault_hooks import FAULT_KINDS, A10FaultHookRefused, A10FaultHooks
from mycelium_router.a10_fault_negatives import CPU_SURFACE, run_fault_negative
from mycelium_router.a10_qualification import (
   REQUIRED_RUNTIME_BATCH_NEGATIVES,
   RESIDUAL_RESOURCE_FIELDS,
   RUNTIME_BATCH_EVIDENCE_PROTOCOL,
   canonical_digest,
)
from mycelium_router.a10_runtime_batch import (
   ACCEPTANCE_INVENTORY_DIGEST,
   QualificationRequest,
   qualify_runtime_batch,
)
from mycelium_router.a10_target_verification import (
   TargetVerificationQualificationRequest,
   qualify_target_verifier,
)
from mycelium_router.a10_verifier_matrix import (
   VERIFIER_NEGATIVE_ORDER,
   MatrixPlan,
   run_verifier_matrix,
)
from tests.a10_acceptance.benchmark_amendment import (
   RUN_PROTOCOL,
   evaluate_throughput,
   load_protocol,
   protocol_digest,
)

GATE_PROTOCOL = "mycelium.a10_physical_gate_run.v1"
PROTOCOL = load_protocol()
REQUIREMENTS = PROTOCOL["benchmark_requirements"]
CALIBRATION = PROTOCOL["calibration"]
MEASURED_WINDOWS = PROTOCOL["measured_windows"]
# Offered per window: enough that a window completing at the calibrated 90% floor
# still has the registered minimum of completed requests.
MINIMUM_WINDOW_REQUESTS = math.ceil(
   max(
      int(REQUIREMENTS["minimum_completed_requests_per_window"]),
      int(CALIBRATION["minimum_requests_per_trial"]),
   )
   / float(CALIBRATION["minimum_baseline_completion_fraction"])
)
MINIMUM_INTERACTIVE = int(REQUIREMENTS["minimum_interactive_requests_per_window"])
MINIMUM_LATE_ARRIVALS = int(REQUIREMENTS["minimum_late_arrivals_per_window"])
RETAINED_OUTCOMES = tuple(PROTOCOL["retained_outcomes"])
TERMINAL_KINDS = {"completed", "cancelled", "failed"}
PROMPT_BUCKETS = {"prompt_short": 16, "prompt_long": 96}
OUTPUT_BUCKETS = {"output_short": 8, "output_long": 32}
#: ``RequestGatewayService`` default admission bound.  A queue-overflow burst only
#: proves refusal when it EXCEEDS this bound; one that merely reaches it is admitted.
GATEWAY_MAX_PENDING_REQUESTS = 64
QUEUE_OVERFLOW_EXCESS = 8


def _token_content(data: str) -> bytes:
   """The request-independent content of one SSE token ``data:`` payload.

   A real token event carries ``request_id`` and ``sequence``, which differ per
   request, so hashing the raw line makes equal token streams compare unequal.
   Only token position, text and id are output; any other payload hashes as-is.
   """

   try:
      document = json.loads(data)
   except ValueError:
      return data.encode("utf-8")
   if (
      isinstance(document, dict)
      and document.get("type") == "token"
      and isinstance(document.get("text"), str)
      and isinstance(document.get("token_index"), int)
   ):
      content = {"token_index": document["token_index"], "text": document["text"]}
      if "token_id" in document:
         content["token_id"] = document["token_id"]
      return json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
   return data.encode("utf-8")


class GateError(RuntimeError):
   """A coded gate failure; the code is the only thing recorded."""


# ---------------------------------------------------------------------------
# Owner-private output
# ---------------------------------------------------------------------------


def _private_directory(path: Path) -> Path:
   path.mkdir(parents=True, exist_ok=True, mode=0o700)
   os.chmod(path, 0o700)
   return path


def write_private_json(path: Path, document: object) -> None:
   _private_directory(path.parent)
   temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
   descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
   with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
      json.dump(document, handle, sort_keys=True, indent=1, allow_nan=False)
      handle.write("\n")
      handle.flush()
      os.fsync(handle.fileno())
   os.replace(temporary, path)


def read_json(path: Path) -> Any:
   return json.loads(Path(path).read_text("utf-8"))


# ---------------------------------------------------------------------------
# verifier-matrix: a colocated exact target on this host
# ---------------------------------------------------------------------------


def _safetensors_keys(path: Path) -> list[str]:
   with path.open("rb") as handle:
      length = int.from_bytes(handle.read(8), "little")
      if length <= 0 or length > 100 * 1024 * 1024:
         raise GateError("safetensors_header_invalid")
      header = json.loads(handle.read(length))
   return sorted(key for key in header if key != "__metadata__")


def _sha256_file(path: Path) -> str:
   digest = hashlib.sha256()
   with path.open("rb") as handle:
      for chunk in iter(lambda: handle.read(1 << 22), b""):
         digest.update(chunk)
   return digest.hexdigest()


class _SnapshotFetcher:
   """Resolve files inside one local snapshot only; never download."""

   def __init__(self, root: Path, model_id: str, revision: str) -> None:
      self.root = root
      self.model_id = model_id
      self.revision = revision

   def __call__(self, model_id, revision, filename, cache_root, local_files_only=False):
      if model_id != self.model_id or revision != self.revision or not local_files_only:
         raise GateError("snapshot_fetch_refused")
      candidate = self.root / filename
      if ".." in Path(filename).parts or not candidate.is_file():
         raise GateError("snapshot_file_missing")
      return candidate, True


@dataclass
class LocalTarget:
   graph: Any
   ports: tuple[Any, ...]
   manifest: dict[str, Any]
   backend: str
   runtime_digest: str
   model_digest: str
   representation_digest: str

   def stages(self) -> tuple[Any, ...]:
      return tuple(stage for port in self.ports for stage in port.a10_verifier_stages())


def build_local_target(
   snapshot: Path,
   *,
   model_id: str,
   revision: str,
   backend: str,
   dtype: str,
   quantization: str,
) -> LocalTarget:
   """Load every layer of the exact target on this host, split over two stages."""

   from dataclasses import replace

   from layer_assignment import compile_layer_assignments
   from model_manifest import compile_model_manifest, manifest_digest_ref
   from mycelium_router.contracts import (
      ExecutionGraph,
      LayerRange,
      Placement,
      PlacementEdge,
      Stage,
      StageCost,
   )
   from mycelium_router.layer_builder import layer_load_proof_digest
   from runtime_loader import canonical_json, load_assignment_stage
   from two_process_runtime_qualification import _control_plane_binding
   from weight_provisioning import provision_assignment

   snapshot = Path(snapshot)
   config = read_json(snapshot / "config.json")
   index_path = snapshot / "model.safetensors.index.json"
   if index_path.is_file():
      weight_map = read_json(index_path)["weight_map"]
   else:
      weight_map = {key: "model.safetensors" for key in _safetensors_keys(snapshot / "model.safetensors")}
   files = sorted(set(weight_map.values()))
   manifest = compile_model_manifest(
      model_id=model_id,
      requested_revision=revision,
      resolved_commit=revision,
      config=config,
      checkpoint_index={"weight_map": weight_map},
      file_metadata={
         name: {"size_bytes": (snapshot / name).stat().st_size, "sha256": _sha256_file(snapshot / name)}
         for name in files
      },
   )
   layers = int(manifest["num_layers"])
   if layers < 2:
      raise GateError("target_needs_two_layers")
   split = layers // 2
   nodes = ("verifier-entry", "verifier-final")
   binding = _control_plane_binding()
   route = {
      "ok": True,
      "protocol": "mycelium.manual_provisioning_route.v1",
      "claim_boundary": "colocated verifier target on one host; not a route-readiness claim",
      "model": {
         "model_id": manifest["model_id"],
         "num_layers": layers,
         "manifest_digest": manifest_digest_ref(manifest),
         "resolved_commit": manifest["resolved_commit"],
      },
      "route": [
         {"node_id": nodes[0], "range": {"start_layer": 0, "end_layer_exclusive": split, "layer_count": split}},
         {"node_id": nodes[1], "range": {"start_layer": split, "end_layer_exclusive": layers, "layer_count": layers - split}},
      ],
      "node_order": list(nodes),
   }
   assignments = compile_layer_assignments(
      route_plan=route,
      manifest=manifest,
      deployment_id=binding["deployment_id"],
      deployment_epoch=binding["deployment_epoch"],
      cache_roots={node: str(snapshot) for node in nodes},
      runtime_by_node={node: {"backend": backend, "dtype": dtype, "quantization": quantization} for node in nodes},
      control_plane_binding=binding,
   )
   fetcher = _SnapshotFetcher(snapshot, model_id, revision)
   loaded = tuple(
      load_assignment_stage(
         assignment,
         provision_assignment(assignment, fetch_file=fetcher, local_files_only=True),
         load_generation=1,
      )
      for assignment in assignments
   )
   plain = lambda value: json.loads(canonical_json(value))  # noqa: E731
   stages = []
   placements = []
   for index, (assignment, stage_loaded) in enumerate(zip(assignments, loaded)):
      stage_id = f"stage-{index:03d}"
      placement = Placement(
         placement_id=f"placement-{index:03d}",
         node_id=assignment["node_id"],
         replica_group_id=f"{stage_id}-replicas",
         assignment_id=assignment["assignment_id"],
         stage_signature="pending-stage-signature",
         load_proof_digest=layer_load_proof_digest(plain(stage_loaded.proof)),
         runtime_backend=assignment["runtime"]["backend"],
         runtime_endpoint=f"memory://{assignment['node_id']}/{stage_id}",
      )
      placements.append(placement)
      layer_range = assignment["range"]
      stages.append(
         Stage(
            stage_id=stage_id,
            layer_range=LayerRange(**layer_range),
            component_roles=tuple(assignment["components"]),
            stage_cost=StageCost(
               prefill_work_units_per_prompt_token=1.0,
               decode_work_units_per_token=1.0,
               kv_bytes_per_context_token=32,
            ),
            placements=(placement,),
         )
      )
   graph = ExecutionGraph(
      deployment_id=assignments[0]["deployment_id"],
      deployment_epoch=assignments[0]["deployment_epoch"],
      topology_version=1,
      model_id=assignments[0]["model_id"],
      resolved_commit=assignments[0]["resolved_commit"],
      manifest_digest=assignments[0]["manifest_digest"],
      entry_stage_id=stages[0].stage_id,
      final_stage_id=stages[-1].stage_id,
      hidden_size=assignments[0]["runtime"]["model_config"]["n_embd"],
      activation_bytes=4,
      token_envelope_bytes=9,
      stages=tuple(stages),
      edges=(
         PlacementEdge(
            edge_id="forward:placement-000->placement-001",
            from_placement_id=placements[0].placement_id,
            to_placement_id=placements[1].placement_id,
            link_id="local:verifier-entry->verifier-final",
         ),
      ),
      loopback_edges=(
         PlacementEdge(
            edge_id="loopback:placement-001->placement-000",
            from_placement_id=placements[1].placement_id,
            to_placement_id=placements[0].placement_id,
            link_id="local:verifier-final->verifier-entry",
         ),
      ),
   )
   if backend == "mlx":
      from mycelium_router.mlx_runtime import MLXRuntimePort as Port, _stage_signature
   elif backend == "numpy":
      from mycelium_router.numpy_runtime import NumpyRuntimePort as Port, _stage_signature
   else:
      raise GateError("unsupported_backend")
   graph = replace(
      graph,
      stages=tuple(
         replace(
            stage,
            placements=(replace(stage.placements[0], stage_signature=_stage_signature(graph, stage, stage_loaded.proof)),),
         )
         for stage, stage_loaded in zip(graph.stages, loaded)
      ),
   )
   extra = {} if backend == "mlx" else {"decode_mode": "stage_local_kv"}
   ports = tuple(
      Port(assignment["node_id"], graph, {graph.stages[index].placements[0].placement_id: loaded[index]}, **extra)
      for index, assignment in enumerate(assignments)
   )
   runtime_digest = canonical_digest(
      {
         "backend": backend,
         "dtype": dtype,
         "quantization": quantization,
         "load_proofs": [layer_load_proof_digest(plain(item.proof)) for item in loaded],
      }
   )
   return LocalTarget(
      graph=graph,
      ports=ports,
      manifest=manifest,
      backend=backend,
      runtime_digest=runtime_digest,
      model_digest=manifest_digest_ref(manifest),
      representation_digest=canonical_digest({"dtype": dtype, "quantization": quantization}),
   )


def run_verifier_gate(
   target: LocalTarget,
   *,
   widths: Mapping[str, int],
   prefix_buckets: Mapping[str, tuple[int, ...]],
   operations: int,
   provenance: str,
   out: Path,
   progress: Callable[[str], None] = lambda message: None,
) -> dict[str, Any]:
   stages = target.stages()

   def open_verifier(prefix: tuple[int, ...], ref: str) -> Any:
      return target.ports[0].begin_target_verifier(prefix, verification_ref=ref, stages=stages)

   def cancel(verifier: Any) -> None:
      for port in target.ports:
         port.cancel(verifier.path_id)

   plan = MatrixPlan(
      widths=dict(widths),
      prefix_buckets={name: tuple(lengths) for name, lengths in prefix_buckets.items()},
      operations=operations,
      vocab_size=stages[0].vocab_size,
   )
   started = time.monotonic()
   records, negatives = run_verifier_matrix(
      plan,
      open_verifier=open_verifier,
      cancel_verification=cancel,
      runtime_digest=target.runtime_digest,
      model_digest=target.model_digest,
      representation_digest=target.representation_digest,
      backend=target.backend,
      provenance=provenance,
      execution_path="direct_runtime",
      on_operation=lambda index, record: progress(f"operation {index + 1}/{operations}"),
   )
   request = TargetVerificationQualificationRequest(
      owner_private_root=str(out.name),
      runtime_digest=target.runtime_digest,
      width_classes=("one", "qualified_default", "qualified_maximum"),
      prefix_length_buckets=len(prefix_buckets),
      operations=operations,
      sequential_parity_proof_digest=canonical_digest(records),
   )
   bundle = qualify_target_verifier(request, records, negatives)
   summary = {
      "protocol": GATE_PROTOCOL,
      "subcommand": "verifier-matrix",
      "backend": target.backend,
      "elapsed_seconds": round(time.monotonic() - started, 3),
      "decision": bundle["decision"],
      "matrix_passed": bundle["matrix_passed"],
      "blockers": bundle["blockers"],
   }
   write_private_json(out / "verifier-operations.json", records)
   write_private_json(out / "verifier-negatives.json", negatives)
   write_private_json(out / "verifier-qualification.json", bundle)
   write_private_json(out / "verifier-summary.json", summary)
   return bundle


# ---------------------------------------------------------------------------
# benchmark: the calibrated v2 protocol through the product gateway
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestShape:
   index: int
   prompt_bucket: str
   output_bucket: str
   qos_class: str
   arrival_offset: float

   @property
   def shape_key(self) -> str:
      return f"{self.prompt_bucket}:{self.output_bucket}:{self.index}"


def window_workload(
   *, rate: float, count: int, seed: int
) -> list[RequestShape]:
   """A frozen, deterministic arrival schedule covering every bucket and QoS."""

   rng = random.Random(seed)
   offset = 0.0
   shapes = []
   for index in range(count):
      shapes.append(
         RequestShape(
            index=index,
            prompt_bucket=tuple(PROMPT_BUCKETS)[index % 2],
            output_bucket=tuple(OUTPUT_BUCKETS)[(index // 2) % 2],
            qos_class="interactive" if index % 2 == 0 else "batch",
            arrival_offset=offset,
         )
      )
      offset += -math.log(1.0 - rng.random()) / rate
   return shapes


def prompt_text(shape: RequestShape) -> str:
   return f"A10 benchmark request {shape.index}:" + " benchmark" * PROMPT_BUCKETS[shape.prompt_bucket]


@dataclass
class RequestResult:
   shape: RequestShape
   outcome: str
   submitted_at: float
   first_token_at: float | None
   finished_at: float
   tokens: int
   output_digest: str | None


@dataclass(frozen=True)
class RequestDiagnostic:
   """Opt-in client observation; not product admission or terminal evidence."""

   shape_index: int
   request_ref: str | None
   stage: str
   outcome: str
   status: int | None
   token_events: int
   terminal_kind: str | None = None
   cancel_outcome: str | None = None
   gateway_code: str | None = None


_DIAGNOSTIC_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z")
_DIAGNOSTIC_HTTP_STATUS = re.compile(r"http_(?:post|get)_([1-5][0-9]{2})\Z")


def _diagnostic_status(error: Exception) -> int | None:
   # Only the coded ProductSession error, never free-form exception text.
   from scripts.run_a5_benchmark_gate import GateError as SessionError

   match = _DIAGNOSTIC_HTTP_STATUS.fullmatch(str(error)) if type(error) is SessionError else None
   return int(match[1]) if match else None


def _diagnostic_request_ref(accepted: object) -> str | None:
   identifier = accepted.get("request_id") if isinstance(accepted, Mapping) else None
   if not isinstance(identifier, str) or not _DIAGNOSTIC_REQUEST_ID.fullmatch(identifier):
      return None
   return "sha256:" + hashlib.sha256(identifier.encode("ascii")).hexdigest()


class ProductClient:
   """Submit one request through the ordinary gateway and stream it to terminal."""

   def __init__(self, base_url: str, *, diagnostic_limit: int = 0) -> None:
      from scripts.run_a5_benchmark_gate import ProductSession, RotatingProductSessions

      if type(diagnostic_limit) is not int or not 0 <= diagnostic_limit <= 4096:
         raise ValueError("diagnostic_limit_invalid")
      self.base_url = base_url
      self._session = ProductSession(base_url)
      self._sessions = RotatingProductSessions(self._session, base_url)
      self._diagnostics: deque[RequestDiagnostic] | None = deque(maxlen=diagnostic_limit) if diagnostic_limit else None
      self._diagnostics_lock = threading.Lock()

   def request_diagnostics(self) -> tuple[RequestDiagnostic, ...]:
      """Return a bounded immutable snapshot; diagnostics do no file I/O."""
      with self._diagnostics_lock:
         return tuple(self._diagnostics) if self._diagnostics is not None else ()

   def _observe(self, shape: RequestShape, accepted: object, stage: str, outcome: str, status: int | None, tokens: int, terminal_kind: str | None = None, cancel_outcome: str | None = None, gateway_code: str | None = None) -> None:
      if self._diagnostics is None:
         return
      record = RequestDiagnostic(shape.index, _diagnostic_request_ref(accepted), stage, outcome, status, tokens, terminal_kind, cancel_outcome, gateway_code)
      with self._diagnostics_lock:
         self._diagnostics.append(record)

   def run(self, shape: RequestShape) -> RequestResult:
      session = self._sessions.fork_for_submission()
      submitted = time.monotonic()
      try:
         accepted = session.submit(
            prompt=prompt_text(shape),
            maximum_new_tokens=OUTPUT_BUCKETS[shape.output_bucket],
            workload_profile_id="interactive_chat_v1" if shape.qos_class == "interactive" else "sustained_batch_v1",
            qos_class=shape.qos_class,
         )
      except Exception as error:  # noqa: BLE001 - only the code is recorded
         code = str(error)
         from scripts.run_a5_benchmark_gate import GateError as SessionError
         gateway_code = getattr(error, "gateway_code", None) if type(error) is SessionError else None
         outcome = "overflowed" if code.endswith("_429") or code.endswith("_503") or gateway_code == "gateway_capacity_exhausted" else "failed"
         if self._diagnostics is not None:
            self._observe(shape, None, "submit_error", outcome, _diagnostic_status(error), 0, gateway_code=gateway_code)
         return RequestResult(shape, outcome, submitted, None, time.monotonic(), 0, None)
      first = None
      tokens = 0
      terminal = None
      observed_terminal = None
      stream_exception = False
      stream_status = None
      output = hashlib.sha256()
      try:
         with session.request("GET", accepted["event_path"]) as response:
            kind = None
            for raw in response:
               line = raw.decode("utf-8", "strict").rstrip("\r\n")
               if line.startswith("event: "):
                  kind = line[7:]
                  if kind in TERMINAL_KINDS:
                     terminal = kind
                     observed_terminal = kind
                     break
               elif line.startswith("data: ") and kind == "token":
                  tokens += 1
                  if first is None:
                     first = time.monotonic()
                  output.update(_token_content(line[6:]))
      except Exception as error:  # noqa: BLE001
         terminal = "failed"
         stream_exception = True
         if self._diagnostics is not None:
            stream_status = _diagnostic_status(error)
      cancel_outcome = None
      if observed_terminal is None:
         try:
            session.cancel(accepted)
            cancel_outcome = "sent"
         except Exception:  # noqa: BLE001 - best effort, never retain private transport text
            cancel_outcome = "failed"
      outcome = {"completed": "completed", "cancelled": "cancelled"}.get(terminal or "", "failed")
      self._observe(
         shape, accepted,
         "stream_exception" if stream_exception else "terminal" if terminal is not None else "stream_eof",
         outcome, stream_status, tokens, observed_terminal, cancel_outcome,
      )
      return RequestResult(
         shape,
         outcome,
         submitted,
         first,
         time.monotonic(),
         tokens,
         "sha256:" + output.hexdigest() if outcome == "completed" else None,
      )

   def live_status(self) -> dict[str, Any]:
      from scripts.run_a5_benchmark_gate import public_json

      return public_json(self.base_url, "/__mycelium/live-status")

   # -- product-drivable runtime-batch negatives ------------------------------
   def _zero_residue(self, timeout: float = 120.0) -> int:
      """0 when the route returns to zero queued work and reservations in time."""

      from scripts.run_a5_benchmark_gate import _zero_live_resources, public_json

      deadline = time.monotonic() + timeout
      while time.monotonic() < deadline:
         try:
            if _zero_live_resources(public_json(self.base_url, "/__mycelium/runtime/admission-status")):
               return 0
         except Exception:  # noqa: BLE001
            pass
         time.sleep(1.0)
      return 1

   def _outcome(self, passed: bool) -> dict[str, Any]:
      residue = self._zero_residue()
      return {
         "outcome": "failed_closed_within_scope" if passed and residue == 0 else "violated",
         "residue": residue,
      }

   def cancellation_isolation(self, shape: RequestShape) -> dict[str, Any]:
      """Cancel one of two concurrent requests; only the cancelled one may stop."""

      victim_session = self._sessions.fork_for_submission()
      victim = victim_session.submit(
         prompt=prompt_text(shape),
         maximum_new_tokens=OUTPUT_BUCKETS["output_long"],
         workload_profile_id="interactive_chat_v1",
         qos_class="interactive",
      )
      survivor: dict[str, RequestResult] = {}
      thread = threading.Thread(
         target=lambda: survivor.setdefault("result", self.run(RequestShape(1, "prompt_short", "output_long", "interactive", 0.0))),
         daemon=True,
      )
      thread.start()
      time.sleep(0.5)
      victim_session.cancel(victim)
      thread.join(timeout=300)
      result = survivor.get("result")
      return self._outcome(result is not None and result.outcome == "completed")

   def queue_overflow(self, shape: RequestShape) -> dict[str, Any]:
      """A burst beyond the admission bound is refused before dispatch, not queued."""

      size = GATEWAY_MAX_PENDING_REQUESTS + QUEUE_OVERFLOW_EXCESS
      burst = [RequestShape(index, "prompt_short", "output_short", "batch", 0.0) for index in range(size)]
      results: list[RequestResult] = []
      lock = threading.Lock()

      def submit(item: RequestShape) -> None:
         outcome = self.run(item)
         with lock:
            results.append(outcome)

      threads = [threading.Thread(target=submit, args=(item,), daemon=True) for item in burst]
      for thread in threads:
         thread.start()
      for thread in threads:
         thread.join(timeout=600)
      overflowed = sum(1 for item in results if item.outcome == "overflowed")
      terminal = len(results) == len(burst)
      return self._outcome(terminal and overflowed > 0)

   def slow_consumer(self, shape: RequestShape) -> dict[str, Any]:
      """A consumer that stops reading must not block another request."""

      stalled_session = self._sessions.fork_for_submission()
      stalled = stalled_session.submit(
         prompt=prompt_text(shape),
         maximum_new_tokens=OUTPUT_BUCKETS["output_long"],
         workload_profile_id="interactive_chat_v1",
         qos_class="interactive",
      )
      response = stalled_session.request("GET", stalled["event_path"])
      try:
         other = self.run(RequestShape(2, "prompt_short", "output_short", "interactive", 0.0))
      finally:
         response.close()
      return self._outcome(other.outcome == "completed")


def publications_from_status(status: Mapping[str, Any]) -> list[dict[str, Any]]:
   """The nodes' A10 publications projected by the live status (None when absent)."""

   peers = status.get("peers")
   if not isinstance(peers, list):
      return []
   return [peer["a10_publication"] for peer in peers if isinstance(peer, Mapping) and isinstance(peer.get("a10_publication"), Mapping)]


def batched_intervals_since(publications: Sequence[Mapping[str, Any]], since_wall: float) -> int:
   """Stage-compute intervals executed inside a runtime batch after ``since_wall``.

   Node-local monotonic times are mapped to wall time by each publication's own
   clock anchor, so the count covers exactly the window that just ran.
   """

   count = 0
   for publication in publications:
      clock = publication.get("clock", {})
      offset = clock.get("wall_anchor", 0.0) - clock.get("monotonic_anchor", 0.0)
      for interval in publication.get("intervals", ()):
         if interval.get("runtime_batch_ref") and interval["started_at"] + offset >= since_wall:
            count += 1
   return count


def run_window(
   client: Any,
   *,
   rate: float,
   count: int,
   seed: int,
   time_scale: float = 1.0,
) -> list[RequestResult]:
   shapes = window_workload(rate=rate, count=count, seed=seed)
   results: list[RequestResult | None] = [None] * len(shapes)
   threads = []
   started = time.monotonic()

   def worker(position: int, shape: RequestShape) -> None:
      results[position] = client.run(shape)

   for position, shape in enumerate(shapes):
      delay = started + shape.arrival_offset * time_scale - time.monotonic()
      if delay > 0:
         time.sleep(delay)
      thread = threading.Thread(target=worker, args=(position, shape), daemon=True)
      thread.start()
      threads.append(thread)
   for thread in threads:
      thread.join()
   return [result for result in results if result is not None]


def late_arrivals(results: Sequence[RequestResult]) -> int:
   """Requests submitted while an earlier request was already decoding."""

   late = 0
   for result in results:
      if any(
         other is not result
         and other.first_token_at is not None
         and other.first_token_at <= result.submitted_at < other.finished_at
         for other in results
      ):
         late += 1
   return late


def window_record(
   *,
   index: int,
   pair_id: int,
   mode: str,
   rate: float,
   results: Sequence[RequestResult],
   mode_evidence: str,
) -> dict[str, Any]:
   outcomes = {name: 0 for name in RETAINED_OUTCOMES}
   for result in results:
      outcomes[result.outcome if result.outcome in outcomes else "failed"] += 1
   start = min(result.submitted_at for result in results)
   end = max(result.finished_at for result in results)
   invalidations = []
   if mode_evidence == "absent":
      invalidations.append("missing_measurement")
   elif mode_evidence == "contradicted":
      invalidations.append("software_configuration_or_instrumentation_changed")
   return {
      "index": index,
      "pair_id": pair_id,
      "mode": mode,
      "offered_arrival_rate_per_second": rate,
      "offered_request_count": len(results),
      "request_count": len(results),
      "outcomes": outcomes,
      "completed_generated_tokens": sum(r.tokens for r in results if r.outcome == "completed"),
      "wall_clock_seconds": max(end - start, 1e-9),
      "invalidations": invalidations,
   }


def _p95(values: Sequence[float]) -> float | None:
   if not values:
      return None
   ordered = sorted(values)
   return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]


def window_latency(results: Sequence[RequestResult]) -> dict[str, float | None]:
   interactive = [r for r in results if r.shape.qos_class == "interactive" and r.outcome == "completed" and r.first_token_at]
   ttft = [1000.0 * (r.first_token_at - r.submitted_at) for r in interactive]
   tpot = [
      1000.0 * (r.finished_at - r.first_token_at) / (r.tokens - 1)
      for r in interactive
      if r.tokens > 1
   ]
   return {"interactive_p95_ttft": _p95(ttft), "interactive_p95_tpot": _p95(tpot)}


def run_benchmark(
   client: Any,
   *,
   switch_mode: Callable[[str], None],
   observe_mode: Callable[[str, float], str],
   out: Path,
   time_scale: float = 1.0,
   seed: int = 165,
   browser_sessions: Callable[[], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
   """Run gate 1's browser phase, then calibrate, warm up and run the 16 pairs.

   ``browser_sessions`` is the two-clean-browser-session phase (specification
   §10 gate 1, :class:`BrowserSessionsPhase`).  It runs first, under the
   candidate policy, before any scored traffic; its sealed evidence is written to
   ``browser-sessions.json`` and a failed phase stops the run before
   calibration, so a refused browser gate never costs the 3-4 h benchmark.
   Without it the summary records gate 1's browser requirement as unproven.
   """

   if browser_sessions is None:
      browser = {"executed": False, "passed": False, "reason": "not_configured"}
   else:
      switch_mode("candidate")
      browser = dict(browser_sessions())
      write_private_json(out / "browser-sessions.json", browser)
      if browser.get("passed") is not True:
         blockers = browser.get("blockers") or ["browser_sessions_unproven"]
         raise GateError(f"browser_sessions_failed:{blockers[0]}")
   switch_mode("baseline")
   calibration = []
   selected = None
   for rate in CALIBRATION["rate_ladder_per_second"]:
      results = run_window(client, rate=rate, count=MINIMUM_WINDOW_REQUESTS, seed=seed, time_scale=time_scale)
      outcomes = {name: 0 for name in RETAINED_OUTCOMES}
      for result in results:
         outcomes[result.outcome if result.outcome in outcomes else "failed"] += 1
      calibration.append(
         {
            "ordinal": len(calibration),
            "offered_arrival_rate_per_second": rate,
            "offered_request_count": len(results),
            "request_count": len(results),
            "outcomes": outcomes,
         }
      )
      if outcomes["completed"] >= CALIBRATION["minimum_baseline_completion_fraction"] * len(results):
         selected = rate
         break
   windows: list[dict[str, Any]] = []
   latency: dict[str, list[float]] = {}
   parity: dict[str, dict[str, str]] = {"baseline": {}, "candidate": {}}
   reliability: dict[str, Counter[str]] = {}
   if selected is not None:
      for mode in ("baseline", "candidate"):
         switch_mode(mode)
         run_window(client, rate=selected, count=MINIMUM_WINDOW_REQUESTS, seed=seed + 1, time_scale=time_scale)
      for expected in MEASURED_WINDOWS:
         mode = expected["mode"]
         switch_mode(mode)
         since = time.time()
         results = run_window(
            client,
            rate=selected,
            count=MINIMUM_WINDOW_REQUESTS,
            seed=seed + 100 + expected["pair_id"],
            time_scale=time_scale,
         )
         evidence = observe_mode(mode, since)
         record = window_record(
            index=expected["index"],
            pair_id=expected["pair_id"],
            mode=mode,
            rate=selected,
            results=results,
            mode_evidence=evidence,
         )
         record_meta = {
            "late_arrivals": late_arrivals(results),
            "interactive": sum(1 for r in results if r.shape.qos_class == "interactive"),
            "prompt_buckets": len({r.shape.prompt_bucket for r in results}),
            "output_buckets": len({r.shape.output_bucket for r in results}),
            "mode_evidence": evidence,
         }
         if record_meta["late_arrivals"] < MINIMUM_LATE_ARRIVALS or record_meta["interactive"] < MINIMUM_INTERACTIVE:
            record["invalidations"].append("missing_measurement")
         windows.append(record)
         for name, value in window_latency(results).items():
            if value is not None:
               latency.setdefault(f"{mode}_{name}", []).append(value)
         for result in results:
            if result.output_digest is not None:
               parity[mode][f"{expected['pair_id']}:{result.shape.shape_key}"] = result.output_digest
         reliability.setdefault(mode, Counter()).update(result.outcome for result in results)
         write_private_json(out / "benchmark-windows" / f"window-{expected['index']:02d}.json", {**record, "meta": record_meta})
   run = {
      "protocol": RUN_PROTOCOL,
      "benchmark_protocol_digest": protocol_digest(PROTOCOL),
      "calibration": {
         "mode": "baseline",
         "scored": False,
         "rate_ladder_per_second": list(CALIBRATION["rate_ladder_per_second"]),
         "trials": calibration,
         "selected_offered_arrival_rate_per_second": selected,
      },
      "windows": windows,
   }
   decision = evaluate_throughput(run)
   compared = [key for key in parity["baseline"] if key in parity["candidate"]]
   mismatched = [key for key in compared if parity["baseline"][key] != parity["candidate"][key]]
   summary = {
      "protocol": GATE_PROTOCOL,
      "subcommand": "benchmark",
      "calibrated_rate": selected,
      "throughput": decision,
      "latency_ms": {key: max(values) for key, values in latency.items()},
      "output_parity": {"compared_requests": len(compared), "mismatched_requests": len(mismatched)},
      "reliability": {mode: dict(counts) for mode, counts in reliability.items()},
      "browser_sessions": browser,
   }
   write_private_json(out / "benchmark-run.json", run)
   write_private_json(out / "benchmark-summary.json", summary)
   return summary


def mode_observer(client: Any) -> Callable[[str, float], str]:
   def observe(mode: str, since: float) -> str:
      try:
         publications = publications_from_status(client.live_status())
      except Exception:  # noqa: BLE001
         return "absent"
      if not publications:
         return "absent"
      batched = batched_intervals_since(publications, since)
      if mode == "candidate":
         return "verified" if batched > 0 else "contradicted"
      return "verified" if batched == 0 else "contradicted"

   return observe


def command_switcher(commands: Mapping[str, str]) -> Callable[[str], None]:
   def switch(mode: str) -> None:
      command = commands.get(mode)
      if not command:
         raise GateError(f"mode_command_missing:{mode}")
      completed = subprocess.run(shlex.split(command), check=False, timeout=600)
      if completed.returncode != 0:
         raise GateError(f"mode_command_failed:{mode}")

   return switch


# ---------------------------------------------------------------------------
# gate 1: two clean browser sessions submit the frozen workload
# ---------------------------------------------------------------------------

BROWSER_PROTOCOL = "mycelium.a10_browser_sessions.v1"
BROWSER_WORKLOAD_PROTOCOL = "mycelium.a10_browser_workload.v1"
BROWSER_EVIDENCE_PROTOCOL = "mycelium.a10_gate1_browser_evidence.v1"
BROWSER_HARNESS = REPO_ROOT / "ui" / "web" / "scripts" / "a10-browser-sessions.mjs"
BROWSER_ENGINES = ("chromium", "firefox", "webkit")
BROWSER_MODES = ("live", "local_conformance")
#: The harness's ordered checks (``REQUIRED_CHECKS`` in the .mjs); all must pass.
REQUIRED_BROWSER_CHECKS = (
   "live_status_preflight",
   "fresh_independent_sessions",
   "concurrent_round_submitted_through_ui",
   "concurrent_overlap_observed",
   "late_arrival_round_submitted_through_ui",
   "late_arrival_observed",
   "all_requests_completed",
   "cross_session_privacy",
   "clean_third_session_privacy",
   "same_origin_inference_only_mutations",
)
#: Frozen browser rounds over the benchmark's own request shapes: equal prompt
#: lengths (single-digit indexes, ``prompt_short``) so concurrent members can share a
#: decode position, interactive QoS (the UI's ordinary policy), and a long leader so
#: the late arrival lands while it decodes.
BROWSER_ROUNDS = (
   (
      "concurrent",
      (
         ("a", RequestShape(0, "prompt_short", "output_long", "interactive", 0.0)),
         ("b", RequestShape(2, "prompt_short", "output_long", "interactive", 0.0)),
      ),
   ),
   (
      "late_arrival",
      (
         ("a", RequestShape(4, "prompt_short", "output_long", "interactive", 0.0)),
         ("b", RequestShape(6, "prompt_short", "output_short", "interactive", 0.0)),
      ),
   ),
)
_BROWSER_REPORT_FIELDS = frozenset(
   {
      "protocol", "mode", "engine", "origin", "deployment_id", "workload_digest",
      "independent_sessions", "checks", "failures", "rounds", "requests", "request_count",
      "passed", "live_evidence", "qualification_claim", "physical_claim",
   }
)
_BROWSER_REQUEST_FIELDS = frozenset(
   {
      "session", "round", "index", "request_ref", "max_new_tokens", "qos_class",
      "accepted_at_ms", "submitted_through_ui", "frozen_submission_verified", "terminal",
      "tokens", "terminal_observed_at_ms",
   }
)
_REF = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HARNESS_CODE = re.compile(r"[a-z][a-z0-9_]{0,40}(?::[a-z0-9_]{1,80}){0,3}\Z")


def _sha256_json(document: object) -> str:
   canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)
   return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def browser_workload() -> dict[str, Any]:
   """The frozen two-session browser workload, digest-bound (the harness re-checks it)."""

   body = {
      "protocol": BROWSER_WORKLOAD_PROTOCOL,
      "sessions": ["a", "b"],
      "rounds": [
         {
            "round": name,
            "submissions": [
               {
                  "session": session,
                  "index": shape.index,
                  "prompt": prompt_text(shape),
                  "max_new_tokens": OUTPUT_BUCKETS[shape.output_bucket],
                  "qos_class": shape.qos_class,
                  "workload_profile_id": "interactive_chat_v1",
               }
               for session, shape in submissions
            ],
         }
         for name, submissions in BROWSER_ROUNDS
      ],
   }
   return {**body, "workload_digest": _sha256_json(body)}


def _browser_request_valid(record: object, expected: Mapping[str, Any], round_name: str) -> bool:
   extra = set()
   if round_name == "late_arrival":
      extra = {"first_token_observed_at_ms"} if expected["session"] == "a" else {"arrived_while_leader_decoding"}
   if not isinstance(record, Mapping) or set(record) != _BROWSER_REQUEST_FIELDS | extra:
      return False
   tokens = record["tokens"]
   return (
      record["session"] == expected["session"]
      and record["round"] == round_name
      and record["index"] == expected["index"]
      and record["max_new_tokens"] == expected["max_new_tokens"]
      and record["qos_class"] == expected["qos_class"]
      and isinstance(record["request_ref"], str)
      and _REF.fullmatch(record["request_ref"]) is not None
      and record["submitted_through_ui"] is True
      and record["frozen_submission_verified"] is True
      and record["terminal"] == "completed"
      and type(tokens) is int
      and 1 <= tokens <= expected["max_new_tokens"]
      and (record.get("arrived_while_leader_decoding", True) is True)
   )


def validate_browser_report(
   report: object, *, workload: Mapping[str, Any], mode: str
) -> dict[str, Any]:
   """Decide one harness report; any deviation is a coded refusal (GateError)."""

   if not isinstance(report, Mapping) or set(report) != _BROWSER_REPORT_FIELDS:
      raise GateError("browser_report_shape_invalid")
   if report["protocol"] != BROWSER_PROTOCOL:
      raise GateError("browser_report_protocol_invalid")
   if report["mode"] != mode or mode not in BROWSER_MODES:
      raise GateError("browser_report_mode_mismatch")
   if report["workload_digest"] != workload["workload_digest"]:
      raise GateError("browser_report_workload_mismatch")
   if report["independent_sessions"] != 2:
      raise GateError("browser_report_sessions_invalid")
   if report["passed"] is not True or report["failures"] != []:
      raise GateError("browser_report_not_passed")
   if list(report["checks"]) != list(REQUIRED_BROWSER_CHECKS):
      raise GateError("browser_report_checks_incomplete")
   if report["live_evidence"] is not (mode == "live"):
      raise GateError("browser_report_not_live_evidence")
   if report["qualification_claim"] is not False or report["physical_claim"] is not False:
      raise GateError("browser_report_claim_invalid")
   expected = [
      (round_["round"], item) for round_ in workload["rounds"] for item in round_["submissions"]
   ]
   records = report["requests"]
   if (
      not isinstance(records, list)
      or len(records) != len(expected)
      or not all(
         _browser_request_valid(record, item, name)
         for record, (name, item) in zip(records, expected)
      )
      or len({record["request_ref"] for record in records}) != len(records)
   ):
      raise GateError("browser_report_requests_invalid")
   if report["engine"] not in BROWSER_ENGINES or not isinstance(report["deployment_id"], str):
      raise GateError("browser_report_shape_invalid")
   rounds = report["rounds"] if isinstance(report["rounds"], Mapping) else {}

   def _count(round_name: str, key: str) -> int | None:
      value = rounds.get(round_name, {}).get(key) if isinstance(rounds.get(round_name), Mapping) else None
      return value if type(value) is int and value >= 0 else None

   return {
      "mode": mode,
      "engine": report["engine"],
      "deployment_id": report["deployment_id"],
      "workload_digest": report["workload_digest"],
      "concurrent_acceptance_skew_ms": _count("concurrent", "acceptance_skew_ms"),
      "leader_tokens_before_late_submit": _count("late_arrival", "leader_tokens_before_late_submit"),
      "request_refs": [record["request_ref"] for record in records],
      "requests": [
         {key: record[key] for key in ("session", "round", "index", "tokens", "terminal")}
         for record in records
      ],
   }


def browser_runtime_evidence(
   publications: Sequence[Mapping[str, Any]], request_refs: Sequence[str]
) -> dict[str, Any]:
   """What the route's own runtime publications show for the browser requests.

   A request is *executed on the route* when a node published a stage-compute
   interval under its pseudonym, and *in a true runtime batch* when one of its
   intervals carries a ``runtime_batch_ref`` shared with at least one other
   request.  Absent publications count as nothing, never as success.
   """

   refs = set(request_refs)
   executed: set[str] = set()
   members: dict[str, set[str]] = {}
   for publication in publications:
      for interval in publication.get("intervals", ()) if isinstance(publication, Mapping) else ():
         if not isinstance(interval, Mapping):
            continue
         ref = interval.get("request_ref")
         if ref in refs:
            executed.add(ref)
         batch = interval.get("runtime_batch_ref")
         if isinstance(batch, str) and batch and isinstance(ref, str):
            members.setdefault(batch, set()).add(ref)
   true_batches = {batch: refs_in for batch, refs_in in members.items() if len(refs_in) >= 2}
   batched = {ref for refs_in in true_batches.values() for ref in refs_in if ref in refs}
   with_browser = [refs_in for refs_in in true_batches.values() if refs_in & refs]
   return {
      "browser_requests": len(refs),
      "executed_on_route": len(executed),
      "in_true_runtime_batch": len(batched),
      "browser_pair_in_one_batch": any(len(refs_in & refs) >= 2 for refs_in in with_browser),
      "true_batches_with_browser_members": len(with_browser),
   }


def browser_runtime_blockers(evidence: Mapping[str, Any]) -> list[str]:
   blockers = []
   if evidence["browser_requests"] == 0 or evidence["executed_on_route"] != evidence["browser_requests"]:
      blockers.append("browser_request_not_executed_on_route")
   if evidence["in_true_runtime_batch"] < 1:
      blockers.append("browser_requests_not_in_true_runtime_batch")
   return blockers


class _PublicationCollector:
   """Union the route's A10 publications while the browser phase runs.

   A publication retains only its most recent intervals, so the live status is
   read repeatedly and intervals are merged by their node-unique ``interval_ref``.
   """

   def __init__(self, live_status: Callable[[], Mapping[str, Any]], interval_s: float = 1.0) -> None:
      self._live_status = live_status
      self._interval_s = interval_s
      self._intervals: dict[str, dict[str, Any]] = {}
      self._lock = threading.Lock()
      self._stop = threading.Event()
      self._thread = threading.Thread(target=self._run, name="a10-browser-publications", daemon=True)
      self.deployment_id: str | None = None

   def _poll(self) -> None:
      try:
         status = self._live_status()
      except Exception:  # noqa: BLE001 - a missed read is simply not evidence
         return
      if isinstance(status.get("deployment_id"), str):
         self.deployment_id = status["deployment_id"]
      with self._lock:
         for publication in publications_from_status(status):
            for interval in publication.get("intervals", ()):
               if isinstance(interval, Mapping) and isinstance(interval.get("interval_ref"), str):
                  self._intervals[interval["interval_ref"]] = dict(interval)

   def _run(self) -> None:
      while not self._stop.wait(self._interval_s):
         self._poll()

   def __enter__(self) -> "_PublicationCollector":
      self._poll()
      self._thread.start()
      return self

   def __exit__(self, *_exc: object) -> None:
      self._stop.set()
      self._thread.join(timeout=30)
      self._poll()

   def publications(self) -> list[dict[str, Any]]:
      with self._lock:
         return [{"intervals": list(self._intervals.values())}]


class BrowserSessionsPhase:
   """Run the two-session browser harness against the gate's own base URL.

   One engine, one invocation (``node a10-browser-sessions.mjs --engine=…``), with
   the frozen workload written next to the evidence.  ``preflight`` checks the
   configuration and the engine executable without contacting the origin, so a
   missing browser refuses before the benchmark starts.  ``__call__`` returns the
   sealed gate-1 browser evidence; it passes only when the harness report is a
   complete, live, passed report for the frozen workload, bound to the live
   deployment, and the route's runtime publications show every browser request
   executed and at least one in a true runtime batch.
   """

   def __init__(
      self,
      *,
      base_url: str,
      out: Path,
      live_status: Callable[[], Mapping[str, Any]],
      engine: str = "chromium",
      node: str = "node",
      harness: Path = BROWSER_HARNESS,
      mode: str = "live",
      timeout_s: float = 1800.0,
      environment: Mapping[str, str] | None = None,
   ) -> None:
      if engine not in BROWSER_ENGINES:
         raise GateError("browser_engine_invalid")
      if mode not in BROWSER_MODES:
         raise GateError("browser_mode_invalid")
      self.base_url = base_url.rstrip("/")
      self.out = Path(out)
      self.evidence = self.out / "browser-sessions"
      self.live_status = live_status
      self.engine = engine
      self.node = node
      self.harness = Path(harness)
      self.mode = mode
      self.timeout_s = timeout_s
      self.workload = browser_workload()
      self._environment = dict(environment or {})

   def _env(self) -> dict[str, str]:
      env = {
         key: value
         for key, value in os.environ.items()
         if key in {"HOME", "PATH", "TMPDIR", "LANG", "PLAYWRIGHT_BROWSERS_PATH"}
      }
      env.update(self._environment)
      env.update(
         A10_BROWSER_ORIGIN=self.base_url,
         A10_BROWSER_EVIDENCE=str(self.evidence / self.engine),
         A10_BROWSER_WORKLOAD=str(self.out / "browser-workload.json"),
         A10_BROWSER_MODE=self.mode,
      )
      return env

   def _invoke(self, *arguments: str, timeout: float) -> subprocess.CompletedProcess[str]:
      try:
         return subprocess.run(
            [self.node, str(self.harness), f"--engine={self.engine}", *arguments],
            cwd=str(self.harness.parent.parent),
            env=self._env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
         )
      except (OSError, subprocess.TimeoutExpired):
         raise GateError("browser_harness_unavailable") from None

   def preflight(self) -> dict[str, Any]:
      if not self.harness.is_file():
         raise GateError("browser_harness_missing")
      if self.evidence.exists():
         raise GateError("browser_evidence_directory_exists")
      write_private_json(self.out / "browser-workload.json", self.workload)
      completed = self._invoke("--preflight", timeout=120)
      try:
         document = json.loads(completed.stdout.strip().splitlines()[-1])
      except (IndexError, ValueError):
         document = None
      if (
         completed.returncode != 0
         or not isinstance(document, Mapping)
         or document.get("engine_executable_present") is not True
         or document.get("workload_digest") != self.workload["workload_digest"]
      ):
         raise GateError("browser_harness_preflight_failed")
      return dict(document)

   def __call__(self) -> dict[str, Any]:
      blockers: list[str] = []
      decided: dict[str, Any] | None = None
      runtime: dict[str, Any] | None = None
      with _PublicationCollector(self.live_status) as collector:
         completed = self._invoke(timeout=self.timeout_s)
      try:
         report = read_json(self.evidence / self.engine / "browser-report.json")
      except (OSError, ValueError):
         report = None
         blockers.append("browser_report_missing")
      if report is not None:
         try:
            decided = validate_browser_report(report, workload=self.workload, mode=self.mode)
         except GateError as error:
            blockers.append(str(error))
            failures = report.get("failures") if isinstance(report, Mapping) else None
            for item in failures[:8] if isinstance(failures, list) else ():
               # Only closed codes are recorded; free text could carry a path.
               code = item if isinstance(item, str) and _HARNESS_CODE.fullmatch(item) else "unclassified"
               blockers.append(f"harness:{code}")
      if completed.returncode != 0 and not blockers:
         blockers.append("browser_harness_exit_nonzero")
      if decided is not None:
         if collector.deployment_id is None or decided["deployment_id"] != collector.deployment_id:
            blockers.append("browser_deployment_binding_mismatch")
         runtime = browser_runtime_evidence(collector.publications(), decided["request_refs"])
         blockers.extend(browser_runtime_blockers(runtime))
      checks_passed = decided is not None and not blockers
      if self.mode != "live":
         blockers.append("local_conformance_is_not_gate_evidence")
      return {
         "protocol": BROWSER_EVIDENCE_PROTOCOL,
         "mode": self.mode,
         "engine": self.engine,
         "workload_digest": self.workload["workload_digest"],
         "report_digest": None if report is None else _sha256_json(report),
         "browser": None
         if decided is None
         else {key: value for key, value in decided.items() if key != "request_refs"},
         "runtime": runtime,
         "checks_passed": checks_passed,
         "passed": checks_passed and self.mode == "live",
         "blockers": blockers,
      }


# ---------------------------------------------------------------------------
# negatives and qualify
# ---------------------------------------------------------------------------


def fault_negative_runner(grant: object) -> Callable[[str], dict[str, Any]]:
   """The CPU fault-hook executor for one gate run; refuses without the exact grant."""

   try:
      A10FaultHooks(grant=grant)
   except A10FaultHookRefused:
      raise GateError("explicit_fleet_grant_required") from None
   return lambda kind: run_fault_negative(kind, grant=grant)


def run_product_negatives(
   client: Any,
   *,
   out: Path,
   fault_runner: Callable[[str], Mapping[str, Any]] | None = None,
   fault_unavailable_reason: str = "requires_fault_hook",
) -> dict[str, Any]:
   """Execute the negatives the product API drives, then the fault-hook negatives.

   Every required negative is recorded.  One that has no executor stays
   ``not_executed`` with its reason (never a pass); a hook that raises is recorded
   ``violated`` by code only, never by exception text.
   """

   results: dict[str, dict[str, Any]] = {}
   shape = RequestShape(0, "prompt_short", "output_long", "interactive", 0.0)
   # Cancellation isolation: cancel one of two concurrent requests; the other must
   # complete and the route must return to zero residue.
   if hasattr(client, "cancellation_isolation"):
      results["cancellation_isolation"] = client.cancellation_isolation(shape)
   if hasattr(client, "queue_overflow"):
      results["queue_overflow"] = client.queue_overflow(shape)
   if hasattr(client, "slow_consumer"):
      results["slow_consumer"] = client.slow_consumer(shape)
   for case_id in REQUIRED_RUNTIME_BATCH_NEGATIVES:
      if case_id in results:
         continue
      if fault_runner is not None and case_id in FAULT_KINDS:
         try:
            results[case_id] = dict(fault_runner(case_id))
         except Exception:  # noqa: BLE001 - a hook failure is recorded by code only
            results[case_id] = {
               "outcome": "violated",
               "reason": "fault_hook_raised",
               "residue": None,
               "surface": CPU_SURFACE,
               "physical_pending": True,
            }
         continue
      results[case_id] = {"outcome": "not_executed", "reason": fault_unavailable_reason, "residue": None}
   write_private_json(out / "negatives.json", results)
   return results


def assemble_and_qualify(out: Path, *, benchmark_manifest_digest: str, cross_host_clock_uncertainty: float, provenance: str) -> dict[str, Any]:
   summary = read_json(out / "benchmark-summary.json")
   run = read_json(out / "benchmark-run.json")
   status = read_json(out / "live-status-final.json") if (out / "live-status-final.json").is_file() else {}
   latency = summary["latency_ms"]
   counts = summary["reliability"]

   def rates(mode: str) -> dict[str, float]:
      total = max(1, sum(counts.get(mode, {}).values()))
      return {
         "terminal_error_rate": counts.get(mode, {}).get("failed", 0) / total,
         "timeout_rate": 0.0,
         "cancellation_rate": counts.get(mode, {}).get("cancelled", 0) / total,
         "cleanup_success_rate": 1.0,
      }

   evidence = {
      "protocol": RUNTIME_BATCH_EVIDENCE_PROTOCOL,
      "provenance": provenance,
      "execution_path": "product_dispatcher",
      "benchmark_manifest_digest": benchmark_manifest_digest,
      "acceptance_inventory_digest": ACCEPTANCE_INVENTORY_DIGEST,
      "publications": publications_from_status(status),
      "cross_host_clock_uncertainty": cross_host_clock_uncertainty,
      "benchmark_run": run,
      "latency_ms": {
         "baseline_interactive_p95_ttft": latency.get("baseline_interactive_p95_ttft", math.inf),
         "candidate_interactive_p95_ttft": latency.get("candidate_interactive_p95_ttft", math.inf),
         "baseline_interactive_p95_tpot": latency.get("baseline_interactive_p95_tpot", math.inf),
         "candidate_interactive_p95_tpot": latency.get("candidate_interactive_p95_tpot", math.inf),
      },
      "reliability": {"baseline": rates("baseline"), "candidate": rates("candidate")},
      "output_parity": summary["output_parity"],
      "residual_resources": read_json(out / "residual-resources.json")
      if (out / "residual-resources.json").is_file()
      else {field: -1 for field in RESIDUAL_RESOURCE_FIELDS},
      "negatives": read_json(out / "negatives.json") if (out / "negatives.json").is_file() else {},
   }
   for key, value in evidence["latency_ms"].items():
      if not math.isfinite(value):
         evidence["latency_ms"][key] = 1e12
   residual = evidence["residual_resources"]
   if any(value < 0 for value in residual.values()):
      evidence["residual_resources"] = {field: 1 for field in RESIDUAL_RESOURCE_FIELDS}
   request = QualificationRequest(
      owner_private_root=str(out.name),
      benchmark_manifest_digest=benchmark_manifest_digest,
      acceptance_inventory_digest=ACCEPTANCE_INVENTORY_DIGEST,
      baseline_windows=16,
      candidate_windows=16,
      valid_pairs=12,
   )
   try:
      bundle = qualify_runtime_batch(request, evidence)
   except Exception as error:  # noqa: BLE001 - a refusal is recorded by code only
      bundle = {"decision": "not_qualified", "blockers": [getattr(error, "code", type(error).__name__)]}
   write_private_json(out / "runtime-batch-qualification.json", bundle)
   return bundle


def gate_decision(
   out: Path,
   bundle: Mapping[str, Any],
   *,
   accept_cpu_fault_hook_negatives: bool = False,
) -> dict[str, Any]:
   """Gate-level decision: the runtime-batch bundle plus gate 1's browser sessions.

   The sealed runtime-batch bundle is left untouched; this document binds it and
   the gate-1 browser evidence, and is ``qualified`` only when both are.  A negative
   that was executed only through a CPU fault hook (``physical_pending``) is listed
   in ``physical_negatives_pending`` and blocks the decision unless the owner
   explicitly accepted CPU hook execution; the list is recorded either way.
   """

   summary = read_json(out / "benchmark-summary.json") if (out / "benchmark-summary.json").is_file() else {}
   browser = summary.get("browser_sessions") if isinstance(summary, Mapping) else None
   negatives = read_json(out / "negatives.json") if (out / "negatives.json").is_file() else {}
   pending = sorted(
      case_id
      for case_id, result in (negatives.items() if isinstance(negatives, Mapping) else ())
      if isinstance(result, Mapping) and result.get("physical_pending") is True
   )
   blockers = list(bundle.get("blockers", []))
   if not isinstance(browser, Mapping) or browser.get("passed") is not True:
      blockers.append("browser_sessions_not_proven")
   if not accept_cpu_fault_hook_negatives:
      blockers.extend(f"physical_negative_execution_pending:{case_id}" for case_id in pending)
   decision = {
      "protocol": GATE_PROTOCOL,
      "subcommand": "qualify",
      "runtime_batch_qualification_binding": bundle.get("qualification_binding"),
      "browser_sessions_digest": _sha256_json(browser) if isinstance(browser, Mapping) else None,
      "physical_negatives_pending": pending,
      "cpu_fault_hook_negatives_accepted": bool(accept_cpu_fault_hook_negatives),
      "decision": "qualified" if bundle.get("decision") == "qualified" and not blockers else "not_qualified",
      "blockers": sorted(set(blockers)),
   }
   write_private_json(out / "gate-decision.json", decision)
   return decision


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# verifier-product: the same matrix through the product dispatcher
# ---------------------------------------------------------------------------

_REFUSAL_CODE = re.compile(r"[a-z][a-z0-9_:]{0,80}\Z")


class ProductVerifierClient:
   """Send one operation to the grant-gated loopback A10 control of a live serve."""

   def __init__(self, control_url: str, *, grant: str, timeout_s: float) -> None:
      from urllib.parse import urlsplit

      from mycelium_a10_verify_control import CONTROL_PATH

      parts = urlsplit(control_url)
      if (
         parts.scheme != "http"
         or parts.hostname != "127.0.0.1"
         or parts.port is None
         or parts.path != CONTROL_PATH
         or parts.query
         or parts.fragment
      ):
         raise GateError("control_url_invalid")
      self._port = parts.port
      self._path = CONTROL_PATH
      self._grant = grant
      self._timeout_s = timeout_s

   def __call__(self, node_id: str, payload: Mapping[str, Any]) -> tuple[str, Any]:
      import http.client

      body = json.dumps(
         {"node_id": node_id, "operation": dict(payload), "timeout_s": self._timeout_s}
      ).encode("utf-8")
      connection = http.client.HTTPConnection("127.0.0.1", self._port, timeout=self._timeout_s + 30)
      try:
         connection.request("POST", self._path, body=body, headers={
            "Content-Type": "application/json",
            "X-Mycelium-A10-Fleet-Grant": self._grant,
         })
         response = connection.getresponse()
         status, raw = response.status, response.read()
      except OSError:
         raise GateError("product_verification_transport_failed") from None
      finally:
         connection.close()
      try:
         document = json.loads(raw.decode("utf-8"))
      except (ValueError, UnicodeDecodeError):
         raise GateError("product_verification_response_invalid") from None
      if status != 200:
         code = document.get("code") if isinstance(document, dict) else None
         if not isinstance(code, str) or not _REFUSAL_CODE.fullmatch(code):
            code = "unknown"
         raise GateError(f"product_verification_refused:{code}")
      if not isinstance(document, dict) or set(document) != {"kind", "result"}:
         raise GateError("product_verification_response_invalid")
      return document["kind"], document["result"]


def run_product_verifier_gate(
   client: Callable[[str, Mapping[str, Any]], tuple[str, Any]],
   *,
   node_id: str,
   plan: MatrixPlan,
   binding: Mapping[str, str],
   out: Path,
   progress: Callable[[str], None] = lambda message: None,
) -> dict[str, Any]:
   from mycelium_router.a10_product_verifier import (
      A10ProductVerifierError,
      operation_payload,
      unwrap_product_result,
   )
   from mycelium_router.a10_qualification import VERIFIER_NEGATIVE_FIELDS

   started = time.monotonic()
   records: list[dict[str, Any]] = []
   negatives: list[dict[str, Any]] = []
   try:
      for index in range(plan.operations):
         payload = operation_payload(plan, index=index, **binding)
         kind, record = client(node_id, payload)
         if kind != "operation":
            raise GateError("product_verification_response_invalid")
         records.append(unwrap_product_result(payload, record)[1])
         progress(f"operation {index + 1}/{plan.operations}")
      for case_id in VERIFIER_NEGATIVE_ORDER:
         payload = operation_payload(plan, case_id=case_id, **binding)
         kind, negative = client(node_id, payload)
         if (
            kind != "negative"
            or not isinstance(negative, Mapping)
            or set(negative) != VERIFIER_NEGATIVE_FIELDS
            or negative["case_id"] != case_id
         ):
            raise GateError("product_verification_response_invalid")
         negatives.append(dict(negative))
   except A10ProductVerifierError as error:
      raise GateError(f"product_verification_invalid:{error.code}") from None
   request = TargetVerificationQualificationRequest(
      owner_private_root=str(out.name),
      runtime_digest=binding["runtime_digest"],
      width_classes=("one", "qualified_default", "qualified_maximum"),
      prefix_length_buckets=len(plan.prefix_buckets),
      operations=plan.operations,
      sequential_parity_proof_digest=canonical_digest(records),
   )
   bundle = qualify_target_verifier(request, records, negatives)
   summary = {
      "protocol": GATE_PROTOCOL,
      "subcommand": "verifier-product",
      "backend": binding["backend"],
      "execution_path": "product_dispatcher",
      "elapsed_seconds": round(time.monotonic() - started, 3),
      "decision": bundle["decision"],
      "matrix_passed": bundle["matrix_passed"],
      "blockers": bundle["blockers"],
   }
   write_private_json(out / "verifier-operations.json", records)
   write_private_json(out / "verifier-negatives.json", negatives)
   write_private_json(out / "verifier-qualification.json", bundle)
   write_private_json(out / "verifier-summary.json", summary)
   return bundle


def _widths(value: str) -> dict[str, int]:
   parts = [int(item) for item in value.split(",")]
   if len(parts) != 3:
      raise argparse.ArgumentTypeError("widths are one,default,maximum")
   return {"one": parts[0], "qualified_default": parts[1], "qualified_maximum": parts[2]}


def _lengths(value: str) -> tuple[int, ...]:
   return tuple(int(item) for item in value.split(","))


def parser() -> argparse.ArgumentParser:
   root = argparse.ArgumentParser(description=__doc__.splitlines()[0])
   sub = root.add_subparsers(dest="command", required=True)
   matrix = sub.add_parser("verifier-matrix")
   matrix.add_argument("--snapshot", type=Path, required=True)
   matrix.add_argument("--model-id", required=True)
   matrix.add_argument("--revision", required=True)
   matrix.add_argument("--backend", choices=("mlx", "numpy"), default="mlx")
   matrix.add_argument("--dtype", default="float16")
   matrix.add_argument("--quantization", default="int8-weight-only")
   matrix.add_argument("--widths", type=_widths, default=_widths("1,4,8"))
   matrix.add_argument("--short-prefix", type=_lengths, default=(16, 24))
   matrix.add_argument("--long-prefix", type=_lengths, default=(96, 128))
   matrix.add_argument("--operations", type=int, default=102)
   matrix.add_argument("--provenance", choices=("live", "fixture"), default="live")
   matrix.add_argument("--out", type=Path, required=True)
   product = sub.add_parser("verifier-product")
   product.add_argument("--control-url", required=True)
   product.add_argument("--node-id", required=True)
   product.add_argument("--runtime-digest", required=True)
   product.add_argument("--model-digest", required=True)
   product.add_argument("--representation-digest", required=True)
   product.add_argument("--backend", choices=("mlx", "numpy"), required=True)
   product.add_argument("--vocab-size", type=int, required=True)
   product.add_argument("--widths", type=_widths, default=_widths("1,4,8"))
   product.add_argument("--short-prefix", type=_lengths, default=(16, 24))
   product.add_argument("--long-prefix", type=_lengths, default=(96, 128))
   product.add_argument("--operations", type=int, default=102)
   product.add_argument("--timeout-s", type=float, default=60.0)
   product.add_argument("--out", type=Path, required=True)
   bench = sub.add_parser("benchmark")
   bench.add_argument("--base-url", required=True)
   bench.add_argument("--baseline-command", required=True)
   bench.add_argument("--candidate-command", required=True)
   bench.add_argument("--out", type=Path, required=True)
   bench.add_argument("--browser-engine", choices=BROWSER_ENGINES, default="chromium")
   bench.add_argument("--node", default=shutil.which("node") or "node")
   negative = sub.add_parser("negatives")
   negative.add_argument("--base-url", required=True)
   negative.add_argument("--out", type=Path, required=True)
   negative.add_argument(
      "--no-fault-hooks",
      action="store_true",
      help="record the nine fault-hook negatives as not_executed instead of running them on CPU",
   )
   qualify = sub.add_parser("qualify")
   qualify.add_argument("--out", type=Path, required=True)
   qualify.add_argument("--benchmark-manifest-digest", required=True)
   qualify.add_argument("--cross-host-clock-uncertainty", type=float, required=True)
   qualify.add_argument("--provenance", choices=("live", "fixture"), default="live")
   qualify.add_argument(
      "--accept-cpu-fault-hook-negatives",
      action="store_true",
      help="owner acceptance of negatives executed only through the CPU fault hooks",
   )
   return root


def main(argv: Sequence[str] | None = None) -> int:
   arguments = parser().parse_args(argv)
   out = _private_directory(arguments.out)
   try:
      if arguments.command == "verifier-matrix":
         target = build_local_target(
            arguments.snapshot,
            model_id=arguments.model_id,
            revision=arguments.revision,
            backend=arguments.backend,
            dtype=arguments.dtype,
            quantization=arguments.quantization,
         )
         bundle = run_verifier_gate(
            target,
            widths=arguments.widths,
            prefix_buckets={"short": arguments.short_prefix, "long": arguments.long_prefix},
            operations=arguments.operations,
            provenance=arguments.provenance,
            out=out,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
         )
         print(json.dumps({"decision": bundle["decision"], "matrix_passed": bundle["matrix_passed"], "blockers": bundle["blockers"]}, sort_keys=True))
         return 0 if bundle["matrix_passed"] else 1
      if arguments.command == "verifier-product":
         from mycelium_a10_verify_control import grant_from_environment

         grant = grant_from_environment()
         if grant is None:
            raise GateError("explicit_fleet_grant_required")
         if not 0 < arguments.timeout_s <= 120:
            raise GateError("timeout_invalid")
         client = ProductVerifierClient(
            arguments.control_url, grant=grant, timeout_s=arguments.timeout_s
         )
         try:
            plan = MatrixPlan(
               widths=dict(arguments.widths),
               prefix_buckets={"short": arguments.short_prefix, "long": arguments.long_prefix},
               operations=arguments.operations,
               vocab_size=arguments.vocab_size,
            )
         except Exception:  # noqa: BLE001 - only the code is recorded
            raise GateError("matrix_plan_invalid") from None
         bundle = run_product_verifier_gate(
            client,
            node_id=arguments.node_id,
            plan=plan,
            binding={
               "runtime_digest": arguments.runtime_digest,
               "model_digest": arguments.model_digest,
               "representation_digest": arguments.representation_digest,
               "backend": arguments.backend,
            },
            out=out,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
         )
         print(json.dumps({"decision": bundle["decision"], "matrix_passed": bundle["matrix_passed"], "blockers": bundle["blockers"]}, sort_keys=True))
         return 0 if bundle["matrix_passed"] else 1
      if arguments.command == "benchmark":
         # Gate 1's two clean browser sessions: refuse before any traffic when the
         # harness or its browser engine is unavailable (no origin contact here).
         browser = BrowserSessionsPhase(
            base_url=arguments.base_url,
            out=out,
            live_status=lambda: client.live_status(),
            engine=arguments.browser_engine,
            node=arguments.node,
         )
         browser.preflight()
      client = ProductClient(arguments.base_url) if hasattr(arguments, "base_url") else None
      if arguments.command == "benchmark":
         summary = run_benchmark(
            client,
            switch_mode=command_switcher({"baseline": arguments.baseline_command, "candidate": arguments.candidate_command}),
            observe_mode=mode_observer(client),
            out=out,
            browser_sessions=browser,
         )
         write_private_json(out / "live-status-final.json", client.live_status())
         print(
            json.dumps(
               {"throughput": summary["throughput"]["decision"], "browser_sessions": summary["browser_sessions"]["passed"]},
               sort_keys=True,
            )
         )
         return 0
      if arguments.command == "negatives":
         fault_runner: Callable[[str], Mapping[str, Any]] | None = None
         unavailable = "requires_fault_hook"
         if not arguments.no_fault_hooks:
            from mycelium_a10_verify_control import grant_from_environment

            grant = grant_from_environment()
            if grant is None:
               unavailable = "fault_hook_grant_missing"
            else:
               fault_runner = fault_negative_runner(grant)
         results = run_product_negatives(
            client, out=out, fault_runner=fault_runner, fault_unavailable_reason=unavailable
         )
         print(json.dumps({case: item["outcome"] for case, item in sorted(results.items())}, sort_keys=True))
         return 0
      bundle = assemble_and_qualify(
         out,
         benchmark_manifest_digest=arguments.benchmark_manifest_digest,
         cross_host_clock_uncertainty=arguments.cross_host_clock_uncertainty,
         provenance=arguments.provenance,
      )
      decision = gate_decision(
         out, bundle, accept_cpu_fault_hook_negatives=arguments.accept_cpu_fault_hook_negatives
      )
      print(
         json.dumps(
            {
               "decision": decision["decision"],
               "blockers": decision["blockers"],
               "physical_negatives_pending": decision["physical_negatives_pending"],
            },
            sort_keys=True,
         )
      )
      return 0 if decision["decision"] == "qualified" else 1
   except GateError as error:
      print(json.dumps({"error": str(error)}), file=sys.stderr)
      return 2


if __name__ == "__main__":
   raise SystemExit(main())
