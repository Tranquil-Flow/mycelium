# SPDX-License-Identifier: AGPL-3.0-only
"""Health probes for the demo watchdog: bounded, idempotent, read-only, fail-closed.

Three probes, each a small ``run() -> ProbeResult`` object so a test can replace
it outright:

``SSHProbe``
    Laptop reachability through a bounded ``ssh`` (BatchMode, ConnectTimeout),
    the same shape the round-bringup scripts use.
``ProcessProbe``
    Local node/seed process liveness, read from ``ps`` rather than signalled.
``HTTPHealthProbe``
    Origin health read through the private-capability header shape the renewal
    observer uses (``/__mycelium/live-status``), i.e. ``route_ready`` and alive
    and not simulated and no incidents.

Failure discipline: a probe outcome is ``True`` (healthy), ``False`` (observed
unhealthy) or ``None`` (could not be determined).  Only ``True`` counts as
healthy -- an unknown is never a health claim.  No probe raises; no probe writes.
"""
from __future__ import annotations

from mycelium_http_identity import with_user_agent
import json
import os
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

__all__ = [
    "HTTPHealthProbe",
    "HealthReport",
    "ProbeResult",
    "ProcessProbe",
    "SCRUB_ENV_KEYS",
    "SSHProbe",
    "health_from_status",
    "private_capability_headers",
    "probe_once",
    "scrubbed_env",
]

#: Variables the program-dir discipline requires to be absent from every child.
SCRUB_ENV_KEYS: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "PYTHONPATH",
    "PYTHONHOME",
    "VIRTUAL_ENV",
)

REMOTE_SHELL_MARKER = "REMOTE_SHELL_REACHED"
_MAX_BODY_BYTES = 400_000


