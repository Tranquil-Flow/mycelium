#!/usr/bin/env python3
"""A12 M5 route harness: the 0.5B route with the phone as its middle stage.

Mac node-0 (entry) -> phone (2 middle layers, ``mobile-rust-qwen2`` over
``external_iroh``) -> laptop node-2 (final).  It builds, binds, stages and
serves that route on loopback, sends ONE real browser request through it, offers
single hops during the A12 heat window, and closes the route.  The A12 gate
driver (``scripts/run_a12_mobile_physical_gate.py``) calls three subcommands:

  browser-request   ``--browser-request-command``: exit 0 only when the browser
                    request completed AND the phone placement is a middle stage
                    of the live route AND the phone forwarded frames for it.
  thermal-probe     ``--thermal-probe-command``: exit 0 when the offered request
                    did not run on the phone (expected: refused at its ceiling),
                    3 when it ran, 2 when inconclusive.  The refusal itself is
                    judged by the driver from the phone's signed observations.
  down              ``--route-close-command``: stop the serve (closes the route),
                    the artifact source and the controller endpoint.

Everything before the gate is one ``run <step>`` per step, in ``plan`` order.
``plan`` is the dry run: it validates the config and the topology, checks the
local inputs exist, and prints every step's exact command.  It never starts a
process, opens a socket, writes a file or touches a device.  ``prepare`` is the
builder's ``--phone-prepare-only`` rehearsal (assignment inputs only).

Steps that compose existing product commands are thin wrappers; ``evidence``
composes product functions in process because no command exists for it: over
the operator's controller endpoint it runs hello / stage_load / configure /
snapshot / stop against the phone, then asks the seed for the builder
authorization (``mycelium_seed.phone_preparation``) and writes the
``mycelium.phone_builder_evidence.v1`` document the builder takes.

This file is product-owned (not a run-dir script) because the driver's exit-code
contract and the route checks are product semantics, it composes product
functions in process, and the gate runs it from the same L2 checkout the APK and
the host executor are built from.
"""

# ruff: noqa: E402 -- direct script execution needs the repository on sys.path.

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
   sys.path.insert(0, str(REPO_ROOT))

CONFIG_PROTOCOL = "mycelium.a12_phone_route_config.v1"
EVIDENCE_PROTOCOL = "mycelium.phone_builder_evidence.v1"
PHONE_BACKEND = "mobile-rust-qwen2"
PHONE_TRANSPORT = "external_iroh"
DESKTOP_BACKENDS = frozenset({"mlx", "numpy"})
PHONE_LAYER_COUNT = 2
MAX_NEW_TOKENS_CEILING = 16
GOLDEN_SEED = 42
GOLDEN_SEQ = 4
#: Exit codes (the driver's contract).
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INCONCLUSIVE = 2
EXIT_PROBE_EXECUTED = 3

CONFIG_FIELDS = frozenset(
   {
      "protocol", "checkout", "work_dir", "python", "seed_state_root", "seed_key_digest", "seed_url",
      "model_root", "model_id", "resolved_commit", "template_plan", "topology", "phone_node_id",
      "phone_layers", "route_generation", "phone_executor", "phone_apk", "phone_source_root",
      "build_receipt", "artifact_source", "controller", "serve", "request",
   }
)


class HarnessError(RuntimeError):
   """A coded harness refusal."""


def _require(ok: object, code: str) -> None:
   if not ok:
      raise HarnessError(code)


# -- config ----------------------------------------------------------------------


