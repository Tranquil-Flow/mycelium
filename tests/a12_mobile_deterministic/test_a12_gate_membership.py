# SPDX-License-Identifier: AGPL-3.0-only
"""Seed authority (a): node-0 and node-2 as temporary members of the A12 gate seed.

Owner decision 2026-10-13: for the A12 route session the Mac (node-0, entry)
and the laptop (node-2, final) take a second, temporary membership in the
throwaway A12 gate seed (swarm ``a12-gate``), next to the phone.  The fleet
seed (seed-r7) is never touched.  These tests pin, without devices, SSH or a
seed process:

* the gate seed is reachable from the laptop (LAN http bind or an https proxy);
* every configured seed path, member dir and URL is refused if it is the fleet
  seed's (config block and its launchd agent);
* invites come from the gate seed, the agents use gate-scoped dirs and the
  topology's data-plane endpoint ids;
* the route topology is bound to the gate seed's current generations;
* teardown stops both agents and revokes both gate memberships, at the end and
  on every failure path.
"""

from __future__ import annotations

import json
import plistlib
from pathlib import Path

import pytest

from scripts import a12_gate_membership as gm
from scripts import run_a12_phone_route as harness
from tests.a12_mobile_deterministic.test_run_a12_phone_route import PHONE, PHONE_ENDPOINT, _config, _topology

ENTRY_ENDPOINT = "01" * 32
FINAL_ENDPOINT = "02" * 32
GATE_DIGEST = "sha256:" + "5" * 64


def _write_topology(config) -> None:
   path = Path(config["topology"])
   path.parent.mkdir(parents=True, exist_ok=True)
   path.write_text(json.dumps({"nodes": _topology()}))


def _row(node_id, endpoint, *, generation=1, state="RUNNING", lease=10_000.0, incarnation=None, peer_class="mac_mlx_iroh"):
   return {"node_id": node_id, "endpoint_id": endpoint, "generation": generation, "lifecycle_state": state,
           "lease_expires_at": lease, "incarnation": incarnation or f"a12-gate-{node_id}", "peer_class": peer_class}


class FakeSeed:
   """The gate seed's durable store and the revoke seam, as the harness sees them."""

   def __init__(self, members=None, *, swarm="a12-gate", digest=GATE_DIGEST):
      self.members = dict(members or {})
      self.binding = {"swarm_id": swarm, "seed_key_digest": digest}
      self.revoked: list[tuple[str, str, int]] = []
      self.reads: list[str] = []

   def read(self, root):
      self.reads.append(str(root))
      return {"binding": dict(self.binding), "members": {k: dict(v) for k, v in self.members.items()}}

   def revoke(self, root, node_id, generation):
      self.revoked.append((str(root), node_id, generation))
      self.members[node_id] = {**self.members[node_id], "lifecycle_state": "STOPPED", "generation": generation + 1}
      return {"lifecycle_state": "STOPPED", "generation": generation + 1}


class FakeHosts:
   """Local spawns/kills and SSH scripts, recorded; SSH exit codes scripted by marker."""

   def __init__(self, ssh_codes=None):
      self.spawned: list[list[str]] = []
      self.killed: list[int] = []
      self.ssh_calls: list[tuple[str, str, bytes | None]] = []
      self.ssh_codes = dict(ssh_codes or {})

   def spawn(self, argv, *, cwd, log_path):
      self.spawned.append(list(argv))
      return 4242

   def stop(self, pid):
      self.killed.append(pid)
      return "stopped"

   def ssh(self, target, script, *, stdin=None, timeout=60.0):
      self.ssh_calls.append((target, script, stdin))
      for marker, code in self.ssh_codes.items():
         if marker in script:
            return code, ""
      return 0, "777\n"


