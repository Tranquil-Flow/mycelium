# SPDX-License-Identifier: AGPL-3.0-only
"""A7: in-process shadowing, fenced cutover and honest fallback over real runtimes."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import mycelium_a7_kv_successor as a7
from mycelium_a7_checkpoint_channel import StandbyCheckpointKey
from mycelium_a7_fence_store import DurableFenceStore, FenceStoreError
from mycelium_a7_standby_store import StandbyShadowStore
from mycelium_a7_successor_coordinator import (
   GATEWAY_INSTALLERS,
   StandbySide,
   SuccessorRecoveryCoordinator,
)
from mycelium_qualification.signing import build_ed25519_verifier, generate_ed25519_signer
from test_router_numpy_qwen_kv import _case, _reference
from tests.a7_acceptance.test_kv_checkpoint_transfer import _LEASE, _MAX, _execute, _fresh_ports

_REQ = "request:a7-coordinator"
_PROMPT = (1, 7, 11, 5)


def _digest(text: str) -> str:
   return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _prefix(tokens) -> str:
   return _digest("prefix:" + ",".join(str(token) for token in tokens))


def _identity(**overrides):
   values = {field: _digest(f"identity:{field}") for field in a7.COMPATIBILITY_IDENTITY_FIELDS}
   values.update(overrides)
   return values


class _Harness:
   def __init__(self, tmp_path: Path, *, compatible=True, a6_available=True) -> None:
      self.case = _case(tmp_path, "qwen2")
      signer = generate_ed25519_signer(endpoint_id="standby")
      placement = self.case.graph.stages[1].placements[0].placement_id
      incarnation = _digest("incarnation:standby:1")
      placement_digest = _digest(f"placement:{placement}:standby")
      self.standby = StandbySide(
         port=_fresh_ports(self.case)[1],
         placement_id=placement,
         placement_digest=placement_digest,
         incarnation=incarnation,
         signer=signer,
         key=StandbyCheckpointKey(signer=signer, standby_placement_id=placement_digest, standby_membership_incarnation=incarnation),
         store=StandbyShadowStore(max_entry_bytes=_MAX, max_entries=4),
         max_checkpoint_bytes=_MAX,
      )
      self.fences = DurableFenceStore(tmp_path / "fence")
      successor_identity = _identity() if compatible else _identity(dtype=_digest("float16"))
      self.coordinator = SuccessorRecoveryCoordinator(
         request_id=_REQ,
         session_id="session:a7",
         stage_id=_digest("stage:1"),
         source_placement_digest=_digest(f"placement:{placement}:source"),
         standby=self.standby,
         standby_key_record=self.standby.key.public_record(),
         verify_standby_signature=build_ed25519_verifier([signer.public_key_record()]),
         fence_store=self.fences,
         compatibility=a7.compare_compatibility(_identity(), successor_identity),
         a6_available=a6_available,
      )
      self.tokens: list[int] = []

   def decode(self, steps: int) -> None:
      ports = self.case.ports
      self.tokens.append(_execute(ports, self.case, _PROMPT, phase="PREFILL", request_id=_REQ, path_id="path:src", path_attempt=0, token_index=-1, position=0))
      self.ship()
      for step in range(1, steps + 1):
         self.tokens.append(_execute(ports, self.case, (self.tokens[-1],), phase="DECODE", request_id=_REQ, path_id="path:src", path_attempt=0, token_index=step, position=len(_PROMPT) + step - 1))
         self.ship()

   def ship(self) -> str:
      return self.coordinator.ship_checkpoint(
         self.case.ports[1],
         "path:src",
         committed_token_count=len(self.tokens),
         committed_prefix_digest=_prefix(self.tokens),
      )

   def cutover(self, *, committed=None, position=None, qualifier=True, installers=None):
      committed = len(self.tokens) if committed is None else committed
      self.case.ports[1].close(reason="worker_crash")  # the source placement fails
      return self.coordinator.cutover(
         gateway_committed_token_count=committed,
         gateway_committed_prefix_digest=_prefix(self.tokens[:committed]),
         expected_kv_position=len(_PROMPT) + len(self.tokens) - 1 if position is None else position,
         qualifier_current=qualifier,
         surviving=[(self.case.ports[0], "path:src")],
         successor_path_id="path:successor",
         lease_expires_at=_LEASE,
         installers=installers or {name: (lambda generation: self.fences.accepts(_REQ, generation)) for name in GATEWAY_INSTALLERS},
      )


def test_fenced_cutover_continues_without_replay_and_cleans_up(tmp_path: Path) -> None:
   harness = _Harness(tmp_path)
   harness.decode(3)
   assert harness.coordinator.route_watermark["committed_token_count"] == 4
   result = harness.cutover()
   assert result["outcome"] == "fenced_kv_successor" and result["kv_continuity_claimed"] is True
   assert result["acknowledgement_lag"] == 0
   assert result["request_attempt"] == 2 and result["fencing_generation"] == 2
   assert result["checkpoint_sequences_shipped"] == 4

   ports = (harness.case.ports[0], harness.standby.port)
   resumed = _execute(ports, harness.case, (harness.tokens[-1],), phase="DECODE", request_id=_REQ, path_id="path:successor", path_attempt=2, token_index=4, position=len(_PROMPT) + 3)
   assert resumed == _reference(harness.case, _PROMPT + tuple(harness.tokens))
   assert harness.standby.port.kv_snapshot()["prefill_operation_count"] == 0
   # Exactly one publisher: the fenced source generation can never publish again.
   assert not harness.fences.accepts(_REQ, 1)
   with pytest.raises(FenceStoreError) as duplicate:
      harness.fences.grant_publication(_REQ, generation=2)
   assert duplicate.value.code == "duplicate_publisher"
   cleanup = harness.coordinator.finish(outcome="fenced_kv_successor")
   assert cleanup["shadow_entry_count"] == 0 and cleanup["shadow_kv_bytes"] == 0
   assert harness.fences.current(_REQ) is None


@pytest.mark.parametrize(
   ("kwargs", "harness_kwargs", "outcome", "reason"),
   [
      ({"committed": 3}, {}, "full_context_replay", "watermark_ahead_of_gateway"),
      ({"qualifier": False}, {}, "full_context_replay", "qualification_not_current"),
      ({}, {"compatible": False}, "full_context_replay", "compatibility_incompatible"),
      ({}, {"compatible": False, "a6_available": False}, "aborted", "compatibility_incompatible"),
      ({"position": 3}, {}, "full_context_replay", "compatibility_incompatible"),
      ({"installers": {"request_gateway": lambda generation: True, "router": lambda generation: True}}, {}, "full_context_replay", "fence_not_installed"),
   ],
)
def test_every_rejection_falls_back_without_successor_state(tmp_path: Path, kwargs, harness_kwargs, outcome, reason) -> None:
   harness = _Harness(tmp_path, **harness_kwargs)
   harness.decode(3)
   result = harness.cutover(**kwargs)
   assert (result["outcome"], result["reason"]) == (outcome, reason)
   assert result["kv_continuity_claimed"] is False
   assert result["successor_checkpoint_digest"] is None
   assert harness.standby.port.kv_snapshot()["active_state_count"] == 0
   # The failed attempt is fenced even when falling back, so A6 owns a newer attempt.
   assert not harness.fences.accepts(_REQ, 1)
   snapshot = harness.case.ports[0].kv_snapshot()
   assert "path:successor" not in snapshot["states"]


def test_lagging_standby_reports_lag_and_falls_back(tmp_path: Path) -> None:
   harness = _Harness(tmp_path)
   harness.decode(2)
   # One more token commits at the gateway but its checkpoint never reaches the standby.
   harness.tokens.append(_execute(harness.case.ports, harness.case, (harness.tokens[-1],), phase="DECODE", request_id=_REQ, path_id="path:src", path_attempt=0, token_index=3, position=len(_PROMPT) + 2))
   result = harness.cutover()
   assert (result["outcome"], result["reason"]) == ("full_context_replay", "watermark_behind_gateway")
   assert result["acknowledgement_lag"] == 1


def test_a_stale_source_generation_cannot_ship_after_cutover(tmp_path: Path) -> None:
   harness = _Harness(tmp_path)
   harness.decode(1)
   harness.cutover()
   with pytest.raises(a7.A7ContractViolation) as refused:
      harness.coordinator.ship_checkpoint(harness.case.ports[0], "path:successor", committed_token_count=2, committed_prefix_digest=_prefix(harness.tokens))
   assert str(refused.value) == "stale_generation"
