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
# Install Mycelium for this user. No admin, no network, nothing outside this folder is trusted blindly.
#
# You approved this script once (Privacy & Security > Open Anyway).  Before it extends that approval to
# the app next to it, it checks the downloaded folder against SHA256SUMS and the app file by file against
# APP-FILES.sha256.  Only if every check passes does it remove the download-quarantine mark from THIS
# app folder (and nothing else), so the installer can start without a second block.  The installer then
# verifies the Ed25519-signed manifest and the bundle digest again before it writes anything, and clears
# the mark on the installed copy.  Any mismatch: nothing is changed and nothing is run.
set -eu
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
APP="$HERE/Mycelium A13 Demo.app"
EXE="$APP/Contents/MacOS/Mycelium A13 Demo"
XATTR=/usr/bin/xattr
refuse() {
  echo "Mycelium is not installed: $1. Nothing was changed and nothing was run." >&2
  echo "Download the zip again and compare its SHA-256 with the one on the release page." >&2
  exit 2
}
cd "$HERE" || refuse "this folder cannot be read"
[ -f SHA256SUMS ] || refuse "SHA256SUMS is missing"
for doc in installation-package-manifest.json install-source.json "Install Mycelium.command" APP-FILES.sha256; do
  sed -n 's/^[0-9a-f]\\{64\\}  //p' SHA256SUMS | grep -Fxq -- "$doc" || refuse "$doc is not covered by SHA256SUMS"
done
/usr/bin/shasum -a 256 -c SHA256SUMS >/dev/null 2>&1 || refuse "a file in this folder does not match SHA256SUMS"
[ -d "$APP" ] && [ ! -L "$APP" ] && [ -f "$EXE" ] && [ ! -L "$EXE" ] || refuse "the app is missing or is not a plain folder here"
(cd "$APP" && /usr/bin/shasum -a 256 -c "$HERE/APP-FILES.sha256" >/dev/null 2>&1) || refuse "the app does not match APP-FILES.sha256"
found="$(cd "$APP" && find . ! -type d ! -type l | sed 's|^\\./||' | LC_ALL=C sort)"
listed="$(sed 's/^[0-9a-f]\\{64\\}  //' APP-FILES.sha256 | LC_ALL=C sort)"
[ "$found" = "$listed" ] || refuse "the app holds files that APP-FILES.sha256 does not list"
if (cd "$APP" && find . -type l -exec readlink {} \\;) | grep -q '^/'; then refuse "the app holds a link to an absolute path"; fi
"$XATTR" -d -r -s com.apple.quarantine "$APP" >/dev/null 2>&1 || true
if "$XATTR" -p com.apple.quarantine "$EXE" >/dev/null 2>&1; then refuse "the download mark could not be removed from the app"; fi
RUNNER="$EXE"
if "$RUNNER" --install --bundle "$HERE"; then
  echo
  echo "Mycelium is installed for this user: open 'Mycelium A13' in the Applications folder of your home folder."
else
  status=$?
  echo "Mycelium is not installed: the installer refused (see above)." >&2
  exit "$status"
fi
"""

_README = """\
Mycelium A13 macOS pilot bundle (owner_private_media)
=====================================================

Not signed with an Apple Developer ID and not notarized (ad-hoc signature only).
macOS will block "Install Mycelium.command" once: approve that one script in
System Settings > Privacy & Security > Open Anyway (the exact steps are in
QUICKSTART-MACOS.md). Do not disable Gatekeeper.

Install for this user only (no admin, no network): run "Install Mycelium.command".
It checks this folder against SHA256SUMS and the app against APP-FILES.sha256,
and only then removes the download mark from this app so it can start. The
installer then verifies the Ed25519-signed installation-package-manifest.json
and the bundle digest before writing anything, clears the mark on the
installed copy only, and links ~/Applications/Mycelium A13.app to it.
Uninstall keeps your identity and membership state unless you ask otherwise.
"""


def app_files_listing(app: Path) -> str:
    """``shasum -a 256 -c`` input for every regular file of the app (paths relative to the app, links excluded).

    The install script checks it before it removes the download mark from the app; links are covered by
    the signed bundle digest the installer verifies next (the script refuses absolute ones itself).
    """

    lines = []
    for relative, value in _build.bundle_tree(app).items():
        if value.startswith("sha256:"):
            if "\n" in relative or "\\" in relative:
                raise SystemExit("macos_app_path_unsupported")
            lines.append(f"{value[len('sha256:'):]}  {relative}\n")
    return "".join(lines)


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
    (output_root / "APP-FILES.sha256").write_text(app_files_listing(payload))
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
