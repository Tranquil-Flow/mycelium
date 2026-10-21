# SPDX-License-Identifier: AGPL-3.0-only
"""Behavioural tests for the L0-L4 test ladder tooling (packet m0-test-ladder).

The interesting behaviour is not this file: it is that a marker is applied from
the collected module, that genuine L3/L4 scenario modules leave L2 while
controller / launcher / security modules stay in it, and that an unregistered
higher-level scenario stops the session instead of hiding inside L2.  Those are
checked twice: against the classification function, and by running real pytest
sessions - on throwaway probe trees with the repository ``conftest.py`` loaded
as a plugin, and on this repository's own paths with the L2 expression.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFTEST_PATH = REPO_ROOT / "conftest.py"
QUARANTINE_PATH = REPO_ROOT / "tests" / "QUARANTINE.md"

LEVEL_MARKERS = ("unit", "integration", "emulation", "physical", "flaky")

#: Modules that must stay in L2 (PLAN v2 §2.1 + packet m0-test-ladder-return).
L2_REGRESSION_MODULES = (
    "tests/physical_qualification/test_runner_lock.py",
    "tests/physical_qualification/test_transfer_size_bound.py",
    "tests/physical_qualification/test_controller.py",
    "tests/physical_qualification/test_plan_builder.py",
    "tests/physical_preflight/test_live_plan_bridge.py",
    "tests/physical_runner/test_config.py",
    "tests/a13_operator/test_soak_counters.py",
    "tests/testbed/test_soak_falsification.py",
)

#: Modules whose level is emulation/physical because they execute the scenario.
HIGHER_LEVEL_REGRESSION_MODULES = {
    "tests/testbed/test_emulation_chain.py": "emulation",
    "tests/testbed/test_emulation_faults.py": "emulation",
    "tests/testbed/test_soak_real_path.py": "emulation",
    "test_two_process_inference_qualification.py": "emulation",
    "test_two_process_runtime_qualification.py": "emulation",
    "tests/physical_qualification/test_pixel8_distributed_inference.py": "physical",
}


def _load_ladder_module():
    spec = importlib.util.spec_from_file_location("m0_ladder_conftest", CONFTEST_PATH)
    assert spec is not None and spec.loader is not None, "conftest.py is not importable"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ladder = _load_ladder_module()


def _repo_modules() -> list[Path]:
    return sorted(REPO_ROOT.glob("test_*.py")) + sorted(REPO_ROOT.glob("tests/**/test_*.py"))


def _relative(path: Path) -> str:
    return PurePosixPath(path.relative_to(REPO_ROOT).as_posix()).as_posix()


def _level(relative: str) -> str | None:
    return ladder.classify(relative, REPO_ROOT / relative)


# --------------------------------------------------------------------------- #
# pytest.ini: markers, default filter, L2 selection
# --------------------------------------------------------------------------- #


def test_level_markers_are_registered(pytestconfig: pytest.Config) -> None:
    registered = "\n".join(pytestconfig.getini("markers"))
    assert registered, "pytest.ini registers no markers at all"
    for name in LEVEL_MARKERS:
        assert re.search(rf"^{name}:", registered, re.M), f"{name!r} is not registered"
    assert re.search(r"^scaffold_red:", registered, re.M), "scaffold_red opt-in was removed"


def test_default_filter_still_honours_the_scaffold_red_opt_in(pytestconfig: pytest.Config) -> None:
    addopts = str(pytestconfig.getini("addopts"))
    assert "not scaffold_red" in addopts
    # Path-explicit runs (lane L0/L1, the readiness gate's emulated-chain check)
    # must keep collecting what they name, so the default filter is not the L2
    # expression.
    for level in ("emulation", "physical", "flaky"):
        assert level not in addopts, f"default addopts must not deselect {level}"


def test_l2_selection_expression_is_complete(pytestconfig: pytest.Config) -> None:
    expression = str(pytestconfig.getini("l2_marker_expression"))
    for term in ("not scaffold_red", "not flaky", "not emulation", "not physical"):
        assert term in expression, f"L2 expression is missing {term!r}: {expression!r}"


def test_unregistered_high_level_scenarios_stop_the_session(pytestconfig: pytest.Config) -> None:
    strict = str(pytestconfig.getini("mycelium_ladder_strict")).strip().lower()
    assert strict in {"1", "true", "yes", "on"}, "strict registration is off by default"


# --------------------------------------------------------------------------- #
# The classification itself
# --------------------------------------------------------------------------- #


def test_registered_scenario_modules_keep_their_level() -> None:
    for relative, expected in HIGHER_LEVEL_REGRESSION_MODULES.items():
        path = REPO_ROOT / relative
        assert path.exists(), f"expected module missing: {relative}"
        assert _level(relative) == expected, f"{relative} is {_level(relative)}, expected {expected}"
        assert relative in ladder.EMULATION_MODULES or relative in ladder.PHYSICAL_MODULES


def test_lower_level_modules_are_not_hidden_from_l2() -> None:
    """Packet m0-test-ladder-return: no exclusion by file name or directory."""
    for relative in L2_REGRESSION_MODULES:
        path = REPO_ROOT / relative
        assert path.exists(), f"expected module missing: {relative}"
        level = _level(relative)
        assert level in {"unit", "integration"}, f"{relative} was moved out of L2 as {level}"
    # A parser-style module under a risky name stays in L2 - the shape the
    # packet names as tests/testbed/test_emulation_cli.py, exercised against the
    # live file when it exists and by a probe tree otherwise.
    probe = REPO_ROOT / "tests/testbed/test_emulation_cli.py"
    if probe.exists():
        assert _level("tests/testbed/test_emulation_cli.py") in {"unit", "integration"}


def test_risky_module_names_need_evidence_not_names() -> None:
    for relative in L2_REGRESSION_MODULES:
        if not ladder.is_risky(relative):
            continue
        assert not ladder.module_needs_higher_level_registration(REPO_ROOT / relative), (
            f"{relative} shows higher-level execution evidence and must be registered"
        )


def test_no_unregistered_high_level_scenario_in_the_tree() -> None:
    offenders = []
    for path in _repo_modules():
        relative = _relative(path)
        if relative in ladder.EMULATION_MODULES or relative in ladder.PHYSICAL_MODULES:
            continue
        if ladder.is_risky(relative) and ladder.module_needs_higher_level_registration(path):
            offenders.append(relative)
    assert offenders == [], f"unregistered higher-level scenario modules: {offenders}"


def test_not_calling_all_tests_unit() -> None:
    levels: dict[str | None, int] = {}
    for path in _repo_modules():
        level = _level(_relative(path))
        levels[level] = levels.get(level, 0) + 1
    assert levels.get(None, 0) == 0, f"unclassified modules: {levels}"
    assert levels.get("unit", 0) > 50, f"level distribution looks wrong: {levels}"
    assert levels.get("integration", 0) > 50, f"level distribution looks wrong: {levels}"
    assert levels.get("emulation", 0) >= 3, f"level distribution looks wrong: {levels}"
    assert levels.get("physical", 0) > 0, f"level distribution looks wrong: {levels}"


# --------------------------------------------------------------------------- #
# Quarantine register: empty, documented, and not a hidden skip
# --------------------------------------------------------------------------- #


def test_quarantine_register_is_empty() -> None:
    text = QUARANTINE_PATH.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.strip().startswith("|")]
    body = [line for line in rows if "---" not in line and not line.startswith("| Test id")]
    entries = []
    for line in body:
        first_cell = line.strip().strip("|").split("|")[0]
        normalised = first_cell.replace("_", "").replace("*", "").strip()
        if normalised and normalised not in {"(none)", "-"}:
            entries.append(line)
    assert entries == [], f"quarantined without three isolation receipts: {entries}"


def test_quarantine_documents_the_three_receipts_procedure() -> None:
    text = QUARANTINE_PATH.read_text(encoding="utf-8").lower()
    for phrase in ("3/3", "three isolation receipts", "first seen", "suspected cause", "flaky"):
        assert phrase in text, f"quarantine procedure is missing {phrase!r}"


def test_no_test_carries_the_flaky_marker_yet() -> None:
    # Assembled at runtime so that this guard's own source text does not match.
    needle = "mark." + "flaky"
    marked = [
        _relative(path)
        for path in _repo_modules()
        if needle in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert marked == [], f"flaky marker used outside the register: {marked}"


# --------------------------------------------------------------------------- #
# Behaviour: real pytest sessions
# --------------------------------------------------------------------------- #

PROBE_FILES = {
    "tests/a10_acceptance/test_probe_unit.py": "def test_probe_unit():\n    assert True\n",
    "tests/lane_audit/test_probe_integration.py": (
        "import subprocess\n\n\ndef test_probe_integration():\n    assert True\n"
    ),
    # Same repository-relative path as a registered L3 module: the registration
    # is what decides, not the file name.
    "tests/testbed/test_emulation_chain.py": "def test_probe_emulation():\n    assert True\n",
    "tests/physical_qualification/test_pixel8_distributed_inference.py": (
        "def test_probe_physical():\n    assert True\n"
    ),
    # The shape the packet names: a parser/unit module under a risky name must
    # stay in L2.
    "tests/testbed/test_emulation_cli.py": "def test_probe_emulation_cli():\n    assert True\n",
}

UNREGISTERED_SCENARIO_FILE = (
    "tests/testbed/test_emulation_rogue.py",
    '# evidence of a real higher-level scenario, deliberately unregistered\n'
    'SIDECAR = "native_iroh_sidecar"\n\n\ndef test_probe_rogue():\n    assert True\n',
)


def _write_probe(root: Path, files: dict[str, str]) -> Path:
    for relative, source in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    return root


def _run_pytest_on(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    plugins = [] if Path(root).resolve() == REPO_ROOT else ["-p", "conftest"]
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        *plugins,
        f"--rootdir={root}",
        *args,
    ]
    return subprocess.run(
        command,
        cwd=str(root),
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _collect_count(text: str) -> int:
    """Selected item count from a pytest report (``3/5 tests collected`` -> 3)."""
    match = re.search(r"^(\d+)(?:/\d+)? tests? collected", text, re.M)
    return int(match.group(1)) if match else 0


def _collected_test_ids(text: str) -> set[str]:
    """The test function ids in a ``--collect-only -q`` report."""
    ids = set()
    for token in (text or "").split():
        if "::" in token:
            ids.add(token.strip())
    return ids


def test_l2_expression_keeps_lower_level_probes_and_drops_registered_scenarios(
    tmp_path: Path, pytestconfig: pytest.Config
) -> None:
    root = _write_probe(tmp_path / "probe", PROBE_FILES)
    expression = str(pytestconfig.getini("l2_marker_expression"))
    completed = _run_pytest_on(root, ["--collect-only", "-m", expression])
    assert completed.returncode == 0, completed.stdout + completed.stderr
    output = completed.stdout + completed.stderr
    assert _collected_test_ids(output) == {
        "tests/a10_acceptance/test_probe_unit.py::test_probe_unit",
        "tests/lane_audit/test_probe_integration.py::test_probe_integration",
        "tests/testbed/test_emulation_cli.py::test_probe_emulation_cli",
    }, output
    assert _collect_count(output) == 3, output


def test_registered_scenarios_leave_l2_while_lower_level_paths_stay() -> None:
    """One real run over this repository's own paths, both directions at once."""
    command = [
        "--collect-only",
        "-m",
        ladder.L2_MARKER_EXPRESSION_DEFAULT,
        "tests/physical_qualification/test_runner_lock.py",
        "tests/physical_qualification/test_transfer_size_bound.py",
        "tests/a13_operator/test_soak_counters.py",
        "tests/physical_runner/test_config.py",
        "tests/physical_qualification/test_node_service.py",
        "tests/physical_qualification/test_endpoint_secret_conduit.py",
        "tests/testbed/test_emulation_chain.py",
    ]
    completed = _run_pytest_on(REPO_ROOT, command)
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    ids = _collected_test_ids(output)
    assert ids, output
    assert not any("test_emulation_chain" in node_id for node_id in ids), output
    # Per-test registration: the two-node iroh and host-node-service scenarios
    # leave L2, while the rest of those same modules stays in it.
    assert not any("over_native_iroh" in node_id for node_id in ids), output
    assert not any("host_node_service" in node_id for node_id in ids), output
    assert any(node_id.startswith("tests/physical_qualification/test_node_service.py::") for node_id in ids), output
    assert any(
        node_id.startswith("tests/physical_qualification/test_endpoint_secret_conduit.py::")
        for node_id in ids
    ), output
    for expected in (
        "tests/physical_qualification/test_runner_lock.py::",
        "tests/physical_qualification/test_transfer_size_bound.py::",
        "tests/a13_operator/test_soak_counters.py::",
        "tests/physical_runner/test_config.py::",
    ):
        assert any(node_id.startswith(expected) for node_id in ids), (expected, output)


