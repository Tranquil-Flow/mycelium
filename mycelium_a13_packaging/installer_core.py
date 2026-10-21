# SPDX-License-Identifier: AGPL-3.0-only
"""Platform-neutral per-user install / rollback / uninstall state machine.

Shared by the Windows (``mycelium_a13_windows.installer``) and Linux
(``mycelium_a13_linux.installer``) self-installers.  The invariants:

* **verify before any write** -- the platform's document checks (manifest
  signature, publisher pin, install source, device platform), then the
  rollback floor against the installed record, then the payload tree digest;
* a versioned install: ``<programs_root>/<version>/<payload>``, copied into a
  ``.partial`` sibling and re-verified before it takes its name;
* an owner-only ``versions.json`` (current + previous, publisher pin, floor):
  the first install pins the release key it verified, every later install and
  rollback must match;
* rollback re-verifies the retained version and respects the floor;
* uninstall keeps identity and membership state unless deletion is explicit,
  and never claims a coordinator revocation.

Everything the operating system does (shortcuts, registry, desktop entries,
service managers, deferred self-removal) is a :class:`InstallerPlatform` method.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Protocol

import mycelium_host_platform

from . import build as _build

_VERSIONS = "versions.json"
FIRST_USE_PIN = "first_use_owner_private_media"


class InstallerError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def refuse(code: str) -> None:
    raise InstallerError(code)


def payload_tree(root: Path) -> dict[str, str]:
    try:
        return _build.payload_tree(root)
    except _build.PayloadError as exc:
        raise InstallerError(exc.code) from None


def payload_digest(root: Path) -> str:
    try:
        return _build.payload_digest(root)
    except _build.PayloadError as exc:
        raise InstallerError(exc.code) from None


def numeric(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("-", 1)[0].split("."))


class InstallerPlatform(Protocol):
    protocol: str
    programs_root: Path
    state_root: Path
    payload_directory: str
    payload_executable: str

    def now(self) -> float: ...

    def verify_documents(self, bundle: Path, installed: Mapping[str, Any] | None) -> dict[str, Any]:
        """Platform checks, no writes.  Returns ``version``, ``expected_digests``,
        ``rollback_floor``, ``publisher_key_digest``, ``publisher_pin_source``,
        ``source_revision`` and ``report`` (extra install-report fields)."""

    def register(self, version: str, version_directory: Path) -> dict[str, Any]: ...

    def unregister(self, installed: Mapping[str, Any] | None) -> dict[str, Any]: ...

    def remove_unattended(self) -> str: ...

    def running_executable(self) -> Path: ...

    def remove_directory_after_exit(self, path: Path) -> None: ...


def read_versions(platform: InstallerPlatform) -> dict[str, Any] | None:
    root = platform.programs_root
    if not root.exists() and not root.is_symlink():
        return None
    try:
        with mycelium_host_platform.private_directory(root, create=False) as lease:
            raw = lease.read_file(_VERSIONS, max_bytes=64 * 1024)
    except mycelium_host_platform.HostPlatformError as exc:
        if exc.code == "host_file_missing":
            return None
        refuse("installation_record_untrusted")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("protocol") != platform.protocol:
        refuse("installation_record_invalid")
    return value


def _write_versions(platform: InstallerPlatform, record: Mapping[str, Any]) -> None:
    with mycelium_host_platform.private_directory(platform.programs_root, create=True) as lease:
        lease.atomic_replace(_VERSIONS, json.dumps(record, sort_keys=True, indent=2).encode())


def publisher_pin(installed: Mapping[str, Any] | None, offered_key: object) -> tuple[str, str, str]:
    """(key digest pin, pin source, rollback floor pin) for the next verification."""

    if installed is not None:
        return installed["publisher_key_digest"], installed["publisher_pin_source"], installed["rollback_floor"]
    if not isinstance(offered_key, str):
        refuse("package_publisher_invalid")
    return offered_key, FIRST_USE_PIN, "0.0.0"


def _digest(platform: InstallerPlatform, path: Path) -> str:
    """The platform's payload digest (macOS bundles carry links); default: link-free tree."""
    custom = getattr(platform, "payload_digest", None)
    if custom is None:
        return payload_digest(path)
    try:
        return custom(path)
    except _build.PayloadError as exc:
        raise InstallerError(exc.code) from None


