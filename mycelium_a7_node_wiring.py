"""A7 node wiring: staged A7 configuration -> agent, standby port and entry controller.

A node opts into A7 only through an operator-staged, closed configuration file
inside its artifact root (``a7/node.json``); the route's configure/start
commands are unchanged.  The file declares at most three roles:

* ``standby`` -- this node hosts a STANDBY placement of the deployment graph: its
  own assignment-local stage pack is verified and loaded, bound on a dedicated
  standby runtime port (wrapped with the ordinary port in a
  ``StandbyDispatchRuntime``), and its staged owner-only key material yields the
  Ed25519 acknowledgement signer and the X25519 checkpoint key;
* ``source`` -- this node's ACTIVE placement is covered: committed output makes
  it ship sealed checkpoints to the pinned standby key;
* ``entry`` -- this node is the request entry: it runs the A7 entry controller
  with a durable fence store and the current standby qualification records.

Every refusal is a stable code; nothing is inferred or defaulted into coverage.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mycelium_a7_checkpoint_channel import StandbyCheckpointKey, verify_standby_key_record
from mycelium_a7_entry_controller import A7EntryController, StandbyCoverage, stage_digest
from mycelium_a7_fence_store import DurableFenceStore
from mycelium_a7_node_agent import A7NodeAgent, SourceCoverage
from mycelium_a7_standby_qualification import (
   StandbyQualificationError,
   validate_standby_qualification,
)
from mycelium_a7_standby_store import StandbyShadowStore
from mycelium_a7_successor_coordinator import StandbySide
from mycelium_qualification.signing import Ed25519EvidenceSigner, build_ed25519_verifier

A7_NODE_CONFIG_PROTOCOL = "mycelium.a7_node_config.v1"
A7_NODE_CONFIG_PATH = "a7/node.json"
STANDBY_SECRET_BYTES = 64
MAX_CHECKPOINT_BYTES = 256 * 1024 * 1024
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class A7WiringError(ValueError):
   def __init__(self, code: str) -> None:
      super().__init__(code)
      self.code = code


def _require(condition: bool, code: str) -> None:
   if not condition:
      raise A7WiringError(code)


def _exact(value: Any, fields: set[str], code: str) -> dict[str, Any]:
   _require(isinstance(value, Mapping) and set(value) == fields, code)
   return dict(value)


def _text(value: Any) -> bool:
   return isinstance(value, str) and bool(value) and len(value) <= 512


def _digest(value: Any) -> bool:
   return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def standby_endpoint_id(node_id: str) -> str:
   return f"a7-standby:{node_id}"


def standby_material(secret: bytes, *, node_id: str, placement_digest: str, incarnation: str) -> tuple[Ed25519EvidenceSigner, StandbyCheckpointKey]:
   """The standby's signer and checkpoint key from its staged owner-only secret."""

   _require(isinstance(secret, bytes) and len(secret) == STANDBY_SECRET_BYTES, "a7_standby_secret_invalid")
   private = Ed25519PrivateKey.from_private_bytes(secret[:32])
   signer = Ed25519EvidenceSigner(
      endpoint_id=standby_endpoint_id(node_id),
      _private_key=private,
      _public_key_bytes=private.public_key().public_bytes_raw(),
   )
   key = StandbyCheckpointKey(
      signer=signer,
      standby_placement_id=placement_digest,
      standby_membership_incarnation=incarnation,
      x25519_private_bytes=secret[32:],
   )
   return signer, key


def standby_public_records(secret: bytes, *, node_id: str, placement_digest: str, incarnation: str) -> dict[str, Any]:
   """Public records the operator pins on the source and entry nodes."""

   signer, key = standby_material(secret, node_id=node_id, placement_digest=placement_digest, incarnation=incarnation)
   return {"signer_record": signer.public_key_record(), "key_record": key.public_record()}


