# SPDX-License-Identifier: AGPL-3.0-only
"""Finalize a built macOS A13 app into a signed, self-installing bundle.

Post-build only: ``scripts/build_a13_macos.py`` stays byte-for-byte unchanged
(its L2-qualified output is read, never modified).  This step:

* refuses an app whose bundle contains an absolute symlink, a link that escapes
  the bundle, or a special file (link-aware digest,
  ``mycelium_a13_packaging.build.bundle_tree``);
* copies the app with ``/usr/bin/ditto`` (links, modes and extended attributes
  kept) next to a ``signed_macos_package`` install source and an Ed25519-signed
  ``installation_package_manifest.v1`` that binds the bundle digest;
* marks the publisher Apple-unsigned with the explicit
  ``DEVELOPER_ID_UNSIGNED`` sentinel (owner has not ruled on Developer ID);
* writes ``Install Mycelium.command``, ``SHA256SUMS`` and a deterministic zip
  that keeps the bundle's symlinks (restored by Archive Utility / ``ditto -x -k``).

If Developer ID signing and notarization are ever ruled in, they run on the
app *before* this step (they change the bundle digest).  The signing key is
NEVER read from argv, environment, stdout or any artifact: pass its path with
``--signing-key-file`` (32 raw Ed25519 private-key bytes).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_a13_packaging import build as _build  # noqa: E402

APP_NAME = "Mycelium A13 Demo.app"
EXECUTABLE = "Contents/MacOS/Mycelium A13 Demo"
_CHANNEL = "owner_private_media"
_PUBLISHER_ID = "mycelium-release"
_SIGNER_ENDPOINT_ID = "mycelium-macos-release"
_MACOS_FLOOR = "15.6.1"
LAUNCHD_LABEL = "org.mycelium.managed-host"

_INSTALL_COMMAND = """#!/bin/sh
# Per-user install: verifies the signed manifest and the bundle digest first. No admin.
set -eu
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec "$HERE/Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo" --install --bundle "$HERE"
"""

_README = """\
Mycelium A13 macOS pilot bundle (owner_private_media)
=====================================================

Not signed with an Apple Developer ID and not notarized (ad-hoc signature only).
macOS may refuse to open it. Do not disable Gatekeeper or remove quarantine
attributes; any OS trust decision belongs to you.

