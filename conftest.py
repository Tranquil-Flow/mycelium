# SPDX-License-Identifier: AGPL-3.0-only
"""Test-level markers for the L0-L4 ladder (PLAN v2 §2.1).

The ladder runs the cheapest level that can answer the question, so every
collected item declares which level it belongs to.  The decision is made from
the **collected module**, and it is deliberately conservative: a module leaves
L2 only through an explicit, reason-bearing registration below, never because
its file name or its directory *sounds* like a higher level.

Levels
------
``unit``
    In-process test of module logic: no child process, no listen socket, no
    real model artifact, no sidecar - including controller / launcher /
    security tests that exercise those subjects through doubles.
``integration``
    Drives a real boundary locally: starts a child process, binds a port, runs
    a CLI, needs a real artifact on disk.  Still L2.
``emulation``
    L3 emulation on the shipped path: an actual model / iroh / fault scenario
    is executed (two real worker processes, a real model snapshot, the real
    fault relay, the native iroh sidecar).
``physical``
    L4: an actual hardware scenario is executed (gated on real hardware).

``flaky`` is not assigned here.  Only the quarantine register
(``tests/QUARANTINE.md``) assigns it, after three isolation receipts.

What decides the level
----------------------
1. ``EMULATION_MODULES`` and ``PHYSICAL_MODULES`` - small, explicit maps from
   module path to the evidence for its registration, when the whole module is
   the scenario.
2. ``SCENARIO_TEST_NAMES`` - individual tests inside an otherwise-L2 module
   that execute a higher-level scenario themselves, so the module keeps its L2
   coverage while that one test moves up a level.
3. Everything else stays in L2.  ``unit`` vs ``integration`` comes from a scan
   of that module for real-boundary signals (child process, socket, sidecar,
   real model artifact).  That split never removes anything from L2.

There is no file-name or directory-level exclusion: ``tests/physical_runner``,
``tests/physical_preflight``, ``tests/physical_qualification``,
``test_soak_*``, ``test_two_process_*`` and ``test_emulation*`` modules are all
in L2 unless a module is registered above.  Concretely (regression-checked in
``tests/test_test_ladder.py``): ``tests/physical_qualification/test_runner_lock.py``
(``tmp_path`` lock logic), ``tests/physical_qualification/test_transfer_size_bound.py``
(deterministic bound regression, no multi-GB files materialised) and a
``tests/testbed/test_emulation_cli.py``-style parser unit test stay in L2.

Fail visibly when a higher-level test is not registered
-------------------------------------------------------
An *undecided* module is one whose name matches ``test_emulation*`` /
``test_soak_*`` / ``test_two_process_*`` or which lives under a ``physical*``
directory, **and** whose text carries evidence of executing a higher-level
scenario (a real model snapshot, the native iroh sidecar, spawned qualification
workers, a hardware gate).  That combination means "this looks like L3/L4 that
nobody registered", and under the default ``mycelium_ladder_strict = true`` the
session stops with exit code 4 and names the module, instead of silently
running two-Mac or model scenario work inside L2.  Risky modules *without* that
evidence are kept in L2 - the conservative direction - so the check can only
ever demand a registration for something that really executes.

L2 selection
------------
``pytest.ini`` keeps ``addopts = -m "not scaffold_red"`` so path-explicit runs
(lane L0/L1 commands, the readiness gate's emulated-chain check) still collect
what they name, and registers the L2 selection as the ini value
``l2_marker_expression``::

    not scaffold_red and not flaky and not emulation and not physical

which is what the orchestrator passes with ``-m`` for a full parallel run.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import pytest

#: The four level markers this module applies.
MARKER_LEVELS: tuple[str, ...] = ("unit", "integration", "emulation", "physical")

#: Default L2 selection, mirrored by ``pytest.ini`` (the ini value wins).
L2_MARKER_EXPRESSION_DEFAULT = (
    "not scaffold_red and not flaky and not emulation and not physical"
)

#: Modules that execute an actual model / iroh / fault scenario (L3).
EMULATION_MODULES: dict[str, str] = {
    "tests/testbed/test_emulation_chain.py": (
        "single-host two-worker emulation that runs the real thing: two worker processes with "
        "separate homes and identities, the real Qwen2.5-0.5B snapshot, a real https origin on "
        "loopback and the promoted fault proxy on every leg"
    ),
    "tests/testbed/test_emulation_faults.py": (
        "drives the promoted stdlib fault relay itself (seeded replay, half-close, latency and "
        "loss) - fault scenario execution, not a double"
    ),
    "tests/testbed/test_soak_real_path.py": (
        "opt-in real control path (MYCELIUM_TESTBED_IROH_E2E=1): the only soak test that builds "
        "and launches the native iroh sidecar and sweeps over it"
    ),
    "tests/l3/test_real_worker_kill.py": (
        "kills the real shipped a13_macos managed worker process group (SIGKILL to the process the "
        "runtime started, not a mailbox re-subscribe) and bounds the recovery it observes: real "
        "TargetApplication.start_work -> ManagedTargetRuntime -> ManagedWorker with its native child, "
        "real iroh sidecar installed; no model load and no fault proxy"
    ),
    "test_two_process_inference_qualification.py": (
        "executable qualification: spawns two real MLX router workers and exercises the shipped "
        "router path and its timeout reaping"
    ),
    "test_two_process_runtime_qualification.py": (
        "executable qualification: spawns two real assignment-bound MLX loads (multiprocessing "
        "spawn) and asserts the offline assignment evidence"
    ),
}

#: Modules that execute an actual hardware scenario (L4).
PHYSICAL_MODULES: dict[str, str] = {
    "tests/physical_qualification/test_pixel8_distributed_inference.py": (
        "the hardware half is gated on a deliberately enabled, authenticated Pixel 8 Pro "
        "(skipif without that gate), i.e. a real device scenario"
    ),
}

#: Individual tests inside an L2 module that execute a higher-level scenario.
#: Per-item, not per-module: the rest of ``tests/physical_qualification/test_node_service.py``
#: and ``tests/physical_qualification/test_endpoint_secret_conduit.py`` stays in L2,
#: because those modules only *contain* scenario tests, they are not scenario
#: tests themselves.
SCENARIO_TEST_NAMES: tuple[tuple[str, str], ...] = (
    (
        r"native_iroh",
        "runs two node subprocesses over the native iroh sidecar and executes distributed "
        "inference through them (needs native/iroh_transport built)",
    ),
    (
        r"host_node_service",
        "starts the real host node service against the native iroh sidecar binary",
    ),
)

#: Names that may hide a higher-level scenario and therefore need a decision.
_RISKY_NAME = re.compile(r"^(test_emulation.*|test_soak_.*|test_two_process_.*)\.py$")

#: Evidence that a module actually executes a higher-level scenario.  Only used
#: for *undecided* risky modules: with this evidence they must be registered.
HIGHER_LEVEL_EVIDENCE = re.compile(
    r"native_iroh_sidecar|MYCELIUM_TESTBED_IROH_E2E|Qwen2\.5-0\.5B|"
    r"_spawn_runtime_workers|_spawn_assignment_loads|authenticated Pixel 8 Pro|"
    r"physical_inference_qualification\.run|require_two_mac|two_mac_scenario",
)

#: Evidence a module drives a real boundary locally (integration, still L2).
INTEGRATION_SIGNAL = re.compile(
    r"subprocess|Popen|multiprocessing|os\.fork|socket\.|free_port|find_port|"
    r"iroh|sidecar|mlx\.|MLX|safetensors|load_model|model_path|LiveRoute|live_route|spawn",
)


def is_risky(relative_path: str) -> bool:
    """True when the module's name or directory may hide a higher-level test."""
    parts = PurePosixPath(relative_path).parts
    if not parts:
        return False
    if _RISKY_NAME.match(parts[-1]):
        return True
    return any(segment.startswith("physical") for segment in parts[:-1])


