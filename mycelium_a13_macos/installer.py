# SPDX-License-Identifier: AGPL-3.0-only
"""Per-user self-installer for the A13 macOS package (no admin).

The finalized bundle (``scripts/finalize_a13_macos.py``), extracted from
``mycelium-<ver>-macos-arm64.zip`` by Archive Utility or ``ditto -x -k``, is::

  <bundle>/Mycelium A13 Demo.app                the PyInstaller app (the payload; links kept)
  <bundle>/installation-package-manifest.json   Ed25519-signed, package_digest = link-aware bundle digest
  <bundle>/install-source.json                  artifact_digest = the same digest
  <bundle>/Install Mycelium.command             -> the app's executable --install --bundle <bundle>

``install`` runs the shared verify-before-write state machine
(:mod:`mycelium_a13_packaging.installer_core`) with the macOS adapter:

* the bundle digest is **link-aware** (``mycelium_a13_packaging.build.bundle_tree``):
  a PyInstaller ``.app`` holds relative symlinks; any absolute link or one that
  escapes the bundle is refused;
* the copy is ``/usr/bin/ditto``, which keeps links, modes and **extended
  attributes** -- in particular ``com.apple.quarantine``, so Gatekeeper still
  makes its own first-launch decision.  ``shutil`` would drop extended
  attributes on macOS, which would silently bypass Gatekeeper; this installer
  never strips quarantine;
* versions live in ``~/Library/Application Support/Mycelium/programs/<version>``
  (current + previous, for rollback); ``~/Applications/Mycelium A13.app`` is a
  link to the current version (per user, no admin, no duplicated 255 MB copy);
* the existing state root (``~/Library/Application Support/Mycelium A13 macOS
  Demo``) is never moved and is kept on uninstall unless deletion is explicit.

The pilot is Apple-unsigned (ad-hoc, not notarized) until the owner rules on
Developer ID; the manifest says so with :data:`DEVELOPER_ID_UNSIGNED`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import platform as _platform
import re
import shutil
import subprocess
import sys
import time
from typing import Any

from mycelium_a13_packaging import build as _build
from mycelium_a13_packaging import installer_core as _core

from .install_source import InstallSourceError, verify_macos_install_source
from .package_manifest import PackageManifestError, verify_macos_package

PROTOCOL = "mycelium.a13_macos_installer.v1"
PAYLOAD_DIRECTORY = "Mycelium A13 Demo.app"
PAYLOAD_EXECUTABLE = "Contents/MacOS/Mycelium A13 Demo"
APPLICATIONS_ENTRY = "Mycelium A13.app"
PUBLISHER_ID = "mycelium-release"
_ARTIFACT_VERSION = re.compile(r"^mycelium-(\d+\.\d+\.\d+(?:-[a-z0-9.]+)?)-macos-arm64$")

InstallerError = _core.InstallerError
_refuse = _core.refuse


def bundle_digest(path: Path) -> str:
    try:
        return _build.bundle_digest(path)
    except _build.PayloadError as exc:
        raise InstallerError(exc.code) from None


def _default_observe_platform() -> dict[str, str]:
    return {"system": _platform.system(), "machine": _platform.machine(), "macos_version": _platform.mac_ver()[0]}


def _default_copy(source: Path, destination: Path) -> int:
    return subprocess.run(["/usr/bin/ditto", str(source), str(destination)], check=False).returncode


@dataclass
class MacSeams:
    """Every OS side effect of install/uninstall; tests replace them."""

    home: Path = field(default_factory=Path.home)
    observe_platform: Callable[[], dict[str, str]] = _default_observe_platform
    copy: Callable[[Path, Path], int] = _default_copy
    running_executable: Callable[[], Path] = lambda: Path(sys.executable).resolve()
    now: Callable[[], float] = time.time

    @property
    def programs_root(self) -> Path:
        return self.home / "Library" / "Application Support" / "Mycelium" / "programs"

    @property
    def state_root(self) -> Path:
        return self.home / "Library" / "Application Support" / "Mycelium A13 macOS Demo"

    @property
    def applications_entry(self) -> Path:
        return self.home / "Applications" / APPLICATIONS_ENTRY


class _MacPlatform:
    protocol = PROTOCOL
    payload_directory = PAYLOAD_DIRECTORY
    payload_executable = PAYLOAD_EXECUTABLE

    def __init__(self, seams: MacSeams) -> None:
        self.seams = seams
        self.programs_root = seams.programs_root
        self.state_root = seams.state_root

    def now(self) -> float:
        return self.seams.now()

    def payload_digest(self, path: Path) -> str:
        return _build.bundle_digest(path)

    def copy_payload(self, source: Path, destination: Path) -> None:
        if self.seams.copy(source, destination) != 0:
            shutil.rmtree(destination, ignore_errors=True)
            _refuse("payload_copy_failed")
        os.chmod(destination.parent, 0o700)

    def verify_documents(self, bundle: Path, installed: Mapping[str, Any] | None) -> dict[str, Any]:
        def read(name: str) -> dict[str, Any]:
            path = bundle / name
            if path.is_symlink() or not path.is_file():
                _refuse("bundle_incomplete")
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _refuse("bundle_incomplete")
            if not isinstance(value, dict):
                _refuse("bundle_incomplete")
            return value

        manifest = read("installation-package-manifest.json")
        source = read("install-source.json")
        observed = self.seams.observe_platform()
        if observed.get("system") != "Darwin" or observed.get("machine") != "arm64":
            _refuse("platform_unsupported")
        publisher = manifest.get("publisher_identity") if isinstance(manifest.get("publisher_identity"), dict) else {}
        record = publisher.get("verification_key_record") if isinstance(publisher.get("verification_key_record"), dict) else {}
        key_pin, pin_source, floor_pin = _core.publisher_pin(installed, record.get("verification_key_digest"))
        team = publisher.get("developer_id_team")
        floor = str(manifest.get("rollback_floor", "0.0.0"))
        common = dict(expected_publisher=PUBLISHER_ID, expected_developer_id_team=team, expected_architecture="arm64",
                      observed_macos_version=str(observed.get("macos_version", "")), installed_version=floor,
                      rollback_floor_pin=floor_pin, now=self.seams.now())
        try:
            checked = verify_macos_package(manifest, publisher_key_digest_pin=key_pin, **common)
            checked_source = verify_macos_install_source(source, **common)
        except (PackageManifestError, InstallSourceError) as exc:
            _refuse(exc.code)
        if checked_source["install_mode"] != "launchd_user":
            _refuse("install_mode_unsupported")
        match = _ARTIFACT_VERSION.fullmatch(checked_source["artifact_name"])
        if match is None:
            _refuse("artifact_name_invalid")
        return {"version": match.group(1),
                "expected_digests": (checked["package_digest"], checked_source["artifact_digest"]),
                "rollback_floor": checked["rollback_floor"], "publisher_key_digest": key_pin,
                "publisher_pin_source": pin_source, "source_revision": checked["source_revision"],
                "report": {"developer_id_team": team, "macos_version": observed.get("macos_version")}}

    def _ours(self, entry: Path) -> bool:
        if not entry.is_symlink():
            return False
        target = Path(os.readlink(entry))
        return target.is_absolute() and target.is_relative_to(self.programs_root)

    def register(self, version: str, version_directory: Path) -> dict[str, Any]:
        entry = self.seams.applications_entry
        if (entry.exists() or entry.is_symlink()) and not self._ours(entry):
            _refuse("register_target_not_ours")  # never replace an app the user put there
        entry.parent.mkdir(parents=True, exist_ok=True)
        temporary = entry.with_name("." + entry.name + ".tmp")
        if temporary.is_symlink() or temporary.exists():
            temporary.unlink()
        temporary.symlink_to(version_directory / PAYLOAD_DIRECTORY)
        os.replace(temporary, entry)
        return {}

    def unregister(self, installed: Mapping[str, Any] | None) -> dict[str, Any]:
        entry = self.seams.applications_entry
        if self._ours(entry):
            entry.unlink()
            return {"removed_entries": [APPLICATIONS_ENTRY]}
        return {"removed_entries": []}

    def remove_unattended(self) -> str:
        return "absent"  # the macOS launchd user agent is not installed by this tranche yet

    def running_executable(self) -> Path:
        return self.seams.running_executable()

    def remove_directory_after_exit(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)  # POSIX: a running bundle's directory can be unlinked


def install(bundle: str | os.PathLike[str], *, seams: MacSeams | None = None) -> dict[str, Any]:
    return _core.install(bundle, _MacPlatform(seams or MacSeams()))


def rollback(*, seams: MacSeams | None = None) -> dict[str, Any]:
    return _core.rollback(_MacPlatform(seams or MacSeams()))


def uninstall(*, delete_identity_and_state: bool = False, seams: MacSeams | None = None) -> dict[str, Any]:
    return _core.uninstall(_MacPlatform(seams or MacSeams()), delete_identity_and_state=delete_identity_and_state)


def status(*, seams: MacSeams | None = None) -> dict[str, Any]:
    return _core.status(_MacPlatform(seams or MacSeams()))


def main(argv: list[str] | None = None, *, seams: MacSeams | None = None) -> int:
    args = _core.cli_parser("Mycelium A13 Demo", __doc__.split("\n\n")[0]).parse_args(argv)

    def run() -> dict[str, Any]:
        if args.install:
            # <bundle>/Mycelium A13 Demo.app/Contents/MacOS/<exe> -> <bundle>
            bundle = args.bundle or Path(sys.executable).resolve().parents[3]
            return install(bundle, seams=seams)
        if args.uninstall:
            return uninstall(delete_identity_and_state=args.delete_identity_and_state, seams=seams)
        if args.rollback:
            return rollback(seams=seams)
        return status(seams=seams)

    return _core.emit(PROTOCOL, run)


__all__ = ["APPLICATIONS_ENTRY", "InstallerError", "MacSeams", "bundle_digest", "install", "main",
           "rollback", "status", "uninstall"]
