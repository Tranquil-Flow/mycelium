# SPDX-License-Identifier: AGPL-3.0-only
"""A7: node agents exchange checkpoints, acks and cutovers over real Router frames."""

from __future__ import annotations

import hashlib
from collections import deque
from pathlib import Path

import pytest

from mycelium_a7_checkpoint_channel import StandbyCheckpointKey
from mycelium_a7_kv_successor import A7ContractViolation
from mycelium_a7_node_agent import A7NodeAgent
from mycelium_a7_standby_store import StandbyShadowStore
from mycelium_a7_successor_coordinator import StandbySide
from mycelium_qualification.signing import build_ed25519_verifier, generate_ed25519_signer
from mycelium_router.contracts import (
   KVCheckpointAck,
   KVCheckpointShipment,
   KVSuccessorCutover,
   KVShadowRelease,
   KVSuccessorCutoverReceipt,
)
from mycelium_router.router import Router
from mycelium_router.wire import WireError, decode_frame, encode_frame
from test_router_numpy_qwen_kv import _case, _reference
from tests.a7_acceptance.test_kv_checkpoint_transfer import _LEASE, _MAX, _execute, _fresh_ports

_REQ = "request:a7-agents"
_PROMPT = (1, 7, 11, 5)


def _digest(text: str) -> str:
   return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


class _Network:
   """Synchronous loopback: every message is a real encoded Router frame."""

   def __init__(self) -> None:
      self.agents: dict[str, A7NodeAgent] = {}
      self.queue: deque[tuple[str, str, bytes]] = deque()
      self.delivered: list[str] = []

   def sender(self, origin: str):
      def send(destination: str, message: object, payload: bytes) -> None:
         self.queue.append((origin, destination, encode_frame(message, payload)))

      return send

   def pump(self) -> None:
      while self.queue:
         origin, destination, frame = self.queue.popleft()
         decoded = decode_frame(frame)
         message, agent = decoded.message, self.agents[destination]
         self.delivered.append(type(message).__name__)
         if isinstance(message, KVCheckpointShipment):
            assert len(decoded.payload) == message.byte_size
            ok = agent.receive_shipment(message, decoded.payload, source_node_id=origin)
         elif isinstance(message, KVCheckpointAck):
            ok = agent.receive_ack(message, source_node_id=origin)
         elif isinstance(message, KVSuccessorCutover):
            ok = agent.receive_cutover(message, source_node_id=origin)
         else:
            ok = agent.receive_cutover_receipt(message, source_node_id=origin)
         assert ok, type(message).__name__


@pytest.fixture()
def topology(tmp_path: Path):
   case = _case(tmp_path, "qwen2")
   network = _Network()
   # node-a: entry + stage 0 (survives); node-b: stage-1 source; node-s: stage-1 standby.
   for node in ("node-a", "node-b", "node-s"):
      network.agents[node] = A7NodeAgent(node, send=network.sender(node), clock_ms=lambda: 1_800_000_000_000)
   signer = generate_ed25519_signer(endpoint_id="node-s")
   verifier = build_ed25519_verifier([signer.public_key_record()])
   placement = case.graph.stages[1].placements[0].placement_id
   placement_digest = _digest("placement:standby")
   incarnation = _digest("incarnation:node-s:1")
   standby = StandbySide(
      port=_fresh_ports(case)[1],
      placement_id=placement,
      placement_digest=placement_digest,
      incarnation=incarnation,
      signer=signer,
      key=StandbyCheckpointKey(signer=signer, standby_placement_id=placement_digest, standby_membership_incarnation=incarnation),
      store=StandbyShadowStore(max_entry_bytes=_MAX, max_entries=4),
      max_checkpoint_bytes=_MAX,
   )
   network.agents["node-s"].register_standby(standby, session_digest=_digest("session"))
   stage_id = _digest("stage:1")
   network.agents["node-b"].register_source(
      _REQ,
      stage_id=stage_id,
      port=case.ports[1],
      path_id="path:src",
      path_attempt=0,
      standby_node_id="node-s",
      standby_placement_id=placement,
      standby_placement_digest=placement_digest,
      standby_incarnation=incarnation,
      standby_key_record=standby.key.public_record(),
      verify_standby_signature=verifier,
      source_placement_digest=_digest("placement:source"),
      entry_node_id="node-a",
      max_bytes=_MAX,
   )
   network.agents["node-a"].register_entry(_REQ, stage_id=stage_id, request_attempt=1, standby_incarnation=incarnation, verify_standby_signature=verifier)
   network.agents["node-a"].register_port(case.graph.stages[0].placements[0].placement_id, case.ports[0])
   return case, network, standby