def verify_bundle(bundle: Path, platform: InstallerPlatform, *, installed: Mapping[str, Any] | None) -> dict[str, Any]:
    """Every check, no writes.  Returns what install needs."""

    checked = platform.verify_documents(bundle, installed)
    version = checked["version"]
    if installed is not None and numeric(version) < numeric(installed["rollback_floor"]):
        refuse("package_downgrade_rejected")
    payload = bundle / platform.payload_directory
    if payload.is_symlink() or not (payload / platform.payload_executable).is_file():
        refuse("bundle_incomplete")
    digest = _digest(platform, payload)
    if any(digest != expected for expected in checked["expected_digests"]):
        refuse("package_digest_mismatch")
    return {**checked, "payload_digest": digest}


def _copy_payload(source: Path, target: Path, expected: str, platform: InstallerPlatform | None = None) -> None:
    partial = target.with_name(target.name + ".partial")
    if partial.exists():
        shutil.rmtree(partial)
    custom_copy = getattr(platform, "copy_payload", None)
    if custom_copy is not None:
        custom_copy(source, partial)  # e.g. macOS ditto: links and extended attributes kept
    else:
        with mycelium_host_platform.private_directory(partial, create=True):
            pass
        for current, _directories, files in os.walk(source, followlinks=False):
            relative = Path(current).relative_to(source)
            for name in files:
                destination = partial / relative / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(current) / name, destination)  # keeps the executable bit on POSIX
    if (_digest(platform, partial) if platform is not None else payload_digest(partial)) != expected:
        shutil.rmtree(partial, ignore_errors=True)
        refuse("payload_copy_mismatch")
    os.replace(partial, target)


def install(bundle: str | os.PathLike[str], platform: InstallerPlatform) -> dict[str, Any]:
    bundle_path = Path(bundle).absolute()
    installed = read_versions(platform)
    checked = verify_bundle(bundle_path, platform, installed=installed)  # nothing written before this
    version = checked["version"]
    with mycelium_host_platform.private_directory(platform.programs_root, create=True):
        pass
    target = platform.programs_root / version
    if target.exists():
        if _digest(platform, target / platform.payload_directory) != checked["payload_digest"]:
            refuse("installed_version_conflict")
    else:
        with mycelium_host_platform.private_directory(target, create=True):
            pass
        _copy_payload(bundle_path / platform.payload_directory, target / platform.payload_directory,
                      checked["payload_digest"], platform)
    previous = None
    if installed is not None and installed["current"]["version"] != version:
        previous = installed["current"]
    entry = {"version": version, "payload_digest": checked["payload_digest"],
             "rollback_floor": checked["rollback_floor"], "source_revision": checked["source_revision"]}
    record = {"protocol": platform.protocol, "current": entry, "previous": previous,
              "publisher_key_digest": checked["publisher_key_digest"],
              "publisher_pin_source": checked["publisher_pin_source"],
              "rollback_floor": checked["rollback_floor"], "installed_at": platform.now()}
    if installed is not None:
        for key, value in installed.items():  # platform-owned facts (e.g. linger) survive updates
            record.setdefault(key, value)
    _write_versions(platform, record)
    keep = {version} | ({previous["version"]} if previous else set())
    for child in platform.programs_root.iterdir():
        if child.is_dir() and not child.is_symlink() and child.name not in keep:
            shutil.rmtree(child, ignore_errors=True)  # at most current + previous are retained
    platform.register(version, target)
    with mycelium_host_platform.private_directory(platform.state_root, create=True):
        pass  # owner-only; identity is created later by the app, never here
    return {"protocol": platform.protocol, "action": "install", "version": version,
            "previous": previous["version"] if previous else None,
            "publisher_key_digest": checked["publisher_key_digest"],
            "publisher_pin_source": checked["publisher_pin_source"],
            **checked.get("report", {}),
            "identity_created": False, "unattended_task_registered": False}


