# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic seed members + signed capability reports for gate-requirement tests.

Every member is a real Ed25519 identity and every report is really signed, so
the binder's verification path is exercised end to end.  Host-like values
(node ids that look like host names, Tailscale addresses, home paths) are
deliberately planted so redaction tests can prove they never reach a receipt.
"""
from __future__ import annotations

import hashlib
from typing import Any

from mycelium_a13_macos.capability import signed_capability_link_report
from mycelium_platform_neutral.capability_report import (
    CapabilityReport,
    sign_capability_report,
)
from mycelium_qualification.signing import generate_ed25519_signer

NOW = 2_000_000_000.0
GIB = 1024**3

_PEER_RUNTIME = {
    "mac_mlx_iroh": {"runtime_backend": "mlx", "transport": "iroh",
                     "activation_protocol": "mycelium.router_wire.v1"},
    "linux_native_iroh": {"runtime_backend": "numpy", "transport": "iroh",
                          "activation_protocol": "mycelium.router_wire.v1"},
    "windows_native_iroh": {"runtime_backend": "numpy", "transport": "iroh",
                            "activation_protocol": "mycelium.router_wire.v1"},
    "android_native_iroh": {"runtime_backend": "pixel-stdlib", "transport": "iroh",
                            "activation_protocol": "mycelium.router_wire.v1"},
}


class SyntheticMember:
    def __init__(self, node_id: str, *, peer_class: str = "mac_mlx_iroh", generation: int = 3,
                 endpoint_addr: str = "203.0.113.12:4433", lifecycle_state: str = "RUNNING",
                 lease_expires_at: float = NOW + 300.0) -> None:
        self.node_id = node_id
        self.peer_class = peer_class
        self.generation = generation
        self.signer = generate_ed25519_signer(endpoint_id=f"ep-{node_id}")
        self.endpoint_id = "endpoint" + hashlib.sha256(node_id.encode()).hexdigest()[:24]
        self.endpoint_addr = endpoint_addr
        self.lifecycle_state = lifecycle_state
        self.lease_expires_at = lease_expires_at

    def seed_record(self) -> dict[str, Any]:
        """The shape of ``SeedCoordinator.members()`` / the ``seed_members`` row."""
        return {
            "node_id": self.node_id,
            "endpoint_id": self.endpoint_id,
            "endpoint_addrs": [self.endpoint_addr],
            "peer_class": self.peer_class,
            "runtime_capability": dict(_PEER_RUNTIME[self.peer_class]),
            "verification_key_digest": self.signer.verification_key_digest,
            "incarnation": f"inc-{self.node_id}",
            "generation": self.generation,
            "lease_expires_at": self.lease_expires_at,
            "lifecycle_state": self.lifecycle_state,
        }

    def mac_report(self, *, memory_gib: float = 48, disk_gib: float = 200, mlx: bool = True,
                   rtt_ms: float = 30.0, bytes_per_second: float = 20_000_000.0,
                   thermal: str = "nominal", power: str = "external", chip: str = "Apple M4 Pro",
                   observed_at: float = NOW - 10.0, generation: int | None = None,
                   node_id: str | None = None) -> dict[str, Any]:
        device = {
            "chip": chip, "memory_bytes": int(memory_gib * GIB),
            "available_storage_bytes": int(disk_gib * GIB), "os_version": "26.5.1",
            "mlx": {"available": mlx, "device": "Device(gpu, 0)" if mlx else None},
            "power": power, "thermal": thermal, "network": "unmetered",
        }
        link = {"path_kind": "public_https_relay", "rounds": 1, "rtt_ms_median": rtt_ms,
                "rtt_ms_samples": [rtt_ms], "upload_bytes": 1024,
                "upload_bytes_per_second": bytes_per_second, "download_bytes": 1024,
                "download_bytes_per_second": bytes_per_second}
        return signed_capability_link_report(
            signer=self.signer, node_id=node_id or self.node_id, purpose="periodic",
            generation=self.generation if generation is None else generation,
            device=device, link=link, now=observed_at)

    def v2_report(self, *, sustainable_gib: float = 3, free_disk_gib: float = 20,
                  thermal: str = "nominal", power: str = "ac",
                  source_kind: str = "native_observed",
                  issued_at: float = NOW - 10.0) -> dict[str, Any]:
        def family(values: dict[str, Any], kind: str = source_kind) -> dict[str, Any]:
            return {"source_kind": kind, "observed_at": issued_at, "values": values}

        def unknown(names: tuple[str, ...]) -> dict[str, Any]:
            return {"source_kind": "unknown", "observed_at": None,
                    "values": {name: None for name in names}}

        report = CapabilityReport.from_mapping({
            "protocol": "mycelium.membership.capability_report.v2",
            "report_id": f"report-{self.node_id}", "swarm_id": "swarm-test",
            "node_id": self.node_id, "incarnation": f"inc-{self.node_id}",
            "membership_generation": self.generation,
            "profile_digest": "sha256:" + "1" * 64, "evidence_generation": 1,
            "observations": {
                "memory": family({"sustainable_bytes": int(sustainable_gib * GIB),
                                  "available_bytes": int(sustainable_gib * GIB),
                                  "workspace_bytes": None, "kv_headroom_bytes": None}),
                "storage": family({"assignment_local_bytes": None,
                                   "free_bytes": int(free_disk_gib * GIB)}),
                "limits": unknown(("supported_context_tokens", "active_requests", "batch_size",
                                   "prefill_tokens", "decode_tokens", "multi_position_verify",
                                   "speculative_draft")),
                "thermal": family({"state": thermal, "sustained_class": None,
                                   "observation_duration_s": None, "throttle_behavior": None}),
                "power": family({"source": power, "battery_class": None, "battery_level": None,
                                 "low_power_state": None, "minimum_contribution": None,
                                 "drain_deadline_s": None}),
                "lifecycle": unknown(("state", "background_execution", "suspend_behavior",
                                      "restart_semantics", "max_background_grace_s")),
                "network": unknown(("activation_protocol", "transport_observations",
                                    "network_loss_behavior", "link_measurement_available",
                                    "freshness_s")),
            },
            "issued_at": issued_at, "expires_at": issued_at + 600.0,
        })
        return sign_capability_report(signer=self.signer, report=report)


def macs(count: int, *, memory_gib: tuple[float, ...] | None = None,
         names: tuple[str, ...] | None = None) -> tuple[list[dict], list[dict], list[SyntheticMember]]:
    """``count`` current Apple-Silicon members, each with a fresh signed report."""
    names = names or tuple(f"mac-{index}" for index in range(count))
    memory_gib = memory_gib or tuple(48 - 8 * index for index in range(count))
    members = [SyntheticMember(name, endpoint_addr=f"100.84.0.{index + 10}:4433")
               for index, name in enumerate(names)]
    reports = [member.mac_report(memory_gib=memory) for member, memory in zip(members, memory_gib)]
    return [member.seed_record() for member in members], reports, members
