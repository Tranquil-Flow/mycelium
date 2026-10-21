#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""quiet_host_guard.py -- fail-closed quiet-host admission for L2/L3/L4.

PLAN 2.1 says L2, L3 and L4 "refuse to start if the 1-minute load average is
above 8, or if any lane child is running a suite".  PLAN 2.4 says the launcher
enforces that refusal.  This module is the decision, as a script rather than a
promise:

* The one-minute load ceiling is a module constant with **no override path**:
  there is no flag, no environment variable and no keyword argument that can
  change it.  Asking for one is itself a refusal (exit 2).
* The process census is **read-only**: one ``ps`` invocation, no signals, no
  writes, no ``/proc`` assumptions.
* Suite detection is **executable-first** and covers the runners this project
  uses: pytest and its launchers, the node family (``vitest``, ``jest``,
  ``playwright test``, ``node --test``, ``bun test``, ``deno test``), the
  package managers (``npm test``, ``npm run test:unit``, ``npx vitest``),
  ``cargo test`` / ``cargo nextest run`` and ``go test``.  Detection keys off
  the executable plus its arguments, so a browser whose ``--user-data-dir``
  happens to contain a ``pytest-of-<user>`` path and arbitrary text arguments
  are not mistaken for a run.
* Ownership is deliberately narrow.  Only the guard's own process, its own
  descendants (which can only exist after a GO) and wrappers whose argv names
  the guard script are excluded.  An **ancestor chain is never excluded**: a
  suite running above the guard, or alongside it as a sibling, is always
  reported and refused.
* Every fact that cannot be established is a refusal, never a pass: an
  unreadable load average, a non-finite load average, or an unreadable process
  table yields NO-GO with a named reason.
* The wrapped command is never started before the guard passes.

Usage
-----
``python3 scripts/quiet_host_guard.py --level L2 [--print-census] -- <cmd...>``

One JSON document is always written to stdout (single line):

``{"guard": ..., "verdict": "GO"|"NO-GO", "blocking_reasons": [...], ...}``

Exit codes
----------
``0``  GO.  With a wrapped command, that command ran and itself exited 0; with
       no wrapped command, the guard admitted the host and started nothing.
``1``  NO-GO on host conditions (overloaded host, unknown load, active lane
       suite, unreadable census, level outside L2/L3/L4).  A wrapped command is
       never started.
``2``  The guard was asked to do something it will never do: change a
       threshold, or an unusable command line.  Also NO-GO, also nothing
       started.

When the verdict is GO *and* a command was wrapped, that command's exit code is
returned verbatim, so a nonzero exit code on its own does not mean the guard
refused -- read the JSON.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

GUARD_NAME = "quiet_host_guard"
GUARD_VERSION = 1

#: Levels the quiet-host guard gates (PLAN 2.1/2.4).
GATED_LEVELS: tuple[str, ...] = ("L2", "L3", "L4")

#: Immutable 1-minute load ceiling.  See module docstring: no override path.
LOAD1_MAX: float = 8.0

#: Read-only census command.  ``-ww`` avoids arg truncation; no header is
#: printed because every field uses the ``=`` suffix.
CENSUS_ARGV: tuple[str, ...] = ("ps", "-Aww", "-o", "pid=,ppid=,comm=,args=")
CENSUS_TIMEOUT_S: float = 15.0
CENSUS_ROW_LIMIT: int = 6000

#: A row whose argv names this file is a guard wrapper, not a running suite.
GUARD_MARKER = "quiet_host_guard.py"

#: Executable names that mean "browser engine": excluded even when their argv
#: mentions pytest (e.g. ``--user-data-dir=.../pytest-of-x/pytest-7``).  The
#: Playwright and Electron *launchers* are deliberately absent: ``playwright
#: test`` is a suite runner, and the browser it drives is a chromium or
#: headless_shell binary, which is covered here.
BROWSER_MARKERS: tuple[str, ...] = (
    "chrome",
    "chromium",
    "safari",
    "firefox",
    "msedge",
    "brave",
    "webkit",
    "geckodriver",
    "headless_shell",
)

#: pytest entry points.
PYTEST_TOKENS: frozenset[str] = frozenset({"pytest", "py.test", "pytest3", "pytest-3"})

#: Node/JS suite runners: these CLIs are suites whatever their arguments say.
NODE_RUNNER_TOKENS: frozenset[str] = frozenset(
    {"vitest", "jest", "mocha", "ava", "tap", "jasmine", "testcafe", "web-test-runner", "nightwatch"}
)

