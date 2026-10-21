# SPDX-License-Identifier: AGPL-3.0-only
"""A10 deterministic member isolation: cancellation points, slow consumers, KV.

The A10 specification (section 5) gives every batch member its own request,
attempt, path, reservation, stream, event sequence and stage-local KV identity.
Backend packing may share temporary tensor storage, but no member may address,
retain, commit or roll back another member's KV, and a slow consumer is detached
from future scheduling instead of blocking its batch peers.

This module implements exactly that deterministic half — the member-local KV
registry, the bounded gateway buffer, the named cancellation points, the
invocation-scoped tensor arena and the padding-excluding generation window.  It
executes no backend, opens no socket, and claims nothing physical.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from mycelium_router.a10_runtime_batch import A10ContractError


#: The five cancellation points named by the implementation contract (resolution 8).
CANCELLATION_POINTS = (
   "pre_collection",
   "pre_launch",
   "post_return",
   "pre_forward",
   "command_cancellation",
)

#: The two declared slow-consumer outcomes; either way only the slow member ends.
DETACHMENT_POLICIES = ("cancel", "terminate")

_BOUND_CEILING = 1 << 30


class A10IsolationError(A10ContractError):
   """A coded, fail-closed member-isolation refusal."""


def _reject(code: str) -> NoReturn:
   raise A10IsolationError(code)


def _text(value: object, *, code: str) -> str:
   if not isinstance(value, str) or not value or value != value.strip():
      _reject(code)
   return value


def _bounded_int(
   value: object,
   *,
   code: str,
   minimum: int,
   maximum: int,
) -> int:
   if (
      isinstance(value, bool)
      or not isinstance(value, int)
      or value < minimum
      or value > maximum
   ):
      _reject(code)
   return value


@dataclass(frozen=True)
class MemberIdentity:
   """One member's private request, attempt, path and KV identity.

   These are opaque private-path pseudonyms: no prompt, output, token or key
   material may appear here.
   """

   member_ref: str
   request_ref: str
   attempt_ref: str
   path_ref: str
   kv_ref: str

   def __post_init__(self) -> None:
      _text(self.member_ref, code="invalid_member_identity")
      _text(self.request_ref, code="invalid_member_identity")
      _text(self.attempt_ref, code="invalid_member_identity")
      _text(self.path_ref, code="invalid_member_identity")
      _text(self.kv_ref, code="invalid_member_identity")


@dataclass(frozen=True)
class MemberKvRecord:
   """One member's committed and tentative KV position."""

   member_ref: str
   kv_ref: str
   committed_prefix: int
   tentative_prefix: int
   retained: bool