_STANDBY_FIELDS = {
   "placement_id",
   "placement_digest",
   "incarnation",
   "assignment_file",
   "manifest_file",
   "stage_pack_file",
   "load_generation",
   "secret_file",
   "max_checkpoint_bytes",
   "max_retained_checkpoints",
}
_SOURCE_FIELDS = {
   "source_placement_id",
   "source_placement_digest",
   "stage_id",
   "standby_node_id",
   "standby_placement_id",
   "standby_placement_digest",
   "standby_incarnation",
   "standby_signer_record",
   "standby_key_record",
   "max_checkpoint_bytes",
}
_COVERAGE_FIELDS = {
   "stage_id",
   "source_placement_id",
   "source_node_id",
   "standby_placement_id",
   "standby_node_id",
   "standby_incarnation",
   "standby_signer_record",
   "qualification_file",
}
_ENTRY_FIELDS = {"fence_directory", "coverage", "a6_available", "ack_timeout_seconds", "receipt_timeout_seconds"}


def validate_node_config(document: Any, *, deployment_id: str, node_id: str) -> dict[str, Any]:
   config = _exact(document, {"protocol", "deployment_id", "node_id", "standby", "source", "entry"}, "a7_node_config_fields")
   _require(config["protocol"] == A7_NODE_CONFIG_PROTOCOL, "a7_node_config_protocol")
   _require(config["deployment_id"] == deployment_id, "a7_node_config_deployment_mismatch")
   _require(config["node_id"] == node_id, "a7_node_config_node_mismatch")
   _require(any(config[role] is not None for role in ("standby", "source", "entry")), "a7_node_config_empty")
   if config["standby"] is not None:
      standby = _exact(config["standby"], _STANDBY_FIELDS, "a7_standby_config_fields")
      _require(all(_text(standby[key]) for key in ("placement_id", "assignment_file", "manifest_file", "stage_pack_file", "secret_file")), "a7_standby_config_invalid")
      _require(_digest(standby["placement_digest"]) and _digest(standby["incarnation"]), "a7_standby_config_invalid")
      _require(type(standby["load_generation"]) is int and standby["load_generation"] > 0, "a7_standby_config_invalid")
      _require(type(standby["max_checkpoint_bytes"]) is int and 1 <= standby["max_checkpoint_bytes"] <= MAX_CHECKPOINT_BYTES, "a7_standby_config_invalid")
      _require(type(standby["max_retained_checkpoints"]) is int and 1 <= standby["max_retained_checkpoints"] <= 4096, "a7_standby_config_invalid")
   if config["source"] is not None:
      source = _exact(config["source"], _SOURCE_FIELDS, "a7_source_config_fields")
      _require(all(_text(source[key]) for key in ("source_placement_id", "stage_id", "standby_node_id", "standby_placement_id")), "a7_source_config_invalid")
      _require(all(_digest(source[key]) for key in ("source_placement_digest", "standby_placement_digest", "standby_incarnation")), "a7_source_config_invalid")
      _require(isinstance(source["standby_signer_record"], Mapping) and isinstance(source["standby_key_record"], Mapping), "a7_source_config_invalid")
      _require(type(source["max_checkpoint_bytes"]) is int and 1 <= source["max_checkpoint_bytes"] <= MAX_CHECKPOINT_BYTES, "a7_source_config_invalid")
   if config["entry"] is not None:
      entry = _exact(config["entry"], _ENTRY_FIELDS, "a7_entry_config_fields")
      _require(_text(entry["fence_directory"]) and type(entry["a6_available"]) is bool, "a7_entry_config_invalid")
      for key in ("ack_timeout_seconds", "receipt_timeout_seconds"):
         _require(isinstance(entry[key], (int, float)) and not isinstance(entry[key], bool) and 0 < entry[key] <= 30, "a7_entry_config_invalid")
      _require(isinstance(entry["coverage"], list) and 1 <= len(entry["coverage"]) <= 16, "a7_entry_config_invalid")
      for item in entry["coverage"]:
         coverage = _exact(item, _COVERAGE_FIELDS, "a7_coverage_config_fields")
         _require(all(_text(coverage[key]) for key in ("stage_id", "source_placement_id", "source_node_id", "standby_placement_id", "standby_node_id", "qualification_file")), "a7_coverage_config_invalid")
         _require(_digest(coverage["standby_incarnation"]) and isinstance(coverage["standby_signer_record"], Mapping), "a7_coverage_config_invalid")
   return config


