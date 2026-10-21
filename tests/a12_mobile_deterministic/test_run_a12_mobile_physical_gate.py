# SPDX-License-Identifier: AGPL-3.0-only
"""The A12 native Android physical gate driver (``scripts/run_a12_mobile_physical_gate.py``).

A simulated seed authority and a simulated device stand in for the Mac seed and the
phone: the operator prompts drive the simulated device through each transition, and
the driver judges every case from the seed's membership authority and from the
device's member-signed observations exactly as it does on hardware.  A device that
misbehaves (keeps renewing after revocation, keeps its incarnation across process
death, never lets its lease lapse) turns the case into a violation and withholds the
role.
"""

from __future__ import annotations

import json
import stat
import time

import pytest

from mycelium_mobile import a12_activation as a12
from mycelium_qualification.signing import generate_ed25519_signer
from scripts import run_a12_mobile_physical_gate as gate_module

BUILD = "sha256:" + "b" * 64
FAMILY = "family:" + "a1" * 16
# The driver decides the role at the real current time, so observations are sealed
# just before it, exactly as a device seals them during the gate.
NOW = time.time() * 1000.0


class SimulatedSwarm:
   """Seed authority + phone.  ``faults`` names the device behaviours to break."""

   def __init__(self, member_key: str, faults: set[str] | None = None, peer_class: str = "android_native_iroh") -> None:
      self.member_key = member_key
      self.faults = faults or set()
      self.peer_class = peer_class
      self.members_state: list[dict] = [
         {
            "node_id": "node-mac",
            "peer_class": "macos_mlx_iroh",
            "generation": 1,
            "incarnation": "mac-1",
            "lifecycle_state": "RUNNING",
            "lease_freshness": "fresh",
            "activation_eligible": True,
            "revocation_state": "active",
         }
      ]
      self.prompts: list[str] = []

   # seed authority
   def members(self):
      return [dict(item) for item in self.members_state]

   def member_key_digest(self, node_id):
      return self.member_key

   def revoke(self, node_id, generation):
      phone = self._phone()
      phone.update(generation=generation + 1, lifecycle_state="STOPPED", revocation_state="revoked")
      if "renews_after_revocation" in self.faults:
         phone.update(revocation_state="active", lease_freshness="fresh")
      return {"lifecycle_state": "STOPPED", "generation": generation + 1}

   def _phone(self):
      return next(item for item in self.members_state if item["node_id"] == "node-phone")

   # the operator performs the prompted action on the simulated phone
   def operator(self, message: str) -> None:
      self.prompts.append(message)
      if message.startswith("Create a fresh invite"):
         self.members_state.append(
            {
               "node_id": "node-phone",
               "peer_class": self.peer_class,
               "generation": 1,
               "incarnation": "inc-1",
               "lifecycle_state": "RUNNING",
               "lease_freshness": "fresh",
               "activation_eligible": True,
               "revocation_state": "active",
            }
         )
      elif message.startswith("Enable airplane mode"):
         if "lease_never_lapses" not in self.faults:
            self._phone()["lease_freshness"] = "expired"
      elif message.startswith("Disable airplane mode"):
         self._phone()["lease_freshness"] = "fresh"
      elif message.startswith("Force-stop"):
         if "incarnation_survives_process_death" not in self.faults:
            self._phone()["incarnation"] = "inc-2"


def immediate_wait(predicate, *, timeout, **_):
   return predicate()


