# SPDX-License-Identifier: AGPL-3.0-only
"""Per-user self-installer for the A13 Windows package (no administrator rights).

The finalized bundle (``scripts/build_a13_windows.py finalize``) is::

  <bundle>\\Mycelium\\Mycelium.exe ...          the PyInstaller one-folder app (the payload)
  <bundle>\\installation-package-manifest.json  Ed25519-signed, package_digest = payload tree digest
  <bundle>\\install-source.json                 artifact_digest = payload tree digest
  <bundle>\\Install Mycelium.cmd                -> Mycelium\\Mycelium.exe --install

``install`` verifies the manifest signature, the publisher key pin, the
install source, the device platform, the rollback floor and the payload tree
digest **before writing anything**; then copies the payload into
``%LOCALAPPDATA%\\Programs\\Mycelium\\<version>`` (re-verifying the copy),
records ``versions.json`` (current + previous, for rollback), writes the
per-user Start-menu shortcut and the ``HKCU`` Apps & features entry, and
creates the owner-only state directory ``%LOCALAPPDATA%\\Mycelium\\A13``.
It never creates identity (the app does, after this verification) and never
registers the unattended task (an explicit, separate consent:
``mycelium_a13_windows.service_install``).

Publisher trust: the first install pins the release key digest it verified
(``publisher_pin_source: first_use_owner_private_media``): without an
Authenticode certificate (owner ruling 2026-09-27 #4) nothing on the device can
anchor the first key, so the owner's private-media hand-over is the anchor and
the zip SHA-256 the owner sends is compared before extraction.  Every later
install, update and rollback must match that pinned digest.

``uninstall`` stops and removes the unattended task when present, removes the
shortcut, the uninstall entry and every program version, and **keeps the state
directory (identity, membership)** unless ``delete_identity_and_state=True``.

Every operating-system side effect goes through :class:`HostSeams`, so the flow
is tested on the fake win32 API; the real bodies (``winreg``, a ``.lnk`` via
PowerShell ``WScript.Shell``, a detached ``cmd /c rmdir`` for the running
version) run for the first time on the device.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

from mycelium_a13_packaging import installer_core as _core

from .install_source import (
    AUTHENTICODE_UNSIGNED_SUBJECT,
    InstallSourceError,
    verify_windows_install_source,
)
from .package_manifest import PackageManifestError, verify_windows_package
from .platform_admission import ADMITTED_ARCHITECTURES, ADMITTED_RELEASES

PROTOCOL = "mycelium.a13_windows_installer.v1"
PAYLOAD_DIRECTORY = "Mycelium"
PAYLOAD_EXECUTABLE = "Mycelium.exe"
PUBLISHER_ID = "mycelium-release"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Mycelium"
_ARTIFACT_VERSION = re.compile(r"^mycelium-(\d+\.\d+\.\d+(?:-[a-z0-9.]+)?)-windows-x86_64$")
_VERSIONS = "versions.json"


InstallerError = _core.InstallerError
_refuse = _core.refuse
payload_tree = _core.payload_tree
payload_digest = _core.payload_digest


# ----------------------------------------------------------------- host seams


def _default_observe_platform() -> dict[str, str]:
    version = sys.getwindowsversion()  # type: ignore[attr-defined]
    machine = os.environ.get("PROCESSOR_ARCHITECTURE", "")
    return {"machine": "x86_64" if machine.upper() == "AMD64" else machine,
            "windows_version": f"{version.major}.{version.minor}", "windows_build": str(version.build)}


def _default_set_uninstall(values: Mapping[str, Any]) -> None:
    import winreg  # noqa: PLC0415 - Windows only

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        for name, value in values.items():
            kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
            winreg.SetValueEx(key, name, 0, kind, value)


def _default_delete_uninstall() -> None:
    import winreg  # noqa: PLC0415 - Windows only

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except FileNotFoundError:
        pass


def _default_create_shortcut(link: Path, target: Path, working_directory: Path) -> None:
    def quoted(value: Path) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut(" + quoted(link) + ");"
              "$s.TargetPath=" + quoted(target) + ";$s.WorkingDirectory=" + quoted(working_directory) + ";$s.Save()")
    link.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], check=True)


def _default_remove_directory_after_exit(path: Path) -> None:
    # The running version's Mycelium.exe cannot delete itself on Windows.
    subprocess.Popen(  # noqa: S603 - fixed argv, no shell string from input
        ["cmd.exe", "/d", "/c", "ping -n 4 127.0.0.1 >nul & rmdir /s /q", str(path)],
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True,
    )


def _local_app_data() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    return Path(value) if value else Path.home() / "AppData" / "Local"


@dataclass
class HostSeams:
    """Every OS side effect of install/uninstall; the fake win32 tests replace them."""

    programs_root: Path = field(default_factory=lambda: _local_app_data() / "Programs" / "Mycelium")
    state_root: Path = field(default_factory=lambda: _local_app_data() / "Mycelium" / "A13")
    service_package_root: Path = field(default_factory=lambda: _local_app_data() / "Mycelium" / "service")
    shortcut: Path = field(default_factory=lambda: Path(os.environ.get("APPDATA", str(Path.home())))
                           / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Mycelium.lnk")
    observe_platform: Callable[[], dict[str, str]] = _default_observe_platform
    set_uninstall_entry: Callable[[Mapping[str, Any]], None] = _default_set_uninstall
    delete_uninstall_entry: Callable[[], None] = _default_delete_uninstall
    create_shortcut: Callable[[Path, Path, Path], None] = _default_create_shortcut
    remove_shortcut: Callable[[Path], None] = lambda link: link.unlink(missing_ok=True)
    remove_directory_after_exit: Callable[[Path], None] = _default_remove_directory_after_exit
    running_executable: Callable[[], Path] = lambda: Path(sys.executable).resolve()
    run: Callable[[list[str]], int] = lambda argv: subprocess.run(argv, check=False).returncode
    now: Callable[[], float] = time.time


# ----------------------------------------------------------------- platform adapter


class _WindowsPlatform:
    """The Windows side of :mod:`mycelium_a13_packaging.installer_core`."""

    protocol = PROTOCOL
    payload_directory = PAYLOAD_DIRECTORY
    payload_executable = PAYLOAD_EXECUTABLE

    def __init__(self, seams: HostSeams) -> None:
        self.seams = seams
        self.programs_root = seams.programs_root
        self.state_root = seams.state_root

    def now(self) -> float:
        return self.seams.now()

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
        declared = ADMITTED_RELEASES["windows"]
        if (observed.get("machine") not in ADMITTED_ARCHITECTURES
                or not str(observed.get("windows_build", "")).isdigit()
                or int(observed["windows_build"]) < int(declared["minimum_build"])):
            _refuse("platform_unsupported")
        publisher = manifest.get("publisher_identity") if isinstance(manifest.get("publisher_identity"), dict) else {}
        record = publisher.get("verification_key_record") if isinstance(publisher.get("verification_key_record"), dict) else {}
        key_pin, pin_source, floor_pin = _core.publisher_pin(installed, record.get("verification_key_digest"))
        authenticode = publisher.get("authenticode_subject", AUTHENTICODE_UNSIGNED_SUBJECT)
        common = dict(expected_publisher=PUBLISHER_ID, expected_authenticode_subject=authenticode,
                      expected_architecture="x86_64", expected_windows_version=declared["minimum_version"],
                      expected_windows_build=declared["minimum_build"], rollback_floor_pin=floor_pin,
                      now=self.seams.now())
        try:
            floor = manifest.get("rollback_floor", "0.0.0")
            checked = verify_windows_package(manifest, publisher_key_digest_pin=key_pin,
                                             installed_version=str(floor), **common)
            checked_source = verify_windows_install_source(source, installed_version=str(floor), **common)
        except (PackageManifestError, InstallSourceError) as exc:
            _refuse(getattr(exc, "code", None) or str(exc).split(":")[0] or "package_invalid")
        if checked_source["install_mode"] != "windows_service_per_user":
            _refuse("install_mode_unsupported")
        match = _ARTIFACT_VERSION.fullmatch(checked_source["artifact_name"])
        if match is None:
            _refuse("artifact_name_invalid")
        return {"version": match.group(1),
                "expected_digests": (checked["package_digest"], checked_source["artifact_digest"]),
                "rollback_floor": checked["rollback_floor"], "publisher_key_digest": key_pin,
                "publisher_pin_source": pin_source, "source_revision": checked["source_revision"],
                "authenticode_subject": authenticode, "report": {"authenticode_subject": authenticode}}

    def register(self, version: str, version_directory: Path) -> dict[str, Any]:
        executable = version_directory / PAYLOAD_DIRECTORY / PAYLOAD_EXECUTABLE
        self.seams.create_shortcut(self.seams.shortcut, executable, version_directory / PAYLOAD_DIRECTORY)
        self.seams.set_uninstall_entry({
            "DisplayName": "Mycelium", "DisplayVersion": version, "Publisher": PUBLISHER_ID,
            "InstallLocation": str(version_directory), "DisplayIcon": str(executable),
            "UninstallString": f'"{executable}" --uninstall', "NoModify": 1, "NoRepair": 1,
            "EstimatedSize": 0,
        })
        return {}

    def unregister(self, installed: Mapping[str, Any] | None) -> dict[str, Any]:
        self.seams.remove_shortcut(self.seams.shortcut)
        self.seams.delete_uninstall_entry()
        return {}

    def remove_unattended(self) -> str:
        if not self.seams.service_package_root.exists():
            return "absent"
        from .service_install import ServiceInstallError, execute

        try:
            execute("unregister", self.seams.service_package_root, runner=self.seams.run)
            return "removed"
        except ServiceInstallError as exc:
            return exc.code  # recorded, never blocks removing the program

    def running_executable(self) -> Path:
        return self.seams.running_executable()

    def remove_directory_after_exit(self, path: Path) -> None:
        self.seams.remove_directory_after_exit(path)


def verify_bundle(bundle: Path, seams: HostSeams, *, installed: Mapping[str, Any] | None) -> dict[str, Any]:
    """Every check, no writes.  Returns what install needs."""

    return _core.verify_bundle(bundle, _WindowsPlatform(seams), installed=installed)


def install(bundle: str | os.PathLike[str], *, seams: HostSeams | None = None) -> dict[str, Any]:
    return _core.install(bundle, _WindowsPlatform(seams or HostSeams()))


def rollback(*, seams: HostSeams | None = None) -> dict[str, Any]:
    return _core.rollback(_WindowsPlatform(seams or HostSeams()))


def uninstall(*, delete_identity_and_state: bool = False, seams: HostSeams | None = None) -> dict[str, Any]:
    return _core.uninstall(_WindowsPlatform(seams or HostSeams()),
                           delete_identity_and_state=delete_identity_and_state)


def status(*, seams: HostSeams | None = None) -> dict[str, Any]:
    return _core.status(_WindowsPlatform(seams or HostSeams()))


def main(argv: list[str] | None = None, *, seams: HostSeams | None = None) -> int:
    args = _core.cli_parser("Mycelium.exe", __doc__.split("\n\n")[0]).parse_args(argv)

    def run() -> dict[str, Any]:
        if args.install:
            bundle = args.bundle or Path(sys.executable).resolve().parent.parent
            return install(bundle, seams=seams)
        if args.uninstall:
            return uninstall(delete_identity_and_state=args.delete_identity_and_state, seams=seams)
        if args.rollback:
            return rollback(seams=seams)
        return status(seams=seams)

    return _core.emit(PROTOCOL, run)


__all__ = ["HostSeams", "InstallerError", "install", "main", "payload_digest", "payload_tree",
           "rollback", "status", "uninstall", "verify_bundle"]