class MemberKvRegistry:
   """Each member owns exactly one KV identity; peer access fails closed."""

   def __init__(self) -> None:
      self._records: dict[str, MemberKvRecord] = {}
      self._identities: dict[str, MemberIdentity] = {}
      self._kv_owners: dict[str, str] = {}

   # -- registration -------------------------------------------------------

   def register(
      self,
      identity: MemberIdentity,
      *,
      committed_prefix: int = 0,
   ) -> MemberKvRecord:
      if not isinstance(identity, MemberIdentity):
         _reject("invalid_member_identity")
      if identity.member_ref in self._records or identity.kv_ref in self._kv_owners:
         _reject("duplicate_member_identity")
      _bounded_int(
         committed_prefix,
         code="invalid_kv_position",
         minimum=0,
         maximum=_BOUND_CEILING,
      )
      record = MemberKvRecord(
         member_ref=identity.member_ref,
         kv_ref=identity.kv_ref,
         committed_prefix=committed_prefix,
         tentative_prefix=committed_prefix,
         retained=False,
      )
      self._records[identity.member_ref] = record
      self._identities[identity.member_ref] = identity
      self._kv_owners[identity.kv_ref] = identity.member_ref
      return record

   def identities(self) -> tuple[MemberIdentity, ...]:
      return tuple(self._identities.values())

   # -- member-local operations -------------------------------------------

   def address(self, *, member_ref: str, kv_ref: str) -> MemberKvRecord:
      return self._own(member_ref=member_ref, kv_ref=kv_ref)

   def retain(self, *, member_ref: str, kv_ref: str) -> MemberKvRecord:
      record = self._own(member_ref=member_ref, kv_ref=kv_ref)
      return self._store(record, retained=True)

   def begin_tentative(
      self,
      *,
      member_ref: str,
      kv_ref: str,
      position: int,
   ) -> MemberKvRecord:
      record = self._own(member_ref=member_ref, kv_ref=kv_ref)
      _bounded_int(
         position, code="invalid_kv_position", minimum=0, maximum=_BOUND_CEILING
      )
      if position < record.committed_prefix:
         _reject("invalid_kv_position")
      return self._store(record, tentative_prefix=position)

   def commit(self, *, member_ref: str, kv_ref: str, position: int) -> MemberKvRecord:
      record = self._own(member_ref=member_ref, kv_ref=kv_ref)
      _bounded_int(
         position, code="invalid_kv_position", minimum=0, maximum=_BOUND_CEILING
      )
      if position < record.committed_prefix:
         _reject("non_monotonic_kv_commit")
      if position > record.tentative_prefix:
         _reject("unverified_kv_commit_fails_closed")
      return self._store(
         record, committed_prefix=position, tentative_prefix=position
      )

   def rollback(self, *, member_ref: str, kv_ref: str) -> MemberKvRecord:
      record = self._own(member_ref=member_ref, kv_ref=kv_ref)
      return self._store(record, tentative_prefix=record.committed_prefix)

   # -- read-only views ----------------------------------------------------

   def snapshot(self) -> tuple[MemberKvRecord, ...]:
      return tuple(self._records.values())

   def prefixes(self) -> dict[str, tuple[int, int]]:
      return {
         record.member_ref: (record.committed_prefix, record.tentative_prefix)
         for record in self._records.values()
      }

   # -- internals ----------------------------------------------------------

   def _own(self, *, member_ref: str, kv_ref: str) -> MemberKvRecord:
      record = self._records.get(member_ref)
      if record is None:
         _reject("unknown_member_kv_identity")
      if kv_ref != record.kv_ref:
         _reject("cross_member_kv_access_fails_closed")
      return record

   def _store(self, record: MemberKvRecord, **changes) -> MemberKvRecord:
      updated = MemberKvRecord(
         member_ref=record.member_ref,
         kv_ref=record.kv_ref,
         committed_prefix=changes.get("committed_prefix", record.committed_prefix),
         tentative_prefix=changes.get("tentative_prefix", record.tentative_prefix),
         retained=changes.get("retained", record.retained),
      )
      self._records[record.member_ref] = updated
      return updated


@dataclass(frozen=True)
class ArenaSlice:
   """A bounded temporary-storage block owned by one invocation."""

   slice_ref: str
   member_ref: str
   size_bytes: int


