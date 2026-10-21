# SPDX-License-Identifier: AGPL-3.0-only
"""Per-user self-installer for the A13 Linux package (no root, no sudo).

The finalized bundle (``scripts/build_a13_linux.py finalize``), extracted from
``mycelium-<ver>-linux-<arch>.tar.gz``, is::

  <bundle>/mycelium/mycelium ...               the PyInstaller one-folder app (the payload)
  <bundle>/installation-package-manifest.json  Ed25519-signed, package_digest = payload tree digest
  <bundle>/install-source.json                 artifact_digest = payload tree digest
  <bundle>/install.sh                          -> mycelium/mycelium --install --bundle <bundle>

``install`` runs the shared verify-before-write state machine
(:mod:`mycelium_a13_packaging.installer_core`) with the Linux adapter:

* program versions in ``${XDG_DATA_HOME:-~/.local/share}/mycelium/<version>``;
* a launcher script ``~/.local/bin/mycelium`` (a two-line ``exec``, not a
  symlink, so the payload tree stays link-free);
* a desktop entry ``${XDG_DATA_HOME}/applications/org.mycelium.Mycelium.desktop``;
* the owner-only state root ``${XDG_STATE_HOME:-~/.local/state}/mycelium/A13``
  (identity is created later by the app, never here).

It never registers the unattended unit or enables linger -- both are separate,
explicit consents (``mycelium_a13_linux.service_install``).  ``uninstall``
removes the unit (and linger, only if this tool enabled it), the launcher, the
desktop entry and the program versions, and keeps the state unless deletion is
explicit.  Every command goes through :class:`LinuxSeams`.
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

from mycelium_a13_packaging import installer_core as _core

from .install_source import InstallSourceError, verify_install_source
from .package_manifest import PackageManifestError, verify_linux_package
from .platform_admission import ADMITTED_ARCHITECTURES, ADMITTED_DISTRIBUTIONS

PROTOCOL = "mycelium.a13_linux_installer.v1"
PAYLOAD_DIRECTORY = "mycelium"
PAYLOAD_EXECUTABLE = "mycelium"
PUBLISHER_ID = "mycelium-release"
DESKTOP_ENTRY = "org.mycelium.Mycelium.desktop"
_MARKER = "# Mycelium A13 launcher (managed by mycelium --install; removed by --uninstall)"
_ARTIFACT_VERSION = re.compile(r"^mycelium-(\d+\.\d+\.\d+(?:-[a-z0-9.]+)?)-linux-(x86_64|aarch64)$")

InstallerError = _core.InstallerError
_refuse = _core.refuse
payload_digest = _core.payload_digest


def _xdg(environ: Mapping[str, str], name: str, default: str) -> Path:
    value = environ.get(name)
    if value and os.path.isabs(value):
        return Path(value)  # XDG: a relative value is invalid and ignored
    home = environ.get("HOME")
    return (Path(home) if home and os.path.isabs(home) else Path.home()) / default


def read_os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"')
    return values


def _default_observe_platform() -> dict[str, str]:
    release = read_os_release()
    return {"system": _platform.system(), "machine": _platform.machine(),
            "distribution_id": release.get("ID", ""), "distribution_version": release.get("VERSION_ID", "")}


@dataclass
class LinuxSeams:
    """Every OS side effect of install/uninstall; tests replace them."""

    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    observe_platform: Callable[[], dict[str, str]] = _default_observe_platform
    run: Callable[[list[str]], int] = lambda argv: subprocess.run(argv, check=False).returncode
    running_executable: Callable[[], Path] = lambda: Path(sys.executable).resolve()
    now: Callable[[], float] = time.time

    @property
    def data_home(self) -> Path:
        return _xdg(self.environ, "XDG_DATA_HOME", ".local/share")

    @property
    def state_home(self) -> Path:
        return _xdg(self.environ, "XDG_STATE_HOME", ".local/state")

    @property
    def config_home(self) -> Path:
        return _xdg(self.environ, "XDG_CONFIG_HOME", ".config")

    @property
    def home(self) -> Path:
        value = self.environ.get("HOME")
        return Path(value) if value and os.path.isabs(value) else Path.home()

    @property
    def bin_directory(self) -> Path:
        return self.home / ".local" / "bin"

    @property
    def programs_root(self) -> Path:
        return self.data_home / "mycelium"

    @property
    def state_root(self) -> Path:
        return self.state_home / "mycelium" / "A13"

    @property
    def service_package_root(self) -> Path:
        return self.config_home / "mycelium" / "service"

    @property
    def unit_directory(self) -> Path:
        return self.config_home / "systemd" / "user"

    @property
    def launcher(self) -> Path:
        return self.bin_directory / "mycelium"

    @property
    def desktop_entry(self) -> Path:
        return self.data_home / "applications" / DESKTOP_ENTRY


def _write_text(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        _refuse("register_target_is_a_link")
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _ours(path: Path) -> bool:
    try:
        return not path.is_symlink() and _MARKER in path.read_text(encoding="utf-8")
    except OSError:
        return False


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _desktop_escape(value: str) -> str:
    # Desktop Entry Exec quoting: double quotes, with \\ " ` $ escaped.
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return f'"{escaped}"'


class _LinuxPlatform:
    protocol = PROTOCOL
    payload_directory = PAYLOAD_DIRECTORY
    payload_executable = PAYLOAD_EXECUTABLE

    def __init__(self, seams: LinuxSeams) -> None:
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
        declared = ADMITTED_DISTRIBUTIONS.get(observed.get("distribution_id", ""))
        machine = observed.get("machine", "")
        try:
            version_ok = declared is not None and tuple(int(p) for p in observed["distribution_version"].split(".")) \
                >= tuple(int(p) for p in declared["minimum_version"].split("."))
        except (KeyError, ValueError):
            version_ok = False
        if observed.get("system") != "Linux" or machine not in ADMITTED_ARCHITECTURES or not version_ok:
            _refuse("platform_unsupported")
        publisher = manifest.get("publisher_identity") if isinstance(manifest.get("publisher_identity"), dict) else {}
        record = publisher.get("verification_key_record") if isinstance(publisher.get("verification_key_record"), dict) else {}
        key_pin, pin_source, floor_pin = _core.publisher_pin(installed, record.get("verification_key_digest"))
        floor = str(manifest.get("rollback_floor", "0.0.0"))
        try:
            checked = verify_linux_package(
                manifest, expected_publisher=PUBLISHER_ID, expected_architecture=machine,
                expected_distribution=observed["distribution_id"], installed_version=floor,
                rollback_floor_pin=floor_pin, publisher_key_digest_pin=key_pin, now=self.seams.now())
            checked_source = verify_install_source(
                source, expected_publisher=PUBLISHER_ID, expected_architecture=machine,
                expected_distribution=observed["distribution_id"], installed_version=floor,
                rollback_floor_pin=floor_pin, now=self.seams.now())
        except (PackageManifestError, InstallSourceError) as exc:
            _refuse(exc.code)
        if checked_source["install_mode"] != "systemd_user":
            _refuse("install_mode_unsupported")
        match = _ARTIFACT_VERSION.fullmatch(checked_source["artifact_name"])
        if match is None or match.group(2) != machine:
            _refuse("artifact_name_invalid")
        return {"version": match.group(1),
                "expected_digests": (checked["package_digest"], checked_source["artifact_digest"]),
                "rollback_floor": checked["rollback_floor"], "publisher_key_digest": key_pin,
                "publisher_pin_source": pin_source, "source_revision": checked["source_revision"],
                "report": {"distribution": f"{observed['distribution_id']}-{observed['distribution_version']}",
                           "architecture": machine}}

    def register(self, version: str, version_directory: Path) -> dict[str, Any]:
        executable = version_directory / PAYLOAD_DIRECTORY / PAYLOAD_EXECUTABLE
        for path in (self.seams.launcher, self.seams.desktop_entry):
            if path.exists() and not _ours(path):
                _refuse("register_target_not_ours")  # never overwrite a file the user owns
        _write_text(self.seams.launcher,
                    f"#!/bin/sh\n{_MARKER}\nexec {_shell_quote(str(executable))} \"$@\"\n", 0o755)
        _write_text(self.seams.desktop_entry, "\n".join([
            "[Desktop Entry]",
            _MARKER,
            "Type=Application",
            "Name=Mycelium",
            "Comment=Cooperative inference peer (A13)",
            f"Exec={_desktop_escape(str(self.seams.launcher))}",
            "Terminal=false",
            "Categories=Network;",
            "StartupNotify=false",
            "",
        ]), 0o644)
        return {}

    def unregister(self, installed: Mapping[str, Any] | None) -> dict[str, Any]:
        removed = []
        for path in (self.seams.launcher, self.seams.desktop_entry):
            if _ours(path):
                path.unlink()
                removed.append(path.name)
        return {"removed_entries": removed}

    def remove_unattended(self) -> str:
        if not self.seams.service_package_root.exists():
            return "absent"
        from .service_install import ServiceInstallError, execute

        try:
            execute("unregister", self.seams.service_package_root, unit_directory=self.seams.unit_directory,
                    runner=self.seams.run, user=self.seams.environ.get("USER"))
            return "removed"
        except ServiceInstallError as exc:
            return exc.code  # recorded, never blocks removing the program

    def running_executable(self) -> Path:
        return self.seams.running_executable()

    def remove_directory_after_exit(self, path: Path) -> None:
        # Linux can unlink the directory of a running executable; the process keeps its inode.
        shutil.rmtree(path, ignore_errors=True)


def verify_bundle(bundle: Path, seams: LinuxSeams, *, installed: Mapping[str, Any] | None) -> dict[str, Any]:
    return _core.verify_bundle(bundle, _LinuxPlatform(seams), installed=installed)


def install(bundle: str | os.PathLike[str], *, seams: LinuxSeams | None = None) -> dict[str, Any]:
    return _core.install(bundle, _LinuxPlatform(seams or LinuxSeams()))


def rollback(*, seams: LinuxSeams | None = None) -> dict[str, Any]:
    return _core.rollback(_LinuxPlatform(seams or LinuxSeams()))


def uninstall(*, delete_identity_and_state: bool = False, seams: LinuxSeams | None = None) -> dict[str, Any]:
    return _core.uninstall(_LinuxPlatform(seams or LinuxSeams()), delete_identity_and_state=delete_identity_and_state)


def status(*, seams: LinuxSeams | None = None) -> dict[str, Any]:
    return _core.status(_LinuxPlatform(seams or LinuxSeams()))


def main(argv: list[str] | None = None, *, seams: LinuxSeams | None = None) -> int:
    args = _core.cli_parser("mycelium", __doc__.split("\n\n")[0]).parse_args(argv)

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


__all__ = ["LinuxSeams", "InstallerError", "install", "main", "payload_digest", "read_os_release",
           "rollback", "status", "uninstall", "verify_bundle"]