def validate_config(document: Any) -> dict[str, Any]:
   """The closed harness config; every path absolute, the request bounded."""

   _require(isinstance(document, Mapping) and set(document) == CONFIG_FIELDS, "config_fields_invalid")
   _require(document["protocol"] == CONFIG_PROTOCOL, "config_protocol_invalid")
   for name in (
      "checkout", "work_dir", "python", "seed_state_root", "model_root", "template_plan", "topology",
      "phone_executor", "phone_apk", "phone_source_root", "build_receipt",
   ):
      _require(isinstance(document[name], str) and Path(document[name]).is_absolute(), f"config_path_invalid:{name}")
   layers = document["phone_layers"]
   _require(
      isinstance(layers, list) and len(layers) == 2 and all(type(v) is int and v >= 0 for v in layers)
      and layers[1] - layers[0] == PHONE_LAYER_COUNT,
      "config_phone_layers_invalid",
   )
   _require(type(document["route_generation"]) is int and document["route_generation"] > 0, "config_route_generation_invalid")
   _require(isinstance(document["phone_node_id"], str) and document["phone_node_id"], "config_phone_node_invalid")
   _require(
      isinstance(document["seed_key_digest"], str) and document["seed_key_digest"].startswith("sha256:"),
      "config_seed_key_digest_invalid",
   )
   source = document["artifact_source"]
   _require(
      isinstance(source, Mapping) and set(source) == {"binary", "key_dir"}
      and all(isinstance(v, str) and Path(v).is_absolute() for v in source.values()),
      "config_artifact_source_invalid",
   )
   controller = document["controller"]
   _require(
      isinstance(controller, Mapping) and set(controller) == {"sidecar", "socket_path", "bootstrap_secret_file"}
      and all(isinstance(v, str) and Path(v).is_absolute() for v in controller.values()),
      "config_controller_invalid",
   )
   serve = document["serve"]
   _require(
      isinstance(serve, Mapping) and set(serve) == {"host", "port", "product_state_root"}
      and serve["host"] == "127.0.0.1" and type(serve["port"]) is int and 1024 < serve["port"] < 65536,
      "config_serve_not_loopback",
   )
   request = document["request"]
   _require(
      isinstance(request, Mapping) and set(request) == {"prompt", "max_new_tokens"}
      and isinstance(request["prompt"], str) and 0 < len(request["prompt"]) <= 200
      and type(request["max_new_tokens"]) is int and 1 <= request["max_new_tokens"] <= MAX_NEW_TOKENS_CEILING,
      "config_request_unbounded",
   )
   return dict(document)


def validate_topology(nodes: Any, *, phone_node_id: str) -> dict[str, Any]:
   """Mac entry -> phone middle stage -> laptop final, nothing else.

   The phone is never the entry (no embedding) nor the final placement (no
   final norm or lm_head): PLAN-phone-full-node section 0.
   """

   _require(isinstance(nodes, list) and len(nodes) == 3, "topology_not_three_hosts")
   _require(all(isinstance(n, Mapping) and isinstance(n.get("node_id"), str) for n in nodes), "topology_node_invalid")
   ids = [n["node_id"] for n in nodes]
   _require(len(set(ids)) == 3, "topology_node_duplicate")
   entry, phone, final = nodes
   _require(phone["node_id"] == phone_node_id, "topology_phone_not_middle")
   _require(
      phone.get("runtime_backend") == PHONE_BACKEND and phone.get("process_transport") == PHONE_TRANSPORT,
      "topology_phone_runtime_invalid",
   )
   for node in (entry, final):
      _require(node.get("runtime_backend") in DESKTOP_BACKENDS, "topology_desktop_runtime_invalid")
      _require(node.get("process_transport") != PHONE_TRANSPORT, "topology_desktop_transport_invalid")
   _require(
      isinstance(phone.get("endpoint_id"), str) and len(phone["endpoint_id"]) == 64
      and type(phone.get("membership_generation")) is int,
      "topology_phone_membership_unbound",
   )
   return {"entry": entry["node_id"], "phone": phone["node_id"], "final": final["node_id"], "final_endpoint_id": final.get("endpoint_id")}


# -- live-status checks -------------------------------------------------------------


def phone_middle_placement(status: Mapping[str, Any], phone_node_id: str) -> dict[str, Any] | None:
   """The phone's placement when the live route runs it as a middle stage."""

   stages = status.get("stages") if isinstance(status, Mapping) else None
   if not isinstance(stages, list) or len(stages) < 3:
      return None
   ordered = sorted(
      (s for s in stages if isinstance(s, Mapping) and type(s.get("start_layer")) is int),
      key=lambda s: s["start_layer"],
   )
   for index, stage in enumerate(ordered):
      if stage.get("node_id") == phone_node_id and stage.get("runtime_backend") == PHONE_BACKEND:
         return dict(stage) if 0 < index < len(ordered) - 1 else None
   return None


def phone_frames_sent(status: Mapping[str, Any], phone_node_id: str) -> int | None:
   """Frames the phone reported forwarding, from its signed route observations."""

   peers = status.get("peers") if isinstance(status, Mapping) else None
   for peer in peers if isinstance(peers, list) else ():
      if isinstance(peer, Mapping) and peer.get("node_id") == phone_node_id:
         value = peer.get("frames_sent")
         return value if type(value) is int and value >= 0 else None
   return None