def _envelopes(signer, count=5, thermal=35.0, hot_at=None, drain=True, hot_reading=40.5, refused_at=None):
   envelopes = []
   for generation in range(1, count + 1):
      hot = hot_at is not None and generation == hot_at
      after_hot = hot_at is not None and generation > hot_at
      document = {
         "protocol": "mycelium.mobile_runtime_observation.v1",
         "platform_family": "android",
         "runtime_build_digest": BUILD,
         "profile_generation": 1,
         "evidence_generation": generation,
         "source_kind": "native_device_observation",
         "observed_at_unix_ms": NOW - 1_000.0,
         "measurement_duration_ms": 120_000.0,
         "sample_count": 4,
         "authority": "mycelium_node_app",
         "metrics": {
            "thermal_max_celsius": hot_reading if hot else thermal,
            "battery_level_percent": 70.0,
            "charging": 0.0,
            "memory_available_bytes": 2.5e9,
            "lifecycle_member": 1.0,
            "relay_online": 1.0,
            "stage_loaded": 0.0,
            "frames_executed_window": 0.0 if (after_hot and drain) else 5.0,
            # Hops the phone itself refused with thermal_ceiling_reached in this window.
            **({"thermal_refusals_window": 1.0 if generation == refused_at else 0.0} if refused_at else {}),
         },
      }
      envelopes.append(
         {
            "protocol": a12.MEMBER_ENVELOPE_PROTOCOL,
            "message": document,
            "signature": signer.sign(document),
            "verification_key": signer.public_key_record(),
         }
      )
   return envelopes


def _loader(envelopes):
   def load(member_key_digest):
      records = [
         a12.observe_mobile_runtime(
            member_ref="node-phone",
            platform_family="android",
            runtime_build_digest=BUILD,
            observation_envelope=envelope,
            expected_member_key_digest=member_key_digest,
            now_unix_ms=NOW,
         )
         for envelope in envelopes
      ]
      return envelopes, records

   return load


#: The owner's invite quota ceiling the simulated phone joined with.
QUOTA_CEILING = 40.0


def _run(tmp_path, swarm, envelopes, thermal_ceiling_celsius=QUOTA_CEILING):
   gate = gate_module.Gate(
      authority=swarm,
      operator=swarm.operator,
      role="probe_contributor",
      route_peer_command=None,
      wait=immediate_wait,
      thermal_ceiling_celsius=thermal_ceiling_celsius,
   )
   return gate_module.run_gate(
      gate,
      evidence_loader=_loader(envelopes),
      member_key_digest=None,
      runtime_build_digest=BUILD,
      family_ref=FAMILY,
      out=tmp_path,
   )


@pytest.fixture()
def signer():
   return generate_ed25519_signer(endpoint_id="phone-endpoint")


def test_a_well_behaved_device_passes_every_case_except_unprovoked_thermal(tmp_path, signer):
   swarm = SimulatedSwarm(signer.verification_key_digest)
   summary = _run(tmp_path, swarm, _envelopes(signer))

   assert summary["positives"] == {
      "native_evidence_published": "passed",
      "off_tailnet_join": "passed",
      "reconnect": "passed",
   }
   assert summary["negatives"] == {
      "background": "failed_closed",
      "network_loss": "failed_closed",
      "process_death": "failed_closed",
      "revocation": "failed_closed",
      "thermal_window": "not_executed",
   }
   # Thermal pressure was never reached, so the role is withheld, not assumed.
   assert summary["decision"] == "withheld"
   assert summary["blockers"] == ["physical_negative_not_passed:thermal_window"]
   bundle = json.loads((tmp_path / "mobile-physical-gate.json").read_text("utf-8"))
   assert bundle["adb_used_during_gate"] is False
   assert bundle["execution_path"] == "signed_native_application"
   assert stat.S_IMODE((tmp_path / "mobile-physical-gate.json").stat().st_mode) == 0o600
   assert swarm.prompts[-1].startswith("Leave the app open")


def test_a_device_that_drains_under_thermal_pressure_qualifies(tmp_path, signer):
   swarm = SimulatedSwarm(signer.verification_key_digest)
   summary = _run(tmp_path, swarm, _envelopes(signer, count=6, hot_at=3))
   assert summary["negatives"]["thermal_window"] == "failed_closed"
   assert summary["decision"] == "eligible", summary["blockers"]
   qualification = json.loads((tmp_path / "mobile-role-qualification.json").read_text("utf-8"))
   assert qualification["role"] == "probe_contributor"
   assert qualification["member_key_digest"] == signer.verification_key_digest


