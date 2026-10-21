# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only macOS resource observations for A13 policy guards.

The frozen public surface is ``observe_policy()`` and
``policy_violation(policy, observation)``.  Observations are small snapshots,
not physical qualification, serving readiness, or durable capability claims.
The native helper is built only by the explicit ``build_policy_probe`` call;
imports and ordinary observation never invoke a compiler.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Final, Mapping


MAX_PROBE_OUTPUT_BYTES: Final = 4096
PROBE_TIMEOUT_SECONDS: Final = 2.5
_BUILD_TIMEOUT_SECONDS: Final = 60.0

_POWER_VALUES: Final = frozenset({"external", "battery", "unknown"})
_THERMAL_VALUES: Final = frozenset(
    {"nominal", "fair", "serious", "critical", "unknown"}
)
_NETWORK_VALUES: Final = frozenset(
    {"unmetered", "metered", "unavailable", "unknown"}
)
_OBSERVATION_KEYS: Final = frozenset({"power", "thermal", "network"})

UNKNOWN_OBSERVATION: Final[dict[str, str]] = {
    "power": "unknown",
    "thermal": "unknown",
    "network": "unknown",
}


def _unknown_observation() -> dict[str, str]:
    return dict(UNKNOWN_OBSERVATION)


def _parse_probe_output(payload: bytes) -> dict[str, str]:
    """Parse the helper's bounded exact JSON schema, else return unknowns."""
    if not isinstance(payload, bytes) or len(payload) > MAX_PROBE_OUTPUT_BYTES:
        return _unknown_observation()
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _unknown_observation()
    if not isinstance(decoded, dict) or set(decoded) != _OBSERVATION_KEYS:
        return _unknown_observation()
    power = decoded.get("power")
    thermal = decoded.get("thermal")
    network = decoded.get("network")
    if (
        not isinstance(power, str)
        or power not in _POWER_VALUES
        or not isinstance(thermal, str)
        or thermal not in _THERMAL_VALUES
        or not isinstance(network, str)
        or network not in _NETWORK_VALUES
    ):
        return _unknown_observation()
    return {"power": power, "thermal": thermal, "network": network}


def _resource_probe_candidates() -> tuple[Path, ...]:
    """Return only deterministic package-relative runtime locations."""
    source_candidate = Path(__file__).resolve().parent / "native" / "resource_probe"
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root is None:
        return (source_candidate,)
    packaged_candidate = (
        Path(bundle_root) / "mycelium_a13_macos" / "native" / "resource_probe"
    )
    if packaged_candidate == source_candidate:
        return (source_candidate,)
    return (packaged_candidate, source_candidate)


def _is_safe_executable(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not path.is_symlink()
        and bool(metadata.st_mode & stat.S_IXUSR)
    )


def _invoke_resource_probe(path: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [os.fspath(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=PROBE_TIMEOUT_SECONDS,
        check=False,
    )


def observe_policy() -> dict[str, str]:
    """Return one current read-only observation, failing closed to unknowns.

    On win32 the owner app, target app and worker all observe through the
    Windows observer (power, network and ACPI thermal via PowerShell/CIM), so a
    Windows owner that also contributes is judged on real readings, not unknowns.
    """
    if sys.platform.startswith("linux"):
        from mycelium_a13_linux.managed_host.resources import observe_policy as observe_linux

        observation = observe_linux()
        return observation if _valid_observation(observation) else _unknown_observation()
    if sys.platform == "win32":
        from mycelium_a13_windows.managed_host.resources import observe_policy as observe_windows

        observation = observe_windows()
        return observation if _valid_observation(observation) else _unknown_observation()
    for candidate in _resource_probe_candidates():
        if not _is_safe_executable(candidate):
            continue
        try:
            completed = _invoke_resource_probe(candidate)
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode != 0:
            continue
        observation = _parse_probe_output(completed.stdout)
        if observation != UNKNOWN_OBSERVATION:
            return observation
        # An all-unknown snapshot may be a valid helper result, but trying a
        # second candidate cannot make that candidate more authoritative.
        return observation
    return _unknown_observation()


def _valid_observation(observation: Mapping[str, object]) -> bool:
    return (
        set(observation) == _OBSERVATION_KEYS
        and observation.get("power") in _POWER_VALUES
        and observation.get("thermal") in _THERMAL_VALUES
        and observation.get("network") in _NETWORK_VALUES
        and all(isinstance(observation.get(name), str) for name in _OBSERVATION_KEYS)
    )


def policy_violation(
    policy: Mapping[str, object], observation: Mapping[str, object]
) -> str | None:
    """Return one bounded guard error code, or ``None`` when policy permits.

    Power is fail-closed for ``ac_only``. Thermal unknown/serious/critical is
    always rejected by ``stop_on_serious``. Network must be positively usable;
    ``unmetered_only`` additionally requires a positively unmetered path.
    """
    if not isinstance(policy, Mapping) or (
        policy.get("power") not in {"ac_only", "battery_allowed"}
        or policy.get("thermal") != "stop_on_serious"
        or policy.get("network") not in {"unmetered_only", "metered_allowed"}
    ):
        return "resource_policy_invalid"
    if not isinstance(observation, Mapping) or not _valid_observation(observation):
        return "resource_observation_invalid"

    power = observation["power"]
    thermal = observation["thermal"]
    network = observation["network"]
    if policy["power"] == "ac_only" and power != "external":
        return "resource_power_policy_violation"
    if thermal in {"serious", "critical", "unknown"}:
        return "resource_thermal_policy_violation"
    if network in {"unavailable", "unknown"}:
        return "resource_network_policy_violation"
    if policy["network"] == "unmetered_only" and network != "unmetered":
        return "resource_network_policy_violation"
    return None


def build_policy_probe(output_path: str | os.PathLike[str]) -> Path:
    """Explicitly compile the tiny public-API helper to ``output_path``.

    This development/build operation has fixed compiler arguments and emits no
    compiler diagnostics through this API. Packaged normal use must include the
    resulting executable and does not need Xcode or a compiler.
    """
    if sys.platform != "darwin":
        raise RuntimeError("resource_probe_build_unsupported")
    output = Path(output_path).expanduser().resolve()
    source = Path(__file__).resolve().parent / "native" / "resource_probe.m"
    if not source.is_file() or source.is_symlink():
        raise RuntimeError("resource_probe_source_unavailable")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.is_symlink() or output.parent.is_symlink():
        raise RuntimeError("resource_probe_output_unsafe")
    temporary = output.with_name(f".{output.name}.building")
    try:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
        completed = subprocess.run(
            [
                "/usr/bin/xcrun",
                "clang",
                "-fobjc-arc",
                "-fblocks",
                "-O2",
                "-mmacosx-version-min=15.0",
                "-framework",
                "Foundation",
                "-framework",
                "Network",
                os.fspath(source),
                "-o",
                os.fspath(temporary),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_BUILD_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode != 0 or not _is_safe_executable(temporary):
            raise RuntimeError("resource_probe_build_failed")
        os.replace(temporary, output)
        output.chmod(0o755)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("resource_probe_build_failed") from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return output