class InvocationTensorArena:
   """Shared temporary tensor storage, scoped to exactly one invocation.

   Blocks live only inside the invocation: after ``close()`` nothing can be
   resolved, so shared packing storage cannot escape into another invocation,
   another member or later evidence.
   """

   def __init__(self, *, invocation_ref: str, capacity_bytes: int | None = None) -> None:
      self._invocation_ref = _text(invocation_ref, code="invalid_arena_ref")
      if capacity_bytes is not None:
         _bounded_int(
            capacity_bytes,
            code="invalid_arena_capacity",
            minimum=0,
            maximum=_BOUND_CEILING,
         )
      self._capacity_bytes = capacity_bytes
      self._slices: dict[str, int] = {}
      self._member_bytes: dict[str, int] = {}
      self._closed = False

   def allocate(self, *, member_ref: str, size_bytes: int) -> ArenaSlice:
      _text(member_ref, code="invalid_arena_member")
      _bounded_int(
         size_bytes, code="invalid_arena_block", minimum=0, maximum=_BOUND_CEILING
      )
      self._require_open()
      if (
         self._capacity_bytes is not None
         and sum(self._slices.values()) + size_bytes > self._capacity_bytes
      ):
         _reject("arena_capacity_exceeded")
      slice_ref = f"{self._invocation_ref}:block:{len(self._slices)}"
      self._slices[slice_ref] = size_bytes
      self._member_bytes[member_ref] = self._member_bytes.get(member_ref, 0) + size_bytes
      return ArenaSlice(slice_ref=slice_ref, member_ref=member_ref, size_bytes=size_bytes)

   def resolve(self, slice_ref: str) -> int:
      self._require_open()
      size = self._slices.get(slice_ref)
      if size is None:
         _reject("unknown_arena_slice")
      return size

   def close(self) -> None:
      self._slices.clear()
      self._member_bytes.clear()
      self._closed = True

   def is_closed(self) -> bool:
      return self._closed

   def open_block_count(self) -> int:
      return 0 if self._closed else len(self._slices)

   def bytes_in_flight(self) -> int:
      return 0 if self._closed else sum(self._slices.values())

   def _require_open(self) -> None:
      if self._closed:
         _reject("arena_escaped_the_invocation")


@dataclass(frozen=True)
class GenerationWindow:
   """The positions one member actually generated.

   ``padded_length`` carries packing/serialization shape metadata only.  Padding
   slots are never generated positions, so they can never be counted as output,
   committed, or attributed to the member.
   """

   member_ref: str
   first_position: int
   real_length: int
   padded_length: int

   def __post_init__(self) -> None:
      _text(self.member_ref, code="invalid_shape_metadata")
      _bounded_int(
         self.first_position,
         code="invalid_shape_metadata",
         minimum=0,
         maximum=_BOUND_CEILING,
      )
      _bounded_int(
         self.real_length,
         code="invalid_shape_metadata",
         minimum=1,
         maximum=_BOUND_CEILING,
      )
      _bounded_int(
         self.padded_length,
         code="invalid_shape_metadata",
         minimum=0,
         maximum=_BOUND_CEILING,
      )
      if self.padded_length < self.real_length:
         _reject("invalid_shape_metadata")

   def positions(self) -> tuple[int, ...]:
      return tuple(
         range(self.first_position, self.first_position + self.real_length)
      )

   def padding_positions(self) -> tuple[int, ...]:
      """Shape metadata for the padded slots; never generated positions."""

      return tuple(
         range(
            self.first_position + self.real_length,
            self.first_position + self.padded_length,
         )
      )

   def generated_token_count(self) -> int:
      return self.real_length

   def includes(self, position: int) -> bool:
      return position in self.positions()


def assert_generated_position(window: GenerationWindow, position: int) -> int:
   """Return ``position`` when it is a generated position, else fail closed."""

   if not isinstance(window, GenerationWindow):
      _reject("invalid_shape_metadata")
   if not isinstance(position, int) or isinstance(position, bool):
      _reject("padding_slot_is_not_a_generated_position")
   if not window.includes(position):
      _reject("padding_slot_is_not_a_generated_position")
   return position


@dataclass
class GatewayBuffer:
   """A bounded per-member gateway buffer; it never grows past its capacity."""

   member_ref: str
   capacity_bytes: int
   buffered_bytes: int = 0
   high_water_bytes: int = 0
   detached: bool = False

   def fits(self, event_bytes: int) -> bool:
      return self.buffered_bytes + event_bytes <= self.capacity_bytes

   def offer(self, event_bytes: int) -> None:
      self.buffered_bytes += event_bytes
      self.high_water_bytes = max(self.high_water_bytes, self.buffered_bytes)

   def drain(self) -> None:
      self.buffered_bytes = 0