@pytest.mark.parametrize(
   ("fault", "case"),
   [
      ("renews_after_revocation", "revocation"),
      ("incarnation_survives_process_death", "process_death"),
      ("lease_never_lapses", "network_loss"),
   ],
)
def test_misbehaving_devices_violate_their_case(tmp_path, signer, fault, case):
   swarm = SimulatedSwarm(signer.verification_key_digest, faults={fault})
   summary = _run(tmp_path, swarm, _envelopes(signer, count=6, hot_at=3))
   assert summary["negatives"][case] == "violated"
   assert summary["decision"] == "withheld"
   assert f"physical_negative_not_passed:{case}" in summary["blockers"]


def test_a_device_that_keeps_executing_while_hot_violates_the_thermal_window(tmp_path, signer):
   swarm = SimulatedSwarm(signer.verification_key_digest)
   summary = _run(tmp_path, swarm, _envelopes(signer, count=6, hot_at=3, drain=False))
   assert summary["negatives"]["thermal_window"] == "violated"


def test_observations_signed_by_another_key_are_refused(tmp_path, signer):
   foreign = generate_ed25519_signer(endpoint_id="other-endpoint")
   swarm = SimulatedSwarm(signer.verification_key_digest)
   with pytest.raises(a12.A12ContractError) as refused:
      _run(tmp_path, swarm, _envelopes(foreign))
   assert refused.value.code == "mobile_observation_signer_not_member"


def test_a_mismatched_operator_key_is_refused(tmp_path, signer):
   swarm = SimulatedSwarm(signer.verification_key_digest)
   gate = gate_module.Gate(authority=swarm, operator=swarm.operator, role="probe_contributor", route_peer_command=None, wait=immediate_wait)
   with pytest.raises(gate_module.GateError) as refused:
      gate_module.run_gate(
         gate,
         evidence_loader=_loader(_envelopes(signer)),
         member_key_digest="sha256:" + "0" * 64,
         runtime_build_digest=BUILD,
         family_ref=FAMILY,
         out=tmp_path,
      )
   assert str(refused.value) == "member_key_digest_mismatch"


def test_the_physical_gate_seam_refuses_without_execution_and_apple_is_deferred():
   with pytest.raises(a12.A12NotImplementedError):
      a12.run_mobile_physical_gate(platform_family="android", roles=("probe_contributor",))
   with pytest.raises(a12.A12ContractError) as deferred:
      a12.run_mobile_physical_gate(platform_family="ipados", roles=("probe_contributor",), execution={})
   assert deferred.value.code == a12.APPLE_DEFERRED_CODE


def _bundle(envelopes, build=BUILD):
   return {
      "protocol": "mycelium.mobile_evidence_export.v1",
      "runtime_build_digest": build,
      "observations": [
         {"name": f"runtime-observation-{i:06d}.json", "envelope": e} for i, e in enumerate(envelopes, start=1)
      ],
   }


def _load(evidence_dir, signer):
   return gate_module.load_observations(
      evidence_dir,
      member_ref="node-phone",
      build=BUILD,
      member_key_digest=signer.verification_key_digest,
      now_unix_ms=NOW,
   )


def test_an_exported_app_bundle_is_read_and_bound_to_the_build(tmp_path, signer):
   envelopes = _envelopes(signer, count=3)
   (tmp_path / "mycelium-evidence-1790000000000.json").write_text(json.dumps(_bundle(envelopes)))
   loaded, records = _load(tmp_path, signer)
   assert loaded == envelopes
   assert len(records) == 3


