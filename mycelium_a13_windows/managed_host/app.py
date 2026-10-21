# SPDX-License-Identifier: AGPL-3.0-only
"""The Windows managed target application and its installation descriptor.

``WindowsTargetApplication`` is the macOS ``TargetApplication`` (staging offers,
signed control pump, native worker, cleanup) with the Windows platform hooks:
the in-process ``WindowsTargetStaging`` with its logical staging root, the
Windows sidecar (``.exe``, loopback channel), the Windows resource policy
observation and the Windows capability observation.  The shared workflow
(``mycelium_a13_macos.workflow.run_target_offer``) drives it unchanged.

The installation descriptor discloses *logical* POSIX names for every path
fact.  The controller never executes those paths on a managed peer
(``_peer_process_argv`` is empty for ``managed_https``) and its plan contracts
admit only POSIX paths, so the physical Windows paths stay on the host.
"""

from __future__ import annotations

import time
from typing import Any

from mycelium_a13_macos.managed_control import _binding_from_member, _segment, sign_managed_statement
from mycelium_a13_macos.installation_descriptor import PROTOCOL as DESCRIPTOR_PROTOCOL
from mycelium_a13_macos.target_app import TargetApplication

from .member import RemoteWindowsMember, installed_sidecar, observe_windows
from .resources import observe_policy
from .staging import WindowsTargetStaging, logical_staging_root


def logical_host_root(node_id: str) -> str:
    return f"/mycelium-managed/{_segment(node_id)}"


class WindowsTargetApplication(TargetApplication):
    def __init__(self, root, *, _test_peer_factory=None):
        super().__init__(root, _test_peer_factory=_test_peer_factory or RemoteWindowsMember)

    def _new_staging(self, run_id, *, node_id, max_archive_bytes, max_disk_bytes,
                     current_member, authorize, artifact_cache_root=None):
        return WindowsTargetStaging(
            self.peer.state_root, node_id=node_id, run_id=run_id,
            max_archive_bytes=max_archive_bytes, max_disk_bytes=max_disk_bytes,
            current_member=current_member, authorize=authorize,
            artifact_cache_root=artifact_cache_root)

    def _installed_sidecar(self):
        return installed_sidecar()

    def _observe_policy(self):
        return observe_policy()

    def _observe_capability(self):
        return observe_windows(self.peer.state_root)


def windows_installation_descriptor(app: WindowsTargetApplication, *, run_id: str, deployment_id: str,
                                    manifest_digest: str, acknowledged: bool) -> dict[str, Any]:
    """The signed ``mycelium.a13.installation_descriptor.v1`` card of a Windows host."""

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
    node_root = logical_host_root(member["node_id"])
    facts = {
        "node_id": member["node_id"],
        "endpoint_id": member["endpoint_id"],
        "membership_generation": member["generation"],
        "staging_root": logical_staging_root(member["node_id"], run_id),
        "python_executable": f"{node_root}/runtime/python",
        "sidecar_binary": f"{node_root}/runtime/mycelium-iroh-sidecar",
        "endpoint_secret_file": f"{node_root}/identity/node.key",
        "process_transport": "managed_https",
        "ssh_target": None,
        "ssh_identity_file": None,
        "runtime_backend": "numpy",
        "source_root": f"{node_root}/serving/source",
        "manifest_digest": manifest_digest,
        "route_ready": False,
    }
    now = time.time()
    return sign_managed_statement(app.peer._signer, {  # noqa: SLF001 - the member's own key
        "protocol": DESCRIPTOR_PROTOCOL, **binding, "swarm_id": retained["swarm_id"],
        "issued_at": now, "expires_at": now + 300, "facts": facts})


__all__ = ["WindowsTargetApplication", "logical_host_root", "windows_installation_descriptor"]
