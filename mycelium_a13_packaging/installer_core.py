# SPDX-License-Identifier: AGPL-3.0-only
"""Platform-neutral per-user install / rollback / uninstall state machine.

Shared by the Windows (``mycelium_a13_windows.installer``) and Linux
(``mycelium_a13_linux.installer``) self-installers.  The invariants:

* **verify before any write** -- the platform's document checks (manifest
  signature, publisher pin, install source, device platform), then the
  rollback floor against the installed record, then the payload tree digest;
* a versioned install: ``<programs_root>/<version>/<payload>``, copied into a
  ``<version>.partial`` staging directory, re-verified, and only then renamed
  (the whole version directory, atomically) to ``<version>``;  a version
  directory without a valid payload is stale and is moved aside to
  ``<version>.stale-<n>``, never deleted;
* an exclusive lock (``<programs_root>/.install.lock``) around install,
  rollback and uninstall: a second concurrent installer refuses with
  ``install_already_running``;
* every failure is the structured ``{"refused": <code>}`` line, never a traceback;
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
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, NoReturn, Protocol

import mycelium_host_platform

from . import build as _build

_VERSIONS = "versions.json"
_LOCK = ".install.lock"
_PARTIAL = ".partial"
_STALE = ".stale-"
FIRST_USE_PIN = "first_use_owner_private_media"


class InstallerError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


#: Plain-language text for the refusals a person can act on (shown next to the code).
MESSAGES = {
    "install_already_running": "Another Mycelium install, rollback or uninstall is running right now. "
                               "Wait for it to finish (or close its Terminal window), then try again.",
    "install_io_error": "The installer could not read or write a file. Check free disk space and that your "
                        "user folder is writable, then run the installer again; nothing was half-installed.",
    "installer_failed": "The installer stopped on an unexpected problem and changed nothing it had not verified. "
                        "Run the installer again; if it repeats, report the code.",
    "previous_version_missing": "The previous version is no longer on disk, so there is nothing to roll back to.",
    "installation_record_invalid": "The record of installed versions is damaged. Uninstall, then install again.",
}


def refuse(code: str) -> NoReturn:
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
    try:
        value = json.loads(raw)
    except ValueError:
        refuse("installation_record_invalid")
    if not isinstance(value, dict) or value.get("protocol") != platform.protocol or not _record_shaped(value):
        refuse("installation_record_invalid")
    return value


def _record_shaped(value: Mapping[str, Any]) -> bool:
    """The fields install/rollback index into; anything else is a damaged record, not a KeyError."""

    entry = value.get("current")
    previous = value.get("previous")
    return (isinstance(entry, dict) and isinstance(entry.get("version"), str)
            and isinstance(entry.get("payload_digest"), str)
            and (previous is None or (isinstance(previous, dict) and isinstance(previous.get("version"), str)
                                      and isinstance(previous.get("payload_digest"), str)))
            and all(isinstance(value.get(key), str)
                    for key in ("rollback_floor", "publisher_key_digest", "publisher_pin_source")))


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


@contextmanager
def install_lock(platform: InstallerPlatform, *, create: bool) -> Iterator[None]:
    """Exclusive, non-blocking lock for every state-changing action.

    ``<programs_root>/.install.lock`` is an advisory ``flock`` (Windows: the host
    seam's equivalent): the operating system drops it when the holder exits or
    is killed, so a Gatekeeper kill or a crash can never leave it stuck.  A
    second holder refuses at once with ``install_already_running`` rather than
    queueing behind a window the user may have forgotten.  ``create=False``
    (rollback, uninstall) does nothing when there is no programs folder yet.
    """

    root = platform.programs_root
    if not create and not root.is_dir():
        yield
        return
    if create:
        with mycelium_host_platform.private_directory(root, create=True):
            pass
    try:
        held = mycelium_host_platform.hold_exclusive_lock(root, _LOCK, blocking=False)
    except mycelium_host_platform.HostPlatformError as exc:
        if exc.code == "host_lock_busy":
            refuse("install_already_running")
        raise
    with held:
        yield


def _discard_staging(path: Path) -> None:
    """Remove one staging directory (``*.partial`` directly inside the programs root); nothing else."""

    if path.name.endswith(_PARTIAL):
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)


def _discard_all_staging(root: Path) -> None:
    for child in list(root.iterdir()):
        if child.name.endswith(_PARTIAL):
            _discard_staging(child)


def _installed_digest(platform: InstallerPlatform, version_directory: Path) -> str | None:
    """Digest of an installed payload, or ``None`` when there is no valid payload there."""

    payload = version_directory / platform.payload_directory
    if (version_directory.is_symlink() or not version_directory.is_dir() or payload.is_symlink()
            or not (payload / platform.payload_executable).is_file()):
        return None
    try:
        return _digest(platform, payload)
    except (InstallerError, OSError):
        return None


def _move_aside(platform: InstallerPlatform, target: Path) -> Path:
    """Rename ``<version>`` to ``<version>.stale-<n>``; the contents are kept, not deleted."""

    stamp = int(platform.now())
    for attempt in range(1000):
        aside = target.with_name(f"{target.name}{_STALE}{stamp}" + (f"-{attempt}" if attempt else ""))
        if not aside.exists() and not aside.is_symlink():
            os.rename(target, aside)
            return aside
    refuse("install_io_error")


def _finalize_payload(platform: InstallerPlatform, payload: Path) -> dict[str, Any]:
    """Platform hook run on a payload copy whose digest was just verified (never before)."""

    hook = getattr(platform, "finalize_payload", None)
    return dict(hook(payload) or {}) if hook is not None else {}


def _stage_payload(source: Path, staging: Path, expected: str, platform: InstallerPlatform) -> dict[str, Any]:
    """Copy into ``staging`` (``<version>.partial``), verify, finalize; the caller renames it into place."""

    _discard_staging(staging)
    payload = staging / platform.payload_directory
    try:
        with mycelium_host_platform.private_directory(staging, create=True):
            pass
        custom_copy = getattr(platform, "copy_payload", None)
        if custom_copy is not None:
            custom_copy(source, payload)  # e.g. macOS ditto: links and extended attributes kept
        else:
            with mycelium_host_platform.private_directory(payload, create=True):
                pass
            for current, _directories, files in os.walk(source, followlinks=False):
                relative = Path(current).relative_to(source)
                for name in files:
                    destination = payload / relative / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(Path(current) / name, destination)  # keeps the executable bit on POSIX
        if _digest(platform, payload) != expected:
            refuse("payload_copy_mismatch")
        return _finalize_payload(platform, payload)  # only now: the staged copy matches the signed digest
    except Exception:
        _discard_staging(staging)  # a failed copy leaves nothing; KeyboardInterrupt leaves staging for the next run
        raise


def _settle_target(platform: InstallerPlatform, target: Path, checked: Mapping[str, Any],
                   installed: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What to do about an existing ``programs/<version>``.

    Returns the finalize report when the verified copy is already installed; ``None``
    when ``target`` is free (absent, or a stale/damaged directory just moved aside).
    """

    if not target.exists() and not target.is_symlink():
        return None
    digest = _installed_digest(platform, target)
    if digest == checked["payload_digest"]:
        return _finalize_payload(platform, target / platform.payload_directory)
    if digest is not None:
        recorded = {entry["version"]: entry["payload_digest"]
                    for entry in ((installed or {}).get("current"), (installed or {}).get("previous")) if entry}
        if recorded.get(checked["version"]) != checked["payload_digest"]:
            refuse("installed_version_conflict")  # a different package claims an installed version number
        # else: our own recorded package, damaged on disk -> repaired below from the verified bundle
    _move_aside(platform, target)  # no valid payload (or damaged): stale, kept for inspection, never deleted
    return None


def install(bundle: str | os.PathLike[str], platform: InstallerPlatform) -> dict[str, Any]:
    bundle_path = Path(bundle).absolute()
    installed = read_versions(platform)
    checked = verify_bundle(bundle_path, platform, installed=installed)  # nothing written before this
    version = checked["version"]
    with install_lock(platform, create=True):
        again = read_versions(platform)
        if again != installed:  # another installer finished between our read and the lock
            installed = again
            checked = verify_bundle(bundle_path, platform, installed=installed)
            version = checked["version"]
        root = platform.programs_root
        _discard_all_staging(root)  # leftovers of a killed installer; safe: we hold the lock
        target = root / version
        finalized = _settle_target(platform, target, checked, installed)
        if finalized is None:
            staging = root / (version + _PARTIAL)
            finalized = _stage_payload(bundle_path / platform.payload_directory, staging,
                                       checked["payload_digest"], platform)
            os.rename(staging, target)  # the whole version directory appears at once, already verified
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
        for child in root.iterdir():
            if (child.is_dir() and not child.is_symlink() and child.name not in keep
                    and _STALE not in child.name):  # stale directories are kept for inspection
                shutil.rmtree(child, ignore_errors=True)  # at most current + previous are retained
        platform.register(version, target)
        with mycelium_host_platform.private_directory(platform.state_root, create=True):
            pass  # owner-only; identity is created later by the app, never here
    return {"protocol": platform.protocol, "action": "install", "version": version,
            "previous": previous["version"] if previous else None,
            "publisher_key_digest": checked["publisher_key_digest"],
            "publisher_pin_source": checked["publisher_pin_source"],
            **checked.get("report", {}), **finalized,
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
    with install_lock(platform, create=False):
        installed = read_versions(platform)
        if installed is None or not installed.get("previous"):
            refuse("no_previous_version")
        previous = installed["previous"]
        if numeric(previous["version"]) < numeric(installed["rollback_floor"]):
            refuse("package_downgrade_rejected")
        directory = platform.programs_root / previous["version"]
        digest = _installed_digest(platform, directory)
        if digest is None:
            refuse("previous_version_missing")
        if digest != previous["payload_digest"]:
            refuse("previous_version_tampered")
        record = {**installed, "current": previous, "previous": installed["current"], "installed_at": platform.now()}
        _write_versions(platform, record)
        platform.register(previous["version"], directory)
    return {"protocol": platform.protocol, "action": "rollback", "version": previous["version"],
            "previous": installed["current"]["version"]}


def uninstall(platform: InstallerPlatform, *, delete_identity_and_state: bool = False) -> dict[str, Any]:
    with install_lock(platform, create=False):
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
                    child.unlink(missing_ok=True)  # includes the lock file; our descriptor stays valid
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


def _refusal(protocol: str, code: str, **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"protocol": protocol, "refused": code, **extra}
    if code in MESSAGES:
        value["message"] = MESSAGES[code]
    return value


def emit(protocol: str, call) -> int:
    """Run one installer action; every failure is one structured ``refused`` line, never a traceback.

    Unexpected errors are mapped to a closed set of codes with only the exception
    *type* (and the errno name) -- never its message, which can carry paths.
    """

    try:
        result = call()
    except InstallerError as exc:
        print(json.dumps(_refusal(protocol, exc.code)), file=sys.stderr)
        return 2
    except OSError as exc:
        name = errno.errorcode.get(exc.errno) if isinstance(exc.errno, int) else None
        print(json.dumps(_refusal(protocol, "install_io_error", error_type=type(exc).__name__,
                                  **({"errno": name} if name else {}))), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - the last line of defence for a double-clicked installer
        print(json.dumps(_refusal(protocol, "installer_failed", error_type=type(exc).__name__)), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


__all__ = [
    "FIRST_USE_PIN",
    "InstallerError",
    "InstallerPlatform",
    "MESSAGES",
    "cli_parser",
    "emit",
    "install",
    "install_lock",
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
