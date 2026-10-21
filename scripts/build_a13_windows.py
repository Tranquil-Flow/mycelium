# SPDX-License-Identifier: AGPL-3.0-only
"""Build the pilot Windows install bundle for A13 (owner_private_media channel).

Mirrors ``scripts/build_a13_macos.py``'s shape for the Windows tranche: this
repository tree is the source of truth, the build stages a deterministic
source closure plus one signed ``mycelium.installation_package_manifest.v1``,
and it never invents a real MSI/EXE -- there is no MSI toolchain on this
macOS build host, and none is required: a zip/staging layout is the shipped
artifact (owner pre-fire build-out packet, 2026-09-27).

Owner ruling 2026-09-27 #4 (no certificate for owner_private_media): the pilot
channel ships with NO Authenticode code-signing certificate.  This build marks
the installer's publisher identity ``authenticode_subject`` with the explicit
unsigned sentinel (``mycelium_a13_windows.install_source.AUTHENTICODE_UNSIGNED_SUBJECT``)
rather than fabricating a ``CN=...`` identity; integrity is bound only by the
pinned Ed25519 verification key baked into the signed package manifest.  A
real Windows install of this bundle will therefore trigger an
unknown-publisher / SmartScreen warning -- expected, not a defect.  A
purchased certificate is only reconsidered before any PUBLIC Windows
distribution.

The signing key is NEVER read from argv, environment, stdout, or any written
artifact: pass its path with ``--signing-key-file`` (32 raw Ed25519
private-key bytes).  Tests use a throwaway generated key, never a real one.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_a13_packaging import build as _build  # noqa: E402

# THIS REPOSITORY IS THE APP'S SOURCE OF TRUTH (see build_a13_macos.py's own
# ERRATA-6 note on why this check exists at all).
_APP_TREE_MARKERS = (
    "mycelium_a13_windows/__init__.py",
    "mycelium_a13_windows/onboarding.py",
    "mycelium_a13_windows/install_source.py",
    "mycelium_a13_windows/package_manifest.py",
    "mycelium_a13_windows/service_lifecycle.py",
    "mycelium_a13_windows/platform_admission.py",
    "mycelium_service_package.py",
)

#: Package(s)/module(s) the closure walk starts from.  Everything else is
#: discovered by static import analysis (never executed) below.
_CLOSURE_SEEDS = ("mycelium_a13_windows", "mycelium_service_package.py")

#: The declared Windows service the manifest's service_descriptor binds to.
_WINDOWS_SERVICE_NAME = "mycelium-win-1-agent"

#: The pilot channel per owner ruling 2026-09-27 #4.
_CHANNEL = "owner_private_media"

#: The signer's Iroh-style endpoint id.  Distinct from any real device
#: endpoint; this is the *release* signer, not a member.
_SIGNER_ENDPOINT_ID = "mycelium-windows-release"

_PUBLISHER_ID = "mycelium-release"


def require_app_tree_root(root: Path = ROOT) -> dict[str, str]:
    """Fail before packaging if this checkout does not look like the app's own tree."""
    return _build.require_app_tree_root(root, _APP_TREE_MARKERS)

def runtime_source_closure(root: Path = ROOT, seeds: tuple[str, ...] = _CLOSURE_SEEDS) -> tuple[str, ...]:
    """Static transitive Python-import closure, seeded at the Windows tranche.

    Never imports or executes a single discovered module (shared helper:
    ``mycelium_a13_packaging.build.runtime_source_closure``).  It deliberately
    does not reuse scripts/build_qwen_live_route.py's closure, which is owned by
    the macOS build lane and seeded for a different (mac/live-route) runtime.
    """
    return _build.runtime_source_closure(root, seeds, label="windows")

def stage_runtime_source(
    root: Path, destination: Path, seeds: tuple[str, ...] = _CLOSURE_SEEDS
) -> dict[str, Any]:
    """Copy the closure's bytes into ``destination``; never a symlink, never outside root."""
    return _build.stage_files(root, destination, runtime_source_closure(root, seeds))