def _prefix(tokens) -> str:
   return _digest("prefix:" + ",".join(map(str, tokens)))


def test_checkpoints_acks_and_cutover_travel_as_router_frames(topology) -> None:
   case, network, standby = topology
   tokens = [_execute(case.ports, case, _PROMPT, phase="PREFILL", request_id=_REQ, path_id="path:src", path_attempt=0, token_index=-1, position=0)]
   network.agents["node-b"].ship(_REQ, request_attempt=1, committed_token_count=1, committed_prefix_digest=_prefix(tokens))
   network.pump()
   for step in (1, 2):
      tokens.append(_execute(case.ports, case, (tokens[-1],), phase="DECODE", request_id=_REQ, path_id="path:src", path_attempt=0, token_index=step, position=len(_PROMPT) + step - 1))
      network.agents["node-b"].ship(_REQ, request_attempt=1, committed_token_count=len(tokens), committed_prefix_digest=_prefix(tokens))
      network.pump()
   assert network.delivered.count("KVCheckpointShipment") == 3
   watermark = network.agents["node-a"].route_watermark(_REQ)
   assert watermark["committed_token_count"] == 3

   # Cutover: the entry asks the standby to import and the surviving stage to rekey.
   network.agents["node-b"].fence_source(_REQ)
   position = len(_PROMPT) + len(tokens) - 1
   for node, placement, role in (
      ("node-s", standby.placement_id, "standby_import"),
      ("node-a", case.graph.stages[0].placements[0].placement_id, "surviving_rekey"),
   ):
      network.queue.append((
         "node-a",
         node,
         encode_frame(KVSuccessorCutover(request_id=_REQ, old_path_id="path:src", old_path_attempt=0, successor_path_id="path:succ", successor_path_attempt=2, fencing_generation=2, placement_id=placement, role=role, expected_next_position=position, lease_expires_at=_LEASE)),
      ))
   network.pump()
   receipts = network.agents["node-a"].cutover_receipts(_REQ)
   assert {receipt.reason for receipt in receipts} == {"installed"} and len(receipts) == 2
   ports = (case.ports[0], standby.port)
   resumed = _execute(ports, case, (tokens[-1],), phase="DECODE", request_id=_REQ, path_id="path:succ", path_attempt=2, token_index=3, position=position)
   assert resumed == _reference(case, _PROMPT + tuple(tokens))
   assert standby.port.kv_snapshot()["prefill_operation_count"] == 0
   with pytest.raises(A7ContractViolation):
      network.agents["node-b"].ship(_REQ, request_attempt=1, committed_token_count=4, committed_prefix_digest=_prefix(tokens))


