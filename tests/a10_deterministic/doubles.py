# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic doubles and builders for the A10 deterministic-half suite.

Nothing here talks to a real runtime, a port, a socket or a model artifact.  Each
dispatch double reports honestly what it actually did — one invocation, a split,
a per-member concurrent dispatch, or a sequential loop of singleton calls — so the
admission path can be judged on the report rather than on a mode label.
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

# Resolve imports inside this lane's worktree even when another test module
# bound ``mycelium_router`` to the standing checkout first (see __init__).
ensure_worktree_imports()

from mycelium_router.a10_batch_admission import (
   BatchExecutionReport,
   BatchMember,
   MemberExecution,
   RuntimeBatchAdmissionPolicy,
   RuntimeBatchRequest,
)
from mycelium_router.a10_member_isolation import MemberIdentity
from mycelium_router.contracts import RuntimeBatchKey, RuntimeResult


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def compatibility_key(**overrides) -> RuntimeBatchKey:
   values = {
      "deployment_id": "deployment-1",
      "deployment_epoch": 7,
      "model_commit": "commit-abc",
      "manifest_digest": "manifest-abc",
      "placement_id": "placement-a",
      "assignment_id": "assignment-a",
      "stage_signature": "stage-signature-a",
      "load_proof_digest": "load-proof-a",
      "runtime_backend": "numpy",
      "phase": "DECODE",
      "hidden_size": 8,
      "activation_bytes": 4,
      "token_span": 1,
   }
   values.update(overrides)
   return RuntimeBatchKey(**values)


def identity(member_ref: str, **overrides) -> MemberIdentity:
   values = {
      "member_ref": member_ref,
      "request_ref": f"request:{member_ref}",
      "attempt_ref": f"attempt:{member_ref}",
      "path_ref": f"path:{member_ref}",
      "kv_ref": f"kv:{member_ref}",
   }
   values.update(overrides)
   return MemberIdentity(**values)


def member(member_ref: str, **overrides) -> BatchMember:
   values = {
      "identity": identity(member_ref),
      "phase": "DECODE",
      "qos_class": "batch",
      "encoded_bytes": 64,
      "enqueued_at": 0.0,
      "deadline_at": 1_000.0,
      "compatibility_key": compatibility_key(),
      "committed_prefix": 0,
   }
   values.update(overrides)
   return BatchMember(**values)


def policy(**overrides) -> RuntimeBatchAdmissionPolicy:
   values = {
      "maximum_members": 8,
      "maximum_batch_bytes": 1 << 16,
      "maximum_queued_members": 16,
      "maximum_queued_bytes": 1 << 20,
      "maximum_active_batches": 2,
      "collection_window_seconds": 0.002,
      "deadline_guard_seconds": 0.001,
      "maximum_consecutive_interactive_selections": 2,
      "maximum_retry_after_seconds": 0.05,
      "maximum_member_event_bytes": 4_096,
      "gateway_buffer_bytes": 64,
      "detachment_policy": "cancel",
   }
   values.update(overrides)
   return RuntimeBatchAdmissionPolicy(**values)


# ---------------------------------------------------------------------------
# Dispatch doubles
# ---------------------------------------------------------------------------


class BatchPort:
   """Batch-capable double: one real invocation, a runtime-owned report.

   It reports the members it executed in the order it accepted them, the
   position it committed for each one, and the bounded gateway events it
   produced.  Subclasses below inject one honesty failure each.
   """

   mode = "runtime_batch"
   incarnation = "incarnation:1"
   arena_open = False

   def __init__(
      self,
      *,
      committed: dict[str, int] | None = None,
      events: dict[str, tuple[int, ...]] | None = None,
      failures: tuple[str, ...] = (),
      partial: tuple[str, ...] = (),
      kv_refs: dict[str, str] | None = None,
   ) -> None:
      self.calls: list[RuntimeBatchRequest] = []
      self._committed = dict(committed or {})
      self._events = dict(events or {})
      self._failures = set(failures)
      self._partial = set(partial)
      self._kv_refs = dict(kv_refs or {})

   # -- override points ----------------------------------------------------

   def dispatch_mode(self) -> str:
      return self.mode

   def backend_invocations(self, request: RuntimeBatchRequest) -> int:
      return 1

   def executed_member_refs(self, request: RuntimeBatchRequest) -> tuple[str, ...]:
      return request.member_refs

   def member_kv_ref(self, member_ref: str) -> str:
      return self._kv_refs.get(member_ref, f"kv:{member_ref}")

   def execute_batch(self, request: RuntimeBatchRequest) -> BatchExecutionReport:
      self.calls.append(request)
      return BatchExecutionReport(
         dispatch_mode=self.dispatch_mode(),
         backend_invocations=self.backend_invocations(request),
         membership_authority="stage_runtime",
         incarnation=self.incarnation,
         executed=tuple(
            self._execution(index, member_ref)
            for index, member_ref in enumerate(self.executed_member_refs(request))
         ),
         arena_open=self.arena_open,
      )

   def _execution(self, index: int, member_ref: str) -> MemberExecution:
      return MemberExecution(
         member_ref=member_ref,
         kv_ref=self.member_kv_ref(member_ref),
         result=RuntimeResult(
            success=member_ref not in self._failures,
            payload=f"out:{member_ref}".encode(),
            token_id=1_000 + index,
         ),
         committed_prefix=self._committed.get(member_ref, 0),
         event_bytes=tuple(self._events.get(member_ref, (4,))),
         partial_commit=member_ref in self._partial,
      )


class SequentialLoopPort(BatchPort):
   """The test-only sequential fake: a loop of singleton executions."""

   mode = "sequential_dispatch"

   def backend_invocations(self, request: RuntimeBatchRequest) -> int:
      return len(request.member_refs)


class ConcurrentMemberPort(BatchPort):
   """Concurrent admission only: one invocation per member, dispatched together."""

   mode = "member_concurrent_dispatch"

   def backend_invocations(self, request: RuntimeBatchRequest) -> int:
      return len(request.member_refs)


class SplitPort(BatchPort):
   """A port that splits the offered group and still labels itself a batch."""

   def backend_invocations(self, request: RuntimeBatchRequest) -> int:
      return 2


class StaleIncarnationPort(BatchPort):
   incarnation = "incarnation:stale"


class MisorderedPort(BatchPort):
   def executed_member_refs(self, request: RuntimeBatchRequest) -> tuple[str, ...]:
      return tuple(reversed(request.member_refs))


class PeerKvPort(BatchPort):
   """Reports a result bound to a peer member's KV identity."""

   def member_kv_ref(self, member_ref: str) -> str:
      return "kv:peer-of-" + member_ref


class PartialCommitPort(BatchPort):
   pass


class ArenaEscapingPort(BatchPort):
   arena_open = True


class UnknownModePort(BatchPort):
   """A port that reports a dispatch mode outside the declared vocabulary."""

   mode = "batchish"