def git_head(root: Path = ROOT) -> str | None:
    return _build.git_head(root)

def zip_runtime_source(source_dir: Path, zip_path: Path) -> bytes:
    """Deterministic zip: sorted entries, fixed timestamp, no host-dependent metadata."""
    return _build.deterministic_zip(source_dir, zip_path)

def load_pinned_signer(key_path: Path, *, endpoint_id: str = _SIGNER_ENDPOINT_ID):
    """Load the pilot's pinned Ed25519 signing key (32 raw private-key bytes).

    The raw bytes are never logged, printed, or placed in any returned/written
    structure (shared helper: ``mycelium_a13_packaging.build.load_pinned_signer``).
    """
    return _build.load_pinned_signer(key_path, endpoint_id=endpoint_id)

def render_windows_service_member(work_root: Path) -> dict[str, Any]:
    """Render the declared least-privilege Windows service through the shared renderer.

    Closes the loop with Gap A3: the package manifest's ``service_descriptor``
    below binds to THIS rendered artifact's digest, not a fabricated one.
    """
    from mycelium_a13_windows.service_lifecycle import (
        PROTOCOL as SERVICE_LIFECYCLE_PROTOCOL,
        windows_service_member_from_shared_renderer,
    )

    descriptor = {
        "protocol": SERVICE_LIFECYCLE_PROTOCOL,
        "service_name": _WINDOWS_SERVICE_NAME,
        "display_name": "Mycelium Agent",
        "start_type": "automatic",
        # Per-user logon task, not an SCM service (lane pm-a9 finding): python.exe
        # has no SCM control handler and an NT SERVICE SID cannot open the
        # owner-only host state the signed-in user paired.  The SCM mode stays
        # validated in service_lifecycle so an owner ruling can bring it back.
        "account_class": "signed_in_user_least_privilege",
        "launch_privilege": "as_invoker_least_privilege",
        "restricted_token": True,
        "public_listener": False,
        "environment": {"MYCELIUM_ROLE": "node"},
        "install_mode": "windows_service_per_user",
        "restart_policy": {
            "restart_limit": 5,
            "restart_delay_seconds": 5,
            "stop_timeout_seconds": 30,
        },
    }
    rendered = windows_service_member_from_shared_renderer(
        descriptor, package_root=work_root / "service-package"
    )
    if not rendered["windows_member_rendered"]:
        raise SystemExit("windows_service_member_not_rendered")
    return rendered


def build_windows_install_source(
    *, signer, artifact_name: str, artifact_digest: str, rollback_floor: str
) -> dict[str, Any]:
    from mycelium_a13_windows.install_source import (
        AUTHENTICODE_UNSIGNED_SUBJECT,
        PROTOCOL,
        validate_windows_install_source,
    )
    from mycelium_a13_windows.platform_admission import ADMITTED_RELEASES

    declared = ADMITTED_RELEASES["windows"]
    document = {
        "protocol": PROTOCOL,
        "channel": _CHANNEL,
        "package_class": "signed_windows_installer",
        "artifact_name": artifact_name,
        "artifact_digest": artifact_digest,
        "publisher_identity": {
            "publisher_id": _PUBLISHER_ID,
            # Owner ruling 2026-09-27 #4: no certificate for owner_private_media.
            "authenticode_subject": AUTHENTICODE_UNSIGNED_SUBJECT,
            "endpoint_id": signer.endpoint_id,
            "verification_key_record": signer.public_key_record(),
        },
        "minimum_os": f"windows-10-{declared['minimum_build']}",
        "platform_architecture": "x86_64",
        "windows_version": declared["minimum_version"],
        "windows_build": declared["minimum_build"],
        "service_manager": declared["service_manager"],
        "install_mode": "windows_service_per_user",
        "rollback_floor": rollback_floor,
        "lifecycle_support": True,
        "expert_tools_required": [],
    }
    return validate_windows_install_source(document)