#: Browser-driving CLIs: a suite only with an explicit test subcommand
#: (``playwright test``, ``cypress run``, ``karma start``).
NODE_RUNNER_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "playwright": frozenset({"test", "tests"}),
    "cypress": frozenset({"run", "open"}),
    "wdio": frozenset({"run"}),
    "karma": frozenset({"start"}),
}

#: Node-family interpreters that can host a built-in test runner.
NODE_INTERPRETERS: frozenset[str] = frozenset({"node", "nodejs", "bun", "deno", "tsx", "ts-node"})

#: The flags that really start node's built-in runner.  Text arguments that
#: merely look like them (``node server.js --test-mode``) do not count.
NODE_TEST_FLAGS: frozenset[str] = frozenset({"--test", "--test-only"})
NODE_TEST_FLAG_PREFIXES: frozenset[str] = frozenset(
    {
        "--test-reporter",
        "--test-name-pattern",
        "--test-concurrency",
        "--test-shard",
        "--test-timeout",
        "--test-force-exit",
        "--test-coverage",
    }
)

#: Package managers run a suite only through a script or a runner binary.
PACKAGE_MANAGERS: frozenset[str] = frozenset({"npm", "pnpm", "yarn", "npx", "pnpx", "bunx"})

#: Package-manager subcommands that execute a script or a binary.
PM_EXECUTE_TOKENS: frozenset[str] = frozenset({"run", "run-script", "exec", "x", "dlx", "t"})

#: Package-manager subcommands that never run a suite.
PM_PASSIVE_TOKENS: frozenset[str] = frozenset(
    {
        "add", "remove", "rm", "uninstall", "install", "i", "ci", "why", "outdated",
        "audit", "config", "init", "create", "up", "upgrade", "update", "patch",
        "publish", "pack", "link", "list", "ls", "cache", "help", "view", "info",
        "search", "licenses", "login", "logout", "owner", "team", "fund", "repo", "docs",
    }
)

#: Rust and Go suite runners.
CARGO_TOKENS: frozenset[str] = frozenset({"cargo", "cargo-nextest"})

#: Test-ish script names, honoured only in the script position of a package manager.
_SCRIPT_NAME = re.compile(
    r"^(tests?|t|specs?|e2e|unit|integration|emulation|physical|acceptance|regression|ci)([-:].*)?$",
    re.IGNORECASE,
)

#: Commands that can start a supported suite after ``run``/``exec``/``--``.
RUNNER_HEADS: frozenset[str] = (
    PYTEST_TOKENS
    | NODE_RUNNER_TOKENS
    | frozenset(NODE_RUNNER_SUBCOMMANDS)
    | CARGO_TOKENS
    | NODE_INTERPRETERS
    | PACKAGE_MANAGERS
    | frozenset({"go"})
)

#: Interpreters that can host ``-m pytest``.
INTERPRETER_PREFIXES: tuple[str, ...] = ("python", "pypy", "graalpy")

#: Runner launchers that can host ``run pytest``.
LAUNCHER_TOKENS: frozenset[str] = frozenset(
    {"uv", "tox", "nox", "hatch", "pdm", "poetry", "pipenv"}
)

#: Named reason codes.  A NO-GO always carries at least one of these.
R_LOAD_OVER = "load_over_limit"
R_LOAD_UNKNOWN = "load_unknown"
R_LANE_SUITE = "lane_suite_active"
R_CENSUS_UNAVAILABLE = "census_unavailable"
R_LEVEL_NOT_GATED = "level_not_gated"
R_OVERRIDE_REFUSED = "threshold_override_refused"
R_USAGE = "usage_error"

BLOCKING = "blocking"
INFO = "info"

#: Any option that looks like an attempt to move a threshold is refused.  The
#: guard has no such option, so one can only ever arrive here as an override
#: attempt.
_OVERRIDE_PATTERN = re.compile(
    r"threshold|force|skip|ignore|bypass|override|no[-_]?guard"
    r"|max[-_]?load|load[0-9]?[-_]?max",
    re.IGNORECASE,
)
_COMBINED_PYTEST = re.compile(r"-mpytest")

USAGE = (
    "usage: quiet_host_guard.py --level {L2|L3|L4} [--print-census] "
    "[--census-only] [--json] [-- <command...>]"
)


