# SPDX-License-Identifier: AGPL-3.0-only
"""Windows host and boot identity for managed hello observations (fakes; no Windows host)."""

from __future__ import annotations

from mycelium_physical_runner import remote_probe


def test_boot_id_prefers_the_per_boot_counter():
    values = {remote_probe._WINDOWS_BOOT_ID: "57"}
    source = remote_probe._windows_boot_source(lambda key, name: values.get((key, name)),
                                               tick_count_ms=lambda: 1_000, clock=lambda: 10_000.0)
    assert source == "windows-boot-id:57"


def test_boot_epoch_fallback_is_stable_within_a_boot_and_changes_across_boots():
    def none(_key, _name):
        return None

    first = remote_probe._windows_boot_source(none, tick_count_ms=lambda: 3_600_000, clock=lambda: 100_000.0)
    later = remote_probe._windows_boot_source(none, tick_count_ms=lambda: 3_610_000, clock=lambda: 100_010.0)
    rebooted = remote_probe._windows_boot_source(none, tick_count_ms=lambda: 5_000, clock=lambda: 100_020.0)
    assert first == later
    assert rebooted != first
    assert first.startswith("windows-boot-epoch:")


def test_boot_identity_uses_the_windows_source(monkeypatch):
    monkeypatch.setattr(remote_probe.platform, "system", lambda: "Windows")
    monkeypatch.setattr(remote_probe, "_windows_boot_source", lambda: "windows-boot-id:9")
    one = remote_probe._boot_identity("host-a")
    monkeypatch.setattr(remote_probe, "_windows_boot_source", lambda: "windows-boot-id:10")
    assert remote_probe._boot_identity("host-a") != one
    monkeypatch.setattr(remote_probe, "_windows_boot_source", lambda: "")
    assert remote_probe._boot_identity("host-a").startswith("boot-")


def test_host_identity_uses_machine_guid(monkeypatch):
    monkeypatch.setattr(remote_probe.platform, "system", lambda: "Windows")
    monkeypatch.setattr(remote_probe, "_windows_registry_value",
                        lambda key, name: "ABCD-1234" if name == "MachineGuid" else None)
    assert remote_probe._host_identity() == "abcd-1234"
