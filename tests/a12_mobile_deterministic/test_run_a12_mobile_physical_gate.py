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

   def __init__(self, member_key: str, faults: set[str] | None = None) -> None:
      self.member_key = member_key
      self.faults = faults or set()
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
               "peer_class": "android_native_iroh",
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


def _envelopes(signer, count=5, thermal=38.0, hot_at=None, drain=True):
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
            "thermal_max_celsius": 46.0 if hot else thermal,
            "battery_level_percent": 70.0,
            "charging": 0.0,
            "memory_available_bytes": 2.5e9,
            "lifecycle_member": 1.0,
            "relay_online": 1.0,
            "stage_loaded": 0.0,
            "frames_executed_window": 0.0 if (after_hot and drain) else 5.0,
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


def _run(tmp_path, swarm, envelopes):
   gate = gate_module.Gate(
      authority=swarm,
      operator=swarm.operator,
      role="probe_contributor",
      route_peer_command=None,
      wait=immediate_wait,
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
