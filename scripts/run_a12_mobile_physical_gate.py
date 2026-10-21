#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Run the A12 native Android physical gate (Mac seed + one Android device).

Android only: iOS/iPadOS is deferred by the 2026-09-29 owner ruling R2.  The
claim is family-bound to the one named initial-build family (2026-09-27 ruling).

The device joins through the ordinary signed native app with a fresh invite; no
ADB, SSH or Termux is used while the gate runs (the run records that it used
none).  Every physical transition is performed by the operator on the device when
prompted, and every outcome is judged by the driver from the seed's durable
membership authority (``mycelium_seed.seed_inventory``) and from the device's own
member-signed runtime observations, never from an operator's say-so.

Cases
-----
Positives: ``off_tailnet_join``, ``reconnect``, ``native_evidence_published``
and, for a model-stage claim, ``router_frames_executed``, ``native_parity`` and
``browser_request_completed``.  The frame and parity cases run the operator's
``--route-peer-command``.  The browser case runs ``--browser-request-command``:
one real browser request through a live product route on which the phone is a
middle stage; exit 0 passes, any other exit is a violation, and the case is
``not_executed`` only when no command is given.  A model-stage phone may be the
reference class (``android_native_iroh``) or the Qwen2 stage class
(``android_native_qwen2_iroh``); a probe contributor is the reference class only.

Negatives (specification section 11, Android): ``background`` (the member drains
or stays a member, never corrupts, and returns), ``network_loss`` (the lease goes
stale, then the same member reconnects), ``revocation`` (the seed fences the
generation and the member cannot renew), ``process_death`` (the old incarnation is
fenced and a restart resumes with a new one) and ``thermal_window``.