def test_scenario_test_names_are_per_test_decisions() -> None:
    native_iroh = "test_two_node_subprocesses_run_distributed_inference_over_native_iroh[mlx]"
    assert ladder.scenario_test_level(native_iroh) == "emulation"
    assert ladder.scenario_test_level("test_host_node_service_reuses_endpoint_identity") == "emulation"
    assert ladder.scenario_test_level("test_two_node_process_lock_is_exclusive") is None
    # The module decision still applies when the test name says nothing.
    assert ladder.level_for_item(
        "tests/testbed/test_emulation_chain.py",
        REPO_ROOT / "tests/testbed/test_emulation_chain.py",
        "test_whatever",
    ) == "emulation"
    # And an L2 module keeps its scanned level for a normal test name.
    assert ladder.level_for_item(
        "tests/physical_qualification/test_runner_lock.py",
        REPO_ROOT / "tests/physical_qualification/test_runner_lock.py",
        "test_runner_lock_is_exclusive",
    ) in {"unit", "integration"}


def test_scenario_markers_select_only_registered_modules(tmp_path: Path) -> None:
    root = _write_probe(tmp_path / "probe", PROBE_FILES)
    completed = _run_pytest_on(root, ["--collect-only", "-m", "emulation or physical"])
    assert completed.returncode == 0, completed.stdout + completed.stderr
    output = completed.stdout + completed.stderr
    assert _collected_test_ids(output) == {
        "tests/testbed/test_emulation_chain.py::test_probe_emulation",
        "tests/physical_qualification/test_pixel8_distributed_inference.py::test_probe_physical",
    }, output


def test_unregistered_scenario_probe_stops_the_session(tmp_path: Path) -> None:
    files = dict(PROBE_FILES)
    files[UNREGISTERED_SCENARIO_FILE[0]] = UNREGISTERED_SCENARIO_FILE[1]
    root = _write_probe(tmp_path / "probe_rogue", files)
    completed = _run_pytest_on(root, [])
    output = completed.stdout + completed.stderr
    assert completed.returncode == 4, output
    assert "not registered" in output
    assert "tests/testbed/test_emulation_rogue.py" in output
    assert "mycelium_ladder_strict" in output


def test_strict_can_only_be_turned_off_explicitly(tmp_path: Path) -> None:
    files = dict(PROBE_FILES)
    files[UNREGISTERED_SCENARIO_FILE[0]] = UNREGISTERED_SCENARIO_FILE[1]
    root = _write_probe(tmp_path / "probe_relaxed", files)
    completed = _run_pytest_on(root, ["-o", "mycelium_ladder_strict=false"])
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "6 passed" in completed.stdout