@pytest.mark.parametrize(
   "mutate, code",
   [
      (lambda b: b.update(runtime_build_digest="sha256:" + "c" * 64), "mobile_evidence_export_build_mismatch"),
      (lambda b: b.update(protocol="mycelium.other.v1"), "mobile_evidence_export_invalid"),
      (lambda b: b.update(extra=True), "mobile_evidence_export_invalid"),
      (lambda b: b["observations"].clear(), "mobile_evidence_export_invalid"),
      (lambda b: b["observations"][0].update(name="member.key"), "mobile_evidence_export_invalid"),
      (lambda b: b["observations"].append(dict(b["observations"][0])), "mobile_evidence_export_invalid"),
   ],
)
def test_a_foreign_or_malformed_export_bundle_is_refused(tmp_path, signer, mutate, code):
   bundle = _bundle(_envelopes(signer, count=2))
   mutate(bundle)
   (tmp_path / "mycelium-evidence-1.json").write_text(json.dumps(bundle))
   with pytest.raises(gate_module.GateError) as refused:
      _load(tmp_path, signer)
   assert str(refused.value) == code


def test_a_bundle_mixed_with_loose_observations_or_a_second_bundle_is_ambiguous(tmp_path, signer):
   envelopes = _envelopes(signer, count=1)
   (tmp_path / "mycelium-evidence-1.json").write_text(json.dumps(_bundle(envelopes)))
   (tmp_path / "runtime-observation-000001.json").write_text(json.dumps(envelopes[0]))
   with pytest.raises(gate_module.GateError) as refused:
      _load(tmp_path, signer)
   assert str(refused.value) == "mobile_evidence_export_ambiguous"
   (tmp_path / "runtime-observation-000001.json").unlink()
   (tmp_path / "mycelium-evidence-2.json").write_text(json.dumps(_bundle(envelopes)))
   with pytest.raises(gate_module.GateError):
      _load(tmp_path, signer)


def test_the_build_digest_is_recomputable_from_the_source_commit_and_core_version():
   commit = "a" * 40
   digest = gate_module.mobile_build_digest(commit, "0.1.0")
   assert digest.startswith("sha256:") and len(digest) == 71
   assert digest == gate_module.mobile_build_digest(commit, "0.1.0")
   assert digest != gate_module.mobile_build_digest("b" * 40, "0.1.0")
   assert digest != gate_module.mobile_build_digest(commit, "0.1.1")
   for bad in ("abc", "A" * 40, "g" * 40):
      with pytest.raises(gate_module.GateError) as refused:
         gate_module.mobile_build_digest(bad, "0.1.0")
      assert str(refused.value) == "mobile_build_commit_invalid"


def test_the_driver_refuses_a_build_digest_that_does_not_match_the_named_commit(tmp_path, capsys):
   code = gate_module.main(
      [
         "--seed-state-root", str(tmp_path / "seed"),
         "--runtime-build-digest", BUILD,
         "--build-commit", "a" * 40,
         "--family-ref", FAMILY,
         "--evidence-dir", str(tmp_path / "evidence"),
         "--out", str(tmp_path / "out"),
      ]
   )
   assert code == 2
   assert "mobile_build_digest_mismatch" in capsys.readouterr().err


# -- M5: browser request, Qwen2 stage phone, quota-ceiling thermal window ------
#
# Owner decisions 2026-10-12: (1a) the thermal window is judged against the
# phone's invite-quota ceiling, needs at least one hop refused with
# ``thermal_ceiling_reached`` and no frame executed after the ceiling; the 42 C
# physical stop rule stands.  (2a) the driver runs a browser request through a
# live route with the phone as a middle stage and accepts the Qwen2 stage-phone
# peer class.


class Commands:
   """Operator commands by name; each returns its scripted exit codes in turn."""

   def __init__(self, swarm, **codes):
      self.swarm = swarm
      self.codes = {name: list(values) for name, values in codes.items()}
      self.calls: list[str] = []

   def __call__(self, command: str) -> int:
      self.calls.append(command)
      # Interleave with the operator prompts so ordering can be asserted.
      self.swarm.prompts.append(f"$ {command}")
      values = self.codes[command]
      return values.pop(0) if len(values) > 1 else values[0]


