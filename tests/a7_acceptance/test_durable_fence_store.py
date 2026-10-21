# SPDX-License-Identifier: AGPL-3.0-only
"""A7: durable fence generations, one publisher, restart reconciliation."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from mycelium_a7_fence_store import (
   DURABILITY,
   DurableFenceStore,
   FenceStoreError,
   request_key,
)

_REQ = "request:a7-fence"


def _code(callable_, *args, **kwargs) -> str:
   with pytest.raises(FenceStoreError) as refused:
      callable_(*args, **kwargs)
   return refused.value.code


def test_advance_is_monotonic_cas_and_survives_restart(tmp_path: Path) -> None:
   store = DurableFenceStore(tmp_path / "fence")
   first = store.advance(_REQ, expected_fencing_generation=0)
   assert (first["request_attempt"], first["path_generation"], first["fencing_generation"]) == (1, 1, 1)
   assert first["durability"] == DURABILITY
   assert _code(store.advance, _REQ, expected_fencing_generation=0) == "fence_generation_conflict"
   second = store.advance(_REQ, expected_fencing_generation=1)
   assert second["fencing_generation"] == 2

   restarted = DurableFenceStore(tmp_path / "fence")
   assert restarted.current(_REQ)["fencing_generation"] == 2
   assert restarted.accepts(_REQ, 2) and not restarted.accepts(_REQ, 1) and not restarted.accepts(_REQ, 3)
   record = tmp_path / "fence" / f"{request_key(_REQ)}.json"
   assert oct(record.stat().st_mode & 0o777) == "0o600"
   assert _REQ.encode() not in record.read_bytes()


def test_exactly_one_publisher_per_generation(tmp_path: Path) -> None:
   store = DurableFenceStore(tmp_path / "fence")
   assert _code(store.grant_publication, _REQ, generation=1) == "fence_publication_unavailable"
   store.advance(_REQ, expected_fencing_generation=0)
   assert _code(store.grant_publication, _REQ, generation=2) == "stale_generation"
   granted = store.grant_publication(_REQ, generation=1)
   assert granted["published_generation"] == 1
   assert _code(store.grant_publication, _REQ, generation=1) == "duplicate_publisher"
   # Advancing the fence voids the previous grant; the old generation is stale forever.
   store.advance(_REQ, expected_fencing_generation=1)
   assert _code(store.grant_publication, _REQ, generation=1) == "stale_generation"
   assert store.grant_publication(_REQ, generation=2)["published_generation"] == 2


def test_concurrent_grants_admit_exactly_one(tmp_path: Path) -> None:
   store = DurableFenceStore(tmp_path / "fence")
   store.advance(_REQ, expected_fencing_generation=0)
   results: list[str] = []
   barrier = threading.Barrier(8)

   def grant() -> None:
      barrier.wait()
      try:
         store.grant_publication(_REQ, generation=1)
         results.append("granted")
      except FenceStoreError as exc:
         results.append(exc.code)

   threads = [threading.Thread(target=grant) for _ in range(8)]
   for thread in threads:
      thread.start()
   for thread in threads:
      thread.join()
   assert sorted(results) == ["duplicate_publisher"] * 7 + ["granted"]


def test_terminal_once_then_release(tmp_path: Path) -> None:
   store = DurableFenceStore(tmp_path / "fence")
   store.advance(_REQ, expected_fencing_generation=0)
   assert _code(store.release, _REQ) == "fence_request_not_terminal"
   assert _code(store.mark_terminal, _REQ, generation=1, outcome="replay") == "fence_terminal_outcome_invalid"
   assert _code(store.mark_terminal, _REQ, generation=2, outcome="aborted") == "stale_generation"
   store.mark_terminal(_REQ, generation=1, outcome="fenced_kv_successor")
   assert _code(store.mark_terminal, _REQ, generation=1, outcome="aborted") == "duplicate_terminal"
   assert _code(store.advance, _REQ, expected_fencing_generation=1) == "fence_request_terminal"
   assert not store.accepts(_REQ, 1)
   assert store.release(_REQ) is True
   assert store.current(_REQ) is None


def test_tampered_or_unsafe_record_is_quarantined(tmp_path: Path) -> None:
   root = tmp_path / "fence"
   store = DurableFenceStore(root)
   store.advance(_REQ, expected_fencing_generation=0)
   record = root / f"{request_key(_REQ)}.json"
   record.write_bytes(record.read_bytes().replace(b'"fencing_generation":1', b'"fencing_generation":9'))
   assert _code(store.current, _REQ) == "fence_record_digest_mismatch"
   assert not record.exists()
   assert list((root / "quarantine").iterdir())
   # A quarantined request has no recoverable authority and is never "released".
   reconciled = store.reconcile(_REQ, shadow_checkpoint_valid=True, a6_available=True)
   assert (reconciled["state"], reconciled["reason"]) == ("fallback_required", "fence_record_quarantined")
   assert store.reconcile(_REQ, shadow_checkpoint_valid=True, a6_available=False)["state"] == "aborted"

   store.advance(_REQ, expected_fencing_generation=0)
   os.chmod(record, 0o644)
   assert _code(store.current, _REQ) == "fence_record_permissions_invalid"


def test_symlinked_root_is_refused(tmp_path: Path) -> None:
   real = tmp_path / "real"
   real.mkdir()
   link = tmp_path / "link"
   link.symlink_to(real)
   assert _code(DurableFenceStore, link) == "fence_store_root_invalid"


@pytest.mark.parametrize(
   ("setup", "shadow", "a6", "state", "reason"),
   [
      ("none", True, True, "released", "no_fence_record"),
      ("advanced", True, True, "shadow_restored", "shadow_checkpoint_valid"),
      ("advanced", False, True, "fallback_required", "shadow_checkpoint_unavailable"),
      ("advanced", False, False, "aborted", "shadow_checkpoint_unavailable"),
      ("publishing", True, True, "fallback_required", "ambiguous_publisher_fenced"),
      ("publishing", True, False, "aborted", "ambiguous_publisher_fenced"),
      ("terminal", True, True, "already_terminal", "full_context_replay"),
   ],
)
def test_restart_reconciliation_is_exactly_one_state(tmp_path: Path, setup, shadow, a6, state, reason) -> None:
   store = DurableFenceStore(tmp_path / "fence")
   if setup != "none":
      store.advance(_REQ, expected_fencing_generation=0)
   if setup == "publishing":
      store.grant_publication(_REQ, generation=1)
   if setup == "terminal":
      store.mark_terminal(_REQ, generation=1, outcome="full_context_replay")
   restarted = DurableFenceStore(tmp_path / "fence")
   result = restarted.reconcile(_REQ, shadow_checkpoint_valid=shadow, a6_available=a6)
   assert (result["state"], result["reason"]) == (state, reason)
   if setup in {"advanced", "publishing"}:
      # The pre-restart generation is fenced out; it can never publish again.
      assert result["fencing_generation"] == 2
      assert not restarted.accepts(_REQ, 1)
      assert _code(restarted.grant_publication, _REQ, generation=1) == "stale_generation"