def module_needs_higher_level_registration(path: Path | str) -> bool:
    """True when an undecided module shows evidence of executing a real scenario."""
    try:
        text = Path(str(path)).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(HIGHER_LEVEL_EVIDENCE.search(text))


def module_has_real_boundary_signal(path: Path | str) -> bool:
    """True when the module text drives a child process, socket or real model."""
    try:
        text = Path(str(path)).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(INTEGRATION_SIGNAL.search(text))


def scan_level(path: Path | str) -> str:
    """``unit`` or ``integration`` for an L2 module, from its real-boundary signals."""
    return "integration" if module_has_real_boundary_signal(path) else "unit"


def scenario_test_level(test_name: str) -> str | None:
    """``emulation`` when the test itself executes a higher-level scenario."""
    for pattern, _reason in SCENARIO_TEST_NAMES:
        if re.search(pattern, test_name):
            return "emulation"
    return None


def classify(relative_path: str, path: Path | str) -> str | None:
    """Return the ladder level for a repository-relative POSIX test path.

    ``None`` means the module looks like an unregistered higher-level scenario:
    the caller treats that as a policy violation, never as a default.
    """
    if relative_path in EMULATION_MODULES:
        return "emulation"
    if relative_path in PHYSICAL_MODULES:
        return "physical"
    if is_risky(relative_path) and module_needs_higher_level_registration(path):
        return None
    return scan_level(path)


def level_for_item(relative_path: str, path: Path | str, test_name: str) -> str | None:
    """The level for one collected item: module decision, then per-test scenario."""
    return scenario_test_level(test_name) or classify(relative_path, path)


def relative_test_path(path: object, rootpath: object) -> str | None:
    """Return ``path`` relative to ``rootpath`` as POSIX text, or ``None``."""
    try:
        resolved = Path(str(path)).resolve()
        relative = resolved.relative_to(Path(str(rootpath)).resolve())
    except (OSError, ValueError):
        return None
    return PurePosixPath(relative.as_posix()).as_posix()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(
        "mycelium_ladder_strict",
        "Stop the session (exit code 4) when a risky test module shows evidence of executing a "
        "higher-level scenario but is not registered in conftest.py's EMULATION_MODULES or "
        "PHYSICAL_MODULES.",
        default="true",
    )
    parser.addini(
        "l2_marker_expression",
        "Marker expression that selects the canonical L2 (full parallel) run.",
        default=L2_MARKER_EXPRESSION_DEFAULT,
    )


def _strict_enabled(config: pytest.Config) -> bool:
    value = config.getini("mycelium_ladder_strict")
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    rootpath = config.rootpath
    unregistered: list[str] = []

    for item in items:
        relative = relative_test_path(item.path, rootpath)
        if relative is None:
            unregistered.append(f"{item.path} (outside rootdir {rootpath})")
            continue
        level = level_for_item(relative, item.path, item.name)
        if level is None:
            unregistered.append(relative)
            continue
        if not any(marker.name in MARKER_LEVELS for marker in item.iter_markers()):
            item.add_marker(getattr(pytest.mark, level))

    if unregistered and _strict_enabled(config):
        unique = sorted(set(unregistered))
        pytest.exit(
            "L0-L4 ladder: {} collected test module(s) execute a higher-level scenario but are "
            "not registered (mycelium_ladder_strict=true). Add the module to EMULATION_MODULES "
            "or PHYSICAL_MODULES in conftest.py with the evidence for it, or register it as an "
            "L2 module by making the evidence not apply. Unregistered: {}".format(
                len(unique), ", ".join(unique[:20])
            ),
            returncode=4,
        )
