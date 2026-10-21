# SPDX-License-Identifier: AGPL-3.0-only
"""Automatic capability + relay-link report contents (deterministic fakes).

The OS observers and the transport are injected so the report contents are
exact; ``test_join_window_flow.py`` exercises the real relay proxy hop.
"""
from __future__ import annotations

import copy
import json

import pytest

from mycelium_a13_macos.capability import (
    CAPABILITY_LINK_REPORT_PROTOCOL,
    MemberReports,
    measure_link,
    observe_device,
    signed_capability_link_report,
    verify_capability_link_report,
)
from mycelium_a13_macos.pairing import PairingError
from mycelium_qualification.signing import generate_ed25519_signer

NOW = 2_000_000_000.0


def _sysctl(outputs):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv[-1])

        class Result:
            stdout = outputs[argv[-1]]
        return Result()
    return run, calls


def test_device_observation_reports_chip_memory_disk_mlx_power_and_thermal(tmp_path):
    run, calls = _sysctl({"hw.memsize": "38654705664\n",
                          "machdep.cpu.brand_string": "Apple M3 Pro\n"})
    value = observe_device(tmp_path, system="Darwin", architecture="arm64", run=run,
        os_version="26.5.1",
        probe=lambda: {"available": True, "device": "Device(gpu, 0)", "value": 2.0},
        observe_policy=lambda: {"power": "battery", "thermal": "fair", "network": "unmetered"})
    assert set(value) == {"chip", "memory_bytes", "available_storage_bytes", "os_version",
                          "mlx", "power", "thermal", "network"}
    assert value["chip"] == "Apple M3 Pro" and value["memory_bytes"] == 38654705664
    assert value["available_storage_bytes"] > 0 and value["os_version"] == "26.5.1"
    assert value["mlx"] == {"available": True, "device": "Device(gpu, 0)"}
    assert (value["power"], value["thermal"], value["network"]) == ("battery", "fair", "unmetered")
    assert sorted(calls) == ["hw.memsize", "machdep.cpu.brand_string"]
    # Unobservable facts are reported as unknown, never invented.
    run, _ = _sysctl({"hw.memsize": "1024\n", "machdep.cpu.brand_string": "\x00weird\n"})
    value = observe_device(tmp_path, system="Darwin", architecture="arm64", run=run,
        os_version="", probe=lambda: {"available": False, "error_type": "ImportError"},
        observe_policy=lambda: (_ for _ in ()).throw(OSError("no probe")))
    assert value["chip"] == "unknown" and value["os_version"] == "unknown"
    assert value["mlx"] == {"available": False, "device": None}
    assert (value["power"], value["thermal"], value["network"]) == ("unknown",) * 3
    with pytest.raises(ValueError, match="unsupported_mac"):
        observe_device(tmp_path, system="Linux", architecture="x86_64")


def test_link_measurement_times_the_real_transport_round_trips():
    ticks = iter([0.0, 0.040, 1.0, 1.041, 2.0, 2.044, 3.0, 3.131072, 4.0, 4.065536])
    sent = []

    def transport(download_kib, upload):
        sent.append((download_kib, len(upload)))
        return json.dumps({"received_bytes": len(upload),
                           "filler": "A" * (download_kib * 1024)}).encode()

    value = measure_link(transport, rounds=3, bulk_bytes=262144, clock=lambda: next(ticks))
    assert sent == [(0, 0), (0, 0), (0, 0), (0, 262144), (256, 0)]
    assert value == {"path_kind": "public_https_relay", "rounds": 3,
                     "rtt_ms_median": 41.0, "rtt_ms_samples": [40.0, 41.0, 44.0],
                     "upload_bytes": 262144, "upload_bytes_per_second": 2_000_000.0,
                     "download_bytes": 262144, "download_bytes_per_second": 4_000_000.0}

    def lying(download_kib, upload):
        return json.dumps({"received_bytes": 7, "filler": ""}).encode()
    with pytest.raises(PairingError, match="link_probe_invalid"):
        measure_link(lying, rounds=1, bulk_bytes=1024, clock=iter(range(100)).__next__)


DEVICE = {"chip": "Apple M2", "memory_bytes": 16 * 1024**3, "available_storage_bytes": 10**11,
          "os_version": "15.6.1", "mlx": {"available": True, "device": "Device(gpu, 0)"},
          "power": "external", "thermal": "nominal", "network": "unmetered"}
