# SPDX-License-Identifier: AGPL-3.0-only
"""Real-process tests for the fail-closed quiet-host guard.

The suite deliberately mixes two kinds of evidence:

* process discovery is exercised against the **real** process table, with a real
  ``python -m pytest`` suite (and a real descendant of it) spawned by this file;
* the load average and any other host fact that cannot be made deterministic is
  injected, so an overloaded or idle developer host cannot change a verdict by
  accident.

Run:  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 -B -m pytest -q \\
        -p no:cacheprovider tests/test_quiet_host_guard.py

It must also hold under the L2 gate shape, ``pytest -n8``: an xdist worker is an
execnet interpreter child of the ``-m pytest`` driver that spawned it, so the
self-identity checks below prove that a real pytest **owns** this process,
instead of assuming that this process **is** the runner.  The product guard is
unchanged and still fail-closed: the self-identity premise only ever accepts a
genuine censused runner invocation that is this process or one of its own
ancestors, never a suite in a sibling lane.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "quiet_host_guard.py"

_spec = importlib.util.spec_from_file_location("quiet_host_guard_under_test", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
qhg = importlib.util.module_from_spec(_spec)
# Register before exec: the module uses ``from __future__ import annotations``
# together with dataclasses, which resolves annotations via sys.modules.
sys.modules[_spec.name] = qhg
_spec.loader.exec_module(qhg)

#: A quiet host, injected: deterministic across loaded developer machines.
QUIET_LOAD = lambda: (0.42, 0.55, 0.61)  # noqa: E731
OVERLOADED_LOAD = lambda: (9.75, 8.5, 7.0)  # noqa: E731

PYTHON = sys.executable or "/opt/homebrew/bin/python3"

INNER_SUITE_SOURCE = '''
import os
import subprocess
import sys
import time
from pathlib import Path


def test_sleeping_suite():
    """A real, running suite: a sleeping pytest process plus one real child."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    Path(os.environ["QUIET_GUARD_PIDFILE"]).write_text(str(child.pid), encoding="utf-8")
    try:
        time.sleep(120)
    finally:
        child.kill()
