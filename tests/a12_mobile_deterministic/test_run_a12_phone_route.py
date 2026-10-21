# SPDX-License-Identifier: AGPL-3.0-only
"""The A12 M5 route harness (``scripts/run_a12_phone_route.py``), without devices.

The harness runs the 0.5B route Mac entry -> phone middle stage -> laptop final
and gives the A12 driver its browser-request, thermal-probe and route-close
commands.  These tests pin what can be decided without hardware: the closed
config, the topology shape, the live-status judgement of the browser request,
the thermal-probe exit codes the driver relies on, and a dry run that has no
side effects.
"""

from __future__ import annotations

import json
import plistlib
from pathlib import Path

import pytest

from scripts import run_a12_mobile_physical_gate as gate_module
from scripts import run_a12_phone_route as harness

PHONE = "node-phone"
PHONE_ENDPOINT = "ab" * 32


def _topology():
   return [
      {"node_id": "node-0", "runtime_backend": "mlx", "process_transport": "local", "endpoint_id": "01" * 32},
      {"node_id": PHONE, "runtime_backend": "mobile-rust-qwen2", "process_transport": "external_iroh",
       "endpoint_id": PHONE_ENDPOINT, "membership_generation": 3},
      {"node_id": "node-2", "runtime_backend": "mlx", "process_transport": "ssh", "endpoint_id": "02" * 32},
   ]


def _fleet_seed(tmp_path: Path) -> dict:
   """The fleet seed (seed-r7 stand-in) and its launchd agent, which the harness must never touch."""

   fleet = tmp_path / "fleet"
   fleet.mkdir(parents=True, exist_ok=True)
   agent = fleet / "org.mycelium.core-pilot.r7.seed.plist"
   if not agent.exists():
      agent.write_bytes(plistlib.dumps({"Label": "org.mycelium.core-pilot.r7.seed", "ProgramArguments": [
         "/usr/bin/env", "/opt/homebrew/bin/python3.14", "-B", "-m", "mycelium_seed", "--bind", "198.51.100.4",
         "--port", "8877", "--data-dir", str(fleet / "seed-r7"), "--advertised-url", "http://198.51.100.4:8877",
         "--swarm-id", "interactive-swarm"]}))
   return {"launch_agent": str(agent), "data_dir": str(fleet / "seed-r7"), "url": "http://198.51.100.4:8877",
           "protected_roots": [str(fleet), "/Users/member/mycelium-physical-run/w6-ethonline"]}


def _config(tmp_path: Path, **overrides):
   root = tmp_path / "inputs"
   config = {
      "protocol": harness.CONFIG_PROTOCOL,
      "checkout": str(root / "checkout"),
      "work_dir": str(tmp_path / "work"),
      "python": "/opt/homebrew/bin/python3.14",
      "seed_state_root": str(root / "seed"),
      "seed_key_digest": "sha256:" + "5" * 64,
      "seed_url": "http://192.0.2.10:18865",
      "model_root": str(root / "model"),
      "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
      "resolved_commit": "7ae557604adf67be50417f59c2c2f167def9a775",
      "template_plan": str(root / "fresh-plan.json"),
      "topology": str(root / "topology.json"),
      "phone_node_id": PHONE,
      "phone_layers": [12, 14],
      "route_generation": 1,
      "phone_executor": str(root / "examples/qwen2_stage_exec"),
      "phone_apk": str(root / "node.apk"),
      "phone_source_root": str(root / "source"),
      "build_receipt": str(root / "receipt.json"),
      "artifact_source": {"binary": str(root / "examples/artifact_source"), "key_dir": str(root / "source-keys")},
      "controller": {"sidecar": str(root / "mycelium-iroh-sidecar"), "socket_path": str(root / "run/c.sock"),
                     "bootstrap_secret_file": str(root / "controller.secret")},
      "serve": {"host": "127.0.0.1", "port": 8797, "product_state_root": str(root / "product-state")},
      "request": {"prompt": "Name one colour of the sky.", "max_new_tokens": 12},
      "gate_seed": {"swarm_id": "a12-gate", "bind": "192.0.2.10", "port": 18865,
                    "advertised_url": "http://192.0.2.10:18865", "trusted_https_proxy": False},
      "gate_members": {
         "invite_ttl_seconds": 900,
         "wait_seconds": 60,
         "entry": {"node_id": "node-0", "data_dir": str(root / "members/node-0"), "advertise": "http://192.0.2.10:18800",
                   "incarnation": "a12-gate-node-0"},
         "final": {"node_id": "node-2", "ssh_target": "member-mac", "python": "/Users/member/venv/bin/python",
                   "checkout": "/Users/member/src", "data_dir": f"/Users/member/a12/{root.name}/members/node-2",
                   "advertise": "http://192.0.2.20:18800", "incarnation": "a12-gate-node-2"},
      },
      "fleet_seed": _fleet_seed(tmp_path),
   }
   config.update(overrides)
   return config


