# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV3 Priority apps: pause sharing automatically while an app the owner listed is running.

Opt-in and local.  Nothing here runs until the owner lists at least one app and acknowledges it; the
list lives in ``priority-apps.json`` in the app state directory.  A match feeds the existing Pause
sharing path (``AdaptiveHostController.set_priority_app``): the wire reason stays ``owner_game_mode``
and no app name is ever published, only shown in the local one-line status.

Observers share one interface (``platform``, ``supported``, ``running_apps()``) so tests inject a
double.  macOS uses NSWorkspace (bundle ids, names, frontmost) and falls back to ``ps`` (names only,
frontmost unknown).  Linux and Windows are stubs until PV29.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final

POLICY_FILENAME: Final = "priority-apps.json"
MAX_POLICY_BYTES: Final = 4096
MAX_APPS: Final = 20
MAX_ENTRY_CHARS: Final = 80
SHOWN_CHARS: Final = 40
SCOPES: Final = ("running", "frontmost")


class PriorityAppsError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AppObservation:
    bundle_id: str | None
    name: str | None
    frontmost: bool = False


# ------------------------------------------------------------------ policy
def _entry(value: object) -> str:
    if type(value) is not str:
        raise PriorityAppsError("priority_apps_invalid")
    text = value.strip()
    if not text or len(text) > MAX_ENTRY_CHARS or not text.isprintable():
        raise PriorityAppsError("priority_apps_invalid")
    return text


def validate_policy(policy: object) -> dict[str, Any]:
    if (not isinstance(policy, dict) or set(policy) != {"schema", "apps", "scope"}
            or policy["schema"] != 1 or type(policy["schema"]) is not int
            or policy["scope"] not in SCOPES or type(policy["apps"]) is not list
            or len(policy["apps"]) > MAX_APPS):
        raise PriorityAppsError("priority_apps_invalid")
    apps: list[str] = []
    seen: set[str] = set()
    for value in policy["apps"]:
        entry = _entry(value)
        if entry.casefold() not in seen:
            seen.add(entry.casefold())
            apps.append(entry)
    return {"schema": 1, "apps": apps, "scope": policy["scope"]}


def read_policy(root: str | os.PathLike[str]) -> dict[str, Any]:
    if sys.platform == "win32":
        return {"schema": 1, "apps": [], "scope": "running"}
    path = Path(root) / POLICY_FILENAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {"schema": 1, "apps": [], "scope": "running"}
    except OSError as error:
        raise PriorityAppsError("priority_apps_invalid") from error
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_POLICY_BYTES):
            raise PriorityAppsError("priority_apps_invalid")
        raw = os.read(fd, MAX_POLICY_BYTES + 1)
    finally:
        os.close(fd)
    try:
        return validate_policy(json.loads(raw))
    except (UnicodeDecodeError, ValueError) as error:
        if isinstance(error, PriorityAppsError):
            raise
        raise PriorityAppsError("priority_apps_invalid") from error


def write_policy(root: str | os.PathLike[str], policy: object) -> None:
    normalized = validate_policy(policy)
    raw = json.dumps(normalized, sort_keys=True).encode("utf-8")
    path = Path(root) / POLICY_FILENAME
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def has_apps(root: str | os.PathLike[str]) -> bool:
    """Whether the owner listed any app (a missing or unreadable file means no)."""
    try:
        return bool(read_policy(root)["apps"])
    except PriorityAppsError:
        return False


# ------------------------------------------------------------------ matching
def _key(text: str | None) -> str | None:
    if not text:
        return None
    text = text.strip()
    if text.casefold().endswith(".app"):
        text = text[:-4]
    return text.casefold() or None


def _shown(text: str) -> str:
    text = "".join(ch if ch.isprintable() else "?" for ch in text.strip())
    return text if len(text) <= SHOWN_CHARS else text[:SHOWN_CHARS - 1] + "…"


def match(policy: dict[str, Any], observations: list[AppObservation]) -> str | None:
    """The display name of the first observed app on the owner's list, else ``None``.

    Exact match (case-insensitive) on the bundle id or the app name; never a substring.
    """
    wanted = {_key(entry) for entry in policy["apps"]}
    wanted.discard(None)
    if not wanted:
        return None
    for seen in observations:
        if policy["scope"] == "frontmost" and not seen.frontmost:
            continue
        if _key(seen.bundle_id) in wanted or _key(seen.name) in wanted:
            label = (seen.name or seen.bundle_id or "").strip()
            if label.casefold().endswith(".app"):
                label = label[:-4]
            return _shown(label) if label else None
    return None


# ------------------------------------------------------------------ observers
def _default_runner(argv: list[str]) -> str:
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          timeout=2.5, check=True, text=True).stdout


def _default_workspace() -> Any:
    from AppKit import NSWorkspace  # type: ignore[import-not-found]  # PyObjC: not always installed
    return NSWorkspace.sharedWorkspace()