Install for this user only (no admin): open "Install Mycelium.command".
It verifies the Ed25519-signed installation-package-manifest.json and the
bundle digest before writing anything, keeps the app's quarantine attribute,
and links ~/Applications/Mycelium A13.app to the installed version.
Uninstall keeps your identity and membership state unless you ask otherwise.
"""


def declared_launchd_member(work_root: Path) -> dict[str, str]:
    """The declared launchd user agent rendered by the shared renderer (template paths).

    The macOS agent is declared, not installed, by this tranche: installing it
    is a later opt-in step, as on Windows and Linux.
    """
    from mycelium_service_package import PROTOCOL as SERVICE_PROTOCOL, build_service_package

    app = "/Users/user/Applications/Mycelium A13.app/Contents/MacOS/Mycelium A13 Demo"
    package = "/Users/user/Library/Application Support/Mycelium/service"
    config = {
        "protocol": SERVICE_PROTOCOL, "package_version": "v0-1-0", "service_id": "managed-host", "role": "node",
        # The app itself, headless (an existing launcher flag): the opt-in unattended agent.
        "argv": [app, "--headless"],
        "working_directory": package, "state_directory": package + "/state", "log_directory": package + "/logs",
        "environment": {}, "health_url": None, "restart_limit": 5, "restart_window_seconds": 600,
        "restart_delay_seconds": 60, "stop_timeout_seconds": 30,
    }
    built = build_service_package(config, package_root=work_root / "service-package")
    raw = built["_payloads"]["org.mycelium.managed-host.plist"]
    return {"name": LAUNCHD_LABEL, "sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}


def finalize_macos_bundle(
    *,
    app_dir: Path,
    output_root: Path,
    signing_key_path: Path,
    version: str,
    rollback_floor: str = "0.1.0",
    issued_at: float | None = None,
    root: Path = ROOT,
    copy=None,
) -> dict[str, Any]:
    import time

    from mycelium_a13_macos.install_source import PROTOCOL as SOURCE_PROTOCOL, validate_macos_install_source
    from mycelium_a13_macos.package_manifest import DEVELOPER_ID_UNSIGNED, build_macos_package_manifest

    if app_dir.name != APP_NAME or app_dir.is_symlink() or not (app_dir / EXECUTABLE).is_file():
        raise SystemExit("macos_app_incomplete")
    source_digest = _build.bundle_digest(app_dir)  # refuses absolute / escaping links before any write
    if output_root.exists():
        raise SystemExit("fresh_output_root_required")
    output_root.mkdir(parents=True, mode=0o700)
    payload = output_root / APP_NAME
    run_copy = copy or (lambda src, dst: subprocess.run(["/usr/bin/ditto", str(src), str(dst)], check=False).returncode)
    if run_copy(app_dir, payload) != 0:
        raise SystemExit("macos_app_copy_failed")
    digest = _build.bundle_digest(payload)
    if digest != source_digest:
        raise SystemExit("macos_app_copy_mismatch")
    artifact_name = f"mycelium-{version}-macos-arm64"
    signer = _build.load_pinned_signer(signing_key_path, endpoint_id=_SIGNER_ENDPOINT_ID)
    member = declared_launchd_member(output_root.parent / (output_root.name + ".work"))
    publisher = {"publisher_id": _PUBLISHER_ID, "developer_id_team": DEVELOPER_ID_UNSIGNED,
                 "endpoint_id": signer.endpoint_id, "verification_key_record": signer.public_key_record()}
    install_source = validate_macos_install_source({
        "protocol": SOURCE_PROTOCOL, "channel": _CHANNEL, "package_class": "signed_macos_package",
        "artifact_name": artifact_name, "artifact_digest": digest, "publisher_identity": publisher,
        "minimum_os": f"macos-{_MACOS_FLOOR}", "platform_architecture": "arm64", "macos_version": _MACOS_FLOOR,
        "service_manager": "launchd", "install_mode": "launchd_user", "rollback_floor": rollback_floor,
        "lifecycle_support": True, "expert_tools_required": [],
    })
    issued = float(time.time() if issued_at is None else issued_at)
    manifest = build_macos_package_manifest(
        signer=signer, issued_at=issued, expires_at=issued + 90 * 86400,
        source_revision=_build.git_head(root) or "0" * 40,
        build_inputs={"builder_id": "mycelium-ci", "build_digest": digest},
        platform_architecture={"system": "macos", "machine": "arm64", "macos_version": _MACOS_FLOOR},
        package_digest=digest, publisher_identity=publisher, minimum_os=f"macos-{_MACOS_FLOOR}",
        service_descriptor={"launchd_label": member["name"], "sha256": member["sha256"],
                            "install_mode": "launchd_user", "lifecycle_support": True},
        update_channel=_CHANNEL, rollback_floor=rollback_floor,
        capability_protocol="mycelium.capability_report.v1", privacy_notice="mycelium-a13-privacy-v1",
        expiry_revocation_policy="expire_after_90d_or_revocation_list",
    )
    (output_root / "install-source.json").write_text(json.dumps(install_source, indent=2) + "\n")
    (output_root / "installation-package-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    command = output_root / "Install Mycelium.command"
    command.write_text(_INSTALL_COMMAND)
    command.chmod(0o755)
    (output_root / "README-PILOT.txt").write_text(_README)
    report = {
        "platform": "macos-arm64", "channel": _CHANNEL,
        "signing": "Ed25519-signed manifest; Apple: ad-hoc only, no Developer ID, NOT notarized",
        "developer_id_team": DEVELOPER_ID_UNSIGNED, "version": version, "artifact_name": artifact_name,
        "bundle_digest": digest, "install_mode": "launchd_user", "source_revision": _build.git_head(root),
        "manifest_publisher_key_digest": signer.verification_key_digest,
    }
    (output_root / "artifact-identity.json").write_text(json.dumps(report, indent=2) + "\n")
    sums = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
                   for p in sorted(output_root.iterdir()) if p.is_file() and not p.is_symlink())
    (output_root / "SHA256SUMS").write_text(sums)
    zip_path = output_root.parent / (artifact_name + ".zip")
    raw = _build.deterministic_zip_with_links(output_root, zip_path, prefix=artifact_name)
    report["zip"] = str(zip_path)
    report["zip_sha256"] = hashlib.sha256(raw).hexdigest()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--app-dir", type=Path, required=True, help="the builder's 'Mycelium A13 Demo.app' (read only)")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--signing-key-file", type=Path, required=True,
                        help="path to 32 raw Ed25519 private-key bytes; NEVER printed or logged")
    parser.add_argument("--version", required=True)
    parser.add_argument("--rollback-floor", default="0.1.0")
    args = parser.parse_args()
    try:
        report = finalize_macos_bundle(app_dir=args.app_dir.expanduser().absolute(),
                                       output_root=args.output_root.expanduser().absolute(),
                                       signing_key_path=args.signing_key_file, version=args.version,
                                       rollback_floor=args.rollback_floor)
    except _build.PayloadError as exc:
        raise SystemExit(exc.code) from None
    print(json.dumps(report))


if __name__ == "__main__":
    main()