@pytest.fixture
def seams(monkeypatch):
   seed, hosts = FakeSeed(), FakeHosts()
   monkeypatch.setattr(gm, "read_gate_seed", seed.read)
   monkeypatch.setattr(gm, "revoke", seed.revoke)
   monkeypatch.setattr(gm, "spawn_local", hosts.spawn)
   monkeypatch.setattr(gm, "stop_local", hosts.stop)
   monkeypatch.setattr(gm, "ssh", hosts.ssh)
   monkeypatch.setattr(gm, "now", lambda: 1_000.0)
   clock = {"t": 0.0}
   monkeypatch.setattr(gm, "monotonic", lambda: clock["t"])
   monkeypatch.setattr(gm, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
   ran = []
   monkeypatch.setattr(harness.subprocess, "run", lambda *a, **k: ran.append((a, k)) or pytest.fail("no command may run"))
   return seed, hosts, ran


def _ready(tmp_path, **overrides):
   config = harness.validate_config(_config(tmp_path, **overrides))
   _write_topology(config)
   return config


def _with_invites(config):
   outbox = Path(config["work_dir"]) / "gate-members" / "invites" / "invite-batch-x"
   outbox.mkdir(parents=True)
   for index in (1, 2):
      (outbox / f"native-node-00{index}.invite.json").write_text(json.dumps({"invite": index}))
   harness._save_state(config, gate_members={"invites": {"node-0": str(outbox / "native-node-001.invite.json"),
                                                          "node-2": str(outbox / "native-node-002.invite.json")}})
   return outbox


# -- config: reachability, gate-scoped dirs ------------------------------------------


def test_the_gate_seed_must_be_reachable_from_the_laptop(tmp_path):
   good = _config(tmp_path)
   assert harness.validate_config(good)["gate_seed"]["advertised_url"] == good["seed_url"]
   proxy = _config(tmp_path, seed_url="https://seed.example.invalid",
                   gate_seed={**good["gate_seed"], "bind": "127.0.0.1", "advertised_url": "https://seed.example.invalid",
                              "trusted_https_proxy": True})
   assert harness.validate_config(proxy)["gate_seed"]["trusted_https_proxy"] is True
   cases = [
      # A loopback http seed is unreachable from the laptop (node-2 is another host).
      ({"seed_url": "http://127.0.0.1:18865",
        "gate_seed": {**good["gate_seed"], "bind": "127.0.0.1", "advertised_url": "http://127.0.0.1:18865"}},
       "config_gate_seed_unreachable_from_laptop"),
      # The advertised URL must be the bound listener (the seed refuses anything else).
      ({"seed_url": "http://192.0.2.99:18865",
        "gate_seed": {**good["gate_seed"], "advertised_url": "http://192.0.2.99:18865"}}, "config_gate_seed_invalid"),
      # https needs the loopback proxy pattern.
      ({"seed_url": "https://seed.example.invalid",
        "gate_seed": {**good["gate_seed"], "advertised_url": "https://seed.example.invalid"}}, "config_gate_seed_invalid"),
      # Invites, members and the serve all use the one advertised URL.
      ({"seed_url": "http://192.0.2.10:18800"}, "config_seed_url_not_gate_advertised"),
   ]
   for override, code in cases:
      with pytest.raises(harness.HarnessError) as refused:
         harness.validate_config(_config(tmp_path, **override))
      assert str(refused.value) == code


def test_member_dirs_are_scoped_to_the_gate_dir(tmp_path):
   good = _config(tmp_path)
   members = good["gate_members"]
   cases = [
      ({**members, "entry": {**members["entry"], "data_dir": str(tmp_path / "elsewhere/node-0")}},
       "gate_member_dir_not_gate_scoped:entry"),
      ({**members, "final": {**members["final"], "data_dir": "/Users/member/mycelium-a12/members/node-2"}},
       "gate_member_dir_not_gate_scoped:final"),
      ({**members, "entry": {**members["entry"], "data_dir": str(Path(good["seed_state_root"]) / "node-0")}},
       "gate_member_dir_inside_seed:entry"),
      ({**members, "final": {**members["final"], "node_id": "node-0"}}, "config_gate_members_invalid"),
   ]
   for value, code in cases:
      with pytest.raises(harness.HarnessError) as refused:
         harness.validate_config(_config(tmp_path, gate_members=value))
      assert str(refused.value) == code


# -- the fleet seed (seed-r7) is refused ---------------------------------------------


def _fleet(config):
   return config["fleet_seed"]


def _gate_at(config, gate_dir: Path) -> dict:
   """Overrides that move the whole gate (seed and member dirs) to ``gate_dir``."""

   members = config["gate_members"]
   return {
      "seed_state_root": str(gate_dir / "seed"),
      "gate_members": {
         **members,
         "entry": {**members["entry"], "data_dir": str(gate_dir / "members/node-0")},
         "final": {**members["final"], "data_dir": f"/Users/member/a12/{gate_dir.name}/members/node-2"},
      },
   }


@pytest.mark.parametrize(
   "mutate, code",
   [
      (lambda c, t: {**_gate_at(c, Path(_fleet(c)["data_dir"]).parent), "seed_state_root": _fleet(c)["data_dir"]},
       "fleet_seed_refused:seed_state_root"),
      (lambda c, t: _gate_at(c, Path(_fleet(c)["protected_roots"][0]) / "a12-gate"), "fleet_seed_refused:seed_state_root"),
      (lambda c, t: {"work_dir": str(Path(_fleet(c)["data_dir"]).parent)}, "fleet_seed_refused:work_dir"),
      (lambda c, t: {"seed_url": "http://198.51.100.4:8877",
                     "gate_seed": {**c["gate_seed"], "bind": "198.51.100.4", "port": 8877,
                                   "advertised_url": "http://198.51.100.4:8877"}}, "fleet_seed_refused:seed_url"),
      (lambda c, t: {"gate_members": {**c["gate_members"], "final": {
                        **c["gate_members"]["final"],
                        "data_dir": "/Users/member/mycelium-physical-run/w6-ethonline/inputs/membership-node-2-r7"}}},
       "fleet_seed_refused:gate_members.final.data_dir"),
      (lambda c, t: {"gate_seed": {**c["gate_seed"], "swarm_id": "interactive-swarm"}},
       "fleet_seed_refused:gate_seed.swarm_id"),
   ],
)
def test_every_configured_seed_path_url_and_member_dir_of_the_fleet_seed_is_refused(tmp_path, mutate, code):
   base = _config(tmp_path)
   config = harness.validate_config({**base, **mutate(base, tmp_path)})
   refusals = gm.fleet_refusals(config)
   assert code in refusals, refusals
   assert gm.fleet_refusals(harness.validate_config(base)) == []


def test_the_fleet_seed_is_also_read_from_its_launch_agent(tmp_path):
   base = _config(tmp_path)
   # The config names some other dir, but the launchd agent runs the fleet seed on the gate's seed dir.
   agent = Path(base["fleet_seed"]["launch_agent"])
   arguments = plistlib.loads(agent.read_bytes())
   arguments["ProgramArguments"][arguments["ProgramArguments"].index("--data-dir") + 1] = base["seed_state_root"]
   agent.write_bytes(plistlib.dumps(arguments))
   assert "fleet_seed_refused:seed_state_root" in gm.fleet_refusals(harness.validate_config(base))
   agent.unlink()
   with pytest.raises(harness.HarnessError) as refused:
      gm.fleet_refusals(harness.validate_config(base))
   assert str(refused.value) == "fleet_seed_launch_agent_unreadable"


@pytest.mark.parametrize("command", [["plan"], ["run", "members-invite"], ["run", "members-enrol"], ["run", "members-down"], ["down"]])
def test_no_command_runs_against_the_fleet_seed(tmp_path, seams, capsys, command):
   seed, hosts, ran = seams
   base = _config(tmp_path)
   fleet_dir = Path(base["fleet_seed"]["data_dir"])
   config = {**base, **_gate_at(base, fleet_dir.parent), "seed_state_root": str(fleet_dir)}
   path = tmp_path / "config.json"
   path.write_text(json.dumps(config))
   code = harness.main(["--config", str(path), *command])
   captured = capsys.readouterr()
   assert "fleet_seed_refused:seed_state_root" in captured.out + captured.err
   assert code == (harness.EXIT_FAILED if command == ["plan"] else harness.EXIT_INCONCLUSIVE)
   assert (ran, hosts.spawned, hosts.ssh_calls, hosts.killed, seed.revoked, seed.reads) == ([], [], [], [], [], [])


# -- invites, enrolment ------------------------------------------------------------------


def test_the_invites_come_from_the_gate_seed(tmp_path, monkeypatch):
   config = _ready(tmp_path)
   step = next(s for s in harness.steps(config, {}) if s.name == "members-invite")
   assert step.argv == [
      config["python"], "-B", f"{config['checkout']}/scripts/mint_swarm_invites.py",
      "--seed-data-dir", config["seed_state_root"], "--seed-url", config["seed_url"], "--swarm-id", "a12-gate",
      "--output-root", str(Path(config["work_dir"]) / "gate-members/invites"), "--count", "2", "--ttl-seconds", "900",
   ]
   seed = FakeSeed()
   monkeypatch.setattr(gm, "read_gate_seed", seed.read)
   batch = Path(config["work_dir"]) / "gate-members/invites/invite-batch-abc"

   class Done:
      returncode = 0
      stdout = json.dumps({"output_directory": str(batch), "invite_count": 2}).encode()
      stderr = b""

   calls = []
   monkeypatch.setattr(harness.subprocess, "run", lambda argv, **k: calls.append(argv) or Done())
   assert harness.run_step(config, "members-invite") == harness.EXIT_OK
   assert calls == [step.argv]
   assert harness._state(config)["gate_members"]["invites"] == {
      "node-0": str(batch / "native-node-001.invite.json"), "node-2": str(batch / "native-node-002.invite.json"),
   }


def test_the_invite_step_refuses_a_seed_that_is_not_the_configured_gate_seed(tmp_path, seams):
   seed, hosts, ran = seams
   seed.binding["swarm_id"] = "interactive-swarm"
   with pytest.raises(harness.HarnessError) as refused:
      harness.run_step(_ready(tmp_path), "members-invite")
   assert str(refused.value) == "gate_seed_binding_mismatch"
   assert ran == []


def test_enrolment_starts_two_gate_scoped_agents_on_the_data_plane_endpoint_ids(tmp_path, seams):
   seed, hosts, ran = seams
   config = _ready(tmp_path)
   outbox = _with_invites(config)
   assert harness.run_step(config, "members-enrol") == harness.EXIT_OK
   entry, final = config["gate_members"]["entry"], config["gate_members"]["final"]
   assert hosts.spawned == [[
      config["python"], "-B", "-m", "mycelium_node", "--data-dir", entry["data_dir"],
      "--join-bundle-file", str(outbox / "native-node-001.invite.json"), "--node-id", "node-0",
      "--peer-class", "mac_mlx_iroh", "--membership-endpoint-id", ENTRY_ENDPOINT, "--advertise", entry["advertise"],
      "--membership-control-only", "--lifecycle-state", "RUNNING", "--incarnation", entry["incarnation"],
   ]]
   (target, copy, stdin), (_, start, _) = hosts.ssh_calls
   assert target == final["ssh_target"]
   assert stdin == (outbox / "native-node-002.invite.json").read_bytes()
   assert f"cat > {final['data_dir']}.invite.json" in copy
   assert f'"seed_key_digest":"{GATE_DIGEST}"' in copy and "exit 97" in copy
   for part in ("-m mycelium_node", f"--data-dir {final['data_dir']}", f"--join-bundle-file {final['data_dir']}.invite.json",
                "--node-id node-2", f"--membership-endpoint-id {FINAL_ENDPOINT}", "--membership-control-only"):
      assert part in start
   fleet = config["fleet_seed"]
   everything = json.dumps([hosts.spawned, [call[1] for call in hosts.ssh_calls]])
   assert fleet["data_dir"] not in everything and "8877" not in everything
   state = harness._state(config)
   assert state["gate_members"]["active"] is True
   assert state["pids"]["member-node-0"] == 4242


def test_enrolment_refuses_a_member_dir_holding_another_seeds_membership(tmp_path, seams):
   seed, hosts, ran = seams
   config = _ready(tmp_path)
   _with_invites(config)
   entry_dir = Path(config["gate_members"]["entry"]["data_dir"])
   entry_dir.mkdir(parents=True)
   state_file = entry_dir / "membership.state.json"
   state_file.write_text(json.dumps({"seed_key_digest": "sha256:" + "0" * 64, "swarm_id": "interactive-swarm"}))
   with pytest.raises(harness.HarnessError) as refused:
      harness.run_step(config, "members-enrol")
   assert str(refused.value) == "gate_member_state_foreign:node-0"
   assert hosts.spawned == [] and hosts.ssh_calls == [] and state_file.exists()
   # A previous attempt's state in THIS gate seed is cleared for a fresh join.
   state_file.write_text(json.dumps({"seed_key_digest": GATE_DIGEST, "swarm_id": "a12-gate"}))
   assert harness.run_step(config, "members-enrol") == harness.EXIT_OK
   assert not state_file.exists() and len(hosts.spawned) == 1


def test_a_failed_remote_enrolment_tears_down_the_entry_member_it_already_started(tmp_path, seams):
   seed, hosts, ran = seams
   config = _ready(tmp_path)
   outbox = _with_invites(config)
   hosts.ssh_codes = {"exit 97": 97}
   seed.members["node-0"] = _row("node-0", ENTRY_ENDPOINT, generation=1)
   assert harness.run_step(config, "members-enrol") == harness.EXIT_FAILED
   assert hosts.killed == [4242]
   assert seed.revoked == [(config["seed_state_root"], "node-0", 1)]
   assert not outbox.exists()
   state = harness._state(config)["gate_members"]
   assert state["active"] is False and state["error"] == "gate_member_state_foreign:node-2"


def test_membership_wait_succeeds_only_for_running_members_on_the_topology_endpoints(tmp_path, seams):
   seed, hosts, ran = seams
   config = _ready(tmp_path)
   outbox = _with_invites(config)
   assert harness.run_step(config, "members-enrol") == harness.EXIT_OK
   seed.members["node-0"] = _row("node-0", ENTRY_ENDPOINT, generation=1)
   seed.members["node-2"] = _row("node-2", FINAL_ENDPOINT, generation=1)
   assert harness.run_step(config, "members-wait") == harness.EXIT_OK
   assert not outbox.exists()
   assert any(f"rm -f {config['gate_members']['final']['data_dir']}.invite.json" in s for _, s, _ in hosts.ssh_calls)
   assert harness._state(config)["gate_members"]["generations"] == {"node-0": 1, "node-2": 1}
   assert seed.revoked == []


@pytest.mark.parametrize(
   "rows",
   [
      {"node-0": _row("node-0", ENTRY_ENDPOINT)},  # node-2 never joined
      {"node-0": _row("node-0", ENTRY_ENDPOINT), "node-2": _row("node-2", "ff" * 32)},  # wrong endpoint
      {"node-0": _row("node-0", ENTRY_ENDPOINT), "node-2": _row("node-2", FINAL_ENDPOINT, lease=10.0)},  # lease expired
   ],
)
def test_a_membership_wait_that_times_out_tears_both_members_down(tmp_path, seams, rows):
   seed, hosts, ran = seams
   config = _ready(tmp_path)
   _with_invites(config)
   assert harness.run_step(config, "members-enrol") == harness.EXIT_OK
   seed.members.update(rows)
   assert harness.run_step(config, "members-wait") == harness.EXIT_FAILED
   assert hosts.killed == [4242]
   assert any("kill" in s and config["gate_members"]["final"]["data_dir"] in s for _, s, _ in hosts.ssh_calls)
   assert sorted(node for _, node, _ in seed.revoked) == sorted(rows)
   assert all(root == config["seed_state_root"] for root, _, _ in seed.revoked)
   state = harness._state(config)["gate_members"]
   assert state["active"] is False and state["error"] == "gate_members_not_running"


# -- the route plan is bound to the gate seed membership -------------------------------------


def test_the_route_topology_is_bound_to_the_gate_seed_generations(tmp_path, seams):
   seed, hosts, ran = seams
   config = _ready(tmp_path)
   seed.members.update({
      "node-0": _row("node-0", ENTRY_ENDPOINT, generation=4),
      "node-2": _row("node-2", FINAL_ENDPOINT, generation=2),
      PHONE: _row(PHONE, PHONE_ENDPOINT, generation=7, peer_class="android_native_qwen2_iroh"),
   })
   assert harness.run_step(config, "bind-topology") == harness.EXIT_OK
   bound = json.loads((Path(config["work_dir"]) / "topology.gate.json").read_text())["nodes"]
   assert [(n["node_id"], n["membership_generation"]) for n in bound] == [("node-0", 4), (PHONE, 7), ("node-2", 2)]
   assert harness._state(config)["phone_binding"] == "gate_seed"
   names = {s.name: s for s in harness.steps(config, harness._state(config))}
   for name in ("prepare", "build"):
      argv = names[name].argv
      assert argv[argv.index("--topology") + 1] == str(Path(config["work_dir"]) / "topology.gate.json")
      assert argv[argv.index("--phone-seed-state-root") + 1] == config["seed_state_root"]
   assert names["serve"].argv[names["serve"].argv.index("--seed-url") + 1] == config["seed_url"]


def test_binding_refuses_members_that_are_not_current_in_the_gate_seed(tmp_path, seams):
   seed, hosts, ran = seams
   config = _ready(tmp_path)
   cases = [
      ({"node-2": _row("node-2", FINAL_ENDPOINT)}, "gate_member_not_current:node-0"),
      ({"node-0": _row("node-0", ENTRY_ENDPOINT, state="STOPPED"), "node-2": _row("node-2", FINAL_ENDPOINT)},
       "gate_member_not_current:node-0"),
      ({"node-0": _row("node-0", ENTRY_ENDPOINT), "node-2": _row("node-2", "ff" * 32)}, "gate_member_not_current:node-2"),
      ({"node-0": _row("node-0", ENTRY_ENDPOINT), "node-2": _row("node-2", FINAL_ENDPOINT),
        PHONE: _row(PHONE, "ee" * 32, peer_class="android_native_qwen2_iroh")}, "gate_phone_endpoint_mismatch"),
   ]
   for rows, code in cases:
      seed.members = dict(rows)
      with pytest.raises(harness.HarnessError) as refused:
         harness.run_step(config, "bind-topology")
      assert str(refused.value) == code


def test_the_full_build_needs_the_phone_bound_from_the_gate_seed(tmp_path, seams):
   seed, hosts, ran = seams
   config = _ready(tmp_path)
   seed.members = {"node-0": _row("node-0", ENTRY_ENDPOINT), "node-2": _row("node-2", FINAL_ENDPOINT)}
   assert harness.run_step(config, "bind-topology") == harness.EXIT_OK  # rehearsal: phone not joined yet
   assert harness._state(config)["phone_binding"] == "topology_placeholder"
   harness._save_state(config, deployment_id="d", run_id="r")
   with pytest.raises(harness.HarnessError) as refused:
      harness.run_step(config, "build")
   assert str(refused.value) == "gate_phone_membership_unbound"
   (Path(config["work_dir"]) / "topology.gate.json").unlink()
   with pytest.raises(harness.HarnessError) as refused:
      harness.run_step(config, "prepare")
   assert str(refused.value) == "gate_topology_unbound"
   assert ran == []


# -- teardown ----------------------------------------------------------------------------------


def _enrolled(tmp_path, seams):
   seed, hosts, ran = seams
   config = _ready(tmp_path)
   _with_invites(config)
   assert harness.run_step(config, "members-enrol") == harness.EXIT_OK
   seed.members.update({"node-0": _row("node-0", ENTRY_ENDPOINT, generation=3),
                        "node-2": _row("node-2", FINAL_ENDPOINT, generation=5)})
   hosts.ssh_calls.clear()
   return config


def test_members_down_stops_both_agents_and_revokes_both_gate_memberships(tmp_path, seams):
   seed, hosts, ran = seams
   config = _enrolled(tmp_path, seams)
   assert harness.run_step(config, "members-down") == harness.EXIT_OK
   assert hosts.killed == [4242]
   assert len(hosts.ssh_calls) == 1 and "kill" in hosts.ssh_calls[0][1]
   assert seed.revoked == [(config["seed_state_root"], "node-0", 3), (config["seed_state_root"], "node-2", 5)]
   state = harness._state(config)
   assert state["gate_members"]["active"] is False
   assert state["gate_members"]["teardown"]["node-0"]["revoked"] == "revoked"
   assert "member-node-0" not in state["pids"]
   # Idempotent: a second teardown revokes nothing twice.
   assert harness.run_step(config, "members-down") == harness.EXIT_OK
   assert len(seed.revoked) == 2


def test_down_closes_the_route_and_tears_the_members_down_even_when_the_route_close_fails(tmp_path, seams, monkeypatch):
   seed, hosts, ran = seams
   config = _enrolled(tmp_path, seams)

   def broken(_config):
      raise OSError("serve stop failed")

   monkeypatch.setattr(harness, "_close_route", broken)
   assert harness.run_step(config, "down") == harness.EXIT_FAILED
   assert [node for _, node, _ in seed.revoked] == ["node-0", "node-2"]
   assert hosts.killed == [4242]
   assert harness._state(config)["gate_members"]["active"] is False


def test_down_without_gate_members_does_not_touch_any_seed(tmp_path, seams, monkeypatch):
   seed, hosts, ran = seams
   config = _ready(tmp_path)
   monkeypatch.setattr(harness, "_close_route", lambda _c: harness.EXIT_OK)
   assert harness.run_step(config, "down") == harness.EXIT_OK
   assert (seed.reads, seed.revoked, hosts.ssh_calls) == ([], [], [])


def test_teardown_continues_past_an_unreachable_laptop_and_reports_the_failure(tmp_path, seams):
   seed, hosts, ran = seams
   config = _enrolled(tmp_path, seams)
   hosts.ssh_codes = {"kill": 255}
   assert harness.run_step(config, "members-down") == harness.EXIT_FAILED
   # The seed-side fence still lands for both: a stray laptop agent can no longer renew.
   assert [node for _, node, _ in seed.revoked] == ["node-0", "node-2"]
   assert harness._state(config)["gate_members"]["teardown"]["node-2"]["agent"] == "ssh_exit_255"


def test_teardown_never_revokes_in_a_seed_that_is_not_the_configured_gate_seed(tmp_path, seams):
   seed, hosts, ran = seams
   config = _enrolled(tmp_path, seams)
   seed.binding["seed_key_digest"] = "sha256:" + "0" * 64
   assert harness.run_step(config, "members-down") == harness.EXIT_FAILED
   assert seed.revoked == []
   assert hosts.killed == [4242]  # the agents still stop
   assert harness._state(config)["gate_members"]["teardown"]["error"] == "gate_seed_binding_mismatch"


def test_the_dry_run_lists_the_membership_steps_and_the_seed_parameters(tmp_path, capsys):
   config = _config(tmp_path)
   path = tmp_path / "config.json"
   path.write_text(json.dumps(config))
   before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
   harness.main(["--config", str(path), "plan"])
   out = capsys.readouterr().out
   assert "gate seed: swarm a12-gate at http://192.0.2.10:18865 (bind 192.0.2.10:18865" in out
   assert f"fleet seed (never touched): {config['fleet_seed']['data_dir']}" in out
   assert "mint_swarm_invites.py" in out and "--membership-control-only" in out
   assert "manage_seed_membership.py revoke" in out and "ssh -o BatchMode=yes -o ConnectTimeout=10 member-mac" in out
   assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