'''


def _ps_line(pid: int, ppid: int, comm: str, args: str) -> str:
    return f"{pid} {ppid} {comm} {args}"


def _rows(lines) -> tuple:
    return qhg.parse_census("".join(line + "\n" for line in lines))


IDLE_HOST = _rows(
    [
        _ps_line(1, 0, "/sbin/launchd", "/sbin/launchd"),
        _ps_line(
            410,
            1,
            "/Users/x/.hermes/qwen38-mlx/venv/bin/python",
            "/Users/x/venv/bin/python /Users/x/venv/mlx_vlm.server --host 127.0.0.1 --port 18088",
        ),
        _ps_line(
            694,
            1,
            "/Applications/Go",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ),
        _ps_line(900, 1, "/usr/local/bin/node", "node /Users/x/server.js --port 3000"),
        _ps_line(
            1000,
            1,
            "/usr/local/bin/python3",
            "python3 -m pip install -r requirements.txt pytest",
        ),
    ]
)


def _evaluate(level: str = "L2", rows=IDLE_HOST, load_reader=QUIET_LOAD, own_pid: int = 4242):
    return qhg.evaluate(
        level, own_pid=own_pid, load_reader=load_reader, census_reader=lambda: rows
    )


# --------------------------------------------------------------------------- #
# load: overloaded and unknown
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("load1", [0.0, 1.5, 7.99, 8.0])
def test_load_at_or_below_ceiling_is_allowed(load1):
    report = _evaluate(load_reader=lambda: (load1, 0.0, 0.0))
    assert report.verdict == "GO", report.blocking_reasons
    assert qhg.R_LOAD_OVER not in report.blocking_reasons


@pytest.mark.parametrize("load1", [12.5, 8.01, 64.0])
def test_load_above_ceiling_refuses_with_named_reason(load1):
    report = _evaluate(load_reader=lambda: (load1, 0.0, 0.0))
    assert report.verdict == "NO-GO"
    assert report.blocking_reasons == (qhg.R_LOAD_OVER,)
    reasons = {reason.code: reason.detail for reason in report.reasons}
    assert f"{load1:.2f}" in reasons[qhg.R_LOAD_OVER]
    assert "immutable" in reasons[qhg.R_LOAD_OVER]


@pytest.mark.parametrize(
    "reader",
    [
        pytest.param(lambda: (_ for _ in ()).throw(OSError("no loadavg")), id="raises-oserror"),
        pytest.param(lambda: (_ for _ in ()).throw(RuntimeError("boom")), id="raises-runtime"),
        pytest.param(lambda: (float("nan"), 0.0, 0.0), id="nan"),
        pytest.param(lambda: (float("inf"), 0.0, 0.0), id="inf"),
        pytest.param(lambda: (-1.0, 0.0, 0.0), id="negative"),
        pytest.param(lambda: ("eight", 0.0, 0.0), id="not-a-number"),
        pytest.param(lambda: (), id="empty"),
        pytest.param(lambda: None, id="none"),
    ],
)
def test_unknown_load_refuses(reader):
    report = _evaluate(load_reader=reader)
    assert report.verdict == "NO-GO", "an unreadable load average must never pass"
    assert qhg.R_LOAD_UNKNOWN in report.blocking_reasons
    assert report.load1 is None


def test_load_unknown_is_also_raised_by_read_load1():
    with pytest.raises(qhg.LoadUnavailable):
        qhg.read_load1(lambda: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(qhg.LoadUnavailable):
        qhg.read_load1(lambda: (float("nan"),))
    assert qhg.read_load1(lambda: (3.25, 1.0, 1.0)) == 3.25


# --------------------------------------------------------------------------- #
# census: unreadable is a refusal
# --------------------------------------------------------------------------- #


def test_unreadable_census_refuses():
    def broken():
        raise OSError("ps exploded")

    report = _evaluate(rows=broken)
    assert report.verdict == "NO-GO"
    assert qhg.R_CENSUS_UNAVAILABLE in report.blocking_reasons
    assert report.census_error


def test_empty_census_refuses():
    report = _evaluate(rows=lambda: ())
    assert report.verdict == "NO-GO"
    assert qhg.R_CENSUS_UNAVAILABLE in report.blocking_reasons


def test_census_command_failure_raises_census_unavailable():
    class _Completed:
        returncode = 1
        stdout = ""
        stderr = "ps: illegal option"

    seen: dict = {}

    def runner(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = dict(kwargs)
        return _Completed()

    with pytest.raises(qhg.CensusUnavailable):
        qhg.read_process_census(runner=runner)
    assert seen["argv"] == list(qhg.CENSUS_ARGV)
    assert seen["kwargs"].get("shell") in (None, False)


def test_read_only_census_uses_one_ps_and_never_writes_or_signals():
    assert qhg.CENSUS_ARGV[0] == "ps"
    assert "-Aww" in qhg.CENSUS_ARGV
    assert any(part.startswith("pid=") for part in qhg.CENSUS_ARGV)
    source = MODULE_PATH.read_text(encoding="utf-8")
    for forbidden in ("os.kill", "killpg", "signal.", "shell=True", "Popen(", "os.remove"):
        assert forbidden not in source, f"census must stay read-only: found {forbidden!r}"


def test_parse_census_skips_garbage_without_raising():
    rows = qhg.parse_census("not a row\n1\n2 0 /sbin/launchd /sbin/launchd\n3 x y z\n")
    assert [row.pid for row in rows] == [2]


# --------------------------------------------------------------------------- #
# the level is gated, and only L2/L3/L4 are
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("level", ["L2", "L3", "L4"])
def test_gated_levels_pass_on_a_quiet_idle_host(level):
    report = _evaluate(level=level)
    assert report.verdict == "GO", report.blocking_reasons


@pytest.mark.parametrize("level", ["L0", "L1", "l2", "", "L5", "production"])
def test_unknown_level_refuses(level):
    report = _evaluate(level=level)
    assert report.verdict == "NO-GO"
    assert qhg.R_LEVEL_NOT_GATED in report.blocking_reasons


# --------------------------------------------------------------------------- #
# suite discovery: executable-first, not substring matching
# --------------------------------------------------------------------------- #


def test_browser_user_data_dir_pytest_path_is_not_a_lane_suite():
    rows = _rows(
        [
            _ps_line(
                694,
                1,
                "/Applications/Go",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
                " --user-data-dir=/private/var/folders/zz/pytest-of-evi/pytest-12/test_x0"
                " --remote-debugging-port=9222",
            ),
            _ps_line(
                4114,
                694,
                "/Applications/Go",
                "/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Helper"
                " --type=gpu-process"
                " --user-data-dir=/private/var/folders/zz/pytest-of-evi/pytest-12/test_x0"
                " -m pytest -q tests/test_quiet_host_guard.py",
            ),
            _ps_line(
                4120,
                694,
                "/Applications/Go",
                "/Applications/Google Chrome.app/Contents/MacOS/chrome_crashpad_handler"
                " --database=/Users/owner/Library/Application Support/Google/Chrome/Crashpad"
                " --annotation=pytest.main",
            ),
        ]
    )
    scan = qhg.scan_suites(rows, own_pid=4242)
    assert scan.roots == ()
    report = _evaluate(rows=rows)
    assert report.verdict == "GO", report.blocking_reasons


@pytest.mark.parametrize(
    "argv",
    [
        "/opt/homebrew/bin/python3 -m pytest -q -p no:cacheprovider tests/test_x.py",
        "/opt/homebrew/bin/python3 -B -m pytest -q tests/y.py",
        "/Users/x/venv/bin/python3.14 -m pytest tests/z.py",
        "/opt/homebrew/bin/pytest -q tests/z.py",
        "uv run pytest -q",
        "poetry run pytest -q",
        "tox -e py -- pytest -q",
        "python3 -mpytest -q",
        'python3 -c "import pytest; raise SystemExit(pytest.main(["-q"]))"',
    ],
)
def test_real_runner_invocations_are_detected(argv):
    comm = argv.split(" ", 1)[0]
    rows = _rows([_ps_line(7000, 1, comm, argv)])
    assert rows[0].kind == "runner", rows[0]
    scan = qhg.scan_suites(rows, own_pid=4242)
    assert [hit.pid for hit in scan.roots] == [7000]


@pytest.mark.parametrize(
    "argv",
    [
        "/Users/x/venv/bin/python /Users/x/venv/mlx_vlm.server --host 127.0.0.1 --port 18088",
        "/Users/x/venv/bin/python /Users/x/reasoning_proxy.py --upstream-port 18088",
        "/usr/local/bin/python3 -c \"print('pytest is installed')\"",
        "/usr/local/bin/python3 -m pip install pytest",
        "/usr/local/bin/python3 manage.py test",
        "/usr/local/bin/node /Users/x/pytest-runner.js",
        "/bin/bash -lc echo pytest",
        "/Users/x/venv/bin/python -m pytest_cov --version",
    ],
)
def test_non_suite_processes_are_not_a_lane_suite(argv):
    comm = argv.split(" ", 1)[0]
    rows = _rows([_ps_line(7100, 1, comm, argv)])
    assert rows[0].kind != "runner", rows[0]
    report = _evaluate(rows=rows)
    assert report.verdict == "GO", report.blocking_reasons


# --------------------------------------------------------------------------- #
# self-match exclusion: the guard's own argv, ancestors, descendants, wrapper
# --------------------------------------------------------------------------- #


def test_own_argv_would_look_like_a_suite_without_the_exclusion():
    own_argv = [
        PYTHON,
        str(MODULE_PATH),
        "--level",
        "L2",
        "--",
        "python3",
        "-m",
        "pytest",
        "-q",
        "tests/test_quiet_host_guard.py",
    ]
    # The guard's own command line really does carry a suite invocation ...
    assert qhg.is_runner_invocation(own_argv) is True
    # ... so the census alone would refuse it.
    control = qhg.scan_suites(_rows([_ps_line(5000, 4242, PYTHON, " ".join(own_argv))]), own_pid=None)
    assert [hit.pid for hit in control.roots] == [5000]
    # With its own pid known, the guard excludes itself: self-match excluded.
    rows = _rows(
        [
            _ps_line(5000, 4242, PYTHON, " ".join(own_argv)),
            _ps_line(
                5001,
                4242,
                PYTHON,
                f"{PYTHON} /Users/x/tree/scripts/quiet_host_guard.py --level L3 -- "
                "python3 -m pytest -q tests/l3.py",
            ),
        ]
    )
    owners = qhg.own_process_sets(rows, 5000)
    assert owners.self_pids == (5000,)
    assert owners.guard_invocations == (5001,)
    assert owners.ancestors == (4242,)
    assert owners.excluded == frozenset({5000, 5001}), (
        "only self and guard wrappers may be excluded; the ancestor chain must not be"
    )
    scan = qhg.scan_suites(rows, own_pid=5000)
    assert scan.roots == ()
    assert _evaluate(rows=rows, own_pid=5000).verdict == "GO"


@pytest.fixture(scope="module")
def live_lane_suite(tmp_path_factory):
    """A real running suite: ``python -m pytest`` plus a real descendant."""
    tmp = tmp_path_factory.mktemp("live_lane_suite")
    pidfile = tmp / "grandchild.pid"
    inner = tmp / "test_inner_sleeping_suite.py"
    inner.write_text(INNER_SUITE_SOURCE, encoding="utf-8")
    # Deterministic child environment: no inherited PYTHONPATH, and third-party
    # pytest plugins are not autoloaded, so this real suite cannot be taken down
    # by whatever the ambient shell exports.
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"}
    }
    env["QUIET_GUARD_PIDFILE"] = str(pidfile)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    proc = subprocess.Popen(
        [PYTHON, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", str(inner)],
        cwd=str(tmp),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    rows = None
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"inner suite exited early with {proc.returncode}")
        rows = qhg.read_process_census()
        if pidfile.exists() and any(row.pid == proc.pid and row.kind == "runner" for row in rows):
            break
        time.sleep(0.25)
    else:
        _kill_group(proc, pidfile)
        raise AssertionError("inner suite never appeared in the real process census")
    try:
        yield SimpleNamespace(pid=proc.pid, pidfile=pidfile, rows=rows, tmp=tmp)
    finally:
        _kill_group(proc, pidfile)


def _kill_group(proc, pidfile) -> None:
    try:
        grandchild = int(pidfile.read_text(encoding="utf-8").strip())
    except Exception:  # noqa: BLE001 - best-effort teardown
        grandchild = None
    if grandchild:
        try:
            os.kill(grandchild, signal.SIGKILL)
        except OSError:
            pass
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            proc.kill()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover - teardown safety
        pass


def _owning_pytest_runner(rows, own_pid: int):
    """The censused real pytest invocation that owns ``own_pid``, or ``None``.

    Single-process pytest: this process *is* the runner.  pytest-xdist (the L2
    gate runs ``-n8``): this process is an execnet worker whose own argv is the
    ``-u -c ... exec(eval(sys.stdin.readline()))`` bootstrap, so the pytest that
    owns it is the ``-m pytest`` driver above it in the process table.  The
    owner must be this process or one of its own ancestors, so a runner in a
    sibling lane - including the live suite this file itself spawns - can never
    satisfy the clause, and a bare interpreter ancestor (a non-suite argv) is
    not accepted by the guard's own classification.
    """
    by_pid = {row.pid: row for row in rows}
    if own_pid not in by_pid:
        return None
    owners = qhg.own_process_sets(rows, own_pid)
    for pid in (own_pid, *owners.ancestors):
        row = by_pid.get(pid)
        if row is not None and row.kind == "runner" and "-m pytest" in " ".join(row.argv):
            return row
    return None


def test_real_pytest_suite_and_its_descendant_are_detected(live_lane_suite):
    rows = qhg.read_process_census()
    by_pid = {row.pid: row for row in rows}
    inner = by_pid[live_lane_suite.pid]
    grandchild = int(live_lane_suite.pidfile.read_text(encoding="utf-8").strip())

    # The census row really is the live ``python -B -m pytest`` we spawned ...
    assert inner.kind == "runner"
    assert "-m pytest" in " ".join(inner.argv)
    # ... and the grandchild really is its own child in the process table.
    assert by_pid[grandchild].ppid == live_lane_suite.pid
    assert by_pid[grandchild].kind != "runner"

    scan = qhg.scan_suites(rows, own_pid=None)
    root_pids = {hit.pid for hit in scan.roots}
    descendant_pids = {hit.pid for hit in scan.descendants}
    assert live_lane_suite.pid in root_pids, "the real running suite must be found"
    assert grandchild not in root_pids, "the child is not itself a runner"

    # Descendant discovery, proven against the real suite alone: drop this
    # test's own process from the census so the only root left is the live
    # suite, then the grandchild must still be attributed to it.
    trimmed = tuple(row for row in rows if row.pid != inner.ppid)
    scan_inner = qhg.scan_suites(trimmed, own_pid=None)
    assert live_lane_suite.pid in {hit.pid for hit in scan_inner.roots}
    assert grandchild in {hit.pid for hit in scan_inner.descendants}, (
        "the live suite's real child must be discovered as its descendant"
    )

    report = qhg.evaluate(
        "L2", own_pid=None, load_reader=QUIET_LOAD, census_reader=lambda: rows
    )
    assert report.verdict == "NO-GO"
    assert qhg.R_LANE_SUITE in report.blocking_reasons
    assert live_lane_suite.pid in scan.suite_pids
    assert grandchild in descendant_pids
    # The human-readable reason is deliberately capped at five runners.
    # Assert membership in the complete structured report, not that this host
    # happened to have fewer than five other suites at census time.
    reported = report.to_json()["lane_suites"]
    assert {hit["pid"] for hit in reported["roots"]} == root_pids
    assert {hit["pid"] for hit in reported["descendants"]} == descendant_pids
    assert live_lane_suite.pid in reported["suite_pids"]
    assert grandchild in reported["suite_pids"]


def test_full_suite_report_survives_truncated_human_detail():
    """Seven runners plus their descendants all remain in the refusal payload."""
    roots = set(range(100, 107))
    children = {pid + 1000 for pid in roots}
    rows = _rows(
        [_ps_line(pid, 1, PYTHON, f"{PYTHON} -m pytest tests/lane-{pid}")
         for pid in sorted(roots)]
        + [_ps_line(pid + 1000, pid, PYTHON, f"{PYTHON} -c pass")
           for pid in sorted(roots)]
    )
    report = _evaluate(rows=rows, own_pid=9999)
    assert report.verdict == "NO-GO"
    assert report.blocking_reasons == (qhg.R_LANE_SUITE,)
    reported = report.to_json()["lane_suites"]
    assert {hit["pid"] for hit in reported["roots"]} == roots
    assert {hit["pid"] for hit in reported["descendants"]} == children
    assert set(reported["suite_pids"]) == roots | children
    detail = {reason.code: reason.detail for reason in report.reasons}[qhg.R_LANE_SUITE]
    assert "(+2 more)" in detail
    assert "pid=106" not in detail  # Omitted from prose, never from detection.


def test_own_process_tree_is_excluded_from_the_real_census(live_lane_suite):
    rows = qhg.read_process_census()
    own_pid = os.getpid()
    assert any(row.pid == own_pid for row in rows), "this test process must be censused"

    # Positive control, xdist-correct: a real pytest must own this process.  In a
    # single-process run this process *is* the runner; under ``-n`` this process
    # is the execnet interpreter child that the ``-m pytest`` driver above it
    # owns.  Either way the owner is a genuine censused runner invocation, and a
    # runner that is neither this process nor one of its ancestors (a sibling
    # lane, or the live suite this file spawns itself) never satisfies it.
    own_row = {row.pid: row for row in rows}[own_pid]
    assert own_row.kind in {"runner", "interpreter"}, (
        "this file must run inside a real interpreter: "
        f"own pid={own_pid} kind={own_row.kind!r} argv={own_row.argv!r}"
    )
    owner = _owning_pytest_runner(rows, own_pid)
    assert owner is not None, (
        "this file really is running under a real pytest process: "
        f"own pid={own_pid} kind={own_row.kind!r} comm={own_row.comm!r} "
        f"argv={own_row.argv!r}"
    )
    assert owner.kind == "runner" and "-m pytest" in " ".join(owner.argv)

    owners = qhg.own_process_sets(rows, own_pid)
    assert own_pid in owners.self_pids
    assert live_lane_suite.pid in owners.descendants

    scan = qhg.scan_suites(rows, own_pid=own_pid)
    assert own_pid not in {hit.pid for hit in scan.roots}, "self-match excluded"
    assert live_lane_suite.pid not in {hit.pid for hit in scan.roots}, "own descendant excluded"
    assert own_pid not in scan.suite_pids

    report = qhg.evaluate(
        "L2", own_pid=own_pid, load_reader=QUIET_LOAD, census_reader=lambda: rows
    )
    # Honest either way: the verdict must agree with what the exclusion left behind.
    assert (report.verdict == "GO") == (scan.roots == ())
    if scan.roots:
        assert qhg.R_LANE_SUITE in report.blocking_reasons


def test_owning_pytest_runner_requires_a_real_pytest_owner_and_no_mislabel():
    """Negative control for the self-identity premise above.

    The premise must be greened only by a genuine pytest that owns this process:
    a real ``python -m pytest`` running in another lane, a bare interpreter, and
    a non-pytest module invocation are all censused here and none of them may be
    accepted as the owner.  The guard's own classification is the same one the
    live suite uses, so a mislabel would have to be a product defect, not a
    weakened assertion.
    """
    worker = 5_101
    driver = 5_100
    sibling_lane = 5_200
    module_host = 5_300
    module_child = 5_301
    bootstrap = f"{PYTHON} -u -c import sys;exec(eval(sys.stdin.readline()))"
    rows = _rows(
        [
            _ps_line(driver, 1, PYTHON, f"{PYTHON} -B -m pytest -q -n2 tests/lane.py"),
            _ps_line(worker, driver, PYTHON, bootstrap),
            _ps_line(sibling_lane, 1, PYTHON, f"{PYTHON} -m pytest -q tests/other_lane.py"),
            _ps_line(module_host, 1, PYTHON, f"{PYTHON} -m mycelium_seed.service --serve"),
            _ps_line(module_child, module_host, PYTHON, bootstrap),
        ]
    )
    by_pid = {row.pid: row for row in rows}
    assert by_pid[driver].kind == "runner"
    assert by_pid[sibling_lane].kind == "runner", "the decoy lane is a real suite"
    assert by_pid[module_host].kind != "runner", (
        "a non-pytest module invocation must not classify as a runner: "
        f"{module_host} classified {by_pid[module_host].kind!r}"
    )

    # Single-process shape: this process is its own owner.
    own_owner = _owning_pytest_runner(rows, driver)
    assert own_owner is not None and own_owner.pid == driver
    # xdist shape: the ``-m pytest`` driver above the worker owns the worker.
    worker_owner = _owning_pytest_runner(rows, worker)
    assert worker_owner is not None and worker_owner.pid == driver
    # A suite in a sibling lane is not an owner, however real it is ...
    assert _owning_pytest_runner(rows, module_child) is None, (
        "a runner that is neither this process nor one of its ancestors must not "
        "be accepted as the owner"
    )
    # ... and the guard really does exclude only self and its own ancestors.
    owners = qhg.own_process_sets(rows, worker)
    assert sibling_lane not in owners.ancestors
    assert sibling_lane not in owners.descendants


# --------------------------------------------------------------------------- #
# the wrapped command runs only after a GO
# --------------------------------------------------------------------------- #


def test_wrapped_command_is_not_started_on_no_go(tmp_path):
    sentinel = tmp_path / "started.txt"
    command = [PYTHON, "-c", f"open({str(sentinel)!r}, 'w').write('ran')"]
    outcome = qhg.run_guarded(
        command,
        level="L2",
        own_pid=os.getpid(),
        load_reader=OVERLOADED_LOAD,
        census_reader=lambda: IDLE_HOST,
    )
    assert outcome.ran is False
    assert outcome.exit_code is None
    assert outcome.report.verdict == "NO-GO"
    assert sentinel.exists() is False, "a refused launch must not start the command"


def test_wrapped_command_runs_after_go(tmp_path):
    sentinel = tmp_path / "started.txt"
    command = [PYTHON, "-c", f"open({str(sentinel)!r}, 'w').write('ran')"]
    outcome = qhg.run_guarded(
        command,
        level="L3",
        own_pid=os.getpid(),
        load_reader=QUIET_LOAD,
        census_reader=lambda: IDLE_HOST,
    )
    assert outcome.ran is True
    assert outcome.exit_code == 0
    assert sentinel.read_text(encoding="utf-8") == "ran"
    assert outcome.report.wrapped_exit_code == 0

    failing = qhg.run_guarded(
        [PYTHON, "-c", "import sys; sys.exit(7)"],
        level="L3",
        own_pid=os.getpid(),
        load_reader=QUIET_LOAD,
        census_reader=lambda: IDLE_HOST,
    )
    assert failing.ran is True
    assert failing.exit_code == 7


# --------------------------------------------------------------------------- #
# CLI contract
# --------------------------------------------------------------------------- #


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, "-B", str(MODULE_PATH), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        check=False,
        timeout=120,
    )


def test_cli_emits_named_go_no_go_json_and_consistent_exit_code(tmp_path):
    sentinel = tmp_path / "cli-started.txt"
    completed = _run_cli(
        "--level",
        "L2",
        "--print-census",
        "--",
        PYTHON,
        "-c",
        f"open({str(sentinel)!r}, 'w').write('ran')",
    )
    document = json.loads(completed.stdout.strip().splitlines()[-1])
    for key in (
        "guard",
        "level",
        "verdict",
        "blocking_reasons",
        "reasons",
        "checks",
        "thresholds",
        "load1",
        "census",
        "lane_suites",
        "excluded_processes",
        "wrapped_command",
        "wrapped_exit_code",
        "read_only",
    ):
        assert key in document, key
    assert document["guard"] == qhg.GUARD_NAME
    assert document["read_only"] is True
    assert document["thresholds"] == {
        "load_average_window": "1min",
        "load1_max": 8.0,
        "overridable": False,
    }
    assert document["census"]["rows"] > 0
    assert document["census_rows"], "printed census must carry the real rows"
    # Ownership is narrow: the guard's own pid is excluded, but the lane's own
    # pytest run above it is NOT blanket-excluded - it is reported, and a suite
    # running alongside the guard is reported too.
    excluded = document["excluded_processes"]
    assert excluded["ancestor_chain_excluded"] is False
    assert os.getpid() in excluded["ancestor_chain"]
    assert os.getpid() not in excluded["excluded"]
    if document["verdict"] == "GO":
        assert completed.returncode == 0
        assert document["wrapped_exit_code"] == 0
        assert sentinel.exists()
    else:
        assert completed.returncode == 1
        assert document["wrapped_exit_code"] is None
        assert sentinel.exists() is False
        assert document["blocking_reasons"]
        if "lane_suite_active" in document["blocking_reasons"]:
            relationships = {hit["relationship"] for hit in document["lane_suites"]["roots"]}
            assert "ancestor" in relationships, "the invoking suite must not be hidden"
    # real discovery: this test's own pytest run is in the census it reported
    assert any(row["pid"] == os.getpid() for row in document["census_rows"])


def test_admitted_without_a_wrapped_command_does_nothing_and_is_not_a_refusal():
    outcome = qhg.run_guarded(
        (),
        level="L2",
        own_pid=os.getpid(),
        load_reader=QUIET_LOAD,
        census_reader=lambda: IDLE_HOST,
    )
    assert outcome.report.verdict == "GO"
    assert outcome.ran is False
    assert outcome.exit_code is None


def test_cli_evaluate_only_mode_emits_a_verdict_and_consistent_exit_code():
    for extra in ((), ("--census-only",)):
        completed = _run_cli("--level", "L2", *extra)
        document = json.loads(completed.stdout.strip().splitlines()[-1])
        assert document["wrapped_command"] == []
        assert document["wrapped_exit_code"] is None
        expected = 0 if document["verdict"] == "GO" else 1
        assert completed.returncode == expected, document["blocking_reasons"]


def test_cli_refuses_unknown_level():
    completed = _run_cli("--level", "L5")
    document = json.loads(completed.stdout.strip().splitlines()[-1])
    assert completed.returncode == 1
    assert document["verdict"] == "NO-GO"
    assert qhg.R_LEVEL_NOT_GATED in document["blocking_reasons"]


def test_cli_missing_level_is_a_usage_refusal():
    completed = _run_cli("--print-census")
    document = json.loads(completed.stdout.strip().splitlines()[-1])
    assert completed.returncode == 2
    assert document["verdict"] == "NO-GO"
    assert qhg.R_USAGE in document["blocking_reasons"]


# --------------------------------------------------------------------------- #
# no threshold override, anywhere
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "flag",
    [
        "--load-threshold",
        "--max-load=99",
        "--load1-max",
        "--force",
        "--skip-load-check",
        "--ignore-load",
        "--no-guard",
        "--override",
    ],
)
def test_cli_refuses_every_threshold_override_attempt(flag, tmp_path):
    sentinel = tmp_path / "override-started.txt"
    completed = _run_cli(
        "--level",
        "L2",
        flag,
        "99",
        "--",
        PYTHON,
        "-c",
        f"open({str(sentinel)!r}, 'w').write('ran')",
    )
    document = json.loads(completed.stdout.strip().splitlines()[-1])
    assert completed.returncode != 0
    assert document["verdict"] == "NO-GO"
    assert qhg.R_OVERRIDE_REFUSED in document["blocking_reasons"]
    assert sentinel.exists() is False


def test_the_ceiling_is_not_a_knob():
    assert qhg.LOAD1_MAX == 8.0
    assert qhg.GATED_LEVELS == ("L2", "L3", "L4")
    for function in (qhg.evaluate, qhg.run_guarded):
        names = set(inspect.signature(function).parameters)
        assert not {name for name in names if "threshold" in name or "force" in name}
        assert not {name for name in names if "load_max" in name or "max_load" in name}
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "os.environ" not in source
    assert "getenv" not in source
    assert "environ[" not in source


# --------------------------------------------------------------------------- #
# suite families beyond pytest: npm/vitest, node --test, Playwright, cargo, go
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "argv",
    [
        "npm test",
        "npm t",
        "npm run test",
        "npm run test:unit",
        "npm run vitest",
        "npm exec vitest",
        "pnpm test",
        "pnpm run test:e2e",
        "pnpm dlx vitest run",
        "yarn test",
        "yarn vitest",
        "npx vitest run",
        "npx --yes playwright test",
        "npx jest --ci",
        "vitest run",
        "jest --ci --coverage",
        "mocha tests/",
        "ava --tap",
        "playwright test tests/e2e",
        "cypress run --browser chrome",
        "karma start karma.conf.js",
        "wdio run wdio.conf.ts",
        "nightwatch --env chrome",
        "testcafe chrome tests/",
        "node --test",
        "node --test --test-reporter=spec tests/",
        "node --test-concurrency=4 --test tests/",
        "/opt/homebrew/bin/node --run test",
        "bun test",
        "deno test --allow-net",
        "tsx --test tests/x.test.ts",
        "cargo test",
        "cargo test --release --workspace",
        "cargo nextest run",
        "go test ./...",
    ],
)
def test_every_supported_suite_family_is_detected(argv):
    comm = argv.split(" ", 1)[0]
    rows = _rows([_ps_line(7200, 1, comm, argv)])
    assert rows[0].kind == "runner", rows[0]


@pytest.mark.parametrize(
    "argv",
    [
        "node server.js --port 3000",
        "node server.js --test-mode",
        "node --version",
        "node -e \"require('pytest')\"",
        "npm install",
        "npm ci",
        "npm install vitest",
        "npm run build",
        "npm run dev",
        "npm run lint",
        "npm audit --json",
        "yarn add vitest",
        "pnpm add -D playwright",
        "pnpm install --frozen-lockfile",
        "npx tsc --noEmit",
        "npx eslint .",
        "cargo build --release",
        "cargo run --example demo",
        "cargo tree",
        "go build ./cmd/node",
        "go vet ./...",
        "bash -lc npm test",
    ],
)
def test_ordinary_tooling_is_not_a_suite(argv):
    comm = argv.split(" ", 1)[0]
    rows = _rows([_ps_line(7300, 1, comm, argv)])
    assert rows[0].kind != "runner", rows[0]
    assert _evaluate(rows=rows).verdict == "GO"


def test_root_ancestor_and_sibling_suites_are_not_hidden():
    """A suite above the guard and a suite beside it are both refused.

    This is the ownership regression: excluding the ancestor chain, or every
    descendant of a shared root, would silently hide exactly these.
    """
    rows = _rows(
        [
            _ps_line(1000, 1, PYTHON, f"{PYTHON} -m pytest -q tests/lane-own"),
            _ps_line(
                1001,
                1000,
                PYTHON,
                f"{PYTHON} scripts/quiet_host_guard.py --level L2 -- "
                "python3 -m pytest -q tests/x.py",
            ),
            _ps_line(1002, 1000, PYTHON, f"{PYTHON} -m pytest -q -n 8 tests/"),
            _ps_line(1003, 1000, "/usr/local/bin/npm", "npm test"),
            _ps_line(1004, 1, PYTHON, f"{PYTHON} /Users/x/reasoning_proxy.py --port 18088"),
        ]
    )
    scan = qhg.scan_suites(rows, own_pid=1001)
    hits = {hit.pid: hit for hit in scan.roots}
    assert set(hits) == {1000, 1002, 1003}
    assert hits[1000].relationship == "ancestor"
    assert hits[1002].relationship == "sibling"
    assert hits[1003].relationship == "sibling"
    assert 1004 not in hits, "a data-plane python process is not a suite"
    assert scan.owners.excluded == frozenset({1001})
    assert 1000 not in scan.owners.excluded

    report = _evaluate(rows=rows, own_pid=1001)
    assert report.verdict == "NO-GO"
    assert qhg.R_LANE_SUITE in report.blocking_reasons
    detail = {reason.code: reason.detail for reason in report.reasons}[qhg.R_LANE_SUITE]
    assert "rel=ancestor" in detail
    assert "rel=sibling" in detail


NODE_SUITE_SOURCES = {
    "t.test.js": (
        "const {test} = require('node:test');\n"
        "const fs = require('node:fs');\n"
        "test('sleeping node suite', async () => {\n"
        "  fs.writeFileSync(process.env.QUIET_GUARD_PIDFILE, String(process.pid));\n"
        "  await new Promise((resolve) => setTimeout(resolve, 120000));\n"
        "});\n"
    ),
    "package.json": (
        '{"name": "quiet-guard-fixture", "version": "1.0.0", "private": true,'
        ' "scripts": {"test": "node --test --test-reporter=spec t.test.js"}}'
    ),
}


@pytest.fixture(scope="module")
def live_node_suite(tmp_path_factory):
    """A real ``npm test`` suite: npm -> sh -> node --test -> test file process."""
    npm = shutil.which("npm")
    if npm is None:  # pragma: no cover - host without node/npm
        pytest.skip("npm is not available on this host")
    tmp = tmp_path_factory.mktemp("live_node_suite")
    pidfile = tmp / "node-suite.pid"
    for name, content in NODE_SUITE_SOURCES.items():
        (tmp / name).write_text(content, encoding="utf-8")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"}
    }
    # Keep npm's own state inside the fixture: nothing global is touched.
    env.update(
        QUIET_GUARD_PIDFILE=str(pidfile),
        npm_config_cache=str(tmp / "npm-cache"),
        npm_config_update_notifier="false",
        npm_config_fund="false",
        npm_config_audit="false",
        npm_config_loglevel="error",
        NO_COLOR="1",
    )
    proc = subprocess.Popen(
        [npm, "test"],
        cwd=str(tmp),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"npm suite exited early with {proc.returncode}")
        rows = qhg.read_process_census()
        if pidfile.exists() and _node_runner_descends_from(rows, proc.pid):
            break
        time.sleep(0.25)
    else:
        _kill_group(proc, pidfile)
        raise AssertionError("npm suite never appeared in the real process census")
    try:
        yield SimpleNamespace(pid=proc.pid, pidfile=pidfile, tmp=tmp)
    finally:
        _kill_group(proc, pidfile)


def _descends_from(by_pid, pid: int, ancestor: int) -> bool:
    cursor = by_pid[pid].ppid if pid in by_pid else 0
    hops = 0
    while cursor > 0 and hops < 64:
        if cursor == ancestor:
            return True
        row = by_pid.get(cursor)
        if row is None:
            return False
        cursor = row.ppid
        hops += 1
    return False


def _node_runner_descends_from(rows, root_pid: int) -> bool:
    by_pid = {row.pid: row for row in rows}
    return any(
        row.kind == "runner"
        and any(token == "--test" for token in row.argv)
        and _descends_from(by_pid, row.pid, root_pid)
        for row in rows
    )


def test_real_npm_suite_and_its_node_descendants_are_detected(live_node_suite):
    rows = qhg.read_process_census()
    by_pid = {row.pid: row for row in rows}
    npm_pid = live_node_suite.pid
    file_pid = int(live_node_suite.pidfile.read_text(encoding="utf-8").strip())

    # npm rewrites its own process title to bare ``npm``, so its arguments are
    # not visible: it must NOT be called a suite on its own, and the run is
    # found through its real descendants instead.
    assert by_pid[npm_pid].kind != "runner", "a bare title-rewritten npm is not a suite"

    scan = qhg.scan_suites(rows, own_pid=None)
    root_pids = {hit.pid for hit in scan.roots}
    descendant_pids = {hit.pid for hit in scan.descendants}
    node_runner_pids = {
        row.pid
        for row in rows
        if row.kind == "runner" and any(token == "--test" for token in row.argv)
    }
    assert node_runner_pids, "node --test must be detected as a suite runner"
    assert node_runner_pids <= root_pids
    assert any(_descends_from(by_pid, pid, npm_pid) for pid in node_runner_pids), (
        "the node runner must be a real descendant of the npm invocation"
    )
    assert by_pid[file_pid].ppid in node_runner_pids
    assert file_pid in descendant_pids, (
        "the node test process must be attributed to the detected suite"
    )
    assert by_pid[file_pid].kind != "runner"

    report = qhg.evaluate(
        "L2", own_pid=None, load_reader=QUIET_LOAD, census_reader=lambda: rows
    )
    assert report.verdict == "NO-GO"
    assert qhg.R_LANE_SUITE in report.blocking_reasons


def test_guard_cli_does_not_hide_ancestor_or_sibling_suites(live_lane_suite):
    """Real sibling evidence: the guard refuses rather than hiding its caller."""
    completed = _run_cli("--level", "L2", "--print-census")
    document = json.loads(completed.stdout.strip().splitlines()[-1])
    excluded = document["excluded_processes"]
    assert excluded["ancestor_chain_excluded"] is False
    assert os.getpid() in excluded["ancestor_chain"]
    assert os.getpid() not in excluded["excluded"]
    assert live_lane_suite.pid not in excluded["excluded"]

    assert document["verdict"] == "NO-GO", document["blocking_reasons"]
    assert "lane_suite_active" in document["blocking_reasons"]
    assert document["wrapped_exit_code"] is None
    assert completed.returncode == 1

    hits = {hit["pid"]: hit for hit in document["lane_suites"]["roots"]}
    assert live_lane_suite.pid in hits, "the suite running beside the guard must be reported"
    assert hits[live_lane_suite.pid]["relationship"] == "sibling"
    assert any(hit["relationship"] == "ancestor" for hit in hits.values())
