# SPDX-License-Identifier: AGPL-3.0-or-later
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

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any

import mycelium_host_platform
from mycelium_a13_packaging import build as _build

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


class InstallerError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _refuse(code: str) -> None:
    raise InstallerError(code)


# ----------------------------------------------------------------- payload digest


def payload_tree(root: Path) -> dict[str, str]:
    """``{posix relative path: sha256}`` of every regular file; links are refused."""

    try:
        return _build.payload_tree(root)
    except _build.PayloadError as exc:
        raise InstallerError(exc.code) from None


def payload_digest(root: Path) -> str:
    """The digest the manifest's ``package_digest`` binds: canonical JSON of the tree."""

    try:
        return _build.payload_digest(root)
    except _build.PayloadError as exc:
        raise InstallerError(exc.code) from None


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


# ----------------------------------------------------------------- versions record


def _versions(seams: HostSeams) -> dict[str, Any] | None:
    if not seams.programs_root.exists() and not seams.programs_root.is_symlink():
        return None
    try:
        with mycelium_host_platform.private_directory(seams.programs_root, create=False) as lease:
            raw = lease.read_file(_VERSIONS, max_bytes=64 * 1024)
    except mycelium_host_platform.HostPlatformError as exc:
        if exc.code == "host_file_missing":
            return None
        _refuse("installation_record_untrusted")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("protocol") != PROTOCOL:
        _refuse("installation_record_invalid")
    return value


def _write_versions(seams: HostSeams, record: Mapping[str, Any]) -> None:
    with mycelium_host_platform.private_directory(seams.programs_root, create=True) as lease:
        lease.atomic_replace(_VERSIONS, json.dumps(record, sort_keys=True, indent=2).encode())


def _numeric(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("-", 1)[0].split("."))


# ----------------------------------------------------------------- verify


def verify_bundle(bundle: Path, seams: HostSeams, *, installed: Mapping[str, Any] | None) -> dict[str, Any]:
    """Every check, no writes.  Returns what install needs."""

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
    observed = seams.observe_platform()
    declared = ADMITTED_RELEASES["windows"]
    if (observed.get("machine") not in ADMITTED_ARCHITECTURES
            or not str(observed.get("windows_build", "")).isdigit()
            or int(observed["windows_build"]) < int(declared["minimum_build"])):
        _refuse("platform_unsupported")
    publisher = manifest.get("publisher_identity") if isinstance(manifest.get("publisher_identity"), dict) else {}
    record = publisher.get("verification_key_record") if isinstance(publisher.get("verification_key_record"), dict) else {}
    offered_key = record.get("verification_key_digest")
    if installed is not None:
        key_pin, pin_source = installed["publisher_key_digest"], installed["publisher_pin_source"]
        floor_pin, current_version = installed["rollback_floor"], installed["current"]["version"]
    else:
        key_pin, pin_source = offered_key, "first_use_owner_private_media"
        floor_pin, current_version = "0.0.0", None
    if not isinstance(key_pin, str):
        _refuse("package_publisher_invalid")
    authenticode = publisher.get("authenticode_subject", AUTHENTICODE_UNSIGNED_SUBJECT)
    common = dict(expected_publisher=PUBLISHER_ID, expected_authenticode_subject=authenticode,
                  expected_architecture="x86_64", expected_windows_version=declared["minimum_version"],
                  expected_windows_build=declared["minimum_build"], rollback_floor_pin=floor_pin,
                  now=seams.now())
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
    version = match.group(1)
    if current_version is not None and _numeric(version) < _numeric(installed["rollback_floor"]):
        _refuse("package_downgrade_rejected")
    payload = bundle / PAYLOAD_DIRECTORY
    if payload.is_symlink() or not (payload / PAYLOAD_EXECUTABLE).is_file():
        _refuse("bundle_incomplete")
    digest = payload_digest(payload)
    if digest != checked["package_digest"] or digest != checked_source["artifact_digest"]:
        _refuse("package_digest_mismatch")
    return {"version": version, "payload_digest": digest, "rollback_floor": checked["rollback_floor"],
            "publisher_key_digest": key_pin, "publisher_pin_source": pin_source,
            "authenticode_subject": authenticode, "source_revision": checked["source_revision"]}