def update_record(platform: InstallerPlatform, **changes: Any) -> dict[str, Any]:
    """Record a platform-owned fact (e.g. that the installer enabled linger)."""

    installed = read_versions(platform)
    if installed is None:
        refuse("not_installed")
    record = {**installed, **changes}
    _write_versions(platform, record)
    return record


def rollback(platform: InstallerPlatform) -> dict[str, Any]:
    installed = read_versions(platform)
    if installed is None or not installed.get("previous"):
        refuse("no_previous_version")
    previous = installed["previous"]
    if numeric(previous["version"]) < numeric(installed["rollback_floor"]):
        refuse("package_downgrade_rejected")
    directory = platform.programs_root / previous["version"]
    if _digest(platform, directory / platform.payload_directory) != previous["payload_digest"]:
        refuse("previous_version_tampered")
    record = {**installed, "current": previous, "previous": installed["current"], "installed_at": platform.now()}
    _write_versions(platform, record)
    platform.register(previous["version"], directory)
    return {"protocol": platform.protocol, "action": "rollback", "version": previous["version"],
            "previous": installed["current"]["version"]}


def uninstall(platform: InstallerPlatform, *, delete_identity_and_state: bool = False) -> dict[str, Any]:
    installed = read_versions(platform)
    task = platform.remove_unattended()
    extra = platform.unregister(installed) or {}
    deferred = None
    running = platform.running_executable()
    root = platform.programs_root
    if root.exists():
        for child in sorted(root.iterdir()):
            if child.is_dir() and running.is_relative_to(child.resolve()):
                deferred = child  # the running executable; removed after this process exits
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        if deferred is None:
            shutil.rmtree(root, ignore_errors=True)
        else:
            platform.remove_directory_after_exit(root)
    state = "retained"
    if delete_identity_and_state:
        shutil.rmtree(platform.state_root, ignore_errors=True)
        state = "deleted"
    return {"protocol": platform.protocol, "action": "uninstall",
            "version": installed["current"]["version"] if installed else None,
            "unattended_task": task, "state": state, "state_root": str(platform.state_root),
            "running_version_removed_after_exit": deferred is not None,
            "coordinator_revocation_claimed": False, **extra}


def status(platform: InstallerPlatform) -> dict[str, Any]:
    installed = read_versions(platform)
    return {"protocol": platform.protocol, "installed": installed is not None,
            "current": installed["current"]["version"] if installed else None,
            "previous": (installed.get("previous") or {}).get("version") if installed else None,
            "publisher_key_digest": installed["publisher_key_digest"] if installed else None}


def cli_parser(prog: str, description: str, *, extra_flags: tuple[tuple[str, str], ...] = ()) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=description)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--install", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    action.add_argument("--rollback", action="store_true")
    action.add_argument("--install-status", action="store_true")
    parser.add_argument("--bundle", type=Path, default=None,
                        help="the extracted bundle (default: the folder above this executable)")
    parser.add_argument("--delete-identity-and-state", action="store_true",
                        help="uninstall also deletes identity and membership state (not recoverable)")
    for flag, help_text in extra_flags:
        parser.add_argument(flag, action="store_true", help=help_text)
    return parser


def emit(protocol: str, call) -> int:
    try:
        result = call()
    except InstallerError as exc:
        print(json.dumps({"protocol": protocol, "refused": exc.code}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


__all__ = [
    "FIRST_USE_PIN",
    "InstallerError",
    "InstallerPlatform",
    "cli_parser",
    "emit",
    "install",
    "numeric",
    "payload_digest",
    "payload_tree",
    "publisher_pin",
    "read_versions",
    "refuse",
    "rollback",
    "status",
    "uninstall",
    "update_record",
    "verify_bundle",
]