def _status(*, phone_index=1, frames=5, alive=True, simulated=False):
   stages = [
      {"stage_id": "s0", "node_id": "node-0", "runtime_backend": "mlx", "start_layer": 0, "end_layer_exclusive": 12},
      {"stage_id": "s1", "node_id": PHONE, "runtime_backend": "mobile-rust-qwen2", "start_layer": 12, "end_layer_exclusive": 14},
      {"stage_id": "s2", "node_id": "node-2", "runtime_backend": "mlx", "start_layer": 14, "end_layer_exclusive": 24},
   ]
   if phone_index == 2:
      stages[1]["node_id"], stages[2]["node_id"] = "node-2", PHONE
      stages[1]["runtime_backend"], stages[2]["runtime_backend"] = "mlx", "mobile-rust-qwen2"
   return {
      "route_alive": alive,
      "simulated": simulated,
      "stages": stages,
      "peers": [{"node_id": "node-0", "frames_sent": 40}, {"node_id": PHONE, "frames_sent": frames}],
   }


def test_the_config_is_closed_and_the_request_bounded(tmp_path):
   assert harness.validate_config(_config(tmp_path))["request"]["max_new_tokens"] == 12
   bad = [
      ({"request": {"prompt": "x", "max_new_tokens": 17}}, "config_request_unbounded"),
      ({"request": {"prompt": "x", "max_new_tokens": 0}}, "config_request_unbounded"),
      ({"serve": {"host": "0.0.0.0", "port": 8797, "product_state_root": "/p"}}, "config_serve_not_loopback"),
      ({"phone_layers": [12, 15]}, "config_phone_layers_invalid"),
      ({"checkout": "relative/checkout"}, "config_path_invalid:checkout"),
   ]
   for override, code in bad:
      with pytest.raises(harness.HarnessError) as refused:
         harness.validate_config(_config(tmp_path, **override))
      assert str(refused.value) == code
   with pytest.raises(harness.HarnessError):
      harness.validate_config({**_config(tmp_path), "extra": True})


def test_the_phone_is_the_middle_stage_between_two_desktop_hosts():
   assert harness.validate_topology(_topology(), phone_node_id=PHONE) == {
      "entry": "node-0", "phone": PHONE, "final": "node-2", "final_endpoint_id": "02" * 32,
   }
   nodes = _topology()
   for order, code in (([1, 0, 2], "topology_phone_not_middle"), ([0, 2, 1], "topology_phone_not_middle")):
      with pytest.raises(harness.HarnessError) as refused:
         harness.validate_topology([nodes[i] for i in order], phone_node_id=PHONE)
      assert str(refused.value) == code
   with pytest.raises(harness.HarnessError) as refused:
      harness.validate_topology(nodes[:2], phone_node_id=PHONE)
   assert str(refused.value) == "topology_not_three_hosts"
   wrong = _topology()
   wrong[1]["runtime_backend"] = "mobile-rust-reference"
   with pytest.raises(harness.HarnessError) as refused:
      harness.validate_topology(wrong, phone_node_id=PHONE)
   assert str(refused.value) == "topology_phone_runtime_invalid"
   unbound = _topology()
   del unbound[1]["membership_generation"]
   with pytest.raises(harness.HarnessError) as refused:
      harness.validate_topology(unbound, phone_node_id=PHONE)
   assert str(refused.value) == "topology_phone_membership_unbound"