class FakeClock:
   def __init__(self) -> None:
      self.now = 0.0

   def __call__(self) -> float:
      return self.now

   def sleep(self, seconds: float) -> None:
      self.now += seconds


def _stage_gate(swarm, commands, *, ceiling=QUOTA_CEILING, browser="browser", probe="probe", close="close", window=600.0):
   clock = FakeClock()
   return gate_module.Gate(
      authority=swarm,
      operator=swarm.operator,
      role="qualified_model_stage",
      route_peer_command="route-peer",
      browser_request_command=browser,
      thermal_probe_command=probe,
      route_close_command=close,
      thermal_ceiling_celsius=ceiling,
      heat_window_seconds=window,
      thermal_probe_interval_seconds=60.0,
      wait=immediate_wait,
      run_command=commands,
      clock=clock,
      sleep=clock.sleep,
   )


def _run_stage(tmp_path, gate, envelopes):
   return gate_module.run_gate(
      gate,
      evidence_loader=_loader(envelopes),
      member_key_digest=None,
      runtime_build_digest=BUILD,
      family_ref=FAMILY,
      out=tmp_path,
   )


def _negative(tmp_path, case):
   return json.loads((tmp_path / "mobile-physical-gate.json").read_text("utf-8"))["negatives"][case]


def _positive(tmp_path, case):
   return json.loads((tmp_path / "mobile-physical-gate.json").read_text("utf-8"))["positives"][case]


def test_a_qwen2_stage_phone_on_a_live_route_that_refuses_hops_at_its_quota_ceiling_qualifies(tmp_path, signer):
   swarm = SimulatedSwarm(signer.verification_key_digest, peer_class="android_native_qwen2_iroh")
   commands = Commands(swarm, **{"route-peer": [0], "browser": [0], "probe": [3, 3, 0], "close": [0]})
   gate = _stage_gate(swarm, commands)
   # 40.0 is AT the 40 C quota ceiling: the old fixed 45 C constant never saw it.
   summary = _run_stage(tmp_path, gate, _envelopes(signer, count=6, hot_at=3, hot_reading=40.0, refused_at=4))

   assert summary["positives"]["off_tailnet_join"] == "passed"
   assert summary["positives"]["browser_request_completed"] == "passed"
   assert summary["negatives"]["thermal_window"] == "failed_closed"
   assert summary["decision"] == "eligible", summary["blockers"]
   thermal = _negative(tmp_path, "thermal_window")
   assert thermal["ceiling_celsius"] == 40.0
   assert thermal["hops_refused"] == 1
   assert thermal["hops_offered"] == 3
   # Hop offers stop at the first offer that did not run: nothing more is sent to a hot phone.
   assert commands.calls == ["route-peer", "browser", "probe", "probe", "probe", "close"]


def test_the_route_closes_after_the_heat_window_and_before_the_airplane_mode_step(tmp_path, signer):
   swarm = SimulatedSwarm(signer.verification_key_digest, peer_class="android_native_qwen2_iroh")
   commands = Commands(swarm, **{"route-peer": [0], "browser": [0], "probe": [0], "close": [0]})
   _run_stage(tmp_path, _stage_gate(swarm, commands), _envelopes(signer, count=6, hot_at=3, refused_at=4))
   def first(prefix):
      return next(index for index, prompt in enumerate(swarm.prompts) if prompt.startswith(prefix))

   assert (
      first("$ route-peer")
      < first("$ browser")
      < first("Heat window")
      < first("$ probe")
      < first("$ close")
      < first("Send the node app to the background")
      < first("Enable airplane mode")
   )