def build_signed_package_manifest(
    *,
    signer,
    source_revision: str,
    build_digest: str,
    package_digest: str,
    service_member: dict[str, Any],
    rollback_floor: str,
    issued_at: float,
) -> dict[str, Any]:
    from mycelium_a13_windows.install_source import AUTHENTICODE_UNSIGNED_SUBJECT
    from mycelium_a13_windows.package_manifest import build_windows_package_manifest
    from mycelium_a13_windows.platform_admission import ADMITTED_RELEASES

    declared = ADMITTED_RELEASES["windows"]
    return build_windows_package_manifest(
        signer=signer,
        issued_at=issued_at,
        expires_at=issued_at + 90 * 86400,
        source_revision=source_revision,
        build_inputs={"builder_id": "mycelium-ci", "build_digest": build_digest},
        platform_architecture={
            "system": "windows",
            "machine": "x86_64",
            "windows_version": declared["minimum_version"],
            "windows_build": declared["minimum_build"],
        },
        package_digest=package_digest,
        publisher_identity={
            "publisher_id": _PUBLISHER_ID,
            # Owner ruling 2026-09-27 #4: no certificate for owner_private_media.
            "authenticode_subject": AUTHENTICODE_UNSIGNED_SUBJECT,
            "endpoint_id": signer.endpoint_id,
            "verification_key_record": signer.public_key_record(),
        },
        minimum_os=f"windows-10-{declared['minimum_build']}",
        service_descriptor={
            "service_name": service_member["declared_windows_service_name"],
            "sha256": service_member["windows_member_sha256"],
            "install_mode": service_member["windows_install_mode"],
            "lifecycle_support": True,
        },
        update_channel=_CHANNEL,
        rollback_floor=rollback_floor,
        capability_protocol="mycelium.capability_report.v1",
        privacy_notice="mycelium-a13-privacy-v1",
        expiry_revocation_policy="expire_after_90d_or_revocation_list",
    )


_README = """\
Mycelium A13 Windows pilot bundle -- STAGED, NOT AN MSI
========================================================

Distribution channel: owner_private_media (private pilot review only).

Per owner ruling 2026-09-27 #4 (no certificate for owner_private_media), this
installer is Authenticode-UNSIGNED. Installing it on Windows is expected to
show an "unknown publisher" / SmartScreen warning -- that is expected, not a
defect. Integrity is instead bound to the pinned Ed25519 verification key
recorded in installation-package-manifest.json; verify that signature (see
mycelium_a13_windows.package_manifest.verify_windows_package) before trusting
this bundle's contents.

This is a source-tranche staging layout (windows-runtime-source/ plus the two
JSON records below), not a compiled MSI/EXE: no MSI toolchain runs on this
macOS build host, and none is required for the pilot review this bundle
supports.
"""