def judge_browser_request(
   *, terminal: str | None, before: Mapping[str, Any], after: Mapping[str, Any], phone_node_id: str
) -> tuple[bool, str]:
   """Pass only for a completed request that ran through the phone's middle stage."""

   if terminal != "completed":
      return False, f"browser_request_not_completed:{terminal}"
   if after.get("route_alive") is not True or after.get("simulated") is not False:
      return False, "physical_route_not_alive"
   if phone_middle_placement(after, phone_node_id) is None:
      return False, "phone_not_a_middle_stage_of_the_live_route"
   sent_before = phone_frames_sent(before, phone_node_id)
   sent_after = phone_frames_sent(after, phone_node_id)
   if sent_before is None or sent_after is None:
      return False, "phone_frame_counters_unavailable"
   if sent_after <= sent_before:
      return False, "phone_forwarded_no_frames"
   return True, "completed_through_phone_middle_stage"


def classify_probe(*, terminal: str | None, sent_before: int | None, sent_after: int | None) -> int:
   """The driver's thermal-probe exit code for one offered request."""

   if terminal == "completed" and sent_before is not None and sent_after is not None and sent_after > sent_before:
      return EXIT_PROBE_EXECUTED
   if terminal in ("failed", "cancelled") and sent_before is not None and sent_after == sent_before:
      return EXIT_OK
   return EXIT_INCONCLUSIVE


# -- steps -----------------------------------------------------------------------------


@dataclass
class Step:
   name: str
   summary: str
   argv: list[str] = field(default_factory=list)
   in_process: bool = False
   background: bool = False
   env: dict[str, str] = field(default_factory=dict)


def _work(config: Mapping[str, Any]) -> Path:
   return Path(config["work_dir"])


def _state(config: Mapping[str, Any]) -> dict[str, Any]:
   path = _work(config) / "state.json"
   return json.loads(path.read_text("utf-8")) if path.exists() else {}


def _save_state(config: Mapping[str, Any], **values: Any) -> None:
   state = {**_state(config), **values}
   path = _work(config) / "state.json"
   path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
   temporary = path.with_name(f".state.{os.getpid()}.tmp")
   temporary.write_text(json.dumps(state, sort_keys=True, indent=1) + "\n", encoding="utf-8")
   os.chmod(temporary, 0o600)
   os.replace(temporary, path)