Thermal window [OWNER DECISION 2026-10-12, replacing the fixed 45 C constant of
2026-09-30]: the ceiling is the ``thermal_ceiling_c`` of the owner's invite quota
(``--invite-quota-file``, the same file the gate seed's pairing window carried),
never a constant.  A quota ceiling above the standing 42 C physical stop rule is
refused before the gate starts.  The sustained observations must cover the frozen
window and show no frame executed after the phone first reached the ceiling (its
first sealed sample at or above the ceiling, or its first signed refusal).  For a
model stage the phone's own sealed observations must also count at least one hop
refused with ``thermal_ceiling_reached`` (``thermal_refusals_window``); a phone
that merely received no work while hot is not a pass, and neither is a failed
hop offer.  To give the phone work to refuse, the driver offers hops during an
operator heat window (``--thermal-probe-command``) and stops offering at the
first offer that did not run on the phone.  The route is closed
(``--route-close-command``) after the heat window and before the airplane-mode
step, because the fleet's network runs through the phone's hotspot.

After the gate, the operator exports the app's sealed observations with the app's
own "Export evidence" share (one ``mycelium-evidence-*.json`` bundle, protocol
``mycelium.mobile_evidence_export.v1``) into ``--evidence-dir``; loose
``runtime-observation-*.json`` copies are still accepted, never mixed with a bundle.
``--build-commit`` recomputes the release build digest from its source commit;
the driver verifies each envelope against the enrolled member key, binds their
digests into the owner-private gate bundle and decides the role with
``qualify_mobile_operation``.
"""

# ruff: noqa: E402 -- direct script execution needs the repository on sys.path.

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
   sys.path.insert(0, str(REPO_ROOT))

from mycelium_mobile import a12_activation as a12

GATE_PROTOCOL = "mycelium.a12_mobile_physical_gate_run.v1"
ANDROID_CLASS = "android_native_iroh"
ANDROID_QWEN2_CLASS = "android_native_qwen2_iroh"
#: Peer classes the gate accepts as the joining phone, per claimed role.
JOIN_CLASSES: dict[str, frozenset[str]] = {
   "probe_contributor": frozenset({ANDROID_CLASS}),
   "qualified_model_stage": frozenset({ANDROID_CLASS, ANDROID_QWEN2_CLASS}),
}
#: The owner's standing physical stop rule: phone work stops at 42 C.  A quota
#: ceiling above it would require breaking the rule, so the gate refuses it.
PHYSICAL_STOP_CELSIUS = 42.0
#: ``--thermal-probe-command`` exit codes: the offered hop did not run on the
#: phone (expected: refused at its ceiling), so offers stop / the hop executed
#: (phone not hot yet).  Any other exit is an inconclusive offer and is retried.
#: Offers only provoke work; the refusal itself is judged from the phone's own
#: sealed ``thermal_refusals_window`` observations.
THERMAL_PROBE_NOT_RUN = 0
THERMAL_PROBE_EXECUTED = 3
#: An operator command that outlives its bound counts as failed (exit 124).
COMMAND_TIMED_OUT = 124
DEFAULT_COMMAND_TIMEOUT_SECONDS = 900.0
DEFAULT_HEAT_WINDOW_SECONDS = 1500.0
DEFAULT_THERMAL_PROBE_INTERVAL_SECONDS = 120.0
POLL_SECONDS = 2.0
EXPORT_PROTOCOL = "mycelium.mobile_evidence_export.v1"
BUILD_PROTOCOL = "mycelium.mobile_runtime_build.v1"
OBSERVATION_NAME = re.compile(r"runtime-observation-[0-9]{6}\.json")


class GateError(RuntimeError):
   """A coded gate failure."""


def invite_quota_thermal_ceiling(path: Path) -> float:
   """The ``thermal_ceiling_c`` of the owner's invite quota file (validated as the seed does)."""

   from mycelium_mobile.android_onboarding import AndroidOnboardingError, validate_android_quota

   try:
      quota = validate_android_quota(json.loads(Path(path).read_text("utf-8")))
   except (OSError, ValueError, AndroidOnboardingError) as error:
      raise GateError("invite_quota_invalid") from error
   return float(quota["thermal_ceiling_c"])


def write_private_json(path: Path, document: object) -> None:
   path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
   os.chmod(path.parent, 0o700)
   temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
   descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
   with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
      json.dump(document, handle, sort_keys=True, indent=1, allow_nan=False)
      handle.write("\n")
   os.replace(temporary, path)


class SeedAuthority:
   """Reads the seed's durable membership store and fences generations."""

   def __init__(self, state_root: Path) -> None:
      self.state_root = Path(state_root)

   def members(self) -> list[dict[str, Any]]:
      from mycelium_seed import seed_inventory

      return list(seed_inventory(self.state_root)["members"])

   def member_key_digest(self, node_id: str) -> str:
      """The member key the seed recorded at the signed join (never the device's claim)."""

      from mycelium_seed.operator import _load_authority

      lease, coordinator, _binding = _load_authority(self.state_root, incarnation="a12-gate-member-key")
      try:
         member = next((item for item in coordinator.members() if item["node_id"] == node_id), None)
      finally:
         lease.close()
      if member is None:
         raise GateError("member_absent")
      return str(member["verification_key_digest"])

   def revoke(self, node_id: str, generation: int) -> dict[str, Any]:
      from mycelium_seed import revoke_seed_member

      return revoke_seed_member(
         self.state_root,
         node_id=node_id,
         expected_generation=generation,
         reason="a12_physical_gate_revocation",
      )


def _member(authority: Any, node_id: str) -> dict[str, Any] | None:
   return next((item for item in authority.members() if item["node_id"] == node_id), None)


def wait_until(
   predicate: Callable[[], Any], *, timeout: float, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep
) -> Any:
   deadline = clock() + timeout
   while True:
      value = predicate()
      if value:
         return value
      if clock() >= deadline:
         return None
      sleep(POLL_SECONDS)


class Gate:
   """One physical gate run over an injectable seed authority and operator."""

   def __init__(
      self,
      *,
      authority: Any,
      operator: Callable[[str], None],
      role: str,
      route_peer_command: str | None,
      browser_request_command: str | None = None,
      thermal_probe_command: str | None = None,
      route_close_command: str | None = None,
      thermal_ceiling_celsius: float | None = None,
      heat_window_seconds: float = DEFAULT_HEAT_WINDOW_SECONDS,
      thermal_probe_interval_seconds: float = DEFAULT_THERMAL_PROBE_INTERVAL_SECONDS,
      timeout: float = 600.0,
      wait: Callable[..., Any] = wait_until,
      run_command: Callable[[str], int] | None = None,
      command_timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
      clock: Callable[[], float] = time.monotonic,
      sleep: Callable[[float], None] = time.sleep,
   ) -> None:
      if role not in ("probe_contributor", "qualified_model_stage"):
         raise GateError("mobile_gate_role_unsupported")
      if thermal_ceiling_celsius is not None and not (
         math.isfinite(thermal_ceiling_celsius) and thermal_ceiling_celsius <= PHYSICAL_STOP_CELSIUS
      ):
         raise GateError("invite_quota_thermal_ceiling_above_physical_stop")
      self.authority = authority
      self.operator = operator
      self.role = role
      self.route_peer_command = route_peer_command
      self.browser_request_command = browser_request_command
      self.thermal_probe_command = thermal_probe_command
      self.route_close_command = route_close_command
      self.thermal_ceiling_celsius = None if thermal_ceiling_celsius is None else float(thermal_ceiling_celsius)
      self.heat_window_seconds = heat_window_seconds
      self.thermal_probe_interval_seconds = thermal_probe_interval_seconds
      self.timeout = timeout
      self.wait = wait
      self.command_timeout_seconds = command_timeout_seconds
      self.run_command = run_command or self._run_command
      self.clock = clock
      self.sleep = sleep
      self.node_id: str | None = None
      self.positives: dict[str, dict[str, Any]] = {}
      self.negatives: dict[str, dict[str, Any]] = {}
      #: Hops offered to the phone during the heat window, in order.
      self.thermal_probes: list[dict[str, Any]] = []
      self.route_closed: bool | None = None

   # -- helpers --------------------------------------------------------------
   def _run_command(self, command: str) -> int:
      try:
         return subprocess.run(shlex.split(command), check=False, timeout=self.command_timeout_seconds).returncode
      except subprocess.TimeoutExpired:
         return COMMAND_TIMED_OUT

   def _current(self) -> dict[str, Any] | None:
      return _member(self.authority, self.node_id) if self.node_id else None

   def _fresh(self) -> dict[str, Any] | None:
      member = self._current()
      return member if member and member["lease_freshness"] == "fresh" and member["revocation_state"] == "active" else None

   def _record(self, table: dict[str, dict[str, Any]], case: str, outcome: str, **facts: Any) -> None:
      table[case] = {"outcome": outcome, **facts}

   # -- positives ------------------------------------------------------------
   def join(self) -> None:
      before = {item["node_id"] for item in self.authority.members()}
      classes = JOIN_CLASSES[self.role]
      self.operator(
         "Create a fresh invite on the Mac and scan it with the node app on the phone "
         "(phone off the tailnet, on its own network). Then tap Start serving."
      )
      joined = self.wait(
         lambda: next(
            (
               item
               for item in self.authority.members()
               if item["node_id"] not in before
               and item["peer_class"] in classes
               and item["lease_freshness"] == "fresh"
            ),
            None,
         ),
         timeout=self.timeout,
      )
      if joined is None:
         self._record(self.positives, "off_tailnet_join", "violated", reason="no_new_android_member")
         raise GateError("mobile_join_not_observed")
      self.node_id = joined["node_id"]
      self._record(self.positives, "off_tailnet_join", "passed", generation=joined["generation"])

   def reconnect(self) -> None:
      before = self._current()
      self.operator("Toggle the phone's network off for ~20 seconds, then back on.")
      renewed = self.wait(
         lambda: (m := self._fresh()) and before and m["generation"] >= before["generation"] and m,
         timeout=self.timeout,
      )
      self._record(self.positives, "reconnect", "passed" if renewed else "violated")

   def route_frames(self) -> None:
      if self.role != "qualified_model_stage":
         return
      if not self.route_peer_command:
         for case in ("router_frames_executed", "native_parity"):
            self._record(self.positives, case, "not_executed", reason="route_peer_command_absent")
      else:
         code = self.run_command(self.route_peer_command)
         outcome = "passed" if code == 0 else "violated"
         self._record(self.positives, "router_frames_executed", outcome)
         self._record(self.positives, "native_parity", outcome)

   def browser_request(self) -> None:
      """One real browser request through a live route with the phone as a middle stage.

      The command owns the route evidence: it exits 0 only when the request
      completed and the route shows the phone placement executed.
      """
      if self.role != "qualified_model_stage":
         return
      if not self.browser_request_command:
         self._record(self.positives, "browser_request_completed", "not_executed", reason="browser_request_command_absent")
         return
      self.operator(
         "Route bring-up: enrol node-0 and node-2 as temporary members of this gate seed, issue the phone's Qwen2 "
         "stage assignment and bring the route up (Mac entry -> phone middle stage -> laptop final) with the route "
         "harness, up to and including `serve`. Keep the phone cool and the app serving. Press Enter once the "
         "harness reports the route alive."
      )
      code = self.run_command(self.browser_request_command)
      self._record(self.positives, "browser_request_completed", "passed" if code == 0 else "violated", exit_code=code)

   def heat_window(self) -> None:
      """Offer hops while the operator warms the phone; stop at the first refusal.

      Runs after the browser request (a frame executed after the phone's first
      sample at the ceiling would violate the thermal window) and only for a
      model stage, the only role that is offered work.
      """
      if self.role != "qualified_model_stage" or not self.thermal_probe_command or self.thermal_ceiling_celsius is None:
         return
      ceiling = f"{self.thermal_ceiling_celsius:g}"
      self.operator(
         f"Heat window: keep the node app serving, screen at full brightness, hotspot on, charging from a warm "
         f"charger. The driver offers a hop every {self.thermal_probe_interval_seconds:g} s for up to "
         f"{self.heat_window_seconds / 60:g} min. STOP heating at the quota ceiling {ceiling} C and never let the "
         f"phone pass {PHYSICAL_STOP_CELSIUS:g} C. Press Enter to start the hop offers."
      )
      started = self.clock()
      while True:
         code = self.run_command(self.thermal_probe_command)
         outcome = {THERMAL_PROBE_NOT_RUN: "not_run", THERMAL_PROBE_EXECUTED: "executed"}.get(code, "inconclusive")
         self.thermal_probes.append({"at_unix_ms": int(time.time() * 1000), "exit_code": code, "outcome": outcome})
         if outcome == "not_run" or self.clock() - started + self.thermal_probe_interval_seconds > self.heat_window_seconds:
            break
         self.sleep(self.thermal_probe_interval_seconds)
      if outcome == "not_run":
         self.operator(
            f"An offered hop did not run on the phone (expected: refused at its {ceiling} C ceiling). Keep the phone "
            f"at or just above {ceiling} C for about 3 more minutes (two observations), never past "
            f"{PHYSICAL_STOP_CELSIUS:g} C, then stop heating and let it cool. No more hops are offered."
         )
      else:
         self.operator("Every offered hop ran during the heat window. Stop heating and let the phone cool.")

   def close_route(self) -> None:
      """Close the live route before the network-loss step cuts the phone's hotspot."""
      if self.role != "qualified_model_stage" or not self.route_close_command:
         return
      self.route_closed = self.run_command(self.route_close_command) == 0

   # -- negatives ------------------------------------------------------------
   def background(self) -> None:
      self.operator("Send the node app to the background for 60 seconds, then bring it back.")
      member = self.wait(self._fresh, timeout=self.timeout)
      self._record(self.negatives, "background", "failed_closed" if member else "violated")

   def network_loss(self) -> None:
      before = self._current()
      self.operator("Enable airplane mode and keep it on until prompted again (the lease must expire).")
      stale = self.wait(lambda: (m := self._current()) and m["lease_freshness"] == "expired" and m, timeout=self.timeout)
      self.operator("Disable airplane mode now.")
      back = self.wait(self._fresh, timeout=self.timeout)
      same_member = back is not None and before is not None and back["node_id"] == before["node_id"]
      self._record(
         self.negatives,
         "network_loss",
         "failed_closed" if stale and same_member else "violated",
         lease_went_stale=bool(stale),
         reconnected=bool(back),
      )

   def process_death(self) -> None:
      before = self._current()
      self.operator("Force-stop the node app from Android settings, wait 10 seconds, then reopen it.")
      restarted = self.wait(
         lambda: (m := self._fresh()) and before and m["incarnation"] != before["incarnation"] and m,
         timeout=self.timeout,
      )
      self._record(self.negatives, "process_death", "failed_closed" if restarted else "violated", new_incarnation=bool(restarted))

   def revocation(self) -> None:
      member = self._current()
      if member is None:
         self._record(self.negatives, "revocation", "violated", reason="member_absent")
         return
      fenced = self.authority.revoke(member["node_id"], member["generation"])
      self.operator("Leave the app open for 90 seconds (it must not regain membership).")
      regained = self.wait(self._fresh, timeout=min(self.timeout, 90.0))
      after = self._current()
      ok = fenced.get("lifecycle_state") == "STOPPED" and not regained and after is not None and after["revocation_state"] == "revoked"
      self._record(self.negatives, "revocation", "failed_closed" if ok else "violated")

   def thermal_window(self, observations: Sequence[Mapping[str, Any]]) -> None:
      ceiling = self.thermal_ceiling_celsius
      if ceiling is None:
         self._record(self.negatives, "thermal_window", "not_executed", reason="invite_quota_thermal_ceiling_absent")
         return
      documents = [item["observation"] for item in observations]
      covered = sum(float(doc["measurement_duration_ms"]) for doc in documents)
      thermal = [doc["metrics"].get("thermal_max_celsius", "unknown") for doc in documents]
      if covered < a12.SUSTAINED_OBSERVATION_WINDOW_MS or any(value == "unknown" for value in thermal):
         self._record(self.negatives, "thermal_window", "violated", reason="sustained_thermal_window_not_observed")
         return
      facts = {"window_ms": covered, "ceiling_celsius": ceiling}
      # The phone's own signed count of hops it refused with thermal_ceiling_reached
      # (absent from builds that predate the metric: counts as none).
      refusals = [doc["metrics"].get("thermal_refusals_window", 0) for doc in documents]
      refusals = [value if isinstance(value, (int, float)) and value >= 0 else 0 for value in refusals]
      reached = [index for index, value in enumerate(thermal) if float(value) >= ceiling or refusals[index] > 0]
      if not reached:
         self._record(self.negatives, "thermal_window", "not_executed", reason="thermal_pressure_not_reached", **facts)
         return
      after = documents[reached[0] + 1 :]
      drained = all(doc["metrics"].get("frames_executed_window") in (0, 0.0) for doc in after) and bool(after)
      if not drained:
         self._record(self.negatives, "thermal_window", "violated", reason="frames_executed_after_ceiling", **facts)
         return
      refused = int(sum(refusals))
      if self.role == "qualified_model_stage" and not refused:
         self._record(
            self.negatives, "thermal_window", "not_executed", reason="thermal_refusal_not_observed",
            hops_offered=len(self.thermal_probes), **facts,
         )
         return
      self._record(
         self.negatives, "thermal_window", "failed_closed", hops_offered=len(self.thermal_probes), hops_refused=refused, **facts
      )


def mobile_build_digest(source_commit: str, core_version: str) -> str:
   """The build identity a release node app stamps (``MYCELIUM_NODE_BUILD_DIGEST``).

   Recomputable by anyone holding the source commit the APK was built from.
   """
   if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
      raise GateError("mobile_build_commit_invalid")
   document = {"protocol": BUILD_PROTOCOL, "source_commit": source_commit, "mobile_core_version": core_version}
   canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
   return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def mobile_core_version() -> str:
   manifest = (REPO_ROOT / "native" / "mobile_core" / "Cargo.toml").read_text("utf-8")
   match = re.search(r'^version\s*=\s*"([^"]+)"', manifest, re.MULTILINE)
   if match is None:
      raise GateError("mobile_core_version_unknown")
   return match.group(1)


def _exported_envelopes(path: Path, build: str) -> list[dict[str, Any]]:
   """Unpack the app's shared export bundle; only signed runtime observations pass."""
   bundle = json.loads(path.read_text("utf-8"))
   observations = bundle.get("observations") if isinstance(bundle, dict) else None
   if (
      not isinstance(bundle, dict)
      or set(bundle) != {"protocol", "runtime_build_digest", "observations"}
      or bundle["protocol"] != EXPORT_PROTOCOL
      or not isinstance(observations, list)
      or not observations
   ):
      raise GateError("mobile_evidence_export_invalid")
   if bundle["runtime_build_digest"] != build:
      raise GateError("mobile_evidence_export_build_mismatch")
   names = [item.get("name") if isinstance(item, dict) else None for item in observations]
   if (
      any(not isinstance(name, str) or not OBSERVATION_NAME.fullmatch(name) for name in names)
      or len(set(names)) != len(names)
      or any(set(item) != {"name", "envelope"} for item in observations)
   ):
      raise GateError("mobile_evidence_export_invalid")
   return [item["envelope"] for item in sorted(observations, key=lambda item: item["name"])]


def load_observations(
   evidence_dir: Path, *, member_ref: str, build: str, member_key_digest: str, now_unix_ms: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
   bundles = sorted(Path(evidence_dir).glob("mycelium-evidence-*.json"))
   loose = sorted(Path(evidence_dir).glob("runtime-observation-*.json"))
   if len(bundles) > 1 or (bundles and loose):
      raise GateError("mobile_evidence_export_ambiguous")
   if bundles:
      envelopes = _exported_envelopes(bundles[0], build)
   else:
      envelopes = [json.loads(path.read_text("utf-8")) for path in loose]
   records = [
      a12.observe_mobile_runtime(
         member_ref=member_ref,
         platform_family="android",
         runtime_build_digest=build,
         observation_envelope=envelope,
         expected_member_key_digest=member_key_digest,
         now_unix_ms=now_unix_ms,
      )
      for envelope in envelopes
   ]
   return envelopes, records


def run_gate(
   gate: Gate,
   *,
   evidence_loader: Callable[[str], tuple[list[dict[str, Any]], list[dict[str, Any]]]],
   member_key_digest: str | None,
   runtime_build_digest: str,
   family_ref: str,
   out: Path,
   provenance: str = "live",
) -> dict[str, Any]:
   started = time.time()
   gate.join()
   recorded_key = gate.authority.member_key_digest(gate.node_id)
   if member_key_digest is not None and member_key_digest != recorded_key:
      raise GateError("member_key_digest_mismatch")
   member_key_digest = recorded_key
   gate.reconnect()
   try:
      gate.route_frames()
      gate.browser_request()
      gate.heat_window()
   finally:
      # The fleet's network is the phone's hotspot: close before airplane mode.  It
      # also runs when the gate aborts after bring-up: the route close tears down
      # the temporary gate memberships of the desktop placements (seed authority a).
      gate.close_route()
   gate.background()
   gate.network_loss()
   gate.process_death()
   gate.operator(
      "Export the app's sealed evidence now: tap 'Export evidence' in the node app and save the shared "
      "mycelium-evidence-*.json into the evidence directory (the gate itself is over, so a cable may be used "
      "for the copy)."
   )
   envelopes, observations = evidence_loader(member_key_digest)
   gate.positives["native_evidence_published"] = {
      "outcome": "passed" if observations else "violated",
      "observations": len(observations),
   }
   gate.thermal_window(observations)
   # Revocation is terminal for this member, so it runs last.
   gate.revocation()
   bundle = a12.run_mobile_physical_gate(
      platform_family="android",
      roles=(gate.role,),
      execution={
         "member_key_digest": member_key_digest,
         "runtime_build_digest": runtime_build_digest,
         "family_ref": family_ref,
         "execution_path": "signed_native_application",
         "positives": gate.positives,
         "negatives": gate.negatives,
         "adb_used_during_gate": False,
         "provenance": provenance,
         "observation_digests": [item["observation_digest"] for item in observations],
      },
   )
   qualification = a12.qualify_mobile_operation(
      member_ref=gate.node_id or "member:unknown",
      role=gate.role,
      platform_family="android",
      runtime_build_digest=runtime_build_digest,
      prior_rung_decisions={
         "signed_membership": "eligible",
         "capability_eligibility": "eligible",
         **({"artifact_readiness": "eligible"} if gate.role == "qualified_model_stage" else {}),
      },
      observation_envelopes=envelopes,
      physical_gate=bundle,
      member_key_digest=member_key_digest,
      family_ref=family_ref,
   ) if envelopes else {"decision": "withheld", "blockers": ["mobile_observations_absent"]}
   summary = {
      "protocol": GATE_PROTOCOL,
      "started_at_unix_ms": int(started * 1000),
      "finished_at_unix_ms": int(time.time() * 1000),
      "role": gate.role,
      "positives": {case: item["outcome"] for case, item in sorted(gate.positives.items())},
      "negatives": {case: item["outcome"] for case, item in sorted(gate.negatives.items())},
      "decision": qualification["decision"],
      "blockers": qualification["blockers"],
      "thermal_ceiling_celsius": gate.thermal_ceiling_celsius,
      "thermal_probes": gate.thermal_probes,
      "route_closed": gate.route_closed,
   }
   write_private_json(out / "mobile-physical-gate.json", bundle)
   write_private_json(out / "mobile-role-qualification.json", qualification)
   write_private_json(out / "mobile-gate-summary.json", summary)
   return summary


def _operator_prompt(message: str) -> None:
   print(f"\n[operator] {message}\nPress Enter when done.", file=sys.stderr, flush=True)
   sys.stdin.readline()


def parser() -> argparse.ArgumentParser:
   root = argparse.ArgumentParser(description=__doc__.splitlines()[0])
   root.add_argument("--seed-state-root", type=Path, required=True)
   root.add_argument("--role", choices=("probe_contributor", "qualified_model_stage"), default="probe_contributor")
   root.add_argument("--member-key-digest", help="optional cross-check of the seed-recorded member key")
   root.add_argument("--runtime-build-digest", required=True)
   root.add_argument(
      "--build-commit", help="source commit the APK was built from; the build digest must be recomputable from it"
   )
   root.add_argument("--family-ref", required=True)
   root.add_argument("--evidence-dir", type=Path, required=True)
   root.add_argument("--route-peer-command")
   root.add_argument(
      "--browser-request-command",
      help="one real browser request through a live route with the phone as a middle stage; exit 0 = completed "
      "and the phone placement executed",
   )
   root.add_argument(
      "--thermal-probe-command",
      help=f"offer one hop to the phone during the heat window; exit {THERMAL_PROBE_NOT_RUN} = the hop did not run "
      f"on the phone (offers stop), {THERMAL_PROBE_EXECUTED} = executed, anything else = inconclusive; the refusal "
      "itself is judged from the phone's signed thermal_refusals_window",
   )
   root.add_argument("--route-close-command", help="close the live route after the heat window, before airplane mode")
   root.add_argument(
      "--invite-quota-file",
      type=Path,
      help="the owner quota the gate seed's pairing window carried; its thermal_ceiling_c judges the thermal window",
   )
   root.add_argument("--command-timeout-seconds", type=float, default=DEFAULT_COMMAND_TIMEOUT_SECONDS)
   root.add_argument("--heat-window-seconds", type=float, default=DEFAULT_HEAT_WINDOW_SECONDS)
   root.add_argument("--thermal-probe-interval-seconds", type=float, default=DEFAULT_THERMAL_PROBE_INTERVAL_SECONDS)
   root.add_argument("--timeout", type=float, default=600.0)
   root.add_argument("--out", type=Path, required=True)
   return root


def main(argv: Sequence[str] | None = None) -> int:
   arguments = parser().parse_args(argv)
   if arguments.build_commit is not None:
      try:
         expected = mobile_build_digest(arguments.build_commit, mobile_core_version())
      except GateError as error:
         print(json.dumps({"error": str(error)}), file=sys.stderr)
         return 2
      if expected != arguments.runtime_build_digest:
         print(json.dumps({"error": "mobile_build_digest_mismatch", "expected": expected}), file=sys.stderr)
         return 2
   try:
      ceiling = None if arguments.invite_quota_file is None else invite_quota_thermal_ceiling(arguments.invite_quota_file)
      gate = Gate(
         authority=SeedAuthority(arguments.seed_state_root),
         operator=_operator_prompt,
         role=arguments.role,
         route_peer_command=arguments.route_peer_command,
         browser_request_command=arguments.browser_request_command,
         thermal_probe_command=arguments.thermal_probe_command,
         route_close_command=arguments.route_close_command,
         thermal_ceiling_celsius=ceiling,
         heat_window_seconds=arguments.heat_window_seconds,
         thermal_probe_interval_seconds=arguments.thermal_probe_interval_seconds,
         command_timeout_seconds=arguments.command_timeout_seconds,
         timeout=arguments.timeout,
      )
   except GateError as error:
      print(json.dumps({"error": str(error)}), file=sys.stderr)
      return 2
   try:
      summary = run_gate(
         gate,
         evidence_loader=lambda key_digest: load_observations(
            arguments.evidence_dir,
            member_ref=gate.node_id or "member:unknown",
            build=arguments.runtime_build_digest,
            member_key_digest=key_digest,
            now_unix_ms=time.time() * 1000,
         ),
         member_key_digest=arguments.member_key_digest,
         runtime_build_digest=arguments.runtime_build_digest,
         family_ref=arguments.family_ref,
         out=arguments.out,
      )
   except GateError as error:
      print(json.dumps({"error": str(error)}), file=sys.stderr)
      return 2
   print(json.dumps({"decision": summary["decision"], "blockers": summary["blockers"]}, sort_keys=True))
   return 0 if summary["decision"] == "eligible" else 1


if __name__ == "__main__":
   raise SystemExit(main())