# ----------------------------------------------------------------- install


def _copy_payload(source: Path, target: Path, expected: str) -> None:
    partial = target.with_name(target.name + ".partial")
    if partial.exists():
        shutil.rmtree(partial)
    with mycelium_host_platform.private_directory(partial, create=True):
        pass
    for current, _directories, files in os.walk(source, followlinks=False):
        relative = Path(current).relative_to(source)
        for name in files:
            destination = partial / relative / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(current) / name, destination)
    if payload_digest(partial) != expected:
        shutil.rmtree(partial, ignore_errors=True)
        _refuse("payload_copy_mismatch")
    os.replace(partial, target)


def _register(seams: HostSeams, version: str, record: Mapping[str, Any]) -> None:
    directory = seams.programs_root / version
    executable = directory / PAYLOAD_DIRECTORY / PAYLOAD_EXECUTABLE
    seams.create_shortcut(seams.shortcut, executable, directory / PAYLOAD_DIRECTORY)
    seams.set_uninstall_entry({
        "DisplayName": "Mycelium", "DisplayVersion": version, "Publisher": PUBLISHER_ID,
        "InstallLocation": str(directory), "DisplayIcon": str(executable),
        "UninstallString": f'"{executable}" --uninstall', "NoModify": 1, "NoRepair": 1,
        "EstimatedSize": 0,
    })


def install(bundle: str | os.PathLike[str], *, seams: HostSeams | None = None) -> dict[str, Any]:
    seams = seams or HostSeams()
    bundle_path = Path(bundle).absolute()
    installed = _versions(seams)
    checked = verify_bundle(bundle_path, seams, installed=installed)  # nothing written before this
    version = checked["version"]
    with mycelium_host_platform.private_directory(seams.programs_root, create=True):
        pass
    target = seams.programs_root / version
    if target.exists():
        if payload_digest(target / PAYLOAD_DIRECTORY) != checked["payload_digest"]:
            _refuse("installed_version_conflict")
    else:
        target_payload = target / PAYLOAD_DIRECTORY
        with mycelium_host_platform.private_directory(target, create=True):
            pass
        _copy_payload(bundle_path / PAYLOAD_DIRECTORY, target_payload, checked["payload_digest"])
    previous = None
    if installed is not None and installed["current"]["version"] != version:
        previous = installed["current"]
    entry = {"version": version, "payload_digest": checked["payload_digest"],
             "rollback_floor": checked["rollback_floor"], "source_revision": checked["source_revision"]}
    record = {"protocol": PROTOCOL, "current": entry, "previous": previous,
              "publisher_key_digest": checked["publisher_key_digest"],
              "publisher_pin_source": checked["publisher_pin_source"],
              "rollback_floor": checked["rollback_floor"], "installed_at": seams.now()}
    _write_versions(seams, record)
    keep = {version} | ({previous["version"]} if previous else set())
    for child in seams.programs_root.iterdir():
        if child.is_dir() and not child.is_symlink() and child.name not in keep:
            shutil.rmtree(child, ignore_errors=True)  # at most current + previous are retained
    _register(seams, version, record)
    with mycelium_host_platform.private_directory(seams.state_root, create=True):
        pass  # owner-only; identity is created later by the app, never here
    return {"protocol": PROTOCOL, "action": "install", "version": version,
            "previous": previous["version"] if previous else None,
            "publisher_key_digest": checked["publisher_key_digest"],
            "publisher_pin_source": checked["publisher_pin_source"],
            "authenticode_subject": checked["authenticode_subject"],
            "identity_created": False, "unattended_task_registered": False}