def test_forged_or_misrouted_messages_are_refused(topology) -> None:
   case, network, standby = topology
   tokens = [_execute(case.ports, case, _PROMPT, phase="PREFILL", request_id=_REQ, path_id="path:src", path_attempt=0, token_index=-1, position=0)]
   network.agents["node-b"].ship(_REQ, request_attempt=1, committed_token_count=1, committed_prefix_digest=_prefix(tokens))
   origin, destination, frame = network.queue.popleft()
   decoded = decode_frame(frame)
   # A shipment addressed to another placement, or tampered bytes, is refused.
   wrong = KVCheckpointShipment(**{**decoded.message.__dict__, "standby_placement_id": "placement:other"})
   assert network.agents["node-s"].receive_shipment(wrong, decoded.payload, source_node_id=origin) is False
   assert network.agents["node-s"].receive_shipment(decoded.message, decoded.payload[:-1] + b"\x00", source_node_id=origin) is False
   # An ack forwarded by someone other than the standby, or forged, is refused.
   forged = KVCheckpointAck(request_id=_REQ, path_id="path:src", path_attempt=0, hop="standby_to_source", signed_ack="{}")
   assert network.agents["node-b"].receive_ack(forged, source_node_id="node-s") is False
   assert network.agents["node-a"].receive_ack(KVCheckpointAck(request_id=_REQ, path_id="path:src", path_attempt=0, hop="source_to_entry", signed_ack="{}"), source_node_id="node-b") is False
   assert network.agents["node-a"].route_watermark(_REQ) is None
   # A cutover for a placement this node does not hold yields a negative receipt.
   network.agents["node-s"].receive_cutover(
      KVSuccessorCutover(request_id=_REQ, old_path_id="path:src", old_path_attempt=0, successor_path_id="path:succ", successor_path_attempt=2, fencing_generation=2, placement_id="placement:unknown", role="standby_import", expected_next_position=4, lease_expires_at=_LEASE),
      source_node_id="node-a",
   )
   network.pump()
   (receipt,) = network.agents["node-a"].cutover_receipts(_REQ)
   assert (receipt.installed, receipt.reason) == (False, "a7_cutover_placement_unknown")


def _held_shipment(case, network):
   tokens = [_execute(case.ports, case, _PROMPT, phase="PREFILL", request_id=_REQ, path_id="path:src", path_attempt=0, token_index=-1, position=0)]
   network.agents["node-b"].ship(_REQ, request_attempt=1, committed_token_count=1, committed_prefix_digest=_prefix(tokens))
   origin, destination, frame = network.queue.popleft()
   assert (origin, destination) == ("node-b", "node-s") and not network.queue
   return origin, decode_frame(frame)


def _release() -> KVShadowRelease:
   return KVShadowRelease(request_id=_REQ, path_id="path:src", path_attempt=0, reason="completed")


def test_a_shipment_after_the_release_cannot_rebuild_the_shadow_chain(topology) -> None:
   case, network, standby = topology
   origin, decoded = _held_shipment(case, network)
   # The entry's release overtakes the sequence-0 shipment.
   assert network.agents["node-s"].receive_release(_release(), source_node_id="node-a") is True

   assert network.agents["node-s"].receive_shipment(decoded.message, decoded.payload, source_node_id=origin) is False

   snapshot = network.agents["node-s"].shadow_snapshot()
   assert (snapshot["shadow_entry_count"], snapshot["shadow_kv_bytes"], snapshot["shadow_checkpoint_count"]) == (0, 0, 0)
   assert snapshot["refusals"] == {"shipment_request_released": 1}
   assert not network.queue  # no ack goes back to the source


def test_a_release_racing_a_shipment_drops_what_the_shipment_stored(topology, monkeypatch) -> None:
   case, network, standby = topology
   origin, decoded = _held_shipment(case, network)
   receive = type(standby).receive

   def receive_then_release(self, sealed, **kwargs):
      signed = receive(self, sealed, **kwargs)
      assert self.store.checkpoint_snapshot()["shadow_checkpoint_count"] == 1
      assert network.agents["node-s"].receive_release(_release(), source_node_id="node-a") is True
      return signed

   monkeypatch.setattr(type(standby), "receive", receive_then_release)

   assert network.agents["node-s"].receive_shipment(decoded.message, decoded.payload, source_node_id=origin) is False

   snapshot = network.agents["node-s"].shadow_snapshot()
   assert (snapshot["shadow_entry_count"], snapshot["shadow_kv_bytes"], snapshot["shadow_checkpoint_count"]) == (0, 0, 0)
   assert snapshot["refusals"] == {"shipment_request_released": 1}
   assert not network.queue