def validate_against_graph(config: Mapping[str, Any], graph: Any, *, node_id: str) -> None:
   """Every placement the configuration names must exist with the declared role."""

   placements = {placement.placement_id: (stage, placement) for stage in graph.stages for placement in stage.placements}

   def lookup(placement_id: str) -> tuple[Any, Any]:
      record = placements.get(placement_id)
      _require(record is not None, "a7_config_placement_unknown")
      return record

   if config["standby"] is not None:
      _stage, placement = lookup(config["standby"]["placement_id"])
      _require(placement.node_id == node_id and placement.lifecycle_state == "STANDBY", "a7_standby_placement_invalid")
   if config["source"] is not None:
      stage, placement = lookup(config["source"]["source_placement_id"])
      _require(placement.node_id == node_id and placement.lifecycle_state == "ACTIVE", "a7_source_placement_invalid")
      _require(stage.stage_id == config["source"]["stage_id"], "a7_source_stage_mismatch")
      standby_stage, standby = lookup(config["source"]["standby_placement_id"])
      _require(standby.lifecycle_state == "STANDBY" and standby_stage.stage_id == stage.stage_id and standby.node_id == config["source"]["standby_node_id"], "a7_source_standby_invalid")
   if config["entry"] is not None:
      _require(graph.stages[0].placements and any(p.node_id == node_id for p in graph.stages[0].placements), "a7_entry_not_entry_stage")
      for coverage in config["entry"]["coverage"]:
         stage, source = lookup(coverage["source_placement_id"])
         standby_stage, standby = lookup(coverage["standby_placement_id"])
         _require(
            source.lifecycle_state == "ACTIVE"
            and source.node_id == coverage["source_node_id"]
            and standby.lifecycle_state == "STANDBY"
            and standby.node_id == coverage["standby_node_id"]
            and stage.stage_id == standby_stage.stage_id == coverage["stage_id"],
            "a7_coverage_placement_invalid",
         )


