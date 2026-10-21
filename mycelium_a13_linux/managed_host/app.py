# SPDX-License-Identifier: AGPL-3.0-only
"""The Linux managed target application and its installation descriptor.

``LinuxTargetApplication`` is the macOS ``TargetApplication`` (staging offers,
signed control pump, native worker, cleanup) with the Linux member class, the
Linux sidecar, and the Linux resource and capability observations.  Linux is
POSIX, so the macOS target staging (the canonical POSIX staging script) and
the worker run unchanged, and the installation descriptor discloses the real
POSIX paths exactly like the macOS target does.
"""

from __future__ import annotations

from pathlib import Path
import sys
import time
from typing import Any

from mycelium_a13_macos.installation_descriptor import PROTOCOL as DESCRIPTOR_PROTOCOL
from mycelium_a13_macos.managed_control import _binding_from_member, _segment, sign_managed_statement
from mycelium_a13_macos.target_app import TargetApplication

from .member import RemoteLinuxMember, installed_sidecar, observe_linux
from .resources import observe_policy


class LinuxTargetApplication(TargetApplication):
    def __init__(self, root, *, _test_peer_factory=None):
        super().__init__(root, _test_peer_factory=_test_peer_factory or RemoteLinuxMember)

    def _installed_sidecar(self):
        return installed_sidecar()

    def _observe_policy(self):
        return observe_policy()

    def _observe_capability(self):
        return observe_linux(self.peer.state_root)


def linux_installation_descriptor(app: LinuxTargetApplication, *, run_id: str, deployment_id: str,
                                  manifest_digest: str, acknowledged: bool) -> dict[str, Any]:
    """The signed ``mycelium.a13.installation_descriptor.v1`` card of a Linux host."""

    if acknowledged is not True:
        raise ValueError("installation_disclosure_consent_required")
    run_id = _segment(run_id)
    deployment_id = _segment(deployment_id)
    from mycelium_node.durable_membership import load_membership_state

    member = app.peer.staging_control_snapshot()
    retained = load_membership_state(app.peer.state_root)
    if retained is None:
        raise ValueError("installation_member_required")
    binding = _binding_from_member(member, run_id, deployment_id)
    app._endpoint_identity_file(member)  # the pinned endpoint key must exist and match
    root = Path(app.peer.state_root)
    facts = {
        "node_id": member["node_id"],
        "endpoint_id": member["endpoint_id"],
        "membership_generation": member["generation"],
        "staging_root": str(root / "managed" / run_id / "mycelium-stage"),
        "python_executable": str(Path(sys.executable).resolve()),
        "sidecar_binary": str(app._installed_sidecar()),  # noqa: SLF001
        "endpoint_secret_file": str(app._endpoint_identity_file(member)),  # noqa: SLF001
        "process_transport": "managed_https",
        "ssh_target": None,
        "ssh_identity_file": None,
        "runtime_backend": "numpy",
        "source_root": str(root / "serving" / "source"),
        "manifest_digest": manifest_digest,
        "route_ready": False,
    }
    now = time.time()
    return sign_managed_statement(app.peer._signer, {  # noqa: SLF001 - the member's own key
        "protocol": DESCRIPTOR_PROTOCOL, **binding, "swarm_id": retained["swarm_id"],
        "issued_at": now, "expires_at": now + 300, "facts": facts})


__all__ = ["LinuxTargetApplication", "linux_installation_descriptor"]