def rollback(*, seams: HostSeams | None = None) -> dict[str, Any]:
    seams = seams or HostSeams()
    installed = _versions(seams)
    if installed is None or not installed.get("previous"):
        _refuse("no_previous_version")
    previous = installed["previous"]
    if _numeric(previous["version"]) < _numeric(installed["rollback_floor"]):
        _refuse("package_downgrade_rejected")
    if payload_digest(seams.programs_root / previous["version"] / PAYLOAD_DIRECTORY) != previous["payload_digest"]:
        _refuse("previous_version_tampered")
    record = {**installed, "current": previous, "previous": installed["current"], "installed_at": seams.now()}
    _write_versions(seams, record)
    _register(seams, previous["version"], record)
    return {"protocol": PROTOCOL, "action": "rollback", "version": previous["version"],
            "previous": installed["current"]["version"]}


def uninstall(*, delete_identity_and_state: bool = False, seams: HostSeams | None = None) -> dict[str, Any]:
    seams = seams or HostSeams()
    installed = _versions(seams)
    task = "absent"
    if seams.service_package_root.exists():
        from .service_install import ServiceInstallError, execute

        try:
            execute("unregister", seams.service_package_root, runner=seams.run)
            task = "removed"
        except ServiceInstallError as exc:
            task = exc.code  # recorded, never blocks removing the program
    seams.remove_shortcut(seams.shortcut)
    seams.delete_uninstall_entry()
    deferred = None
    running = seams.running_executable()
    if seams.programs_root.exists():
        for child in sorted(seams.programs_root.iterdir()):
            if child.is_dir() and running.is_relative_to(child.resolve()):
                deferred = child  # the running Mycelium.exe; removed after this process exits
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        if deferred is None:
            shutil.rmtree(seams.programs_root, ignore_errors=True)
        else:
            seams.remove_directory_after_exit(seams.programs_root)
    state = "retained"
    if delete_identity_and_state:
        shutil.rmtree(seams.state_root, ignore_errors=True)
        state = "deleted"
    return {"protocol": PROTOCOL, "action": "uninstall",
            "version": installed["current"]["version"] if installed else None,
            "unattended_task": task, "state": state, "state_root": str(seams.state_root),
            "running_version_removed_after_exit": deferred is not None,
            "coordinator_revocation_claimed": False}


def status(*, seams: HostSeams | None = None) -> dict[str, Any]:
    seams = seams or HostSeams()
    installed = _versions(seams)
    return {"protocol": PROTOCOL, "installed": installed is not None,
            "current": installed["current"]["version"] if installed else None,
            "previous": (installed.get("previous") or {}).get("version") if installed else None,
            "publisher_key_digest": installed["publisher_key_digest"] if installed else None}


def main(argv: list[str] | None = None, *, seams: HostSeams | None = None) -> int:
    parser = argparse.ArgumentParser(prog="Mycelium.exe", description=__doc__.split("\n\n")[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--install", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    action.add_argument("--rollback", action="store_true")
    action.add_argument("--install-status", action="store_true")
    parser.add_argument("--bundle", type=Path, default=None,
                        help="the extracted bundle (default: the folder above this Mycelium.exe)")
    parser.add_argument("--delete-identity-and-state", action="store_true",
                        help="uninstall also deletes identity and membership state (not recoverable)")
    args = parser.parse_args(argv)
    try:
        if args.install:
            bundle = args.bundle or Path(sys.executable).resolve().parent.parent
            result = install(bundle, seams=seams)
        elif args.uninstall:
            result = uninstall(delete_identity_and_state=args.delete_identity_and_state, seams=seams)
        elif args.rollback:
            result = rollback(seams=seams)
        else:
            result = status(seams=seams)
    except InstallerError as exc:
        print(json.dumps({"protocol": PROTOCOL, "refused": exc.code}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


__all__ = ["HostSeams", "InstallerError", "install", "main", "payload_digest", "payload_tree",
           "rollback", "status", "uninstall", "verify_bundle"]