def test_the_heat_prompt_names_the_quota_ceiling_and_the_42_c_stop_rule(tmp_path, signer):
   swarm = SimulatedSwarm(signer.verification_key_digest)
   commands = Commands(swarm, **{"route-peer": [0], "browser": [0], "probe": [0], "close": [0]})
   _run_stage(tmp_path, _stage_gate(swarm, commands, ceiling=39.5), _envelopes(signer, count=6, hot_at=3))
   heat = next(p for p in swarm.prompts if p.startswith("Heat window"))
   assert "39.5" in heat and "42" in heat


@pytest.mark.parametrize(("code", "outcome"), [(0, "passed"), (1, "violated"), (7, "violated")])
def test_the_browser_request_command_decides_the_browser_case(tmp_path, signer, code, outcome):
   swarm = SimulatedSwarm(signer.verification_key_digest)
   commands = Commands(swarm, **{"route-peer": [0], "browser": [code], "probe": [0], "close": [0]})
   summary = _run_stage(tmp_path, _stage_gate(swarm, commands), _envelopes(signer, count=6, hot_at=3))
   assert summary["positives"]["browser_request_completed"] == outcome
   assert _positive(tmp_path, "browser_request_completed")["exit_code"] == code


def test_without_a_browser_request_command_the_browser_case_is_not_executed(tmp_path, signer):
   swarm = SimulatedSwarm(signer.verification_key_digest)
   commands = Commands(swarm, **{"route-peer": [0], "probe": [0], "close": [0]})
   summary = _run_stage(tmp_path, _stage_gate(swarm, commands, browser=None), _envelopes(signer, count=6, hot_at=3))
   assert summary["positives"]["browser_request_completed"] == "not_executed"
   assert _positive(tmp_path, "browser_request_completed")["reason"] == "browser_request_command_absent"
   assert "physical_positive_not_passed:browser_request_completed" in summary["blockers"]


def test_a_qwen2_class_member_is_not_taken_for_a_probe_contributor(tmp_path, signer):
   swarm = SimulatedSwarm(signer.verification_key_digest, peer_class="android_native_qwen2_iroh")
   with pytest.raises(gate_module.GateError) as refused:
      _run(tmp_path, swarm, _envelopes(signer))
   assert str(refused.value) == "mobile_join_not_observed"


def test_a_hot_stage_phone_that_never_refused_an_offered_hop_is_not_passed(tmp_path, signer):
   swarm = SimulatedSwarm(signer.verification_key_digest)
   # Every offered hop executed (exit 3): the phone never refused at its ceiling.
   commands = Commands(swarm, **{"route-peer": [0], "browser": [0], "probe": [3], "close": [0]})
   summary = _run_stage(tmp_path, _stage_gate(swarm, commands, window=300.0), _envelopes(signer, count=6, hot_at=3))
   assert summary["negatives"]["thermal_window"] == "not_executed"
   assert _negative(tmp_path, "thermal_window")["reason"] == "thermal_refusal_not_observed"
   assert summary["decision"] == "withheld"
   # The offers stop when the heat window ends (300 s / 60 s => 6 offers).
   assert commands.calls.count("probe") == 6


def test_a_failed_hop_offer_is_not_a_refusal_without_the_phones_signed_word(tmp_path, signer):
   swarm = SimulatedSwarm(signer.verification_key_digest)
   # The offered request did not run (exit 0), but the phone's sealed observations
   # never counted a hop refused with thermal_ceiling_reached: not a pass.
   commands = Commands(swarm, **{"route-peer": [0], "browser": [0], "probe": [0], "close": [0]})
   summary = _run_stage(tmp_path, _stage_gate(swarm, commands), _envelopes(signer, count=6, hot_at=3))
   assert summary["negatives"]["thermal_window"] == "not_executed"
   assert _negative(tmp_path, "thermal_window")["reason"] == "thermal_refusal_not_observed"