def steps(config: Mapping[str, Any], state: Mapping[str, Any]) -> list[Step]:
   """Every step in order, with its exact command (placeholders until known)."""

   py, ck, work = config["python"], config["checkout"], _work(config)
   start, end = config["phone_layers"]
   deployment = state.get("deployment_id", "<deployment-id: fixed by `run prepare`>")
   controller_id = state.get("controller_endpoint_id", "<controller endpoint id: printed by `run controller-up`>")
   source_spec = state.get("artifact_source_spec", "<artifact source spec: printed by `run artifact-source-up`>")
   final_endpoint = state.get("final_endpoint_id", "<laptop node-2 endpoint id from the topology>")
   serve = config["serve"]
   builder = [py, "-B", f"{ck}/scripts/build_qwen_live_route.py", "--template-plan", str(work / "template-plan.json"),
              "--model-root", config["model_root"], "--model-id", config["model_id"],
              "--resolved-commit", config["resolved_commit"], "--topology", config["topology"],
              "--deployment-id", deployment, "--route-label", "a12", "--phone-seed-state-root", config["seed_state_root"]]
   return [
      Step("controller-up", "start the operator's controller iroh endpoint (owner-local sidecar; the phone allow-lists "
           "its endpoint id through the assignment); records its endpoint id",
           [config["controller"]["sidecar"], "--uds", config["controller"]["socket_path"], "--bootstrap-fd", "0"],
           background=True),
      Step("template", "write the bound template plan with controller.external_control for the controller "
           "endpoint, and the seed's public key record for the artifact source", in_process=True),
      Step("golden", "host Rust golden for the phone layers (same commit as the APK)",
           [str(Path(config["phone_executor"]).with_name("qwen2_stage_golden")), config["model_root"],
            str(start), str(end), str(GOLDEN_SEED), str(GOLDEN_SEQ)]),
      Step("manifest", "v2 stage manifest for the phone layers, with the host golden",
           [py, "-B", "-m", "mycelium_mobile.qwen_stage_manifest", "build", "--snapshot", config["model_root"],
            "--model-id", config["model_id"], "--resolved-commit", config["resolved_commit"],
            "--stage-id", "stage-001", "--layers", f"{start}:{end}", "--golden", str(work / "golden.json"),
            "--out", str(work / "stage-manifest.json")]),
      Step("export", "export the per-tensor objects the artifact source serves",
           [py, "-B", "-m", "mycelium_mobile.qwen_stage_manifest", "export", "--snapshot", config["model_root"],
            "--manifest", str(work / "stage-manifest.json"), "--out-dir", str(work / "objects")]),
      Step("artifact-source-up", "serve the objects to the phone under seed grants (A2-over-iroh)",
           [config["artifact_source"]["binary"], str(work / "objects"), str(work / "seed-key-record.json"),
            config["artifact_source"]["key_dir"]], background=True),
      Step("prepare", "REHEARSAL: builder --phone-prepare-only (assignment inputs only, no load/route authority)",
           [*builder, "--phone-prepare-only", "--output-root", str(work / "prepare")]),
      Step("assign", "seed-signed v2 route assignment to the phone; wait until the phone applied it "
           "(acquired, loaded, golden passed)",
           [py, "-B", "-m", "mycelium_seed.route_assignment", "--state-root", config["seed_state_root"], "issue",
            "--node-id", config["phone_node_id"], "--stage-pack", str(work / "stage-manifest.json"),
            "--preparation-assignment", str(work / "phone-assignment.json"), "--peer-endpoint-id", final_endpoint,
            "--route-generation", str(config["route_generation"]), "--controller-endpoint-id", controller_id,
            "--artifact-source", source_spec]),
      Step("assign-wait", "wait until the phone reports the assignment applied",
           [py, "-B", "-m", "mycelium_seed.route_assignment", "--state-root", config["seed_state_root"], "status",
            "--node-id", config["phone_node_id"], "--wait-applied", "900"]),
      Step("evidence", "hello/stage_load/configure/snapshot over the control ALPN, then the seed's builder "
           "authorization -> phone-builder-evidence.json", in_process=True),
      Step("build", "full route build with the phone evidence and build provenance",
           [*builder, "--phone-evidence", str(work / "phone-builder-evidence.json"),
            "--phone-seed-key-digest", config["seed_key_digest"], "--phone-executor", config["phone_executor"],
            "--phone-apk", config["phone_apk"], "--phone-source-root", config["phone_source_root"],
            "--output-root", str(work / "route")]),
      Step("stage", "stage the shards and control documents on the laptop",
           [py, "-B", f"{ck}/scripts/stage_live_route.py", "--operator-plan", str(work / "route/operator-plan.json")]),
      Step("serve", "serve the route on loopback (the public demo stays off); waits for route_alive",
           [py, "-B", "-m", "mycelium_demo", "serve", "--mode", "live", "--host", serve["host"],
            "--port", str(serve["port"]), "--operator-plan", str(work / "route/operator-plan.json"),
            "--seed-state-root", config["seed_state_root"], "--seed-url", config["seed_url"],
            "--static-root", f"{ck}/ui/web/dist", "--product-state-root", serve["product_state_root"]],
           background=True),
      Step("browser-request", f"ONE Playwright chromium request, max_new_tokens={config['request']['max_new_tokens']}; "
           "exit 0 only if completed through the phone's middle stage (driver --browser-request-command)",
           ["node", f"{ck}/ui/web/scripts/a12-phone-route-browser.mjs"],
           env={"MYCELIUM_A12_PRODUCT_ORIGIN": f"http://{serve['host']}:{serve['port']}",
                "MYCELIUM_A12_BROWSER_TOKENS": str(config["request"]["max_new_tokens"]),
                "MYCELIUM_A12_BROWSER_EVIDENCE": str(work / "browser-request.json")}),
      Step("thermal-probe", "one bounded API request during the heat window (driver --thermal-probe-command)",
           in_process=True),
      Step("down", "stop the serve (closes the route), the artifact source and the controller "
           "(driver --route-close-command)", in_process=True),
   ]


GATE_STEPS = ("browser-request", "thermal-probe", "down")


