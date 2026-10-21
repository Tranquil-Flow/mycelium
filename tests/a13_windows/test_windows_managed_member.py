# SPDX-License-Identifier: AGPL-3.0-only
"""The Windows managed member enrols through the real A13 owner seed and discloses logical facts.

Real DemoAuthority seed over pinned local TLS (the macOS owner), a real
RemoteWindowsMember enrolment, and the Windows installation descriptor checked
by the owner's own verifier.  Platform observations use fakes; nothing here is
a Windows run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mycelium_a13_macos.installation_descriptor import verify_installation_descriptor
from mycelium_a13_macos.resources import policy_violation
from mycelium_a13_windows.managed_host import resources
from mycelium_a13_windows.managed_host.app import (
    WindowsTargetApplication,
    windows_installation_descriptor,
)
from mycelium_a13_windows.managed_host.member import (
    RemoteWindowsMember,
    installed_sidecar,
    observe_windows,
)
from mycelium_a13_windows.managed_host.staging import WindowsTargetStaging
from mycelium_node.durable_membership import load_membership_state

from tests.a13_macos.test_peer import CONSENT, QUOTA, seed  # noqa: F401 - pytest fixture

POLICY = {"power": "ac_only", "thermal": "stop_on_serious", "network": "metered_allowed"}


def _enrolled_app(tmp_path, seed_fixture):
    authority, factory = seed_fixture
    app = WindowsTargetApplication(
        tmp_path / "windows-target",
        _test_peer_factory=lambda root: RemoteWindowsMember(root, _test_seed_client_factory=factory),
    )
    app.start()
    request = app.peer.pair_request(CONSENT, ttl_seconds=300)
    handoff = authority.approve_pairing(request, ttl_seconds=120, quota=QUOTA)
    app.accept_pairing(handoff)
    return authority, app, request


def test_windows_member_enrols_with_its_own_class_and_runtime(tmp_path, seed):  # noqa: F811
    authority, app, request = _enrolled_app(tmp_path, seed)
    try:
        member = authority.coordinator.member(request["node_id"])
        assert member["peer_class"] == "windows_native_iroh"
        assert member["runtime_capability"]["runtime_backend"] == "numpy"
        # Owner parity ruling 2026-10-07: full activation eligibility.
        assert member["activation_eligible"] is True
        assert app.status()["membership_state"] == "current"
    finally:
        app.stop()


def test_installation_descriptor_is_logical_and_verifies_on_the_owner(tmp_path, seed):  # noqa: F811
    authority, app, request = _enrolled_app(tmp_path, seed)
    try:
        with pytest.raises(ValueError, match="consent_required"):
            windows_installation_descriptor(app, run_id="run-one", deployment_id="dep-one",
                                            manifest_digest="sha256:" + "a" * 64, acknowledged=False)
        card = windows_installation_descriptor(app, run_id="run-one", deployment_id="dep-one",
                                               manifest_digest="sha256:" + "a" * 64, acknowledged=True)
        member = authority.coordinator.member(request["node_id"])
        swarm_id = load_membership_state(app.peer.state_root)["swarm_id"]
        facts = verify_installation_descriptor(card, member=member, swarm_id=swarm_id,
                                               run_id="run-one", deployment_id="dep-one")
        assert facts["runtime_backend"] == "numpy"
        assert facts["staging_root"] == f"/mycelium-managed/{request['node_id']}/run-one/mycelium-stage"
        rendered = json.dumps(card)
        assert str(app.peer.state_root) not in rendered  # no physical path leaves the host
        staging = app._new_staging("run-one", node_id=request["node_id"], max_archive_bytes=1000,
                                   max_disk_bytes=1000, current_member=lambda: True,
                                   authorize=lambda intent: True)
        assert isinstance(staging, WindowsTargetStaging)
        assert staging.logical_root == facts["staging_root"]
    finally:
        app.stop()


def test_resource_observation_maps_to_the_shared_policy():
    def runner(document):
        return lambda _command: json.dumps(document)

    nominal = resources.observe_policy(runner=runner({"kelvin": [320.0, 330.0], "power": "external",
                                                     "network": "unmetered"}))
    assert nominal == {"power": "external", "thermal": "nominal", "network": "unmetered"}
    assert policy_violation(POLICY, nominal) is None
    hot = resources.observe_policy(runner=runner({"kelvin": 358.15, "power": "battery", "network": "metered"}))
    assert hot == {"power": "battery", "thermal": "serious", "network": "metered"}
    assert policy_violation(POLICY, hot) == "resource_power_policy_violation"
    no_zone = resources.observe_policy(runner=runner({"kelvin": None, "power": "external", "network": "metered"}))
    assert no_zone["thermal"] == "unknown"
    assert policy_violation(POLICY, no_zone) == "resource_thermal_policy_violation"
    assert resources.observe_policy(runner=lambda _c: "garbage") == {
        "power": "unknown", "thermal": "unknown", "network": "unknown"}


def test_capability_and_sidecar_location(tmp_path):
    with pytest.raises(ValueError, match="unsupported_windows"):
        observe_windows(tmp_path, system="Darwin", machine="arm64")
    observed = observe_windows(tmp_path, system="Windows", machine="AMD64", total_memory=16 << 30)
    assert observed["platform"] == "windows" and observed["backends"] == ["numpy"]
    assert observed["independently_qualified"] is False
    module_root = tmp_path / "repo" / "mycelium_a13_windows" / "managed_host"
    module_root.mkdir(parents=True)
    with pytest.raises(ValueError, match="native_sidecar_installation_invalid"):
        installed_sidecar(module_root=module_root, frozen=False)
    built = tmp_path / "repo" / ".build-native" / "release"
    built.mkdir(parents=True)
    (built / "mycelium-iroh-sidecar.exe").write_bytes(b"MZ")
    assert installed_sidecar(module_root=module_root, frozen=False) == (built / "mycelium-iroh-sidecar.exe").resolve()
    Path(built / "mycelium-iroh-sidecar.exe").unlink()
    (built / "mycelium-iroh-sidecar.exe").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="native_sidecar_installation_invalid"):
        installed_sidecar(module_root=module_root, frozen=False)