class LoadUnavailable(RuntimeError):
    """The 1-minute load average could not be established."""


class CensusUnavailable(RuntimeError):
    """The read-only process census could not be established."""


class ThresholdOverrideRefused(RuntimeError):
    """The caller tried to move a threshold the guard owns."""


class UsageError(RuntimeError):
    """The command line is not usable."""


@dataclass(frozen=True)
class Reason:
    code: str
    detail: str
    severity: str = BLOCKING

    def to_json(self) -> dict[str, object]:
        return {"code": self.code, "detail": self.detail, "severity": self.severity}


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # "pass" | "blocking" | "unknown"
    detail: str

    def to_json(self) -> dict[str, object]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class ProcessRow:
    pid: int
    ppid: int
    comm: str
    argv: tuple[str, ...]
    kind: str  # "browser" | "runner" | "interpreter" | "other"

    def to_json(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "kind": self.kind,
            "executable": _basename(self.argv[0]) if self.argv else _basename(self.comm),
            "argv_head": _argv_head(self.argv),
        }


@dataclass(frozen=True)
class SuiteHit:
    pid: int
    ppid: int
    executable: str
    role: str  # "runner" | "descendant"
    argv_head: str
    relationship: str = "unrelated"  # "ancestor" | "sibling" | "unrelated"

    def to_json(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "executable": self.executable,
            "role": self.role,
            "relationship": self.relationship,
            "argv_head": self.argv_head,
        }


@dataclass(frozen=True)
class OwnSets:
    """Ownership of the processes around the guard.

    Exactly three groups are excluded, and the exclusion is deliberately
    narrow: this process, its own descendants (which can only exist after a GO)
    and wrappers whose argv names the guard script.  The **ancestor chain is
    reported but never excluded**, so a suite running above the guard, or
    alongside it as a sibling, cannot be hidden by ownership.
    """

    self_pids: tuple[int, ...]
    ancestors: tuple[int, ...]
    descendants: tuple[int, ...]
    guard_invocations: tuple[int, ...]

    @property
    def excluded(self) -> frozenset[int]:
        return frozenset(
            set(self.self_pids) | set(self.descendants) | set(self.guard_invocations)
        )

    def to_json(self) -> dict[str, object]:
        return {
            "self": list(self.self_pids),
            "descendants": list(self.descendants),
            "guard_invocations": list(self.guard_invocations),
            "excluded": sorted(self.excluded),
            "ancestor_chain": list(self.ancestors),
            "ancestor_chain_excluded": False,
        }


@dataclass(frozen=True)
class SuiteScan:
    roots: tuple[SuiteHit, ...]
    descendants: tuple[SuiteHit, ...]
    owners: OwnSets
    considered: int
    ancestor_rows: tuple[ProcessRow, ...] = ()

    @property
    def suite_pids(self) -> tuple[int, ...]:
        return tuple(sorted({hit.pid for hit in self.roots + self.descendants}))

    def to_json(self) -> dict[str, object]:
        return {
            "roots": [hit.to_json() for hit in self.roots],
            "descendants": [hit.to_json() for hit in self.descendants],
            "suite_pids": list(self.suite_pids),
            "considered_rows": self.considered,
            "ancestor_chain": [
                {
                    "pid": row.pid,
                    "runner": row.kind == "runner",
                    "argv_head": _argv_head(row.argv),
                }
                for row in self.ancestor_rows
            ],
        }


@dataclass(frozen=True)
class GuardReport:
    level: str
    verdict: str
    reasons: tuple[Reason, ...]
    checks: tuple[Check, ...]
    load1: float | None
    rows: tuple[ProcessRow, ...]
    scan: SuiteScan | None
    census_error: str | None
    wrapped_command: tuple[str, ...]
    wrapped_exit_code: int | None
    emitted_at: str

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        return tuple(reason.code for reason in self.reasons if reason.severity == BLOCKING)

    def to_json(self, *, include_rows: bool = False) -> dict[str, object]:
        kinds: dict[str, int] = {}
        for row in self.rows:
            kinds[row.kind] = kinds.get(row.kind, 0) + 1
        document: dict[str, object] = {
            "guard": GUARD_NAME,
            "version": GUARD_VERSION,
            "read_only": True,
            "level": self.level,
            "verdict": self.verdict,
            "blocking_reasons": list(self.blocking_reasons),
            "reasons": [reason.to_json() for reason in self.reasons],
            "checks": [check.to_json() for check in self.checks],
            "thresholds": {
                "load_average_window": "1min",
                "load1_max": LOAD1_MAX,
                "overridable": False,
            },
            "load1": self.load1,
            "census": {
                "source": " ".join(CENSUS_ARGV),
                "rows": len(self.rows),
                "kinds": kinds,
                "error": self.census_error,
            },
            "lane_suites": self.scan.to_json() if self.scan is not None else None,
            "excluded_processes": self.scan.owners.to_json() if self.scan is not None else None,
            "wrapped_command": list(self.wrapped_command),
            "wrapped_exit_code": self.wrapped_exit_code,
            "emitted_at": self.emitted_at,
        }
        if include_rows:
            document["census_rows"] = [row.to_json() for row in self.rows]
        return document