def preflight(config: Mapping[str, Any]) -> list[str]:
   """Local, read-only checks; returns problems (never raises, never writes)."""

   problems: list[str] = []
   for name in ("checkout", "seed_state_root", "model_root", "template_plan", "topology", "phone_executor",
                "phone_apk", "phone_source_root", "build_receipt", "python"):
      if not Path(config[name]).exists():
         problems.append(f"missing:{name}:{config[name]}")
   for name in ("sidecar",):
      if not Path(config["controller"][name]).exists():
         problems.append(f"missing:controller.{name}")
   if not Path(config["artifact_source"]["binary"]).exists():
      problems.append("missing:artifact_source.binary")
   checkout = Path(config["checkout"])
   for relative in ("ui/web/dist/index.html", "ui/web/scripts/a12-phone-route-browser.mjs", "ui/web/node_modules/@playwright/test"):
      if checkout.exists() and not (checkout / relative).exists():
         problems.append(f"missing:checkout/{relative}")
   try:
      topology = json.loads(Path(config["topology"]).read_text("utf-8"))
      nodes = topology.get("nodes", topology) if isinstance(topology, Mapping) else topology
      validate_topology(nodes, phone_node_id=config["phone_node_id"])
   except (OSError, ValueError) as error:
      problems.append(f"topology:{error}")
   except HarnessError as error:
      problems.append(f"topology:{error}")
   return problems


# -- in-process steps -------------------------------------------------------------


def _template(config: Mapping[str, Any]) -> None:
   state = _state(config)
   _require("controller_endpoint_id" in state, "controller_not_started")
   plan = json.loads(Path(config["template_plan"]).read_text("utf-8"))
   plan.setdefault("controller", {})["external_control"] = {
      "socket_path": config["controller"]["socket_path"],
      "bootstrap_secret_file": config["controller"]["bootstrap_secret_file"],
      "controller_endpoint_id": state["controller_endpoint_id"],
   }
   target = _work(config) / "template-plan.json"
   target.write_text(json.dumps(plan, sort_keys=True, indent=1) + "\n", encoding="utf-8")
   os.chmod(target, 0o600)
   from mycelium_seed.operator import _load_authority

   lease, coordinator, _binding = _load_authority(Path(config["seed_state_root"]), incarnation="a12-route-harness")
   try:
      _require(coordinator.signer.verification_key_digest == config["seed_key_digest"], "seed_key_digest_mismatch")
      record = coordinator.signer.public_key_record()
   finally:
      lease.close()
   (_work(config) / "seed-key-record.json").write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")


