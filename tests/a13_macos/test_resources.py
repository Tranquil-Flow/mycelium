# SPDX-License-Identifier: AGPL-3.0-only
"""TDD coverage for the macOS A13 read-only policy observation adapter.

Fixture policy decisions below exercise only deterministic guard logic.  The
native smoke is separately labelled and records a current-host observation;
neither is physical qualification or serving evidence.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from mycelium_a13_macos import resources


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_ROOT = ROOT / "outputs" / "a13-macos-evidence"
PROBE_PATH = EVIDENCE_ROOT / "resources-resource-probe"
ALLOWED = {
    "power": {"external", "battery", "unknown"},
    "thermal": {"nominal", "fair", "serious", "critical", "unknown"},
    "network": {"unmetered", "metered", "unavailable", "unknown"},
}
STRICT_POLICY = {
    "power": "ac_only",
    "thermal": "stop_on_serious",
    "network": "unmetered_only",
}


def _observation(**changes: str) -> dict[str, str]:
    value = {"power": "external", "thermal": "nominal", "network": "unmetered"}
    value.update(changes)
    return value


def test_parse_probe_output_accepts_only_the_frozen_observation_schema() -> None:
    expected = _observation(thermal="fair", network="metered")
    assert resources._parse_probe_output(json.dumps(expected).encode("utf-8")) == expected

    invalid = (
        b"not-json",
        b"[]",
        b'{"power":"external","thermal":"nominal"}',
        b'{"power":"wall","thermal":"nominal","network":"unmetered"}',
        b'{"power":"external","thermal":"nominal","network":"unmetered","host":"private"}',
        b"{}" + (b" " * (resources.MAX_PROBE_OUTPUT_BYTES + 1)),
    )
    for payload in invalid:
        assert resources._parse_probe_output(payload) == resources.UNKNOWN_OBSERVATION


def test_observe_policy_fails_closed_for_absent_invalid_timeout_and_launch_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resources, "_resource_probe_candidates", lambda: ())
    assert resources.observe_policy() == resources.UNKNOWN_OBSERVATION

    monkeypatch.setattr(resources, "_resource_probe_candidates", lambda: (Path("/trusted/probe"),))
    monkeypatch.setattr(resources, "_is_safe_executable", lambda _path: True)
    for outcome in (
        subprocess.CompletedProcess(["probe"], 1, stdout=b"", stderr=b"private details"),
        subprocess.CompletedProcess(["probe"], 0, stdout=b"invalid", stderr=b""),
        subprocess.TimeoutExpired(["probe"], resources.PROBE_TIMEOUT_SECONDS),
        OSError("private launch details"),
    ):
        def invoke(_path: Path, *, _outcome: object = outcome) -> subprocess.CompletedProcess[bytes]:
            if isinstance(_outcome, BaseException):
                raise _outcome
            return _outcome  # type: ignore[return-value]

        monkeypatch.setattr(resources, "_invoke_resource_probe", invoke)
        assert resources.observe_policy() == resources.UNKNOWN_OBSERVATION


def test_policy_violation_fixture_matrix_is_fail_closed_and_bounded() -> None:
    assert resources.policy_violation(STRICT_POLICY, _observation()) is None

    cases = [
        (_observation(power="battery"), "resource_power_policy_violation"),
        (_observation(power="unknown"), "resource_power_policy_violation"),
        (_observation(thermal="serious"), "resource_thermal_policy_violation"),
        (_observation(thermal="critical"), "resource_thermal_policy_violation"),
        (_observation(thermal="unknown"), "resource_thermal_policy_violation"),
        (_observation(network="metered"), "resource_network_policy_violation"),
        (_observation(network="unavailable"), "resource_network_policy_violation"),
        (_observation(network="unknown"), "resource_network_policy_violation"),
    ]
    for observation, expected in cases:
        assert resources.policy_violation(STRICT_POLICY, observation) == expected

    relaxed = {
        "power": "battery_allowed",
        "thermal": "stop_on_serious",
        "network": "metered_allowed",
    }
    assert resources.policy_violation(relaxed, _observation(power="battery", network="metered")) is None
    assert (
        resources.policy_violation(relaxed, _observation(network="unavailable"))
        == "resource_network_policy_violation"
    )
    assert (
        resources.policy_violation(relaxed, _observation(network="unknown"))
        == "resource_network_policy_violation"
    )

    assert resources.policy_violation({}, _observation()) == "resource_policy_invalid"
    assert (
        resources.policy_violation(STRICT_POLICY, {"power": "external"})
        == "resource_observation_invalid"
    )
    assert (
        resources.policy_violation(STRICT_POLICY, _observation(thermal="emergency"))
        == "resource_observation_invalid"
    )


def test_runtime_lookup_is_package_relative_and_never_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "_MEIPASS", "/private/packaged-root", raising=False)
    candidates = resources._resource_probe_candidates()
    assert candidates == (
        Path("/private/packaged-root/mycelium_a13_macos/native/resource_probe"),
        Path(resources.__file__).resolve().parent / "native" / "resource_probe",
    )
    monkeypatch.setattr(resources, "_resource_probe_candidates", lambda: ())
    monkeypatch.setattr(
        resources,
        "build_policy_probe",
        lambda _path: (_ for _ in ()).throw(AssertionError("compiler must not run")),
    )
    assert resources.observe_policy() == resources.UNKNOWN_OBSERVATION


@pytest.fixture(scope="session")
def native_probe() -> Path:
    if sys.platform != "darwin":
        pytest.skip("macOS native resource probe")
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    if PROBE_PATH.exists() or PROBE_PATH.is_symlink():
        PROBE_PATH.unlink()
    built = resources.build_policy_probe(PROBE_PATH)
    assert built == PROBE_PATH.resolve()
    assert built.is_file() and not built.is_symlink()
    # The first exec of a freshly linked binary pays macOS's one-time launch
    # assessment (measured 5-8x the steady-state cost; >2.5 s under a loaded
    # xdist run). Absorb it here so the smoke test below still holds the probe
    # to the production PROBE_TIMEOUT_SECONDS on a steady-state exec.
    subprocess.run([str(built)], check=True, capture_output=True, timeout=30)
    return built


def test_real_current_host_read_only_native_smoke(
    native_probe: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    direct = subprocess.run(
        [str(native_probe)],
        check=True,
        capture_output=True,
        timeout=resources.PROBE_TIMEOUT_SECONDS,
    )
    direct_observation = resources._parse_probe_output(direct.stdout)
    assert direct_observation != resources.UNKNOWN_OBSERVATION
    assert direct_observation["thermal"] != "unknown"

    monkeypatch.setattr(resources, "_resource_probe_candidates", lambda: (native_probe,))
    observed = resources.observe_policy()
    assert set(observed) == set(ALLOWED)
    assert all(observed[name] in values for name, values in ALLOWED.items())
    assert observed["thermal"] != "unknown"

    evidence = {
        "protocol": "mycelium.a13_macos.resource_observation_evidence.v1",
        "evidence_class": "current_host_read_only_native_observation",
        "claim_limit": "observation_only_not_physical_qualification",
        "fixture_policy_matrix": "deterministic_guard_tests_only",
        "native_observation": observed,
    }
    evidence_path = EVIDENCE_ROOT / "resources-native-smoke.json"
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert json.loads(evidence_path.read_text(encoding="utf-8")) == evidence