@dataclass(frozen=True)
class GuardOutcome:
    report: GuardReport
    ran: bool
    exit_code: int | None


def _basename(value: str) -> str:
    text = value.strip().strip('"').strip("'")
    return text.rsplit("/", 1)[-1]


def _argv_head(argv: Sequence[str], limit: int = 220) -> str:
    text = " ".join(argv)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _is_browser_name(name: str) -> bool:
    lowered = name.lower()
    for marker in BROWSER_MARKERS:
        if lowered.startswith(marker):
            return True
        if len(marker) >= 5 and marker in lowered:
            return True
    return False


def _is_interpreter_name(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.startswith(prefix) for prefix in INTERPRETER_PREFIXES)


def _code_invokes_pytest(code: str) -> bool:
    if "pytest" not in code:
        return False
    return "pytest.main" in code or "import pytest" in code or "pytest." in code


def _script_is_suite(name: str) -> bool:
    """True when a package-manager script name runs a suite."""
    lowered = _basename(name).lower()
    if lowered in PYTEST_TOKENS or lowered in NODE_RUNNER_TOKENS:
        return True
    if lowered in NODE_RUNNER_SUBCOMMANDS or lowered in CARGO_TOKENS:
        return True
    return bool(_SCRIPT_NAME.match(lowered))


def _node_runner_cli(head: str, rest: Sequence[str]) -> bool:
    if head in NODE_RUNNER_TOKENS:
        return True
    subcommands = NODE_RUNNER_SUBCOMMANDS.get(head)
    if subcommands is None:
        return False
    return any(_basename(token).lower() in subcommands for token in rest)


def _node_interpreter_suite(head: str, rest: Sequence[str]) -> bool:
    tokens = [_basename(token).lower() for token in rest]
    if head in {"bun", "deno"}:
        return "test" in tokens[:2]
    for index, token in enumerate(tokens):
        if token in NODE_TEST_FLAGS:
            return True
        if token.split("=", 1)[0] in NODE_TEST_FLAG_PREFIXES:
            return True
        if token == "--run" and index + 1 < len(tokens) and tokens[index + 1] in {"test", "t"}:
            return True
    # ``node node_modules/.bin/vitest run``, ``node .../.bin/playwright test``:
    # the JS runner is the first non-flag argument.
    command = _first_command(rest)
    if command is not None:
        found, tail = command
        if found != head and _runner_head_suite(found, tail, rest):
            return True
    return False


def _first_command(tokens: Sequence[str]) -> tuple[str, Sequence[str]] | None:
    """First non-flag token as (command, rest), e.g. ``npx --yes vitest run``."""
    for index, token in enumerate(tokens):
        if not token.startswith("-"):
            return _basename(token).lower(), tuple(tokens[index + 1 :])
    return None


def _package_manager_suite(head: str, args: Sequence[str]) -> bool:
    if head in {"npx", "pnpx", "bunx"}:
        command = _first_command(args)
        if command is None:
            return False
        found, rest = command
        return _script_is_suite(found) or _runner_head_suite(found, rest, args)
    if not args:
        return False
    first = _basename(args[0]).lower()
    if first in {"test", "t"}:
        return True
    if first in PM_EXECUTE_TOKENS:
        command = _first_command(args[1:])
        if command is None:
            return False
        found, rest = command
        return _script_is_suite(found) or _runner_head_suite(found, rest, args)
    if first in PM_PASSIVE_TOKENS:
        return False
    # yarn / pnpm bare script position: ``yarn test``, ``pnpm test:unit``.
    return _script_is_suite(first) or _runner_head_suite(first, tuple(args[1:]), args)