LINK = {"path_kind": "public_https_relay", "rounds": 1, "rtt_ms_median": 30.0,
        "rtt_ms_samples": [30.0], "upload_bytes": 1024, "upload_bytes_per_second": 1.0,
        "download_bytes": 1024, "download_bytes_per_second": 1.0}


def test_signed_report_binds_identity_purpose_generation_and_expiry():
    signer = generate_ed25519_signer(endpoint_id="mac-a")
    report = signed_capability_link_report(signer=signer, node_id="remote-mac-a",
        purpose="pre_run", generation=3, device=DEVICE, link=LINK, now=NOW)
    assert report["protocol"] == CAPABILITY_LINK_REPORT_PROTOCOL
    assert report["independently_qualified"] is False
    verified = verify_capability_link_report(report, now=NOW + 1,
        expected_key_digest=signer.verification_key_digest, node_id="remote-mac-a")
    assert verified["device"] == DEVICE and verified["link"] == LINK
    assert verified["generation"] == 3 and verified["purpose"] == "pre_run"
    for field, value in (("node_id", "remote-mac-b"), ("generation", 4)):
        forged = {**copy.deepcopy(report), field: value}
        with pytest.raises(PairingError, match="capability_report_signature_invalid"):
            verify_capability_link_report(forged, now=NOW,
                expected_key_digest=signer.verification_key_digest, node_id=forged["node_id"])
    with pytest.raises(PairingError, match="capability_report_expired"):
        verify_capability_link_report(report, now=NOW + 601,
            expected_key_digest=signer.verification_key_digest, node_id="remote-mac-a")
    with pytest.raises(PairingError, match="capability_report_invalid"):
        signed_capability_link_report(signer=signer, node_id="remote-mac-a", purpose="pre_run",
            generation=3, device={**DEVICE, "extra": 1}, link=LINK, now=NOW)
    with pytest.raises(PairingError, match="capability_report_invalid"):
        signed_capability_link_report(signer=signer, node_id="remote-mac-a", purpose="qualify",
            generation=3, device=DEVICE, link=LINK, now=NOW)


def test_owner_inbox_accepts_only_current_members_latest_report():
    signer = generate_ed25519_signer(endpoint_id="mac-a")
    members = {"remote-mac-a": {"verification_key_digest": signer.verification_key_digest,
                                "generation": 3, "lifecycle_state": "NEW"}}
    clock = [NOW]
    inbox = MemberReports(lookup=members.get, clock=lambda: clock[0])
    first = signed_capability_link_report(signer=signer, node_id="remote-mac-a",
        purpose="join", generation=3, device=DEVICE, link=LINK, now=NOW)
    assert inbox.submit(first) == {"state": "accepted", "node_id": "remote-mac-a"}
    clock[0] += 5
    later = signed_capability_link_report(signer=signer, node_id="remote-mac-a",
        purpose="pre_run", generation=3, device={**DEVICE, "thermal": "fair"}, link=LINK,
        now=clock[0])
    inbox.submit(later)
    assert inbox.latest()["remote-mac-a"]["device"]["thermal"] == "fair"
    assert inbox.latest()["remote-mac-a"]["purpose"] == "pre_run"
    stale = signed_capability_link_report(signer=signer, node_id="remote-mac-a",
        purpose="periodic", generation=2, device=DEVICE, link=LINK, now=clock[0])
    with pytest.raises(PairingError, match="capability_report_member_not_current"):
        inbox.submit(stale)
    stranger = generate_ed25519_signer(endpoint_id="mac-x")
    foreign = signed_capability_link_report(signer=stranger, node_id="remote-mac-a",
        purpose="periodic", generation=3, device=DEVICE, link=LINK, now=clock[0])
    with pytest.raises(PairingError, match="capability_report_signature_invalid"):
        inbox.submit(foreign)
    members["remote-mac-a"]["lifecycle_state"] = "STOPPED"
    with pytest.raises(PairingError, match="capability_report_member_not_current"):
        inbox.submit(later)
    assert inbox.handle("POST", "/member-report/v1", json.dumps(later).encode())[0] == 409
    assert inbox.handle("POST", "/member-report/v1", b"[]")[0] == 400
    assert inbox.handle("GET", "/member-report/v1", b"")[0] == 405
