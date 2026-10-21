# SPDX-License-Identifier: AGPL-3.0-only
"""A10 `cross_member_kv_isolation` deterministic negatives.

Frozen invariants covered: every member has a distinct request/attempt/path/KV
identity; a member cannot address, retain, commit or roll back a peer's KV; shared
temporary tensor storage cannot escape the invocation; padding and shape metadata
never become generated positions; cross-member access fails closed; committed peer
prefixes remain unchanged; zero KV or result contamination.
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

# Resolve imports inside this lane's worktree even when another test module
# bound ``mycelium_router`` to the standing checkout first (see __init__).
ensure_worktree_imports()

import pytest

from mycelium_router import a10_runtime_batch as scaffold
from mycelium_router.a10_batch_admission import RuntimeBatchAdmission
from mycelium_router.a10_member_isolation import (
   GenerationWindow,
   InvocationTensorArena,
   MemberKvRegistry,
   assert_generated_position,
)
from tests.a10_deterministic.doubles import (
   BatchPort,
   PeerKvPort,
   identity,
   member,
   policy,
)

COLLECT_AT = 0.01
DISPATCH_AT = 0.011


def _code(exc: pytest.ExceptionInfo) -> str:
   return exc.value.code


def _registry_with_two_members() -> MemberKvRegistry:
   registry = MemberKvRegistry()
   registry.register(identity("member:a"), committed_prefix=4)
   registry.register(identity("member:b"), committed_prefix=4)
   return registry


# ---------------------------------------------------------------------------
# Identity: distinct, and peer access fails closed
# ---------------------------------------------------------------------------


def test_members_have_distinct_request_attempt_path_and_kv_identity():
   registry = _registry_with_two_members()
   first, second = registry.identities()

   for attribute in ("member_ref", "request_ref", "attempt_ref", "path_ref", "kv_ref"):
      assert getattr(first, attribute) != getattr(second, attribute)

   with pytest.raises(scaffold.A10ContractError) as raised:
      registry.register(identity("member:c", kv_ref="kv:member:a"), committed_prefix=0)
   assert _code(raised) == "duplicate_member_identity"


@pytest.mark.parametrize(
   "operation",
   [
      lambda registry: registry.address(member_ref="member:a", kv_ref="kv:member:b"),
      lambda registry: registry.retain(member_ref="member:a", kv_ref="kv:member:b"),
      lambda registry: registry.begin_tentative(
         member_ref="member:a", kv_ref="kv:member:b", position=8
      ),
      lambda registry: registry.commit(
         member_ref="member:a", kv_ref="kv:member:b", position=8
      ),
      lambda registry: registry.rollback(member_ref="member:a", kv_ref="kv:member:b"),
   ],
   ids=["address", "retain", "begin_tentative", "commit", "rollback"],
)
def test_member_cannot_address_retain_commit_or_rollback_peer_kv(operation):
   registry = _registry_with_two_members()
   baseline = registry.snapshot()

   with pytest.raises(scaffold.A10ContractError) as raised:
      operation(registry)

   assert _code(raised) == "cross_member_kv_access_fails_closed"
   assert registry.snapshot() == baseline


def test_unknown_member_kv_identity_is_refused():
   registry = _registry_with_two_members()
   with pytest.raises(scaffold.A10ContractError) as raised:
      registry.commit(member_ref="member:zz", kv_ref="kv:member:zz", position=1)
   assert _code(raised) == "unknown_member_kv_identity"


# ---------------------------------------------------------------------------
# Committed peer prefixes stay untouched
# ---------------------------------------------------------------------------


def test_committed_peer_prefixes_remain_unchanged():
   registry = MemberKvRegistry()
   port = BatchPort(committed={"member:a": 8, "member:b": 4})
   admission = RuntimeBatchAdmission(
      policy=policy(),
      port=port,
      incarnation="incarnation:1",
      stage_ref="stage-000",
      kv_registry=registry,
   )
   admission.submit(
      (
         member("member:a", committed_prefix=4),
         member("member:b", committed_prefix=4),
      ),
      now=0.0,
   )

   outcome = admission.dispatch(admission.collect(now=COLLECT_AT), now=DISPATCH_AT)

   assert outcome.forwarded == ("member:a", "member:b")
   assert registry.prefixes() == {"member:a": (8, 8), "member:b": (4, 4)}


def test_cross_member_result_contamination_fails_closed_and_leaves_kv_unchanged():
   registry = MemberKvRegistry()
   # A report that binds every member's execution to a peer KV identity.
   port = PeerKvPort()
   admission = RuntimeBatchAdmission(
      policy=policy(),
      port=port,
      incarnation="incarnation:1",
      stage_ref="stage-000",
      kv_registry=registry,
   )
   admission.submit(
      (
         member("member:a", committed_prefix=4),
         member("member:b", committed_prefix=4),
      ),
      now=0.0,
   )
   baseline = registry.snapshot()
   collection = admission.collect(now=COLLECT_AT)

   with pytest.raises(scaffold.A10ContractError) as raised:
      admission.dispatch(collection, now=DISPATCH_AT)

   assert _code(raised) == "cross_member_kv_isolation_fails_closed"
   assert registry.snapshot() == baseline


def test_zero_kv_or_result_contamination():
   registry = MemberKvRegistry()
   port = BatchPort()
   admission = RuntimeBatchAdmission(
      policy=policy(),
      port=port,
      incarnation="incarnation:1",
      stage_ref="stage-000",
      kv_registry=registry,
   )
   admission.submit(
      (
         member("member:a"),
         member("member:b"),
         member("member:c"),
      ),
      now=0.0,
   )

   outcome = admission.dispatch(admission.collect(now=COLLECT_AT), now=DISPATCH_AT)

   for member_ref, result in outcome.results:
      assert result.payload == f"out:{member_ref}".encode()
   assert [member_ref for member_ref, _ in outcome.results] == [
      "member:a",
      "member:b",
      "member:c",
   ]


# ---------------------------------------------------------------------------
# Shared temporary storage, padding and shape metadata
# ---------------------------------------------------------------------------


def test_temporary_tensor_storage_cannot_escape_the_invocation():
   arena = InvocationTensorArena(invocation_ref="invocation:1")
   block = arena.allocate(member_ref="member:a", size_bytes=64)
   assert arena.open_block_count() == 1

   arena.close()

   assert arena.is_closed() is True
   assert arena.open_block_count() == 0
   with pytest.raises(scaffold.A10ContractError) as raised:
      arena.resolve(block.slice_ref)
   assert _code(raised) == "arena_escaped_the_invocation"
   with pytest.raises(scaffold.A10ContractError) as escaped:
      arena.allocate(member_ref="member:b", size_bytes=8)
   assert _code(escaped) == "arena_escaped_the_invocation"


def test_padding_and_shape_metadata_never_become_generated_positions():
   window = GenerationWindow(
      member_ref="member:a",
      first_position=10,
      real_length=2,
      padded_length=5,
   )

   assert window.positions() == (10, 11)
   assert window.generated_token_count() == 2
   assert window.includes(10) is True
   assert window.includes(11) is True
   assert window.includes(12) is False
   assert window.includes(14) is False

   assert assert_generated_position(window, 11) == 11
   with pytest.raises(scaffold.A10ContractError) as raised:
      assert_generated_position(window, 12)
   assert _code(raised) == "padding_slot_is_not_a_generated_position"


def test_shape_metadata_bounds_are_coded():
   with pytest.raises(scaffold.A10ContractError) as raised:
      GenerationWindow(member_ref="member:a", first_position=0, real_length=3, padded_length=2)
   assert _code(raised) == "invalid_shape_metadata"

   with pytest.raises(scaffold.A10ContractError) as negative:
      GenerationWindow(
         member_ref="member:a", first_position=-1, real_length=1, padded_length=1
      )
   assert _code(negative) == "invalid_shape_metadata"