def _python_suite(head: str, args: Sequence[str]) -> bool:
    tokens = [_basename(token).lower() for token in args]
    for index, token in enumerate(tokens):
        follower = tokens[index + 1] if index + 1 < len(tokens) else ""
        if token == "-m" and follower in PYTEST_TOKENS:
            return True
        if head in LAUNCHER_TOKENS and token in {"run", "exec", "--"} and follower in RUNNER_HEADS:
            return True
    if any(_COMBINED_PYTEST.fullmatch(token) for token in tokens):
        return True
    raw = list(args)
    for index, token in enumerate(raw):
        if token == "-c" and index + 1 < len(raw):
            if _code_invokes_pytest(" ".join(raw[index + 1 :])):
                return True
    return False


def _runner_head_suite(head: str, rest: Sequence[str], full: Sequence[str]) -> bool:
    """True when the command named ``head`` starts one of the supported suites."""
    if head in PYTEST_TOKENS:
        return True
    if head in NODE_RUNNER_TOKENS or head in NODE_RUNNER_SUBCOMMANDS:
        return _node_runner_cli(head, rest)
    if head in CARGO_TOKENS:
        tokens = [_basename(token).lower() for token in rest]
        return "test" in tokens or "nextest" in tokens
    if head == "go":
        return [_basename(token).lower() for token in rest[:1]] == ["test"]
    if head in NODE_INTERPRETERS:
        return _node_interpreter_suite(head, rest)
    if head in PACKAGE_MANAGERS:
        return _package_manager_suite(head, rest)
    if _is_interpreter_name(head) or head in LAUNCHER_TOKENS:
        return _python_suite(head, rest)
    return False


def is_runner_invocation(argv: Sequence[str]) -> bool:
    """True only when this argv really starts a supported suite.

    Executable-first across the runners this project actually uses: pytest and
    its launchers, the node family (``vitest``, ``jest``, ``playwright test``,
    ``node --test``, ``bun test``, ``deno test``), the package managers
    (``npm test``, ``npm run test:unit``, ``npx vitest``), ``cargo test`` /
    ``cargo nextest run`` and ``go test``.

    Ordinary ``node server.js``, ``npm install``, ``cargo build``, ``yarn add
    vitest``, a browser whose user-data-dir carries a pytest-shaped path and
    arbitrary text arguments are not suites.
    """
    if not argv:
        return False
    head = _basename(argv[0]).lower()
    args = tuple(argv[1:])
    return _runner_head_suite(head, args, argv)


def classify_process(argv: Sequence[str], comm: str) -> str:
    """Classify one census row: browser | runner | interpreter | other."""
    names = [name for name in (_basename(comm), _basename(argv[0]) if argv else "") if name]
    if any(_is_browser_name(name) for name in names):
        return "browser"
    if is_runner_invocation(argv):
        return "runner"
    if any(_is_interpreter_name(name) or name.lower() in LAUNCHER_TOKENS for name in names):
        return "interpreter"
    return "other"


def parse_census(text: str, *, limit: int = CENSUS_ROW_LIMIT) -> tuple[ProcessRow, ...]:
    """Parse ``ps -Aww -o pid=,ppid=,comm=,args=`` output into rows."""
    rows: list[ProcessRow] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        comm = parts[2]
        argv = tuple(parts[3].split()) if len(parts) > 3 and parts[3].strip() else ()
        rows.append(
            ProcessRow(pid=pid, ppid=ppid, comm=comm, argv=argv, kind=classify_process(argv, comm))
        )
        if len(rows) >= limit:
            break
    return tuple(rows)