class MacOSObserver:
    platform = "macos"
    supported = True

    def __init__(self, *, workspace: Callable[[], Any] = _default_workspace,
                 runner: Callable[[list[str]], str] = _default_runner) -> None:
        self._workspace = workspace
        self._runner = runner

    def running_apps(self) -> list[AppObservation]:
        try:
            workspace = self._workspace()
            rows = []
            for app in workspace.runningApplications():
                if app.activationPolicy() != 0:  # regular apps only: no agents, daemons or the Dock
                    continue
                rows.append(AppObservation(app.bundleIdentifier() or None, app.localizedName() or None,
                                           bool(app.isActive())))
            return rows
        except Exception:  # noqa: BLE001 - NSWorkspace unavailable (no PyObjC, no window server): use ps
            pass
        try:
            text = self._runner(["ps", "-axo", "comm="])
        except Exception as error:  # noqa: BLE001
            raise PriorityAppsError("priority_apps_observer_failed") from error
        rows, seen = [], set()
        for line in text.splitlines():
            match_ = re.search(r"/([^/]+)\.app/Contents/MacOS/", line)
            if match_ and match_.group(1) not in seen:
                seen.add(match_.group(1))
                rows.append(AppObservation(None, match_.group(1), False))
        return rows


class _StubObserver:
    """Same interface, no detection yet (PV29): never reports an app, never pauses."""

    platform = "stub"
    supported = False

    def running_apps(self) -> list[AppObservation]:
        return []


class LinuxObserver(_StubObserver):
    platform = "linux"


class WindowsObserver(_StubObserver):
    platform = "windows"


class UnsupportedObserver(_StubObserver):
    platform = "unsupported"


def observer_for_platform(platform: str) -> Any:
    if platform == "darwin":
        return MacOSObserver()
    if platform.startswith("linux"):
        return LinuxObserver()
    if platform in ("win32", "cygwin"):
        return WindowsObserver()
    return UnsupportedObserver()


# ------------------------------------------------------------------ app seam
def observer_for(app: Any) -> Any:
    injected = getattr(app, "_priority_observer", None)
    if injected is not None:
        return injected
    cached = getattr(app, "_priority_default_observer", None)
    if cached is None:
        cached = observer_for_platform(sys.platform)
        try:
            app._priority_default_observer = cached
        except AttributeError:
            pass
    return cached


def active_app(app: Any) -> str | None:
    """The listed app running now (display name), else ``None``.  Observes only when the list is non-empty.

    A broken observer never pauses sharing; its bounded error is kept for the status.
    """
    try:
        policy = read_policy(app.root)
    except PriorityAppsError:
        return None
    if not policy["apps"]:
        return None
    try:
        found = match(policy, observer_for(app).running_apps())
        app._priority_observer_error = None
        return found
    except Exception:  # noqa: BLE001 - only a bounded code is kept
        try:
            app._priority_observer_error = "priority_apps_observer_failed"
        except AttributeError:
            pass
        return None


# ------------------------------------------------------------------ owner surface
PRIORITY_ROUTES: Final = frozenset({"/api/priority-apps"})
SENTENCE_OFF: Final = "Priority apps: none listed, so Mycelium does not look at which apps are running."
_PLATFORM_NOTE: Final = "This Mac cannot tell which apps are running yet, so Priority apps cannot pause sharing here."


def _error(code: str) -> Exception:
    from .app import DemoError
    return DemoError(code)


def priority_status(app: Any) -> dict[str, Any]:
    """The Priority apps block of /api/status (local only; the list is the owner's own text)."""
    try:
        policy = read_policy(app.root)
        error = None
    except PriorityAppsError as caught:
        policy, error = {"schema": 1, "apps": [], "scope": "running"}, caught.code
    observer = observer_for(app)
    active = None
    if policy["apps"]:
        runtime = getattr(app, "_adaptive_runtime", None)
        if runtime is not None:
            active = runtime.controller.priority_app
    if not policy["apps"]:
        sentence = SENTENCE_OFF
    elif not getattr(observer, "supported", False):
        sentence = _PLATFORM_NOTE
    elif active:
        sentence = f"Priority apps: sharing is paused because {active} is running."
    else:
        word = "in front" if policy["scope"] == "frontmost" else "running"
        sentence = f"Priority apps: sharing pauses automatically while any of {len(policy['apps'])} listed app(s) is {word}."
    return {"apps": list(policy["apps"]), "scope": policy["scope"], "active": active,
            "platform": getattr(observer, "platform", "unknown"), "supported": bool(getattr(observer, "supported", False)),
            "observer_error": getattr(app, "_priority_observer_error", None), "policy_error": error,
            "sentence": sentence}


def priority_action(app: Any, path: str, body: Any) -> dict[str, Any]:
    if path != "/api/priority-apps":
        raise _error("unknown_action")
    if (not isinstance(body, dict) or set(body) != {"apps", "scope", "acknowledged"}
            or type(body["acknowledged"]) is not bool):
        raise _error("invalid_request")
    try:
        policy = validate_policy({"schema": 1, "apps": body["apps"], "scope": body["scope"]})
    except PriorityAppsError:
        raise _error("priority_apps_invalid") from None
    if policy["apps"] and not body["acknowledged"]:
        raise _error("priority_apps_acknowledgement_required")
    if not policy["apps"] and not has_apps(app.root) and not os.path.lexists(os.path.join(app.root, POLICY_FILENAME)):
        return priority_status(app)  # nothing listed and nothing stored: nothing to write
    write_policy(app.root, policy)
    app._priority_observer_error = None
    return priority_status(app)