def scrubbed_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of ``env`` (or ``os.environ``) with the named poison removed."""

    base = dict(os.environ if env is None else env)
    for name in SCRUB_ENV_KEYS:
        base.pop(name, None)
    base["PYTHONDONTWRITEBYTECODE"] = "1"
    base["GIT_OPTIONAL_LOCKS"] = "0"
    return base


@dataclass(frozen=True)
class ProbeResult:
    """``ok`` is ``True``, ``False`` or ``None`` (undetermined)."""

    ok: bool | None
    detail: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class HealthReport:
    """The composed probe report; ``all_green`` demands strictly ``True``."""

    device_reachable: bool | None
    local_process_alive: bool | None
    origin_healthy: bool | None
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def all_green(self) -> bool:
        return all(
            value is True
            for value in (self.device_reachable, self.local_process_alive, self.origin_healthy)
        )

    @property
    def unknowns(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, value in (
                ("device_reachable", self.device_reachable),
                ("local_process_alive", self.local_process_alive),
                ("origin_healthy", self.origin_healthy),
            )
            if value is None
        )


def _as_result(candidate: object, *, origin: str) -> ProbeResult:
    if isinstance(candidate, ProbeResult):
        return candidate
    return ProbeResult(None, {"origin": origin}, "probe_returned_non_result")


def probe_once(
    *,
    device: Any,
    process: Any,
    origin: Any,
) -> HealthReport:
    """Run the three probes and compose a report.  Never raises."""

    results: dict[str, ProbeResult] = {}
    for name, probe in (("device", device), ("process", process), ("origin", origin)):
        try:
            results[name] = _as_result(probe.run(), origin=name)
        except BaseException as exc:  # noqa: BLE001 - a probe must never take the loop down
            results[name] = ProbeResult(None, {"error_type": type(exc).__name__}, "probe_raised")
    return HealthReport(
        device_reachable=results["device"].ok,
        local_process_alive=results["process"].ok,
        origin_healthy=results["origin"].ok,
        detail={name: dict(result.detail) for name, result in results.items()},
    )


class SSHProbe:
    """Bounded, read-only reachability check of the peer Mac."""

    def __init__(
        self,
        target: str,
        key_path: str | os.PathLike[str],
        *,
        connect_timeout: float = 6.0,
        total_timeout: float = 8.0,
        runner: Callable[..., Any] = subprocess.run,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.target = target
        self.key_path = str(key_path)
        self.connect_timeout = float(connect_timeout)
        self.total_timeout = float(total_timeout)
        self.runner = runner
        self.env = env

    def argv(self) -> tuple[str, ...]:
        return (
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={int(self.connect_timeout)}",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-i",
            self.key_path,
            self.target,
            f"printf '{REMOTE_SHELL_MARKER}'",
        )

    def run(self) -> ProbeResult:
        try:
            completed = self.runner(
                self.argv(),
                capture_output=True,
                text=True,
                timeout=self.total_timeout,
                env=scrubbed_env(self.env),
            )
        except subprocess.TimeoutExpired:
            return ProbeResult(None, {"target": self.target}, "timeout")
        except BaseException as exc:  # noqa: BLE001
            return ProbeResult(None, {"target": self.target, "error_type": type(exc).__name__}, "transport_error")
        if completed.returncode != 0:
            return ProbeResult(
                False,
                {"target": self.target, "returncode": completed.returncode},
                "ssh_unreachable",
            )
        if REMOTE_SHELL_MARKER not in (completed.stdout or ""):
            return ProbeResult(False, {"target": self.target}, "ssh_marker_missing")
        return ProbeResult(True, {"target": self.target}, None)


class ProcessProbe:
    """Local process liveness, read through ``ps`` (no signal is sent)."""

    def __init__(
        self,
        pid: int | None,
        *,
        expected_command: str | None = None,
        runner: Callable[..., Any] = subprocess.run,
        timeout: float = 5.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.pid = pid
        self.expected_command = expected_command
        self.runner = runner
        self.timeout = float(timeout)
        self.env = env

    def argv(self) -> tuple[str, ...]:
        return ("ps", "-p", str(self.pid), "-o", "state=,command=")

    def run(self) -> ProbeResult:
        if self.pid is None:
            return ProbeResult(None, {"pid": None}, "pid_unknown")
        try:
            completed = self.runner(
                self.argv(),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=scrubbed_env(self.env),
            )
        except subprocess.TimeoutExpired:
            return ProbeResult(None, {"pid": self.pid}, "timeout")
        except BaseException as exc:  # noqa: BLE001
            return ProbeResult(None, {"pid": self.pid, "error_type": type(exc).__name__}, "transport_error")
        if completed.returncode != 0:
            return ProbeResult(False, {"pid": self.pid, "returncode": completed.returncode}, "no_such_process")
        line = (completed.stdout or "").strip()
        if not line:
            return ProbeResult(False, {"pid": self.pid}, "no_such_process")
        parts = line.split(None, 1)
        state = parts[0]
        command = parts[1] if len(parts) > 1 else ""
        detail = {"pid": self.pid, "state": state, "command": command}
        if "Z" in state:
            return ProbeResult(False, detail, "zombie")
        if self.expected_command and self.expected_command not in command:
            return ProbeResult(False, detail, "command_mismatch")
        return ProbeResult(True, detail, None)


def health_from_status(status: Any) -> ProbeResult:
    """The origin health verdict, derived from a live-status document.

    Green requires the three required keys to be present *and* to carry the
    serving values: ``route_alive`` true, ``route_ready`` true, ``simulated``
    false, plus no incidents and no explicit degraded flag.  A document that is
    not an object, or that omits a required key, is *unknown*, never green.
    """

    if not isinstance(status, Mapping):
        return ProbeResult(None, {"error_type": type(status).__name__}, "status_not_object")
    required = ("route_alive", "route_ready", "simulated")
    missing = [key for key in required if key not in status]
    if missing:
        return ProbeResult(None, {"missing": missing}, "status_keys_missing")
    detail = {key: status.get(key) for key in required}
    if status.get("route_alive") is not True or status.get("route_ready") is not True:
        return ProbeResult(False, detail, "route_not_healthy")
    if status.get("simulated") is not False:
        return ProbeResult(False, detail, "simulated_route")
    incidents = status.get("incidents")
    detail["incident_count"] = len(incidents) if isinstance(incidents, list) else None
    if isinstance(incidents, list) and incidents:
        return ProbeResult(False, detail, "incidents_present")
    if status.get("degraded") is True:
        return ProbeResult(False, detail, "degraded_flag")
    return ProbeResult(True, detail, None)


def private_capability_headers(
    capability_path: str | os.PathLike[str],
    *,
    authenticated_user: str = "owner",
) -> dict[str, str]:
    """The observer's private-capability read shape, built from the cap file.

    The capability itself is only ever placed in the request header; callers must
    not log or serialize the returned mapping.
    """

    capability = Path(capability_path).read_text().strip()
    return {
        "Host": "mycelium.now",
        "X-Forwarded-Proto": "https",
        "X-Mycelium-Authenticated-User": authenticated_user,
        "X-Mycelium-Proxy-Capability": capability,
        "Accept": "application/json",
    }


class HTTPHealthProbe:
    """Bounded HTTP health read of the origin (``/__mycelium/live-status``)."""

    def __init__(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        *,
        timeout: float = 5.0,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = float(timeout)
        self.opener = opener

    def run(self) -> ProbeResult:
        try:
            request = urllib.request.Request(self.url, headers=with_user_agent(self.headers))
            with self.opener(request, timeout=self.timeout) as response:
                status = getattr(response, "status", None)
                body = response.read(_MAX_BODY_BYTES)
        except BaseException as exc:  # noqa: BLE001
            return ProbeResult(None, {"error_type": type(exc).__name__}, "transport_error")
        if status != 200:
            return ProbeResult(False, {"status": status}, "http_non_200")
        try:
            document = json.loads(body)
        except (ValueError, TypeError):
            return ProbeResult(None, {"bytes": len(body)}, "invalid_json")
        return health_from_status(document)