class A7NodeWiring:
   """Per-node A7 roles built from a validated configuration."""

   def __init__(
      self,
      config: Mapping[str, Any],
      *,
      node_id: str,
      read_document: Callable[[str], dict[str, Any]],
      read_secret: Callable[[str], bytes],
      fence_root: Path,
   ) -> None:
      self.config = dict(config)
      self.node_id = node_id
      self._read_document = read_document
      self._read_secret = read_secret
      self._fence_root = fence_root
      self.standby_side: StandbySide | None = None
      self.standby_port: Any = None
      self.primary_port: Any = None
      self.agent: A7NodeAgent | None = None
      self.controller: A7EntryController | None = None
      self.qualifications: dict[str, dict[str, Any]] = {}

   # -- configure ---------------------------------------------------------------------

   def bind_standby(self, standby_port: Any) -> None:
      standby = self.config["standby"]
      _require(standby is not None, "a7_standby_not_configured")
      _require(standby["placement_id"] in getattr(standby_port, "standby_placement_ids", frozenset()), "a7_standby_port_invalid")
      signer, key = standby_material(
         self._read_secret(standby["secret_file"]),
         node_id=self.node_id,
         placement_digest=standby["placement_digest"],
         incarnation=standby["incarnation"],
      )
      self.standby_port = standby_port
      self.standby_side = StandbySide(
         port=standby_port,
         placement_id=standby["placement_id"],
         placement_digest=standby["placement_digest"],
         incarnation=standby["incarnation"],
         signer=signer,
         key=key,
         store=StandbyShadowStore(max_entry_bytes=standby["max_checkpoint_bytes"], max_entries=standby["max_retained_checkpoints"]),
         max_checkpoint_bytes=standby["max_checkpoint_bytes"],
      )

   def load_qualifications(self, *, now_unix_ms: int) -> None:
      entry = self.config["entry"]
      if entry is None:
         return
      for coverage in entry["coverage"]:
         try:
            record = validate_standby_qualification(self._read_document(coverage["qualification_file"]))
         except StandbyQualificationError as exc:
            raise A7WiringError("a7_qualification_invalid") from exc
         expected = "sha256:" + hashlib.sha256(coverage["standby_placement_id"].encode("utf-8")).hexdigest()
         _require(record["standby_placement_id"] == expected, "a7_qualification_placement_mismatch")
         self.qualifications[coverage["source_placement_id"]] = record

   # -- start ---------------------------------------------------------------------------

   def attach(self, router: Any, *, send: Callable[[str, object, bytes], None], primary_port: Any, ports_by_placement: Mapping[str, Any]) -> A7NodeAgent:
      agent = A7NodeAgent(self.node_id, send=send)
      self.primary_port = primary_port
      for placement_id, port in ports_by_placement.items():
         agent.register_port(placement_id, port)
      if self.standby_side is not None:
         agent.register_standby(self.standby_side, session_digest="sha256:" + hashlib.sha256(f"a7-session:{self.node_id}".encode()).hexdigest())
      source = self.config["source"]
      if source is not None:
         verifier = build_ed25519_verifier([source["standby_signer_record"]])
         verify_standby_key_record(
            source["standby_key_record"],
            verify_signature=verifier,
            standby_placement_id=source["standby_placement_digest"],
            standby_membership_incarnation=source["standby_incarnation"],
         )
         agent.register_source_coverage(
            source["source_placement_id"],
            SourceCoverage(
               stage_id=stage_digest(source["stage_id"]),
               port=ports_by_placement[source["source_placement_id"]],
               standby_node_id=source["standby_node_id"],
               standby_placement_id=source["standby_placement_id"],
               standby_placement_digest=source["standby_placement_digest"],
               standby_incarnation=source["standby_incarnation"],
               standby_key_record=source["standby_key_record"],
               verify_standby_signature=verifier,
               source_placement_digest=source["source_placement_digest"],
               max_bytes=source["max_checkpoint_bytes"],
            ),
         )
      entry = self.config["entry"]
      if entry is not None:
         coverage = [
            StandbyCoverage(
               stage_id=item["stage_id"],
               source_placement_id=item["source_placement_id"],
               source_node_id=item["source_node_id"],
               standby_placement_id=item["standby_placement_id"],
               standby_node_id=item["standby_node_id"],
               standby_incarnation=item["standby_incarnation"],
               verify_standby_signature=build_ed25519_verifier([item["standby_signer_record"]]),
               qualification=self.qualifications[item["source_placement_id"]],
            )
            for item in entry["coverage"]
         ]
         self.controller = A7EntryController(
            node_id=self.node_id,
            agent=agent,
            send=send,
            fence_store=DurableFenceStore(self._fence_root / entry["fence_directory"]),
            coverage=coverage,
            a6_available=entry["a6_available"],
            ack_timeout_seconds=float(entry["ack_timeout_seconds"]),
            receipt_timeout_seconds=float(entry["receipt_timeout_seconds"]),
            release_remote_asynchronously=True,
         )
         router.entry.a7_controller = self.controller
      router.a7_agent = agent
      self.agent = agent
      return agent

   def forward_failure_observer(self, router: Any, *, node_id: str) -> Any:
      """Turn an undeliverable hop to a covered source into the entry's failure report.

      Only hops of requests this entry covers, addressed to the covered source
      placement, are reported (scope PLACEMENT); everything else keeps its
      existing handling.
      """

      from mycelium_router.contracts import FailureReport

      controller = self.controller

      def observe(header: Any, _destination: str, code: str) -> None:
         if controller is None or not controller.covers_failed_hop(header.request_id, header.path_id, header.destination_placement_id):
            return
         # Observed locally by this entry's own transport: no remote origin to
         # authenticate, and the waiting decode driver needs the successor to
         # re-send the failed step.
         router.entry.receive_failure_report(
            FailureReport(
               request_id=header.request_id,
               path_id=header.path_id,
               path_attempt=header.path_attempt,
               token_index=header.token_index,
               scope="PLACEMENT",
               reason=code,
               placement_id=header.destination_placement_id,
               node_id=node_id,
            ),
            redispatch_successor=True,
         )

      return observe

   def snapshot(self, request_id: str | None = None) -> dict[str, Any]:
      """Privacy-reduced A7 node evidence (counts, bytes, states; never KV)."""

      document: dict[str, Any] = {
         "roles": sorted(role for role in ("standby", "source", "entry") if self.config[role] is not None),
         "agent": None if self.agent is None else self.agent.shadow_snapshot(),
      }
      if self.standby_port is not None:
         snapshot = self.standby_port.kv_snapshot()
         document["standby_runtime"] = {
            key: snapshot.get(key)
            for key in ("active_state_count", "active_kv_bytes", "prefill_operation_count", "decode_operation_count")
         }
      if self.controller is not None and request_id is not None:
         document["request"] = self.controller.snapshot(request_id)
      return document