def build_windows_bundle(
    *,
    output_root: Path,
    work_root: Path,
    signing_key_path: Path,
    version: str,
    rollback_floor: str | None = None,
    root: Path = ROOT,
) -> dict[str, Any]:
    # ``version`` labels the artifact filename/output directory and may carry a
    # preview/build suffix; ``rollback_floor`` binds the install-source and
    # manifest's numeric downgrade floor and must be strictly dotted-numeric
    # (mycelium_a13_windows.install_source._version's own requirement).
    floor = rollback_floor if rollback_floor is not None else "0.1.0"
    require_app_tree_root(root)
    if output_root.exists():
        raise SystemExit("fresh_output_root_required")
    output_root.mkdir(parents=True, mode=0o700)
    work_root.mkdir(parents=True, exist_ok=True)

    source_dir = work_root / "windows-runtime-source"
    source_identity = stage_runtime_source(root, source_dir)
    artifact_name = f"mycelium-{version}-windows-x86_64.zip"
    zip_path = output_root / artifact_name
    zip_raw = zip_runtime_source(source_dir, zip_path)
    artifact_digest = "sha256:" + hashlib.sha256(zip_raw).hexdigest()
    build_digest = "sha256:" + hashlib.sha256(
        json.dumps(source_identity["sha256"], sort_keys=True).encode("utf-8")
    ).hexdigest()

    signer = load_pinned_signer(signing_key_path)
    service_member = render_windows_service_member(work_root)
    install_source = build_windows_install_source(
        signer=signer,
        artifact_name=artifact_name,
        artifact_digest=artifact_digest,
        rollback_floor=floor,
    )
    head = git_head(root)
    manifest = build_signed_package_manifest(
        signer=signer,
        source_revision=head or "0" * 40,
        build_digest=build_digest,
        package_digest=artifact_digest,
        service_member=service_member,
        rollback_floor=floor,
        issued_at=2_000_000_000.0,
    )

    (output_root / "install-source.json").write_text(json.dumps(install_source, indent=2) + "\n")
    (output_root / "installation-package-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    (output_root / "README-PILOT.txt").write_text(_README)

    report = {
        "platform": "windows-x86_64",
        "channel": _CHANNEL,
        "signing": "Ed25519-signed manifest; Authenticode-UNSIGNED (owner ruling 2026-09-27 #4)",
        "version": version,
        "rollback_floor": floor,
        "artifact_name": artifact_name,
        "artifact_digest": artifact_digest,
        "source_revision": head,
        "runtime_source_files": source_identity["files"],
        "windows_service_member": {
            "name": service_member["windows_member_name"],
            "sha256": service_member["windows_member_sha256"],
            "install_mode": service_member["windows_install_mode"],
        },
        "install_source_channel": install_source["channel"],
        "manifest_publisher_key_digest": signer.verification_key_digest,
    }
    (output_root / "artifact-identity.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


# ------------------------------------------------------------------ frozen app
#
# The normal-user Windows artifact (runs/v2/runday/a13/A13-WINDOWS-PACKAGING.md):
# a zip holding a PyInstaller one-folder ``Mycelium\Mycelium.exe`` that installs
# itself per user.  PyInstaller cannot cross-build, so the build is split:
#
#   frozen-input  (any host, offline)  stage the closure + a deterministic spec
#   [run day, Windows]                 pyinstaller mycelium-windows.spec, add the
#                                      msvc sidecar and cloudflared.exe
#   finalize      (any host, offline)  digest the frozen tree, sign the manifest,
#                                      lay out the bundle and zip it

#: The app (same entry as macOS), the Windows host, installer and service runner.
_FROZEN_SEEDS = (
    "mycelium_a13_macos",
    "mycelium_a13_windows",
    "mycelium_service_package.py",
    "mycelium_service_runner.py",
)
#: Non-Python data the app serves.
_FROZEN_DATA = ("mycelium_a13_macos/web",)
#: Files the frozen one-folder app must hold before it can be finalized.
FROZEN_REQUIRED = (
    "Mycelium.exe",
    "_internal/mycelium_a13_macos/native/mycelium-iroh-sidecar.exe",
    "_internal/mycelium_a13_macos/native/cloudflared.exe",
)
#: Pinned in runs/v2/runday/a13/fetch_windows_packaging_deps.sh.
PYINSTALLER_PIN = "6.22.3"
WINDOWS_PYTHON = "3.14"
FROZEN_INPUT_PROTOCOL = "mycelium.a13_windows_frozen_input.v1"

_SPEC = """\
# Generated by scripts/build_a13_windows.py frozen-input; do not edit.
# Build on Windows: pyinstaller --noconfirm --clean mycelium-windows.spec
# (PyInstaller {pyinstaller}, CPython {python}, from windows-offline wheels only.)
a = Analysis(
    ['source/mycelium_a13_macos/__main__.py'],
    pathex=['source'],
    binaries=[
        ('native/mycelium-iroh-sidecar.exe', 'mycelium_a13_macos/native'),
        ('native/cloudflared.exe', 'mycelium_a13_macos/native'),
    ],
    datas=[
        ('source/mycelium_a13_macos/web', 'mycelium_a13_macos/web'),
        ('native/cloudflared-LICENSE', 'third_party'),
    ],
    hiddenimports={hidden!r},
    excludes=['mlx', 'tkinter'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Mycelium', console=True,
          disable_windowed_traceback=True, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name='Mycelium', upx=False)
"""

_HIDDEN_IMPORTS = (
    "mycelium_a13_macos.serving",
    "mycelium_a13_macos.workflow",
    "mycelium_a13_macos.managed_observations",
    "mycelium_a13_windows.installer",
    "mycelium_a13_windows.import_model",
    "mycelium_a13_windows.service_install",
    "mycelium_a13_windows.managed_host.__main__",
    "mycelium_service_runner",
)

_INSTALL_CMD = (
    "@echo off\r\n"
    "rem Per-user install: verifies the signed manifest and payload digest first.\r\n"
    '"%~dp0Mycelium\\Mycelium.exe" --install --bundle "%~dp0."\r\n'
    "pause\r\n"
)


def build_frozen_input(*, output_root: Path, root: Path = ROOT) -> dict[str, Any]:
    """Stage everything PyInstaller needs on the Windows host; unsigned, offline."""
    require_app_tree_root(root)
    if output_root.exists():
        raise SystemExit("fresh_output_root_required")
    output_root.mkdir(parents=True, mode=0o700)
    source = output_root / "source"
    identity = stage_runtime_source(root, source, _FROZEN_SEEDS)
    for relative in _FROZEN_DATA:
        _build.stage_data_tree(root, relative, source, identity["sha256"])
    (output_root / "native").mkdir()
    spec = _SPEC.format(pyinstaller=PYINSTALLER_PIN, python=WINDOWS_PYTHON, hidden=list(_HIDDEN_IMPORTS))
    (output_root / "mycelium-windows.spec").write_text(spec, encoding="utf-8")
    input_digest = "sha256:" + hashlib.sha256(
        json.dumps({"files": identity["sha256"], "spec": spec}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    record = {
        "protocol": FROZEN_INPUT_PROTOCOL,
        "source_revision": git_head(root),
        "input_digest": input_digest,
        "files": dict(sorted(identity["sha256"].items())),
        "pyinstaller": PYINSTALLER_PIN,
        "windows_python": WINDOWS_PYTHON,
        "native_inputs_required": {
            "native/mycelium-iroh-sidecar.exe": "cargo build --release --offline --locked (msvc), A9 sheet",
            "native/cloudflared.exe": "windows-offline/packaging/cloudflared/cloudflared-windows-amd64.exe",
            "native/cloudflared-LICENSE": "windows-offline/packaging/cloudflared/LICENSE",
        },
        "signed": False,
        "authenticode": "none (owner ruling 2026-09-27 #4)",
    }
    (output_root / "package-input.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return record


def finalize_windows_bundle(
    *,
    frozen_dir: Path,
    output_root: Path,
    signing_key_path: Path,
    version: str,
    rollback_floor: str = "0.1.0",
    issued_at: float | None = None,
    root: Path = ROOT,
) -> dict[str, Any]:
    """Lay out and sign the self-installing bundle around a frozen one-folder app."""
    import shutil
    import time

    from mycelium_a13_windows.installer import PAYLOAD_DIRECTORY, payload_digest

    for relative in FROZEN_REQUIRED:
        path = frozen_dir / relative
        if path.is_symlink() or not path.is_file():
            raise SystemExit("frozen_app_incomplete:" + relative)
    if output_root.exists():
        raise SystemExit("fresh_output_root_required")
    output_root.mkdir(parents=True, mode=0o700)
    payload = output_root / PAYLOAD_DIRECTORY
    shutil.copytree(frozen_dir, payload, symlinks=True)
    digest = payload_digest(payload)
    artifact_name = f"mycelium-{version}-windows-x86_64"
    signer = load_pinned_signer(signing_key_path)
    service_member = render_windows_service_member(output_root.parent / (output_root.name + ".work"))
    install_source = build_windows_install_source(
        signer=signer, artifact_name=artifact_name, artifact_digest=digest, rollback_floor=rollback_floor
    )
    issued = float(time.time() if issued_at is None else issued_at)
    manifest = build_signed_package_manifest(
        signer=signer,
        source_revision=git_head(root) or "0" * 40,
        build_digest=digest,
        package_digest=digest,
        service_member=service_member,
        rollback_floor=rollback_floor,
        issued_at=issued,
    )
    (output_root / "install-source.json").write_text(json.dumps(install_source, indent=2) + "\n")
    (output_root / "installation-package-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output_root / "Install Mycelium.cmd").write_bytes(_INSTALL_CMD.encode("ascii"))
    (output_root / "README-PILOT.txt").write_text(_README)
    report = {
        "platform": "windows-x86_64",
        "channel": _CHANNEL,
        "signing": "Ed25519-signed manifest; Authenticode-UNSIGNED (owner ruling 2026-09-27 #4)",
        "version": version,
        "artifact_name": artifact_name,
        "payload_digest": digest,
        "install_mode": install_source["install_mode"],
        "source_revision": git_head(root),
        "manifest_publisher_key_digest": signer.verification_key_digest,
    }
    (output_root / "artifact-identity.json").write_text(json.dumps(report, indent=2) + "\n")
    sums = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(output_root.iterdir())
        if path.is_file()
    )
    (output_root / "SHA256SUMS").write_text(sums)
    zip_path = output_root.parent / (artifact_name + ".zip")
    zip_raw = zip_runtime_source(output_root, zip_path)
    report["zip"] = str(zip_path)
    report["zip_sha256"] = hashlib.sha256(zip_raw).hexdigest()
    return report


def main() -> None:
    if sys.argv[1:2] == ["frozen-input"]:
        parser = argparse.ArgumentParser(description="stage the PyInstaller input (offline, unsigned)")
        parser.add_argument("--output-root", type=Path, required=True)
        args = parser.parse_args(sys.argv[2:])
        print(json.dumps(build_frozen_input(output_root=args.output_root.expanduser().absolute())["input_digest"]))
        return
    if sys.argv[1:2] == ["finalize"]:
        parser = argparse.ArgumentParser(description="sign and lay out the self-installing bundle")
        parser.add_argument("--frozen-dir", type=Path, required=True, help="PyInstaller dist\\Mycelium")
        parser.add_argument("--output-root", type=Path, required=True)
        parser.add_argument("--signing-key-file", type=Path, required=True,
                            help="path to 32 raw Ed25519 private-key bytes; NEVER printed or logged")
        parser.add_argument("--version", required=True)
        parser.add_argument("--rollback-floor", default="0.1.0")
        args = parser.parse_args(sys.argv[2:])
        print(json.dumps(finalize_windows_bundle(
            frozen_dir=args.frozen_dir.expanduser().absolute(),
            output_root=args.output_root.expanduser().absolute(),
            signing_key_path=args.signing_key_file, version=args.version,
            rollback_floor=args.rollback_floor)))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=ROOT / "dist/a13-windows-preview.1")
    parser.add_argument("--work-root", type=Path, default=None)
    parser.add_argument(
        "--signing-key-file",
        type=Path,
        required=True,
        help="path to 32 raw Ed25519 private-key bytes; NEVER printed or logged",
    )
    parser.add_argument("--version", default="0.1.0-preview.1")
    parser.add_argument("--rollback-floor", default="0.1.0")
    args = parser.parse_args()
    output = args.output_root.expanduser().absolute()
    work = (args.work_root or (ROOT / "build" / output.name)).expanduser().absolute()
    report = build_windows_bundle(
        output_root=output,
        work_root=work,
        signing_key_path=args.signing_key_file,
        version=args.version,
        rollback_floor=args.rollback_floor,
    )
    print(json.dumps(report))


if __name__ == "__main__":
    main()