@pytest.mark.parametrize(
   "header",
   [{"request_id": "request:a7-other"}, {"checkpoint_sequence": 1}],
   ids=["request", "sequence"],
)
def test_a_shipment_header_that_disagrees_with_the_sealed_binding_is_refused(topology, header) -> None:
   case, network, standby = topology
   origin, decoded = _held_shipment(case, network)
   # Only the sealed binding is authenticated: the outer header cannot steer the
   # released check or name another request or sequence than the one stored.
   forged = KVCheckpointShipment(**{**decoded.message.__dict__, **header})

   assert network.agents["node-s"].receive_shipment(forged, decoded.payload, source_node_id=origin) is False

   snapshot = network.agents["node-s"].shadow_snapshot()
   assert (snapshot["shadow_entry_count"], snapshot["shadow_checkpoint_count"]) == (0, 0)
   assert snapshot["refusals"] == {"shipment_header_mismatch": 1}
   assert not network.queue
   # The genuine shipment is still accepted afterwards.
   assert network.agents["node-s"].receive_shipment(decoded.message, decoded.payload, source_node_id=origin) is True
   assert network.agents["node-s"].shadow_snapshot()["shadow_checkpoint_count"] == 1


def test_a_released_request_is_refused_under_another_request_header(topology) -> None:
   case, network, standby = topology
   origin, decoded = _held_shipment(case, network)
   assert network.agents["node-s"].receive_release(_release(), source_node_id="node-a") is True
   relabelled = KVCheckpointShipment(**{**decoded.message.__dict__, "request_id": "request:a7-other"})

   assert network.agents["node-s"].receive_shipment(relabelled, decoded.payload, source_node_id=origin) is False
   assert network.agents["node-s"].receive_shipment(decoded.message, decoded.payload, source_node_id=origin) is False

   snapshot = network.agents["node-s"].shadow_snapshot()
   assert (snapshot["shadow_entry_count"], snapshot["shadow_checkpoint_count"]) == (0, 0)
   assert snapshot["refusals"] == {"shipment_header_mismatch": 1, "shipment_request_released": 1}
   assert not network.queue


@pytest.mark.parametrize(
   "message",
   [
      KVCheckpointShipment(request_id="r", path_id="p", path_attempt=0, standby_placement_id="s", checkpoint_sequence=0, byte_size=0),
      KVCheckpointAck(request_id="r", path_id="p", path_attempt=0, hop="sideways", signed_ack="{}"),
      KVSuccessorCutover(request_id="r", old_path_id="p", old_path_attempt=1, successor_path_id="p", successor_path_attempt=2, fencing_generation=2, placement_id="x", role="standby_import", expected_next_position=4, lease_expires_at=1.0),
      KVSuccessorCutover(request_id="r", old_path_id="p", old_path_attempt=2, successor_path_id="q", successor_path_attempt=2, fencing_generation=2, placement_id="x", role="standby_import", expected_next_position=4, lease_expires_at=1.0),
      KVSuccessorCutoverReceipt(request_id="r", successor_path_id="q", successor_path_attempt=2, fencing_generation=0, placement_id="x", installed=True, reason="installed", checkpoint_digest=""),
   ],
)
def test_wire_refuses_malformed_a7_messages(message) -> None:
   with pytest.raises(WireError):
      encode_frame(message)


def test_router_without_an_a7_agent_refuses_every_a7_message() -> None:
   router = Router.__new__(Router)
   assert router.receive_kv_checkpoint_shipment(object(), b"", source_node_id="n") is False
   assert router.receive_kv_checkpoint_ack(object(), source_node_id="n") is False
   assert router.receive_kv_successor_cutover(object(), source_node_id="n") is False
   assert router.receive_kv_successor_cutover_receipt(object(), source_node_id="n") is False