def read_process_census(
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    argv: Sequence[str] = CENSUS_ARGV,
    timeout: float = CENSUS_TIMEOUT_S,
) -> tuple[ProcessRow, ...]:
    """Read the process table once.  Read-only: no signals, no writes."""
    try:
        completed = runner(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:  # pragma: no cover - ps always present
        raise CensusUnavailable(f"census command unavailable: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CensusUnavailable(f"census command timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise CensusUnavailable(f"census command failed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip() or f"exit {completed.returncode}"
        raise CensusUnavailable(f"census command exited {completed.returncode}: {detail}")
    rows = parse_census(completed.stdout or "")
    if not rows:
        raise CensusUnavailable("census returned an empty process table")
    return rows


def read_load1(load_reader: Callable[[], Sequence[float]] | None = None) -> float:
    """Return the 1-minute load average, or raise LoadUnavailable."""
    reader = load_reader if load_reader is not None else os.getloadavg
    try:
        values = reader()
    except Exception as exc:  # noqa: BLE001 - any failure to read is unknown
        raise LoadUnavailable(f"1-minute load average unreadable: {type(exc).__name__}: {exc}") from exc
    try:
        value = float(values[0])
    except Exception as exc:  # noqa: BLE001
        raise LoadUnavailable(f"1-minute load average unusable: {exc}") from exc
    if not math.isfinite(value) or value < 0.0:
        raise LoadUnavailable(f"1-minute load average not a usable value: {value!r}")
    return value


def _children_map(rows: Iterable[ProcessRow]) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for row in rows:
        children.setdefault(row.ppid, []).append(row.pid)
    return children


def _descendants(children: dict[int, list[int]], roots: Iterable[int]) -> set[int]:
    seen: set[int] = set()
    queue = [pid for pid in roots]
    while queue:
        pid = queue.pop()
        for child in children.get(pid, ()):  # pragma: no branch
            if child in seen:
                continue
            seen.add(child)
            queue.append(child)
    return seen


def own_process_sets(
    rows: Sequence[ProcessRow],
    own_pid: int,
    *,
    guard_marker: str = GUARD_MARKER,
) -> OwnSets:
    """The processes the guard must not read as another lane's suite.

    * its own pid;
    * its ancestor chain (the wrapper invocation, which carries the command to
      be wrapped, and the lane's own focused run above it);
    * its descendants (only ever created after a GO);
    * any process whose argv names the guard script (a sibling guard wrapper).

    A concurrently running suite in a sibling lane is *not* in this set and is
    still detected.
    """
    by_pid = {row.pid: row for row in rows}
    ancestors: list[int] = []
    cursor = by_pid.get(own_pid)
    guard = 0
    while cursor is not None and guard < 64:
        parent = cursor.ppid
        if parent <= 0 or parent == own_pid or parent in ancestors:
            break
        ancestors.append(parent)
        cursor = by_pid.get(parent)
        guard += 1
    children = _children_map(rows)
    descendants = _descendants(children, [own_pid])
    invocations = tuple(
        row.pid
        for row in rows
        if row.pid != own_pid
        # Exact basename match only: a runner executing
        # ``tests/test_quiet_host_guard.py`` is a suite, not a guard wrapper.
        and any(_basename(token) == guard_marker for token in row.argv)
    )
    return OwnSets(
        self_pids=(own_pid,),
        ancestors=tuple(ancestors),
        descendants=tuple(sorted(descendants)),
        guard_invocations=invocations,
    )


def scan_suites(rows: Sequence[ProcessRow], *, own_pid: int | None = None) -> SuiteScan:
    """Find real lane suites and their descendants in a census.

    ``own_pid`` is the guard's own pid.  Only self, own descendants and guard
    wrappers are excluded.  Runners above the guard are reported with
    ``relationship="ancestor"`` and runners sharing the guard's parent with
    ``relationship="sibling"``: neither is hidden.  With ``own_pid=None``
    nothing is excluded at all (offline audit).
    """
    owners = (
        own_process_sets(rows, own_pid)
        if own_pid is not None
        else OwnSets(self_pids=(), ancestors=(), descendants=(), guard_invocations=())
    )
    excluded = owners.excluded
    by_pid = {row.pid: row for row in rows}
    own_row = by_pid.get(own_pid) if own_pid is not None else None

    def relationship(pid: int) -> str:
        if own_pid is None:
            return "unrelated"
        if pid in owners.ancestors:
            return "ancestor"
        row = by_pid.get(pid)
        if row is not None and own_row is not None and row.ppid == own_row.ppid:
            return "sibling"
        return "unrelated"

    def executable(row: ProcessRow) -> str:
        return _basename(row.argv[0]) if row.argv else _basename(row.comm)

    roots = tuple(
        SuiteHit(
            pid=row.pid,
            ppid=row.ppid,
            executable=executable(row),
            role="runner",
            argv_head=_argv_head(row.argv),
            relationship=relationship(row.pid),
        )
        for row in rows
        if row.kind == "runner" and row.pid not in excluded
    )
    children = _children_map(rows)
    descendant_pids = _descendants(children, [hit.pid for hit in roots]) - set(excluded)
    descendants = tuple(
        SuiteHit(
            pid=by_pid[pid].pid,
            ppid=by_pid[pid].ppid,
            executable=executable(by_pid[pid]),
            role="descendant",
            argv_head=_argv_head(by_pid[pid].argv),
            relationship=relationship(pid),
        )
        for pid in sorted(descendant_pids)
        if pid in by_pid
    )
    ancestor_rows = tuple(by_pid[pid] for pid in owners.ancestors if pid in by_pid)
    return SuiteScan(
        roots=roots,
        descendants=descendants,
        owners=owners,
        considered=len(rows),
        ancestor_rows=ancestor_rows,
    )


def evaluate(
    level: str,
    *,
    wrapped_command: Sequence[str] = (),
    own_pid: int | None = None,
    load_reader: Callable[[], Sequence[float]] | None = None,
    census_reader: Callable[[], Sequence[ProcessRow]] | None = None,
) -> GuardReport:
    """Decide GO/NO-GO.  Any fact that cannot be established is a refusal.

    There is deliberately no threshold parameter: the ceiling is not a knob.
    """
    reader = census_reader if census_reader is not None else read_process_census
    reasons: list[Reason] = []
    checks: list[Check] = []
    import datetime as _datetime

    if level in GATED_LEVELS:
        checks.append(Check("level_gated", "pass", f"{level} is gated by the quiet-host guard"))
    else:
        reasons.append(
            Reason(
                R_LEVEL_NOT_GATED,
                f"level {level!r} is not one of {', '.join(GATED_LEVELS)}",
            )
        )
        checks.append(
            Check("level_gated", "blocking", f"level {level!r} is not gated; refusing to launch")
        )

    load1: float | None = None
    try:
        load1 = read_load1(load_reader)
    except LoadUnavailable as exc:
        reasons.append(Reason(R_LOAD_UNKNOWN, str(exc)))
        checks.append(Check("load1", "blocking", str(exc)))
    else:
        if load1 > LOAD1_MAX:
            detail = (
                f"1-minute load average {load1:.2f} is above the immutable ceiling "
                f"{LOAD1_MAX:.1f}"
            )
            reasons.append(Reason(R_LOAD_OVER, detail))
            checks.append(Check("load1", "blocking", detail))
        else:
            checks.append(
                Check(
                    "load1",
                    "pass",
                    f"1-minute load average {load1:.2f} is at or below {LOAD1_MAX:.1f}",
                )
            )

    rows: tuple[ProcessRow, ...] = ()
    scan: SuiteScan | None = None
    census_error: str | None = None
    try:
        rows = tuple(reader())
        if not rows:
            raise CensusUnavailable("census returned an empty process table")
    except CensusUnavailable as exc:
        census_error = str(exc)
    except Exception as exc:  # noqa: BLE001 - any census failure is a refusal
        census_error = f"{type(exc).__name__}: {exc}"
    if census_error is not None:
        reasons.append(Reason(R_CENSUS_UNAVAILABLE, census_error))
        checks.append(Check("lane_suite", "blocking", f"census unavailable: {census_error}"))
    else:
        scan = scan_suites(rows, own_pid=own_pid)
        if scan.roots:
            detail = "; ".join(
                f"pid={hit.pid} rel={hit.relationship} exe={hit.executable} argv={hit.argv_head}"
                for hit in scan.roots[:5]
            )
            suffix = "" if len(scan.roots) <= 5 else f" (+{len(scan.roots) - 5} more)"
            reasons.append(
                Reason(
                    R_LANE_SUITE,
                    f"{len(scan.roots)} lane suite runner(s) active"
                    f" (descendants: {len(scan.descendants)}): {detail}{suffix}",
                )
            )
            checks.append(Check("lane_suite", "blocking", f"{len(scan.roots)} runner(s) active"))
        else:
            checks.append(
                Check(
                    "lane_suite",
                    "pass",
                    f"no lane suite runner in {scan.considered} censused processes",
                )
            )

    verdict = "NO-GO" if any(reason.severity == BLOCKING for reason in reasons) else "GO"
    return GuardReport(
        level=level,
        verdict=verdict,
        reasons=tuple(reasons),
        checks=tuple(checks),
        load1=load1,
        rows=rows,
        scan=scan,
        census_error=census_error,
        wrapped_command=tuple(wrapped_command),
        wrapped_exit_code=None,
        emitted_at=_datetime.datetime.now(_datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
    )


def run_guarded(
    command: Sequence[str],
    *,
    level: str,
    own_pid: int | None = None,
    load_reader: Callable[[], Sequence[float]] | None = None,
    census_reader: Callable[[], Sequence[ProcessRow]] | None = None,
) -> GuardOutcome:
    """Run ``command`` only after the guard passes.  Fail-closed: never before."""
    report = evaluate(
        level,
        wrapped_command=command,
        own_pid=own_pid,
        load_reader=load_reader,
        census_reader=census_reader,
    )
    if report.verdict != "GO" or not command:
        # Refused, or nothing to wrap: in both cases nothing is spawned.
        return GuardOutcome(report=report, ran=False, exit_code=None)
    completed = subprocess.run(list(command), check=False)
    outcome_report = GuardReport(
        level=report.level,
        verdict="GO",
        reasons=report.reasons,
        checks=report.checks,
        load1=report.load1,
        rows=report.rows,
        scan=report.scan,
        census_error=report.census_error,
        wrapped_command=tuple(command),
        wrapped_exit_code=completed.returncode,
        emitted_at=report.emitted_at,
    )
    return GuardOutcome(report=outcome_report, ran=True, exit_code=completed.returncode)


@dataclass(frozen=True)
class Options:
    level: str | None
    command: tuple[str, ...]
    print_census: bool
    census_only: bool


def parse_argv(argv: Sequence[str]) -> Options:
    """Strict option parsing.  Unknown or override-looking options raise."""
    level: str | None = None
    command: tuple[str, ...] = ()
    print_census = False
    census_only = False
    index = 0
    tokens = list(argv)
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            command = tuple(tokens[index + 1 :])
            break
        if token in ("-h", "--help"):
            raise UsageError("help requested")
        if token == "--json":
            pass
        elif token == "--print-census":
            print_census = True
        elif token == "--census-only":
            census_only = True
        elif token == "--level":
            if index + 1 >= len(tokens):
                raise UsageError("--level requires a value")
            level = tokens[index + 1]
            index += 1
        elif token.startswith("--level="):
            level = token.split("=", 1)[1]
        elif _OVERRIDE_PATTERN.search(token):
            raise ThresholdOverrideRefused(
                f"option {token!r} would change a guard threshold; "
                f"the ceiling {LOAD1_MAX:.1f} on the 1-minute load average is not overridable"
            )
        else:
            raise UsageError(f"unknown option {token!r}")
        index += 1
    return Options(
        level=level, command=command, print_census=print_census, census_only=census_only
    )


def _refusal_report(level: str | None, code: str, detail: str, command: Sequence[str] = ()) -> GuardReport:
    return GuardReport(
        level=level if level is not None else "",
        verdict="NO-GO",
        reasons=(Reason(code, detail),),
        checks=(Check("command_line", "blocking", detail),),
        load1=None,
        rows=(),
        scan=None,
        census_error=None,
        wrapped_command=tuple(command),
        wrapped_exit_code=None,
        emitted_at="",
    )


def main(argv: Sequence[str] | None = None) -> int:
    tokens = list(sys.argv[1:] if argv is None else argv)
    try:
        options = parse_argv(tokens)
    except ThresholdOverrideRefused as exc:
        print(json.dumps(_refusal_report(None, R_OVERRIDE_REFUSED, str(exc)).to_json(), sort_keys=True))
        return 2
    except UsageError as exc:
        if "help requested" in str(exc):
            print(USAGE)
            return 0
        print(json.dumps(_refusal_report(None, R_USAGE, str(exc)).to_json(), sort_keys=True))
        return 2
    if options.level is None:
        detail = f"missing required --level; {USAGE}"
        print(json.dumps(_refusal_report(None, R_USAGE, detail).to_json(), sort_keys=True))
        return 2

    outcome = run_guarded(
        options.command if not options.census_only else (),
        level=options.level,
        own_pid=os.getpid(),
    )
    print(
        json.dumps(
            outcome.report.to_json(include_rows=options.print_census),
            sort_keys=True,
        )
    )
    if outcome.report.verdict != "GO":
        return 1
    if not outcome.ran:
        # Admitted with nothing wrapped (e.g. --census-only): a clean GO.
        return 0
    return int(outcome.exit_code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