def test_a_browser_request_passes_only_when_it_completed_through_the_phone_middle_stage():
   ok, reason = harness.judge_browser_request(terminal="completed", before=_status(frames=5), after=_status(frames=9), phone_node_id=PHONE)
   assert ok and reason == "completed_through_phone_middle_stage"
   cases = [
      (dict(terminal="failed", before=_status(), after=_status(frames=9)), "browser_request_not_completed:failed"),
      (dict(terminal="completed", before=_status(), after=_status(frames=5)), "phone_forwarded_no_frames"),
      (dict(terminal="completed", before=_status(), after=_status(frames=9, phone_index=2)), "phone_not_a_middle_stage_of_the_live_route"),
      (dict(terminal="completed", before=_status(), after=_status(frames=9, simulated=True)), "physical_route_not_alive"),
      (dict(terminal="completed", before={"peers": []}, after=_status(frames=9)), "phone_frame_counters_unavailable"),
   ]
   for kwargs, code in cases:
      assert harness.judge_browser_request(phone_node_id=PHONE, **kwargs) == (False, code)


def test_the_thermal_probe_exit_codes_are_the_drivers_contract():
   assert harness.EXIT_OK == gate_module.THERMAL_PROBE_NOT_RUN
   assert harness.EXIT_PROBE_EXECUTED == gate_module.THERMAL_PROBE_EXECUTED
   assert harness.classify_probe(terminal="completed", sent_before=4, sent_after=6) == gate_module.THERMAL_PROBE_EXECUTED
   assert harness.classify_probe(terminal="failed", sent_before=4, sent_after=4) == gate_module.THERMAL_PROBE_NOT_RUN
   # The phone forwarded something yet the request failed: not a clean non-run.
   assert harness.classify_probe(terminal="failed", sent_before=4, sent_after=5) == harness.EXIT_INCONCLUSIVE
   assert harness.classify_probe(terminal="completed", sent_before=4, sent_after=4) == harness.EXIT_INCONCLUSIVE
   assert harness.classify_probe(terminal=None, sent_before=None, sent_after=None) == harness.EXIT_INCONCLUSIVE


def test_ready_lines_resolve_the_controller_and_artifact_source_identities():
   text = "warming up\n" + json.dumps({"ready": True, "endpoint_id": "cd" * 32, "verification_key_digest": "sha256:" + "e" * 64,
                                      "direct_addrs": ["192.0.2.1:4433"]}) + "\n"
   record = harness.ready_record(text)
   assert harness.artifact_source_spec(record) == f"{'cd' * 32}:sha256:{'e' * 64}@192.0.2.1:4433"
   assert harness.ready_record('{"event":"ready","endpoint_id":"x"}')["endpoint_id"] == "x"
   assert harness.ready_record("no json here") is None


def test_the_dry_run_prints_every_step_and_has_no_side_effects(tmp_path, capsys):
   config_path = tmp_path / "config.json"
   config_path.write_text(json.dumps(_config(tmp_path)))
   before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
   code = harness.main(["--config", str(config_path), "plan"])
   out = capsys.readouterr().out
   assert code == 1  # the inputs do not exist here; the plan still prints, and says so
   assert "problems: missing:checkout" in out
   names = [step.name for step in harness.steps(harness.validate_config(_config(tmp_path)), {})]
   assert names == [
      "controller-up", "template", "golden", "manifest", "export", "artifact-source-up",
      "members-invite", "members-enrol", "members-wait", "bind-topology", "prepare", "assign",
      "assign-wait", "evidence", "build", "stage", "serve", "browser-request", "thermal-probe", "down", "members-down",
   ]
   for name in names:
      assert f". {name} (" in out
   assert "--phone-prepare-only" in out
   assert "MYCELIUM_A12_BROWSER_TOKENS=12" in out
   assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
   assert not (tmp_path / "work").exists()


def test_a_step_with_unresolved_inputs_refuses_before_running(tmp_path, monkeypatch):
   config = harness.validate_config(_config(tmp_path))
   topology = Path(config["topology"])
   topology.parent.mkdir(parents=True)
   topology.write_text(json.dumps(_topology()))
   ran = []
   monkeypatch.setattr(harness.subprocess, "run", lambda *a, **k: ran.append(a))
   with pytest.raises(harness.HarnessError) as refused:
      harness.run_step(config, "assign")
   assert str(refused.value) == "step_inputs_unresolved:assign"
   assert ran == []