def test_a_phone_that_executes_frames_after_its_signed_refusal_violates_the_thermal_window(tmp_path, signer):
   swarm = SimulatedSwarm(signer.verification_key_digest)
   commands = Commands(swarm, **{"route-peer": [0], "browser": [0], "probe": [0], "close": [0]})
   # The phone signed a refusal at its ceiling in window 2, then executed frames in window 3.
   summary = _run_stage(tmp_path, _stage_gate(swarm, commands), _envelopes(signer, count=6, hot_at=3, refused_at=2))
   assert summary["negatives"]["thermal_window"] == "violated"
   assert _negative(tmp_path, "thermal_window")["reason"] == "frames_executed_after_ceiling"


def test_a_stage_phone_without_a_thermal_probe_command_cannot_pass_the_thermal_window(tmp_path, signer):
   swarm = SimulatedSwarm(signer.verification_key_digest)
   commands = Commands(swarm, **{"route-peer": [0], "browser": [0], "close": [0]})
   summary = _run_stage(tmp_path, _stage_gate(swarm, commands, probe=None), _envelopes(signer, count=6, hot_at=3))
   assert summary["negatives"]["thermal_window"] == "not_executed"
   assert _negative(tmp_path, "thermal_window")["reason"] == "thermal_refusal_not_observed"


def test_a_refused_hop_does_not_excuse_frames_executed_after_the_ceiling(tmp_path, signer):
   swarm = SimulatedSwarm(signer.verification_key_digest)
   commands = Commands(swarm, **{"route-peer": [0], "browser": [0], "probe": [0], "close": [0]})
   summary = _run_stage(tmp_path, _stage_gate(swarm, commands), _envelopes(signer, count=6, hot_at=3, drain=False, refused_at=4))
   assert summary["negatives"]["thermal_window"] == "violated"


def test_a_reading_below_the_quota_ceiling_is_not_thermal_pressure(tmp_path, signer):
   swarm = SimulatedSwarm(signer.verification_key_digest)
   commands = Commands(swarm, **{"route-peer": [0], "browser": [0], "probe": [3], "close": [0]})
   summary = _run_stage(tmp_path, _stage_gate(swarm, commands), _envelopes(signer, count=6, hot_at=3, hot_reading=39.9))
   assert summary["negatives"]["thermal_window"] == "not_executed"
   assert _negative(tmp_path, "thermal_window")["reason"] == "thermal_pressure_not_reached"


def test_without_the_invite_quota_the_thermal_window_cannot_be_judged(tmp_path, signer):
   swarm = SimulatedSwarm(signer.verification_key_digest)
   summary = _run(tmp_path, swarm, _envelopes(signer, count=6, hot_at=3), thermal_ceiling_celsius=None)
   assert summary["negatives"]["thermal_window"] == "not_executed"
   assert _negative(tmp_path, "thermal_window")["reason"] == "invite_quota_thermal_ceiling_absent"


@pytest.mark.parametrize("ceiling", [42.5, 45.0, float("nan")])
def test_a_quota_ceiling_above_the_42_c_physical_stop_is_refused_before_the_gate(signer, ceiling):
   swarm = SimulatedSwarm(signer.verification_key_digest)
   with pytest.raises(gate_module.GateError) as refused:
      _stage_gate(swarm, Commands(swarm, x=[0]), ceiling=ceiling)
   assert str(refused.value) == "invite_quota_thermal_ceiling_above_physical_stop"
   assert swarm.prompts == []


def _quota(tmp_path, **overrides):
   quota = {
      "background_allowed": True,
      "battery_floor_percent": 20,
      "network_bytes_per_day": 1073741824,
      "storage_bytes": 1073741824,
      "thermal_ceiling_c": 40,
      **overrides,
   }
   path = tmp_path / "quota.json"
   path.write_text(json.dumps(quota))
   return path


def test_the_ceiling_is_read_from_the_invite_quota_file_not_hard_coded(tmp_path):
   assert gate_module.invite_quota_thermal_ceiling(_quota(tmp_path, thermal_ceiling_c=39)) == 39.0
   assert gate_module.invite_quota_thermal_ceiling(_quota(tmp_path, thermal_ceiling_c=40.5)) == 40.5
   with pytest.raises(gate_module.GateError) as refused:
      gate_module.invite_quota_thermal_ceiling(_quota(tmp_path, extra=1))
   assert str(refused.value) == "invite_quota_invalid"