def _seed_rows(config: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
   from mycelium_seed import route_assignment as ra

   with ra._connect(Path(config["seed_state_root"]) / "state.sqlite3") as connection:
      member = connection.execute("SELECT * FROM seed_members WHERE node_id=?", (config["phone_node_id"],)).fetchone()
      row = connection.execute("SELECT * FROM seed_route_assignments WHERE node_id=?", (config["phone_node_id"],)).fetchone()
   _require(member is not None and row is not None, "phone_assignment_absent")
   return dict(member), dict(row)


def _evidence(config: Mapping[str, Any]) -> None:
   from mycelium_iroh_sidecar import SidecarClient
   from mycelium_live.phone_stage_session import PhoneStageSessionFactory
   from mycelium_seed.phone_preparation import issue_builder_authorization
   from physical_inference_qualification import PeerIdentity
   from scripts.qwen_phone_builder import digest
   from mycelium_qualification.evidence import canonical_json_bytes

   state = _state(config)
   work = _work(config)
   member, row = _seed_rows(config)
   manifest = json.loads(row["stage_pack_json"])
   assignment = json.loads((work / "phone-assignment.json").read_text("utf-8"))
   receipt = json.loads(Path(config["build_receipt"]).read_text("utf-8"))
   descriptor = {
      "socket_path": config["controller"]["socket_path"],
      "bootstrap_secret_file": config["controller"]["bootstrap_secret_file"],
      "controller_endpoint_id": state["controller_endpoint_id"],
   }
   secret = Path(descriptor["bootstrap_secret_file"]).read_bytes()
   # [UNVERIFIED on hardware] relay/discovery dial by endpoint id only.
   with SidecarClient(Path(descriptor["socket_path"]), secret, timeout=10) as client:
      client.configure_peer(member["endpoint_id"], {"id": member["endpoint_id"], "addrs": []}, generation=1)
   factory = PhoneStageSessionFactory.from_descriptor(descriptor)
   run_id = state["run_id"]
   peer = PeerIdentity(node_id=member["node_id"], host_id=member["node_id"], boot_id=member["incarnation"],
                       ssh_target=None, staging_root="/mycelium/phone-stage", process_transport=PHONE_TRANSPORT)
   session = factory(argv=(), node_id=member["node_id"], run_id=run_id, deployment_id=state["deployment_id"],
                     timeout_seconds=30, peer=peer, endpoint_id=member["endpoint_id"],
                     member_key_digest=member["verification_key_digest"])
   transcript: list[dict[str, Any]] = []

   def call(command: str, payload: Mapping[str, Any]) -> dict[str, Any]:
      response = session.send(command_id=f"a12-route-{command}", command=command, payload=dict(payload))
      transcript.append({"command": command, "ok": bool(response.get("ok"))})
      _require(response.get("ok"), f"phone_control_{command}_refused")
      return response

   try:
      call("hello", {})
      loaded = call("stage_load", {})
      generation = loaded["result"]["observation"]["details"]["stage_load_generation"]
      profile = {"assignment_id": assignment["assignment_id"], "stage_id": manifest["message"]["stage_id"],
                 "pack_digest": manifest["message"]["pack_digest"], "manifest_digest": assignment["manifest_digest"],
                 "load_generation": generation}
      configured = call("configure", profile)
      snapshot = call("snapshot", {})
   finally:
      try:
         call("stop", {})
      finally:
         session.close()
   document = {"manifest": manifest, "build_receipt": receipt, "configured": configured, "snapshot": snapshot}
   node = {"node_id": member["node_id"], "endpoint_id": member["endpoint_id"],
           "membership_generation": member["generation"], "staging_root": "/mycelium/phone-stage"}
   document["authorization"] = issue_builder_authorization(
      Path(config["seed_state_root"]), seed_key_digest=config["seed_key_digest"],
      approved_build_receipt_digest=digest(canonical_json_bytes(receipt)), document=document, node=node,
      run_id=run_id, deployment_id=state["deployment_id"], controller_endpoint_id=state["controller_endpoint_id"],
   )
   target = work / "phone-builder-evidence.json"
   target.write_text(json.dumps({"protocol": EVIDENCE_PROTOCOL, "run_id": run_id,
                                 "nodes": {member["node_id"]: document}}, sort_keys=True) + "\n", encoding="utf-8")
   os.chmod(target, 0o600)
   (work / "evidence-transcript.json").write_text(json.dumps(transcript, indent=1) + "\n", encoding="utf-8")


def _live_status(config: Mapping[str, Any]) -> dict[str, Any]:
   import urllib.request

   serve = config["serve"]
   with urllib.request.urlopen(f"http://{serve['host']}:{serve['port']}/__mycelium/live-status", timeout=10) as response:
      return json.loads(response.read().decode("utf-8"))


def _settled_status(config: Mapping[str, Any], before: int | None, *, seconds: float = 60.0) -> dict[str, Any]:
   """Wait (bounded) for the phone's next signed counters to land in live-status."""

   deadline = time.monotonic() + seconds
   status = _live_status(config)
   while time.monotonic() < deadline:
      sent = phone_frames_sent(status, config["phone_node_id"])
      if before is not None and sent is not None and sent > before:
         break
      time.sleep(2.0)
      status = _live_status(config)
   return status


def _browser_request(config: Mapping[str, Any]) -> int:
   step = next(s for s in steps(config, _state(config)) if s.name == "browser-request")
   before = _live_status(config)
   completed = subprocess.run(step.argv, cwd=Path(config["checkout"]) / "ui/web", env={**os.environ, **step.env},
                              check=False, timeout=600)
   report_path = _work(config) / "browser-request.json"
   report = json.loads(report_path.read_text("utf-8")) if report_path.exists() else {}
   terminal = report.get("terminal") if completed.returncode == 0 else f"browser_exit_{completed.returncode}"
   after = _settled_status(config, phone_frames_sent(before, config["phone_node_id"]))
   ok, reason = judge_browser_request(terminal=terminal, before=before, after=after, phone_node_id=config["phone_node_id"])
   _save_state(config, browser_request={"ok": ok, "reason": reason, "terminal": terminal, "at_unix_ms": int(time.time() * 1000)})
   print(json.dumps({"ok": ok, "reason": reason}, sort_keys=True))
   return EXIT_OK if ok else EXIT_FAILED


def _thermal_probe(config: Mapping[str, Any]) -> int:
   from scripts.run_a5_product_gate import GateError, ProductSession

   serve = config["serve"]
   before = phone_frames_sent(_live_status(config), config["phone_node_id"])
   try:
      session = ProductSession(f"http://{serve['host']}:{serve['port']}")
      accepted = session.submit(label="a12-thermal-probe", maximum_new_tokens=1)
      terminal = session.stream_summary(accepted)["terminal"]
   except (GateError, OSError) as error:
      print(json.dumps({"probe": "inconclusive", "error": type(error).__name__}))
      return EXIT_INCONCLUSIVE
   after = phone_frames_sent(_settled_status(config, before, seconds=20.0), config["phone_node_id"])
   code = classify_probe(terminal=terminal, sent_before=before, sent_after=after)
   print(json.dumps({"probe": {0: "not_run", 3: "executed"}.get(code, "inconclusive"), "terminal": terminal}))
   return code


def _down(config: Mapping[str, Any]) -> int:
   """Stop what this harness started; the serve first (it closes the route)."""

   state = _state(config)
   stopped = {}
   for name in ("serve", "artifact-source-up", "controller-up"):
      pid = state.get("pids", {}).get(name)
      if not pid:
         continue
      try:
         os.kill(pid, signal.SIGTERM)
      except ProcessLookupError:
         stopped[name] = "absent"
         continue
      for _ in range(60):
         try:
            os.kill(pid, 0)
         except ProcessLookupError:
            break
         time.sleep(1.0)
      else:
         os.kill(pid, signal.SIGKILL)
      stopped[name] = "stopped"
   _save_state(config, down=stopped, pids={})
   print(json.dumps({"down": stopped}, sort_keys=True))
   return EXIT_OK if stopped.get("serve") in ("stopped", "absent", None) else EXIT_FAILED


def ready_record(text: str) -> dict[str, Any] | None:
   """The first JSON ready line a sidecar / artifact source printed."""

   for line in text.splitlines():
      try:
         record = json.loads(line)
      except ValueError:
         continue
      if isinstance(record, dict) and (record.get("event") == "ready" or record.get("ready") is True):
         return record
   return None


def artifact_source_spec(record: Mapping[str, Any]) -> str:
   """``ENDPOINT_ID:KEY_DIGEST[@IP:PORT,...]`` for ``route_assignment issue --artifact-source``."""

   spec = f"{record['endpoint_id']}:{record['verification_key_digest']}"
   hints = [str(a) for a in record.get("direct_addrs", []) if isinstance(a, str)]
   return spec + ("@" + ",".join(hints) if hints else "")


def _start_background(config: Mapping[str, Any], step: Step) -> None:
   """Start a long-lived process detached, its output in the work dir.

   stdout goes to a file, never a pipe: the harness exits after each step and
   a closed pipe would kill the process on its next write.
   """
   work = _work(config)
   log_path = work / f"{step.name}.log"
   offset = log_path.stat().st_size if log_path.exists() else 0
   with open(log_path, "ab") as log:
      process = subprocess.Popen(step.argv, cwd=config["checkout"], stdin=subprocess.PIPE, stdout=log, stderr=log,
                                 start_new_session=True)
   if step.name == "controller-up":
      process.stdin.write(Path(config["controller"]["bootstrap_secret_file"]).read_bytes())
   process.stdin.close()
   _save_state(config, pids={**_state(config).get("pids", {}), step.name: process.pid})
   if step.name == "serve":
      deadline = time.monotonic() + 900
      while time.monotonic() < deadline and process.poll() is None:
         try:
            if _live_status(config).get("route_alive") is True:
               return
         except OSError:
            pass
         time.sleep(5.0)
      raise HarnessError("serve_route_not_alive")
   record = None
   deadline = time.monotonic() + 60
   while record is None and time.monotonic() < deadline and process.poll() is None:
      time.sleep(0.5)
      record = ready_record(log_path.read_bytes()[offset:].decode("utf-8", "replace"))
   _require(record is not None, f"{step.name}_not_ready")
   if step.name == "controller-up":
      _save_state(config, controller_endpoint_id=record["endpoint_id"])
   else:
      _save_state(config, artifact_source_spec=artifact_source_spec(record))


def topology_facts(config: Mapping[str, Any]) -> dict[str, Any]:
   """Read-only: the validated topology's final endpoint (the phone forwards to it)."""

   document = json.loads(Path(config["topology"]).read_text("utf-8"))
   nodes = document.get("nodes", document) if isinstance(document, Mapping) else document
   facts = validate_topology(nodes, phone_node_id=config["phone_node_id"])
   return {"final_endpoint_id": facts["final_endpoint_id"]} if facts["final_endpoint_id"] else {}


def run_step(config: Mapping[str, Any], name: str) -> int:
   if "final_endpoint_id" not in _state(config):
      _work(config).mkdir(mode=0o700, parents=True, exist_ok=True)
      _save_state(config, **topology_facts(config))
   state = _state(config)
   step = next((s for s in steps(config, state) if s.name == name), None)
   _require(step is not None, f"unknown_step:{name}")
   _work(config).mkdir(mode=0o700, parents=True, exist_ok=True)
   if name == "prepare" and "deployment_id" not in state:
      _save_state(config, deployment_id=str(uuid.uuid4()), run_id=f"a12-route-{uuid.uuid4().hex[:12]}")
      step = next(s for s in steps(config, _state(config)) if s.name == name)
   if any(part.startswith("<") for part in step.argv):
      raise HarnessError(f"step_inputs_unresolved:{name}")
   if name == "template":
      _template(config)
      return EXIT_OK
   if name == "evidence":
      _evidence(config)
      return EXIT_OK
   if name == "browser-request":
      return _browser_request(config)
   if name == "thermal-probe":
      return _thermal_probe(config)
   if name == "down":
      return _down(config)
   if step.background:
      _start_background(config, step)
      return EXIT_OK
   output = subprocess.run(step.argv, cwd=config["checkout"], check=False, capture_output=True, timeout=3600)
   (_work(config) / f"{name}.log").write_bytes(output.stdout + output.stderr)
   if output.returncode == 0 and name == "golden":
      (_work(config) / "golden.json").write_bytes(output.stdout)
   if output.returncode == 0 and name == "prepare":
      prepared = json.loads((_work(config) / "prepare/phone-preparation.json").read_text("utf-8"))
      phone = next(a for a in prepared["assignments"] if a["node_id"] == config["phone_node_id"])
      (_work(config) / "phone-assignment.json").write_text(json.dumps(phone, sort_keys=True) + "\n", encoding="utf-8")
   return EXIT_OK if output.returncode == 0 else EXIT_FAILED


def render_plan(config: Mapping[str, Any], state: Mapping[str, Any], problems: Sequence[str]) -> str:
   lines = [f"A12 phone route harness plan (dry run; nothing started or written). work_dir={config['work_dir']}"]
   for index, step in enumerate(steps(config, state), start=1):
      kind = "in-process" if step.in_process else "background" if step.background else "command"
      gate = "  [called by the A12 driver]" if step.name in GATE_STEPS else ""
      lines.append(f"{index:2d}. {step.name} ({kind}){gate}: {step.summary}")
      if step.argv:
         env = " ".join(f"{k}={v}" for k, v in step.env.items())
         lines.append("      $ " + (env + " " if env else "") + " ".join(step.argv))
   lines.append("problems: " + ("none" if not problems else "; ".join(problems)))
   return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
   root = argparse.ArgumentParser(description=__doc__.splitlines()[0])
   root.add_argument("--config", type=Path, required=True)
   sub = root.add_subparsers(dest="command", required=True)
   sub.add_parser("plan", help="dry run: validate, check local inputs, print every step; no side effects")
   run = sub.add_parser("run", help="run one pre-gate step")
   run.add_argument("step")
   for name in GATE_STEPS:
      sub.add_parser(name)
   return root


def main(argv: Sequence[str] | None = None) -> int:
   arguments = parser().parse_args(argv)
   try:
      config = validate_config(json.loads(arguments.config.read_text("utf-8")))
      if arguments.command == "plan":
         workdir_state = _state(config)
         problems = preflight(config)
         if not problems:
            workdir_state = {**topology_facts(config), **workdir_state}
         print(render_plan(config, workdir_state, problems))
         return EXIT_OK if not problems else EXIT_FAILED
      name = arguments.step if arguments.command == "run" else arguments.command
      return run_step(config, name)
   except HarnessError as error:
      print(json.dumps({"error": str(error)}), file=sys.stderr)
      return EXIT_INCONCLUSIVE


if __name__ == "__main__":
   raise SystemExit(main())
