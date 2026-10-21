# SPDX-License-Identifier: AGPL-3.0-only
"""The Linux managed host enrols through the real A13 owner seed (owner parity ruling 2026-10-07).

Real DemoAuthority seed over pinned local TLS (the macOS owner), a real
RemoteLinuxMember enrolment and the Linux installation descriptor checked by
the owner's own verifier.  Platform observations read fake sysfs trees and a
fake busctl; nothing here is a Linux run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mycelium_a13_linux.managed_host import resources
from mycelium_a13_linux.managed_host.app import LinuxTargetApplication, linux_installation_descriptor
from mycelium_a13_linux.managed_host.member import RemoteLinuxMember, installed_sidecar, observe_linux
from mycelium_a13_macos.installation_descriptor import verify_installation_descriptor
from mycelium_a13_macos.managed_classes import is_managed_peer_class, managed_runtime_backend
from mycelium_a13_macos.managed_staging import TargetStaging
from mycelium_a13_macos.resources import policy_violation
from mycelium_node.durable_membership import load_membership_state

from tests.a13_macos.test_peer import CONSENT, QUOTA, seed  # noqa: F401 - pytest fixture

POLICY = {"power": "ac_only", "thermal": "stop_on_serious", "network": "metered_allowed"}


def _enrolled_app(tmp_path, seed_fixture):
    authority, factory = seed_fixture
    app = LinuxTargetApplication(
        tmp_path / "linux-target",
        _test_peer_factory=lambda root: RemoteLinuxMember(root, _test_seed_client_factory=factory),
    )
    app.start()
    request = app.peer.pair_request(CONSENT, ttl_seconds=300)
    handoff = authority.approve_pairing(request, ttl_seconds=120, quota=QUOTA)
    app.accept_pairing(handoff)
    return authority, app, request


def test_linux_member_enrols_eligible_and_is_a_managed_numpy_host(tmp_path, seed):  # noqa: F811
    authority, app, request = _enrolled_app(tmp_path, seed)
    try:
        member = authority.coordinator.member(request["node_id"])
        assert member["peer_class"] == "linux_native_iroh"
        assert member["runtime_capability"]["runtime_backend"] == "numpy"
        assert member["activation_eligible"] is True
        assert is_managed_peer_class("linux_native_iroh") and managed_runtime_backend("linux_native_iroh") == "numpy"
        assert app.status()["membership_state"] == "current"
    finally:
        app.stop()


def test_installation_descriptor_discloses_posix_paths_and_verifies_on_the_owner(tmp_path, seed, monkeypatch):  # noqa: F811
    authority, app, request = _enrolled_app(tmp_path, seed)
    sidecar = tmp_path / "sidecar"
    sidecar.write_bytes(b"\x7fELF")
    monkeypatch.setattr(app, "_installed_sidecar", lambda: sidecar)
    try:
        with pytest.raises(ValueError, match="consent_required"):
            linux_installation_descriptor(app, run_id="run-one", deployment_id="dep-one",
                                          manifest_digest="sha256:" + "a" * 64, acknowledged=False)
        card = linux_installation_descriptor(app, run_id="run-one", deployment_id="dep-one",
                                             manifest_digest="sha256:" + "a" * 64, acknowledged=True)
        member = authority.coordinator.member(request["node_id"])
        swarm_id = load_membership_state(app.peer.state_root)["swarm_id"]
        facts = verify_installation_descriptor(card, member=member, swarm_id=swarm_id,
                                               run_id="run-one", deployment_id="dep-one")
        assert facts["runtime_backend"] == "numpy"
        assert facts["staging_root"] == str(Path(app.peer.state_root) / "managed/run-one/mycelium-stage")
        assert facts["sidecar_binary"] == str(sidecar)
        staging = app._new_staging("run-one", node_id=request["node_id"], max_archive_bytes=1000,
                                   max_disk_bytes=1000, current_member=lambda: True,
                                   authorize=lambda intent: True)
        assert isinstance(staging, TargetStaging)  # Linux runs the canonical POSIX staging unchanged
    finally:
        app.stop()


def _sysfs(tmp_path: Path, *, supplies: dict, zones: list[int]) -> tuple[Path, Path]:
    power = tmp_path / "power_supply"
    power.mkdir(parents=True)
    for name, (kind, online) in supplies.items():
        (power / name).mkdir()
        (power / name / "type").write_text(kind + "\n")
        if online is not None:
            (power / name / "online").write_text(f"{online}\n")
    thermal = tmp_path / "thermal"
    thermal.mkdir()
    for index, millidegrees in enumerate(zones):
        zone = thermal / f"thermal_zone{index}"
        zone.mkdir()
        (zone / "temp").write_text(f"{millidegrees}\n")
        (zone / "type").write_text("x86_pkg_temp\n")
    return power, thermal


def _busctl(connectivity: int | None, metered: int | None):
    def runner(command):
        assert command[:3] == ["busctl", "--system", "get-property"]
        value = {"Connectivity": connectivity, "Metered": metered}[command[-1]]
        return "" if value is None else f"u {value}\n"
    return runner


def test_resource_observation_maps_to_the_shared_policy(tmp_path):
    power, thermal = _sysfs(tmp_path / "a", supplies={"AC": ("Mains", 1), "BAT0": ("Battery", None)},
                            zones=[45000, 52000])
    nominal = resources.observe_policy(runner=_busctl(4, 4), power_root=power, thermal_root=thermal)
    assert nominal == {"power": "external", "thermal": "nominal", "network": "unmetered"}
    assert policy_violation(POLICY, nominal) is None
    power, thermal = _sysfs(tmp_path / "b", supplies={"AC": ("Mains", 0), "BAT0": ("Battery", None)},
                            zones=[85000])
    hot = resources.observe_policy(runner=_busctl(4, 1), power_root=power, thermal_root=thermal)
    assert hot == {"power": "battery", "thermal": "serious", "network": "metered"}
    assert policy_violation(POLICY, hot) == "resource_power_policy_violation"
    power, thermal = _sysfs(tmp_path / "c", supplies={}, zones=[])
    desktop = resources.observe_policy(runner=_busctl(None, None), power_root=power, thermal_root=thermal)
    assert desktop == {"power": "external", "thermal": "unknown", "network": "unknown"}  # never assumed healthy
    assert policy_violation(POLICY, desktop) == "resource_thermal_policy_violation"
    assert resources.observe_network(_busctl(1, 4)) == "unavailable"
    assert resources.observe_network(_busctl(3, 4)) == "unknown"  # limited connectivity is not usable


def test_owner_observation_dispatches_to_the_linux_observer(monkeypatch):
    from mycelium_a13_linux.managed_host import resources as linux_resources
    from mycelium_a13_macos import resources as shared

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(linux_resources, "observe_policy",
                        lambda: {"power": "external", "thermal": "fair", "network": "metered"})
    assert shared.observe_policy() == {"power": "external", "thermal": "fair", "network": "metered"}
    monkeypatch.setattr(linux_resources, "observe_policy", lambda: {"power": "plugged"})
    assert shared.observe_policy() == {"power": "unknown", "thermal": "unknown", "network": "unknown"}


def test_capability_and_sidecar_location(tmp_path):
    with pytest.raises(ValueError, match="unsupported_linux"):
        observe_linux(tmp_path, system="Darwin", machine="arm64")
    observed = observe_linux(tmp_path, system="Linux", machine="x86_64", total_memory=8 << 30)
    assert observed["platform"] == "linux" and observed["backends"] == ["numpy"]
    assert observed["independently_qualified"] is False
    app_dir = tmp_path / "share" / "mycelium" / "0.1.0" / "mycelium"
    native = app_dir / "_internal" / "mycelium_a13_macos" / "native"
    native.mkdir(parents=True)
    (app_dir / "mycelium").write_bytes(b"\x7fELF")
    sidecar = native / "mycelium-iroh-sidecar"
    sidecar.write_bytes(b"\x7fELF")
    sidecar.chmod(0o644)
    with pytest.raises(ValueError, match="native_sidecar_installation_invalid"):
        installed_sidecar(frozen=True, executable=str(app_dir / "mycelium"))  # not executable
    sidecar.chmod(0o755)
    assert installed_sidecar(frozen=True, executable=str(app_dir / "mycelium")) == sidecar.resolve()
    sidecar.chmod(0o775)
    with pytest.raises(ValueError, match="native_sidecar_installation_invalid"):
        installed_sidecar(frozen=True, executable=str(app_dir / "mycelium"))  # group-writable
    sidecar.unlink()
    sidecar.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="native_sidecar_installation_invalid"):
        installed_sidecar(frozen=True, executable=str(app_dir / "mycelium"))


def test_linux_host_cli_drains_on_sigterm_and_frozen_entry_dispatches(tmp_path, monkeypatch, capsys):
    import os
    import signal
    import threading

    from mycelium_a13_linux.managed_host import __main__ as linux_cli
    from mycelium_a13_macos import __main__ as app_main
    from mycelium_a13_macos import work_acceptance

    events = {"stopped_control": 0}

    class App:
        def __init__(self, root):
            pass

        def start(self):
            return self

        def stop(self):
            pass

        def pending_stage(self):
            return {"offer": 1}

        def pending_cleanup(self):
            return None

        def status(self):
            return {"native_control": {"state": "running"}}

        def stop_control(self):
            events["stopped_control"] += 1

        def _observe_policy(self):
            return {"power": "external", "network": "unmetered", "thermal": "nominal"}

    monkeypatch.setattr(linux_cli, "LinuxTargetApplication", App)
    monkeypatch.setattr(work_acceptance, "accept_work", lambda *a, **k: None)
    monkeypatch.delenv("MYCELIUM_STOP_EVENT", raising=False)  # POSIX: SIGTERM, not a named event
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"peer_visibility": True, "power": "ac_only", "storage_bytes": 200_000_000,
                                  "network": "metered_allowed", "background": False, "diagnostics": False,
                                  "thermal": "stop_on_serious"}))
    limits = tmp_path / "limits.json"
    limits.write_text(json.dumps({"max_rss_bytes": 1 << 30, "max_disk_bytes": 100_000_000,
                                  "max_wall_seconds": 600, "max_commands": 100}))
    preview = tmp_path / "preview.json"
    preview.write_text("{}")
    threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
    argv = ["--root", str(tmp_path / "host"), "run", "--policy", str(policy), "--limits", str(limits),
            "--preview", str(preview), "--acknowledge"]
    monkeypatch.setattr(sys, "argv", ["mycelium", "-m", "mycelium_a13_linux.managed_host", *argv])
    assert app_main.main() == 0  # through the frozen-app allow-list
    assert events["stopped_control"] == 1
    assert "draining (staging kept)" in capsys.readouterr().out
    assert signal.getsignal(signal.SIGTERM) is not linux_cli._drain_on_sigterm  # restored