def test_the_driver_refuses_a_quota_above_the_physical_stop_rule(tmp_path, capsys):
   code = gate_module.main(
      [
         "--seed-state-root", str(tmp_path / "seed"),
         "--role", "qualified_model_stage",
         "--runtime-build-digest", BUILD,
         "--family-ref", FAMILY,
         "--evidence-dir", str(tmp_path / "evidence"),
         "--out", str(tmp_path / "out"),
         "--invite-quota-file", str(_quota(tmp_path, thermal_ceiling_c=45)),
      ]
   )
   assert code == 2
   assert "invite_quota_thermal_ceiling_above_physical_stop" in capsys.readouterr().err


def test_the_driver_exposes_the_m5_commands():
   arguments = gate_module.parser().parse_args(
      [
         "--seed-state-root", "s", "--runtime-build-digest", BUILD, "--family-ref", FAMILY,
         "--evidence-dir", "e", "--out", "o",
         "--browser-request-command", "b", "--thermal-probe-command", "p", "--route-close-command", "c",
         "--invite-quota-file", "q.json", "--heat-window-seconds", "900", "--thermal-probe-interval-seconds", "90",
         "--command-timeout-seconds", "1200",
      ]
   )
   assert arguments.command_timeout_seconds == 1200.0
   assert (arguments.browser_request_command, arguments.thermal_probe_command, arguments.route_close_command) == ("b", "p", "c")
   assert arguments.heat_window_seconds == 900.0 and arguments.thermal_probe_interval_seconds == 90.0


def test_the_operator_brings_the_route_up_before_the_browser_request(tmp_path, signer):
   swarm = SimulatedSwarm(signer.verification_key_digest)
   commands = Commands(swarm, **{"route-peer": [0], "browser": [0], "probe": [0], "close": [0]})
   _run_stage(tmp_path, _stage_gate(swarm, commands), _envelopes(signer, count=6, hot_at=3, refused_at=4))
   bring_up = next(i for i, p in enumerate(swarm.prompts) if p.startswith("Route bring-up"))
   assert swarm.prompts.index("$ route-peer") < bring_up < swarm.prompts.index("$ browser")


def test_a_hung_operator_command_is_a_failed_command_not_a_crashed_gate(signer):
   swarm = SimulatedSwarm(signer.verification_key_digest)
   gate = gate_module.Gate(
      authority=swarm, operator=swarm.operator, role="qualified_model_stage", route_peer_command=None,
      wait=immediate_wait, command_timeout_seconds=0.2,
   )
   started = time.monotonic()
   assert gate.run_command("sleep 5") == gate_module.COMMAND_TIMED_OUT
   assert time.monotonic() - started < 3


def test_the_route_closes_when_the_gate_aborts_before_the_heat_window(tmp_path, signer):
   # Seed authority (a): the route close also tears down node-0's and node-2's
   # temporary gate memberships, so it must run on an aborted gate too.
   swarm = SimulatedSwarm(signer.verification_key_digest, peer_class="android_native_qwen2_iroh")
   commands = Commands(swarm, **{"route-peer": [0], "browser": [0], "probe": [0], "close": [0]})
   gate = _stage_gate(swarm, commands)

   def abort_at_bring_up(message: str) -> None:
      swarm.operator(message)
      if message.startswith("Route bring-up"):
         raise KeyboardInterrupt

   gate.operator = abort_at_bring_up
   with pytest.raises(KeyboardInterrupt):
      _run_stage(tmp_path, gate, _envelopes(signer, count=6, hot_at=3, refused_at=4))
   assert commands.calls == ["route-peer", "close"]
   assert gate.route_closed is True
