#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Physical readiness gate for the Mycelium core pilot (PLAN 5.2 item 5).

Prints ``GO`` / ``NO-GO``.  ``GO`` requires **all six** mandatory checks to be
``PASS``: a ``SKIPPED`` check is never a green - a command the gate could not
discover with confidence, or a prerequisite that is genuinely absent, makes the
verdict ``NO-GO`` and says so.  The process exits ``1`` on ``NO-GO`` and ``0``
on ``GO``.

The six checks (PLAN 5.2 item 5, in order):

``a`` canonical suites + audits green
    The commands are *discovered*, never guessed: the ``run:`` steps of the
    *host-executable* jobs of ``.github/workflows/governance.yml`` (a job whose
    ``runs-on`` names another OS family, e.g. ``windows-latest``, is recorded as
    excluded - its steps are not commands this host can execute) plus the audit
    scripts this packet names (``governance_gate``, ``contract_audit``,
    ``claim_boundary_audit``, ``mycelium_completion_audit``) and the declared
    ``ui/web/package.json`` scripts.  A command that cannot be discovered is
    reported ``SKIPPED-with-reason``, which is a ``NO-GO`` and never an
    invention.
``b`` budget invariants green
    ``mycelium_deadlines.check_composition()`` - which today reports the
    deliberately pinned round-50 violation.  The gate surfaces that violation
    by name; it does not hide it, and it does not offer a flag to hide it.
``c`` seeded soak over N seeds green
    ``mycelium_testbed.soak`` through its own interface (module CLI first,
    documented Python sweep entry point second).  ``N`` comes from ``--seeds``.
    A missing or incomplete interface is a named ``FAIL``.
``d`` the emulated chain reaches a successful terminal
    The emulation test suite under ``tests/testbed/test_emulation*.py``, run
    with pytest; a suite that collects nothing is a named ``FAIL``, not a pass.
``e`` package built from the exact committed bytes with digests recorded
    **Never disableable.**  A dirty tree ``FAIL``s with the dirty paths listed;
    the package is built from ``git archive HEAD`` (the committed bytes) into a
    scratch directory *outside* the repo, every built file is digested, and the
    record is written to ``runs/<id>/PACKAGE.json``.
``f`` the run's pre-registered ``runs/<id>/HYPOTHESIS.md`` exists
    For the ``--run-id`` the gate is given (PLAN 5.3).  The gate never creates
    this file - its absence is the finding.

Evidence (JSON report, human report, ``PACKAGE.json``, built package artifacts)
is written OUTSIDE the repository, under ``<runs-root>/<run-id>/``.

This module imports **no product code**: every check runs as a subprocess so a
broken or absent module is reported as a named finding instead of crashing the
gate.  ``--json`` prints the machine-readable report on stdout and the human
report on stderr; both are always also written to the run directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

PROTOCOL = "mycelium.pilot_readiness_gate.v1"
PACKAGE_PROTOCOL = "mycelium.pilot_readiness_gate.package.v1"

DEFAULT_RUNS_ROOT = Path(os.path.expanduser("~/Documents/mycelium-core-program/runs"))
DEFAULT_SEEDS = 20
DEFAULT_EVENTS = 200
DEFAULT_TIMEOUT_SECONDS = 1800.0
TAIL_CHARS = 2000
REASON_LINES = 12

PASS = "PASS"
FAIL = "FAIL"
SKIPPED = "SKIPPED"

HYPOTHESIS_FILENAME = "HYPOTHESIS.md"
PACKAGE_RECORD_FILENAME = "PACKAGE.json"
WORKFLOW = Path(".github/workflows/governance.yml")
UI_PACKAGE_JSON = Path("ui/web/package.json")
UI_SCRIPTS = ("test", "test:contracts", "typecheck")
UI_SCRIPTS_EXTENDED = ("build",)
SOAK_MODULE = "mycelium_testbed.soak"
EMULATION_GLOB = "tests/testbed/test_emulation*.py"
PACKAGE_BUILDER = Path("scripts/package_m22_service.py")
PACKAGE_CONFIG_GLOB = "release/service-configs/*.json"

#: ``runs-on`` families whose steps this host cannot execute.  Every argv the
#: gate builds is a *host* argv (``ctx.python`` is a path on this host) and is
#: executed here, so a step owned by a job on another OS family is not a
#: canonical command for this host: it is recorded as excluded instead of being
#: executed, or - worse - silently counted as coverage.  ``windows`` matches
#: ``windows-latest`` / ``windows-2022`` / ``Windows``; the POSIX families
#: (``macos-*``, ``ubuntu-*``, ``linux``) run the workflow's steps verbatim,
#: which is why such jobs stay discoverable.  A runner the gate cannot classify
#: (``${{ matrix.os }}``, or a job with no ``runs-on`` at all) is not proof of
#: non-executability, so it stays discoverable too.
NON_HOST_RUNNER_MARKERS = ("windows",)

#: SHA-256 of the `git archive HEAD` tar stream: the exact committed bytes the
#: package (check e) is built from.
UNSET = object()


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CommandResult:
    """One executed command with the evidence a NO-GO needs to be diagnosable."""

    command: tuple[str, ...]
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    discovery_source: str | None = None
    note: str | None = None
    non_zero_tolerated: bool = False

    def shell(self) -> str:
        return shlex.join(self.command)

    def tail(self, limit: int = TAIL_CHARS) -> str:
        text = (self.stdout or "") + (("\n" + self.stderr) if self.stderr else "")
        text = text.strip()
        if len(text) <= limit:
            return text
        return "[...truncated...]\n" + text[-limit:]

    def status(self) -> str:
        if self.timed_out:
            return FAIL
        return PASS if self.exit_code == 0 else FAIL

    def record(self) -> dict[str, object]:
        return {
            "cmd": self.shell(),
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 3),
            "timed_out": self.timed_out,
            "discovery_source": self.discovery_source,
            "note": self.note,
            "tail": self.tail(),
        }


class CommandRunner(Protocol):
    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
    ) -> CommandResult: ...


def subprocess_runner(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> CommandResult:
    """Run one command, capturing a bounded tail; never raises on failure."""

    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return CommandResult(
            command=tuple(command),
            exit_code=127,
            stderr=f"executable_not_found: {exc}",
            duration_seconds=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            command=tuple(command),
            exit_code=None,
            stdout=_decode(exc.stdout),
            stderr=_decode(exc.stderr) + f"\ncommand_timeout_after_{timeout}s",
            duration_seconds=time.monotonic() - started,
            timed_out=True,
        )
    except OSError as exc:  # pragma: no cover - defensive
        return CommandResult(
            command=tuple(command),
            exit_code=126,
            stderr=f"command_failed_to_start: {exc}",
            duration_seconds=time.monotonic() - started,
        )
    return CommandResult(
        command=tuple(command),
        exit_code=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_seconds=time.monotonic() - started,
    )


def _decode(raw: object) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


# --------------------------------------------------------------------------- #
# context
# --------------------------------------------------------------------------- #


@dataclass
class GateContext:
    repo_root: Path
    runs_root: Path
    run_id: str
    python: str = sys.executable
    seeds: int = DEFAULT_SEEDS
    events: int = DEFAULT_EVENTS
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    runner: CommandRunner = subprocess_runner
    extended: bool = False
    expect_head: str | None = None
    package_source: str = "committed"
    package_builder: Path | None = None
    package_config_glob: str = PACKAGE_CONFIG_GLOB
    soak_module: str = SOAK_MODULE
    emulation_glob: str = EMULATION_GLOB
    keep_scratch: bool = False
    scratch_dirs: list[Path] = field(default_factory=list)

    # -- environment ------------------------------------------------------- #

    def child_env(self) -> dict[str, str]:
        """A pristine child environment (the invocation convention of this run)."""

        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
        }
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["GIT_OPTIONAL_LOCKS"] = "0"
        return env

    def python_command(self, *args: str) -> tuple[str, ...]:
        return (self.python, "-B", *args)

    # -- running ----------------------------------------------------------- #

    def run(
        self,
        command: Sequence[str],
        *,
        discovery_source: str | None = None,
        timeout: float | None = None,
        cwd: Path | None = None,
        note: str | None = None,
    ) -> CommandResult:
        result = self.runner(
            tuple(command),
            cwd=cwd or self.repo_root,
            env=self.child_env(),
            timeout=self.timeout if timeout is None else timeout,
        )
        if discovery_source is not None or note is not None:
            result = CommandResult(
                command=result.command,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_seconds=result.duration_seconds,
                timed_out=result.timed_out,
                discovery_source=discovery_source,
                note=note,
            )
        return result

    def git(self, *args: str, discovery_source: str | None = None) -> CommandResult:
        return self.run(
            ("git", "-C", str(self.repo_root), *args),
            discovery_source=discovery_source or "git",
        )

    # -- paths ------------------------------------------------------------- #

    @property
    def run_dir(self) -> Path:
        return self.runs_root / self.run_id

    @property
    def package_record_path(self) -> Path:
        return self.run_dir / PACKAGE_RECORD_FILENAME

    def scratch_dir(self, prefix: str) -> Path:
        directory = Path(tempfile.mkdtemp(prefix=f"pilot-gate-{prefix}-"))
        self.scratch_dirs.append(directory)
        return directory


# --------------------------------------------------------------------------- #
# check results
# --------------------------------------------------------------------------- #


@dataclass
class CheckResult:
    id: str
    title: str
    status: str
    reasons: tuple[str, ...] = ()
    commands: tuple[CommandResult, ...] = ()
    detail: dict[str, object] = field(default_factory=dict)

    def record(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "reasons": list(self.reasons),
            "commands": [item.record() for item in self.commands],
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    run: Callable[[GateContext], CheckResult]


def aggregate(statuses: Sequence[str]) -> str:
    """``PASS`` only when every sub-result passed; a skip is never a green."""

    if not statuses:
        return SKIPPED
    if any(status == FAIL for status in statuses):
        return FAIL
    if any(status == SKIPPED for status in statuses):
        return SKIPPED
    return PASS


# --------------------------------------------------------------------------- #
# check a: canonical suites + audits, discovered
# --------------------------------------------------------------------------- #


def _yaml_scalar(raw: str) -> str:
    """A YAML plain scalar: surrounding whitespace and one layer of quotes off."""

    return raw.strip().strip("\"'")


def _runner_values(value: str) -> list[str]:
    """Split a ``runs-on`` value into its runner labels (flow list or scalar)."""

    items = value[1:-1].split(",") if value.startswith("[") and value.endswith("]") else [value]
    return [item for item in (_yaml_scalar(part) for part in items) if item]


def _runner_is_host_executable(runners: Sequence[str]) -> bool:
    """``False`` only when a runner *proves* a different OS family from this host.

    The gate executes on a POSIX host and builds *host* argv, so a step written
    for a Windows job is not a canonical command here - which is precisely why
    such a job exists.  A runner the gate cannot classify (``${{ matrix.os }}``,
    or a job that declares no ``runs-on`` at all) is not proof of
    non-executability: non-executability has to be *proved* before a canonical
    command is taken out of the discovered set.
    """

    return not any(
        marker in runner.lower() for runner in runners for marker in NON_HOST_RUNNER_MARKERS
    )


def _step_command(raw: str) -> str:
    """The command of one ``run:`` value; a block scalar has no single argv."""

    value = _yaml_scalar(raw)
    return "" if value in {"|", ">", "|-", ">-", "|+", ">+"} else value


@dataclass(frozen=True)
class WorkflowStep:
    """One ``run:`` step, with the job that owns it."""

    job: str
    name: str | None
    command: str


@dataclass(frozen=True)
class WorkflowJob:
    """One CI job: its ``runs-on`` runner label(s) and its ``run:`` steps."""

    name: str
    runners: tuple[str, ...]
    steps: tuple[WorkflowStep, ...]

    @property
    def host_executable(self) -> bool:
        """Whether this host can execute the job's steps (see ``NON_HOST_RUNNER_MARKERS``)."""

        return _runner_is_host_executable(self.runners)


def _workflow_jobs(path: Path) -> tuple[list[WorkflowJob], str | None]:
    """Group the workflow's ``run:`` steps under the job (and ``runs-on``) that owns them.

    A deliberately small, stdlib-only reader - discovery needs exactly three
    things from the workflow (job names, ``runs-on`` values, ``run:`` commands)
    and the file is hand-maintained, so pulling in a YAML dependency to read it
    would be a dependency the gate does not need.  Indentation alone establishes
    ownership, so a step can never be attributed to the wrong job; a job on a
    runner this host cannot execute is returned *as a job*, so discovery can
    exclude it by decision instead of leaking its steps into the canonical set.
    """

    if not path.is_file():
        return [], f"workflow_not_found:{path}"

    order: list[str] = []
    runners: dict[str, list[str]] = {}
    steps: dict[str, list[WorkflowStep]] = {}
    jobs_indent: int | None = None
    job_name: str | None = None
    job_indent = -1
    step_name: str | None = None
    block_runs_on: list[str] | None = None
    block_indent = -1

    def close_block_runs_on() -> None:
        """Attach a ``runs-on:`` block sequence to the job that declared it."""

        nonlocal block_runs_on
        if block_runs_on is not None and job_name is not None:
            runners[job_name].extend(block_runs_on)
        block_runs_on = None

    for raw in path.read_text("utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))

        if jobs_indent is None:
            if re.match(r"^jobs:\s*(?:#.*)?$", stripped):
                jobs_indent = indent
            continue

        if indent <= jobs_indent:
            # A key at or above ``jobs:``: the jobs mapping is finished (or a
            # new one starts), and no later line belongs to the old job.
            close_block_runs_on()
            job_name = None
            job_indent = -1
            step_name = None
            jobs_indent = indent if re.match(r"^jobs:\s*(?:#.*)?$", stripped) else None
            continue

        if job_name is None or indent <= job_indent:
            header = re.match(r"^(?P<name>[A-Za-z0-9_.\-]+):\s*(?:#.*)?$", stripped)
            if header is None:
                continue
            close_block_runs_on()
            declared_name = header.group("name")
            job_name = declared_name
            job_indent = indent
            step_name = None
            if declared_name not in runners:
                order.append(declared_name)
                runners[declared_name] = []
                steps[declared_name] = []
            continue

        if block_runs_on is not None:
            item = re.match(r"^-\s*(?P<value>.+?)\s*$", stripped)
            if item is not None and indent > block_indent:
                block_runs_on.append(_yaml_scalar(item.group("value")))
                continue
            close_block_runs_on()

        declared = re.match(r"^runs-on:\s*(?P<value>.*)$", stripped)
        if declared is not None:
            value = _yaml_scalar(declared.group("value"))
            if value:
                runners[job_name].extend(_runner_values(value))
            else:
                block_runs_on = []
                block_indent = indent
            continue

        body = stripped[1:].strip() if stripped.startswith("-") else stripped
        if stripped.startswith("-"):
            step_name = None
        named = re.match(r"^name:\s*(?P<value>.*)$", body)
        if named is not None:
            step_name = _yaml_scalar(named.group("value")) or None
            continue
        invoked = re.match(r"^run:\s*(?P<value>.*)$", body)
        if invoked is not None:
            steps[job_name].append(
                WorkflowStep(job_name, step_name, _step_command(invoked.group("value")))
            )

    close_block_runs_on()
    return (
        [
            WorkflowJob(name, tuple(runners[name]), tuple(steps[name]))
            for name in order
        ],
        None,
    )


def _argv_for_step(step: str, python: str, *, note: str | None = None) -> tuple[list[str], str | None]:
    """Turn one workflow ``run:`` step into the argv the gate will execute."""

    tokens = shlex.split(step)
    if not tokens:
        return [], "empty_step"
    if tokens[0] in {"python", "python3", "python3.14"}:
        tokens = [python, "-B", *tokens[1:]]
    if any(token.endswith("pytest") or token == "pytest" for token in tokens):
        tokens.append("-p")
        tokens.append("no:cacheprovider")
    return tokens, note


@dataclass(frozen=True)
class DiscoveredCommand:
    """One canonical command the gate discovered, with its provenance."""

    argv: tuple[str, ...]
    source: str
    note: str | None
    kind: str  # "run" | "skip"
    cwd: Path | None = None


def discover_canonical_commands(ctx: GateContext) -> list[DiscoveredCommand]:
    """Return every canonical command with its discovery source.

    ``kind`` is ``"run"`` for something the gate executes or ``"skip"`` for a
    step that is recorded with a reason instead of being executed.
    """

    discovered: list[DiscoveredCommand] = []
    seen: set[tuple[object, ...]] = set()

    def add(
        argv: Sequence[str],
        source: str,
        note: str | None,
        kind: str = "run",
        cwd: Path | None = None,
    ) -> None:
        key = tuple(argv)
        if kind == "run" and not key:
            # An empty argv is not a command: it can never be *run*, so it is
            # recorded as a named skip - which is a NO-GO the report explains -
            # instead of being executed as if it were one.
            kind = "skip"
            note = f"discovered_command_with_empty_argv:{source}"
        record = (kind, key, source, note)
        if kind in {"run", "prereq"} and record in seen:
            # The same step declared by two host-executable jobs is one
            # canonical command, recorded once with its exact text.
            return
        seen.add(record)
        discovered.append(DiscoveredCommand(key, source, note, kind, cwd))

    workflow = ctx.repo_root / WORKFLOW
    jobs, workflow_problem = _workflow_jobs(workflow)
    source = WORKFLOW.as_posix()
    if workflow_problem is not None:
        add((), f"{source}#{workflow_problem}", workflow_problem, "skip")
    found_pytest = False
    for job in jobs:
        if not job.host_executable:
            # A job on another OS family: the gate builds a *host* argv and runs
            # it here, so those steps are not canonical commands for this host.
            # The exclusion is recorded, never dropped in silence - and it is
            # not a coverage hole for the host checks, because the job exists to
            # cover a different platform.
            add(
                (),
                f"{source}#{job.name}",
                f"job_runner_not_host_executable:{'+'.join(job.runners)}",
                "excluded",
            )
            continue
        for step in job.steps:
            if not step.command:
                # A ``run:`` the gate cannot turn into one argv (an empty or a
                # block-scalar command): named, never executed, never dropped.
                add(
                    (),
                    f"{source}#{job.name}",
                    f"workflow_step_without_a_runnable_command:{step.name or 'unnamed'}",
                    "skip",
                )
                continue
            if (
                "pip install" in step.command
                or "requirements" in step.command
                and "install" in step.command
            ):
                # An environment prerequisite, not a verification: the gate never
                # installs dependencies (a network install is a stop condition).
                # It is recorded, with the exact step, and never silently dropped.
                add(
                    (),
                    f"{source}#dependency-install",
                    f"workflow_dependency_install_not_run:{step.command}",
                    "prereq",
                )
                continue
            argv, note = _argv_for_step(step.command, ctx.python)
            if not argv:
                continue
            if any("pytest" in token for token in argv):
                found_pytest = True
            if note is None:
                note = "workflow step"
            add(argv, source, note)
    if not found_pytest:
        add(
            (),
            f"{source}#pytest-step",
            "canonical_pytest_step_not_discoverable",
            "skip",
        )

    audit_scripts = (
        ("scripts/contract_audit.py", ("--json",)),
        ("scripts/claim_boundary_audit.py", ("--repo-root", ".", "--json")),
        ("scripts/mycelium_completion_audit.py", ("--repo-root", ".", "--json")),
    )
    for relative, args in audit_scripts:
        script = ctx.repo_root / relative
        if not script.is_file():
            add((), f"packet-audit:{relative}", f"audit_script_not_found:{relative}", "skip")
            continue
        add(
            tuple(ctx.python_command(relative, *args)),
            f"packet-audit:{relative}",
            "audit CLI read from the script's argparse",
        )

    package_json = ctx.repo_root / UI_PACKAGE_JSON
    ui_scripts = UI_SCRIPTS + (UI_SCRIPTS_EXTENDED if ctx.extended else ())
    ui_dir = ctx.repo_root / "ui" / "web"
    npm = shutil.which("npm")
    if not package_json.is_file():
        add((), f"ui:{UI_PACKAGE_JSON.as_posix()}", "ui_package_json_not_found", "skip")
    elif npm is None:
        add((), f"ui:{UI_PACKAGE_JSON.as_posix()}", "npm_not_on_path", "skip")
    elif not (ui_dir / "node_modules").is_dir():
        add(
            (),
            f"ui:{UI_PACKAGE_JSON.as_posix()}",
            "ui_toolchain_absent:ui/web/node_modules",
            "skip",
        )
    else:
        declared = _declared_ui_scripts(package_json)
        for name in ui_scripts:
            if name not in declared:
                add((), f"ui:{UI_PACKAGE_JSON.as_posix()}#{name}", f"ui_script_not_declared:{name}", "skip")
                continue
            add(
                (npm, "run", name),
                f"ui:{UI_PACKAGE_JSON.as_posix()}#scripts.{name}",
                declared[name],
                cwd=ui_dir,
            )

    if ctx.extended:
        add(
            tuple(ctx.python_command("-m", "pytest", "-q", "-p", "no:cacheprovider")),
            "docs/live-mvp-operator-runbook.md#release-gate",
            "repo-wide canonical suite (opt-in --extended)",
        )
        add(
            ("git", "-C", str(ctx.repo_root), "diff", "--check"),
            "docs/live-mvp-operator-runbook.md#release-gate",
            "workflow step",
        )

    return discovered


def _declared_ui_scripts(package_json: Path) -> dict[str, str]:
    try:
        value = json.loads(package_json.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    scripts = value.get("scripts") if isinstance(value, dict) else None
    if not isinstance(scripts, dict):
        return {}
    return {str(name): str(body) for name, body in scripts.items()}


def check_canonical_suites_and_audits(ctx: GateContext) -> CheckResult:
    commands: list[CommandResult] = []
    statuses: list[str] = []
    reasons: list[str] = []
    skipped: list[str] = []
    prerequisites: list[str] = []
    excluded_non_host_jobs: list[str] = []

    for entry in discover_canonical_commands(ctx):
        if entry.kind == "prereq":
            prerequisites.append(f"{entry.source}:{entry.note}")
            continue
        if entry.kind == "excluded":
            # A job on a runner this host cannot execute: recorded as evidence
            # for the report, and never a green or a failure by itself.
            excluded_non_host_jobs.append(f"{entry.source}:{entry.note}")
            continue
        if entry.kind == "skip":
            statuses.append(SKIPPED)
            skipped.append(entry.source)
            reasons.append(f"{entry.source}:{entry.note}")
            continue
        if not entry.argv:
            # An empty argv is never handed to the runner as a command: it is
            # reported as an undiscoverable step, which is a NO-GO.
            statuses.append(SKIPPED)
            skipped.append(entry.source)
            reasons.append(f"{entry.source}:discovered_command_with_empty_argv")
            continue
        result = ctx.run(entry.argv, discovery_source=entry.source, note=entry.note, cwd=entry.cwd)
        commands.append(result)
        status = result.status()
        statuses.append(status)
        if status == FAIL:
            reasons.append(f"canonical_command_failed:{result.shell()}")

    return CheckResult(
        id="a.canonical_suites_and_audits",
        title="canonical suites + audits green",
        status=aggregate(statuses),
        reasons=tuple(reasons),
        commands=tuple(commands),
        detail={
            "discovered_commands": len(commands),
            "skipped_with_reason": skipped,
            "not_executed_prerequisites": prerequisites,
            "excluded_non_host_jobs": excluded_non_host_jobs,
            "extended": ctx.extended,
        },
    )


# --------------------------------------------------------------------------- #
# check b: budget invariants
# --------------------------------------------------------------------------- #

BUDGET_PROBE = (
    "import json, importlib\n"
    "module = importlib.import_module('mycelium_deadlines')\n"
    "payload = {\n"
    "    'violations': list(module.check_composition()),\n"
    "    'advertised_maximum_total_cleanup_ms': module.advertised_maximum_total_cleanup_ms,\n"
    "    'route_cleanup_fanout_window_ms': module.route_cleanup_fanout_window_ms,\n"
    "}\n"
    "print(json.dumps(payload, sort_keys=True))\n"
)

PINNED_ROUND_50_CODE = "peer_advertised_cleanup_bound_exceeds_route_fanout_window"


def check_budget_invariants(ctx: GateContext) -> CheckResult:
    command = ctx.run(
        ctx.python_command("-c", BUDGET_PROBE),
        discovery_source="mycelium_deadlines.check_composition (wp2-1 registry)",
        note="pinned round-50 violation is reported, never hidden",
    )
    if command.exit_code != 0:
        reason = "budget_registry_unavailable:mycelium_deadlines"
        if "ModuleNotFoundError" in (command.stderr or command.stdout):
            reason = "budget_registry_module_absent:mycelium_deadlines"
        return CheckResult(
            id="b.budget_invariants",
            title="budget invariants green (mycelium_deadlines.check_composition)",
            status=FAIL,
            reasons=(reason,),
            commands=(command,),
            detail={"checker": "mycelium_deadlines.check_composition"},
        )

    try:
        payload = json.loads(command.stdout.strip().splitlines()[-1])
        violations = [str(item) for item in payload["violations"]]
    except (ValueError, KeyError, IndexError) as exc:
        return CheckResult(
            id="b.budget_invariants",
            title="budget invariants green (mycelium_deadlines.check_composition)",
            status=FAIL,
            reasons=(f"budget_violation_report_unreadable:{type(exc).__name__}",),
            commands=(command,),
            detail={"checker": "mycelium_deadlines.check_composition"},
        )

    detail: dict[str, object] = {
        "checker": "mycelium_deadlines.check_composition",
        "violations": violations,
        "advertised_maximum_total_cleanup_ms": payload.get("advertised_maximum_total_cleanup_ms"),
        "route_cleanup_fanout_window_ms": payload.get("route_cleanup_fanout_window_ms"),
    }
    if not violations:
        return CheckResult(
            id="b.budget_invariants",
            title="budget invariants green (mycelium_deadlines.check_composition)",
            status=PASS,
            commands=(command,),
            detail=detail,
        )
    reasons = [f"budget_invariant_violated:{code}" for code in violations]
    detail["pinned_round_50_defect"] = PINNED_ROUND_50_CODE in violations
    return CheckResult(
        id="b.budget_invariants",
        title="budget invariants green (mycelium_deadlines.check_composition)",
        status=FAIL,
        reasons=tuple(reasons),
        commands=(command,),
        detail=detail,
    )


# --------------------------------------------------------------------------- #
# check c: the seeded soak
# --------------------------------------------------------------------------- #

SOAK_PROBE = (
    "import importlib, inspect, json, sys\n"
    "name = sys.argv[1]\n"
    "try:\n"
    "    module = importlib.import_module(name)\n"
    "except Exception as exc:\n"
    "    print(json.dumps({'interface': 'absent', 'error': f'{type(exc).__name__}: {exc}'}))\n"
    "    raise SystemExit(3)\n"
    "report = {'interface': 'module', 'file': getattr(module, '__file__', None), 'callables': {}}\n"
    "if callable(getattr(module, 'main', None)):\n"
    "    report['interface'] = 'cli'\n"
    "for candidate in ('run_sweep', 'sweep', 'run_seeds', 'run'):\n"
    "    function = getattr(module, candidate, None)\n"
    "    if callable(function):\n"
    "        try:\n"
    "            report['callables'][candidate] = sorted(inspect.signature(function).parameters)\n"
    "        except (TypeError, ValueError):\n"
    "            report['callables'][candidate] = None\n"
    "print(json.dumps(report))\n"
)

_SOAK_SEED_FLAGS = ("--seeds", "--seed-count")
_SOAK_EVENT_FLAGS = ("--events", "--event-count")
_SOAK_RUN_ID_FLAGS = ("--run-id", "--run")
_SOAK_RUN_ROOT_FLAGS = ("--runs-root", "--run-root", "--out", "--output-root")
_SOAK_INVARIANTS = (
    "at_most_one_native_execution_per_command",
    "nonterminal_row_not_retired_within_bound",
    "control_loop_death_while_membership_valid",
    "request_without_terminal_frame_within_budget",
)


def _soak_document(stdout: str) -> dict[str, object] | None:
    """The last JSON object printed by the soak CLI (its ``--json`` document)."""

    for line in reversed((stdout or "").splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _soak_violation_codes(document: Mapping[str, object] | None, text: str) -> list[str]:
    """Named invariant codes the sweep reported, document first, text as fallback."""

    codes: set[str] = set()
    if document is not None:
        seeds = document.get("seeds")
        if isinstance(seeds, list):
            for seed in seeds:
                if not isinstance(seed, dict):
                    continue
                fired = seed.get("invariant_codes_fired")
                if isinstance(fired, list):
                    codes.update(str(code) for code in fired)
                violations = seed.get("violations")
                if isinstance(violations, list):
                    for violation in violations:
                        if isinstance(violation, dict) and violation.get("code"):
                            codes.add(str(violation["code"]))
    if not codes:
        codes.update(code for code in _SOAK_INVARIANTS if code in (text or ""))
    return sorted(codes)


def _parse_flags(help_text: str) -> set[str]:
    return set(re.findall(r"--[A-Za-z0-9][A-Za-z0-9-]*", help_text or ""))


def _pick(flags: set[str], candidates: Sequence[str]) -> str | None:
    for candidate in candidates:
        if candidate in flags:
            return candidate
    return None


def _interpret_sweep_result(value: object) -> bool | None:
    """``True``/``False`` when the soak's own result is interpretable, else None."""

    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 0
    if isinstance(value, str):
        return value.lower() in {"ok", "pass", "passed", "green", "true"}
    if isinstance(value, dict):
        if isinstance(value.get("ok"), bool):
            return bool(value["ok"])
        if isinstance(value.get("passed"), bool):
            return bool(value["passed"])
        for key in ("failed_seeds", "failures", "violations", "failed"):
            entry = value.get(key)
            if isinstance(entry, int):
                return entry == 0
            if isinstance(entry, (list, tuple, dict)):
                return len(entry) == 0
        for key in ("seeds", "results"):
            entry = value.get(key)
            if isinstance(entry, list) and entry:
                verdicts = [_interpret_sweep_result(item) for item in entry]
                if all(item is not None for item in verdicts):
                    return all(bool(item) for item in verdicts)
    return None


def check_soak(ctx: GateContext) -> CheckResult:
    title = "seeded soak over N seeds green"
    check_id = "c.soak"
    probe = ctx.run(
        ctx.python_command("-c", SOAK_PROBE, ctx.soak_module),
        discovery_source=f"{ctx.soak_module} import probe",
        timeout=min(ctx.timeout, 120.0),
    )

    try:
        report = json.loads(probe.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return CheckResult(
            id=check_id,
            title=title,
            status=FAIL,
            reasons=(f"soak_probe_unreadable:{probe.exit_code}",),
            commands=(probe,),
            detail={"module": ctx.soak_module},
        )

    if report.get("interface") == "absent":
        return CheckResult(
            id=check_id,
            title=title,
            status=FAIL,
            reasons=(f"soak_module_absent:{ctx.soak_module}", f"probe_error:{report.get('error')}"),
            commands=(probe,),
            detail={"module": ctx.soak_module, "invariant_codes": list(_SOAK_INVARIANTS)},
        )

    if report.get("interface") == "cli":
        help_result = ctx.run(
            ctx.python_command("-m", ctx.soak_module, "--help"),
            discovery_source=f"{ctx.soak_module} --help",
            timeout=min(ctx.timeout, 120.0),
        )
        if help_result.exit_code != 0:
            return CheckResult(
                id=check_id,
                title=title,
                status=FAIL,
                reasons=(f"soak_cli_help_failed:{ctx.soak_module}",),
                commands=(probe, help_result),
                detail={"module": ctx.soak_module},
            )
        flags = _parse_flags(help_result.stdout + help_result.stderr)
        seed_flag = _pick(flags, _SOAK_SEED_FLAGS)
        if seed_flag is None:
            return CheckResult(
                id=check_id,
                title=title,
                status=FAIL,
                reasons=(
                    f"soak_cli_interface_incomplete:{ctx.soak_module}:missing_seed_count_flag",
                    f"flags_seen:{','.join(sorted(flags))}",
                ),
                commands=(probe, help_result),
                detail={"module": ctx.soak_module},
            )
        argv: list[str] = [ctx.python, "-B", "-m", ctx.soak_module, seed_flag, str(ctx.seeds)]
        event_flag = _pick(flags, _SOAK_EVENT_FLAGS)
        if event_flag is not None and ctx.events:
            argv += [event_flag, str(ctx.events)]
        run_id_flag = _pick(flags, _SOAK_RUN_ID_FLAGS)
        if run_id_flag is not None:
            argv += [run_id_flag, f"{ctx.run_id}-soak"]
        run_root_flag = _pick(flags, _SOAK_RUN_ROOT_FLAGS)
        if run_root_flag is not None:
            argv += [run_root_flag, str(ctx.runs_root)]
        wants_document = "--json" in flags
        if wants_document:
            argv.append("--json")
        command = ctx.run(
            tuple(argv),
            discovery_source=f"{ctx.soak_module} CLI (flags from --help)",
            note=f"seeds={ctx.seeds} events={ctx.events}",
        )
        document = _soak_document(command.stdout) if wants_document else None
        codes = _soak_violation_codes(document, command.stdout + command.stderr)
        seeds_run = None
        if document is not None and isinstance(document.get("seeds_run"), int):
            seeds_run = int(document["seeds_run"])
        status = command.status()
        reasons: list[str] = []
        if status == FAIL:
            reasons.append(f"soak_sweep_failed:{ctx.seeds}_seeds")
            reasons += [f"soak_invariant_violation:{code}" for code in codes]
        if seeds_run is not None and seeds_run != ctx.seeds:
            status = FAIL
            reasons.append(f"soak_seed_count_mismatch:requested={ctx.seeds},ran={seeds_run}")
        if seeds_run is None and status == PASS and wants_document and document is None:
            status = FAIL
            reasons.append(f"soak_result_unreadable:{ctx.soak_module} --json produced no document")
        # The sweep's replay identities are RECORDED OBSERVATIONS, not gates (design-review
        # decision 2', revised 2026-09-15): a fixed --events budget covers a variable number of
        # request lifecycles, so product-digest identity held 5/20 canonical runs and
        # event-stream identity 29/30.  Reading either as a gate would fail the pilot ~15 times
        # in 20 for a non-defect reason.  What gates is structural - any invariant firing in any
        # run, and terminal coverage - and the soak names it itself in replay_check.gate_failures,
        # with its non-zero exit code as the primary signal.
        replay = document.get("replay_check") if isinstance(document, dict) else None
        if isinstance(replay, dict):
            gate_failures = replay.get("gate_failures") or ()
            if gate_failures:
                status = FAIL
                reasons.append(
                    "soak_gate_failures:" + ";".join(str(failure) for failure in gate_failures)
                )
        divergence = document.get("divergence_check") if isinstance(document, dict) else None
        if (
            isinstance(divergence, dict)
            and divergence.get("performed")
            and divergence.get("differs_from_first") is False
        ):
            status = FAIL
            reasons.append(f"soak_seeds_do_not_diverge:{divergence.get('seed')}")
        return CheckResult(
            id=check_id,
            title=title,
            status=status,
            reasons=tuple(reasons),
            commands=(probe, command),
            detail={
                "module": ctx.soak_module,
                "seeds": ctx.seeds,
                "events": ctx.events,
                "seeds_run": seeds_run,
                "interface": "cli",
                "invariant_codes": list(_SOAK_INVARIANTS),
                "invariant_codes_fired": list(codes),
                "sweep_green": None if document is None else document.get("green"),
                "sweep_wall_seconds": None if document is None else document.get("wall_seconds"),
                "replay_check": None if document is None else document.get("replay_check"),
                "divergence_check": None if document is None else document.get("divergence_check"),
            },
        )

    callables = report.get("callables") or {}
    entry = next((name for name in ("run_sweep", "sweep", "run_seeds") if name in callables), None)
    if entry is None:
        return CheckResult(
            id=check_id,
            title=title,
            status=FAIL,
            reasons=(f"soak_interface_incomplete:{ctx.soak_module}:no_cli_and_no_sweep_entry_point",),
            commands=(probe,),
            detail={"module": ctx.soak_module, "callables": callables},
        )

    accepted = callables.get(entry) or []
    call_argv: list[str] = []
    if "seeds" in accepted:
        call_argv.append(f"seeds={ctx.seeds}")
    elif "seed_count" in accepted:
        call_argv.append(f"seed_count={ctx.seeds}")
    else:
        return CheckResult(
            id=check_id,
            title=title,
            status=FAIL,
            reasons=(f"soak_interface_incomplete:{ctx.soak_module}:{entry}_takes_no_seed_count",),
            commands=(probe,),
            detail={"module": ctx.soak_module, "callables": callables},
        )
    if "events" in accepted and ctx.events:
        call_argv.append(f"events={ctx.events}")
    if "run_id" in accepted:
        call_argv.append(f"run_id={ctx.run_id + '-soak'!r}")
    if "runs_root" in accepted:
        call_argv.append(f"runs_root={str(ctx.runs_root)!r}")
    script = (
        "import importlib, json, sys\n"
        f"module = importlib.import_module({ctx.soak_module!r})\n"
        f"result = module.{entry}({', '.join(call_argv)})\n"
        "print(json.dumps({'result': result}, default=str, sort_keys=True))\n"
    )
    command = ctx.run(
        ctx.python_command("-c", script),
        discovery_source=f"{ctx.soak_module}.{entry} (Python sweep entry point)",
        note=f"seeds={ctx.seeds} events={ctx.events}",
    )
    if command.exit_code != 0:
        codes = _soak_violation_codes(_soak_document(command.stdout), command.stdout + command.stderr)
        reasons = [f"soak_sweep_failed:{ctx.seeds}_seeds"]
        reasons += [f"soak_invariant_violation:{code}" for code in codes]
        return CheckResult(
            id=check_id,
            title=title,
            status=FAIL,
            reasons=tuple(reasons),
            commands=(probe, command),
            detail={
                "module": ctx.soak_module,
                "seeds": ctx.seeds,
                "events": ctx.events,
                "interface": f"python:{entry}",
                "invariant_codes_fired": list(codes),
            },
        )
    try:
        payload = json.loads(command.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return CheckResult(
            id=check_id,
            title=title,
            status=FAIL,
            reasons=(f"soak_result_unreadable:{ctx.soak_module}.{entry}",),
            commands=(probe, command),
            detail={"module": ctx.soak_module},
        )
    verdict = _interpret_sweep_result(payload.get("result"))
    if verdict is None:
        return CheckResult(
            id=check_id,
            title=title,
            status=FAIL,
            reasons=(f"soak_result_unreadable_to_the_gate:{ctx.soak_module}.{entry}",),
            commands=(probe, command),
            detail={"module": ctx.soak_module},
        )
    return CheckResult(
        id=check_id,
        title=title,
        status=PASS if verdict else FAIL,
        reasons=() if verdict else (f"soak_sweep_failed:{ctx.seeds}_seeds",),
        commands=(probe, command),
        detail={
            "module": ctx.soak_module,
            "seeds": ctx.seeds,
            "events": ctx.events,
            "interface": f"python:{entry}",
        },
    )


# --------------------------------------------------------------------------- #
# check d: the emulated chain reaches a successful terminal
# --------------------------------------------------------------------------- #


def _pytest_counts(text: str) -> dict[str, int | bool]:
    def count(pattern: str) -> int:
        match = re.search(pattern, text)
        return int(match.group(1)) if match else 0

    return {
        "passed": count(r"(\d+) passed"),
        "failed": count(r"(\d+) failed"),
        "errors": count(r"(\d+) error"),
        "no_tests_ran": "no tests ran" in text,
    }


def check_emulated_terminal(ctx: GateContext) -> CheckResult:
    check_id = "d.emulated_terminal"
    title = "emulated chain reaches a successful terminal"
    files = sorted(
        str(path.relative_to(ctx.repo_root))
        for path in ctx.repo_root.glob(ctx.emulation_glob)
    )
    if not files:
        return CheckResult(
            id=check_id,
            title=title,
            status=FAIL,
            reasons=(f"emulation_suite_missing:{ctx.emulation_glob}",),
            detail={"searched_glob": ctx.emulation_glob, "files": []},
        )
    command = ctx.run(
        ctx.python_command("-m", "pytest", "-q", "-p", "no:cacheprovider", *files),
        discovery_source=f"packet wp2-4 emulation suite ({ctx.emulation_glob})",
    )
    counts = _pytest_counts(command.stdout + command.stderr)
    detail: dict[str, object] = {
        "files": files,
        "searched_glob": ctx.emulation_glob,
        **counts,
    }
    if command.status() == FAIL:
        reasons = [f"emulation_suite_failed:{files}" if not command.timed_out else "emulation_suite_timeout"]
        if counts["no_tests_ran"]:
            reasons.append("emulation_suite_vacuous:no_tests_ran")
        if int(counts["failed"]):
            reasons.append(f"emulation_suite_failed_tests:{counts['failed']}")
        if int(counts["errors"]):
            reasons.append(f"emulation_suite_errors:{counts['errors']}")
        return CheckResult(
            id=check_id,
            title=title,
            status=FAIL,
            reasons=tuple(reasons),
            commands=(command,),
            detail=detail,
        )
    if counts["no_tests_ran"] or not int(counts["passed"]):
        return CheckResult(
            id=check_id,
            title=title,
            status=FAIL,
            reasons=(f"emulation_suite_vacuous:{ctx.emulation_glob}:no_terminal_assertion_observed",),
            commands=(command,),
            detail=detail,
        )
    return CheckResult(
        id=check_id,
        title=title,
        status=PASS,
        commands=(command,),
        detail=detail,
    )


# --------------------------------------------------------------------------- #
# check e: package built from the exact committed bytes
# --------------------------------------------------------------------------- #


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _dirty_paths(status_output: str) -> list[str]:
    paths: list[str] = []
    for line in status_output.splitlines():
        entry = line[3:].strip() if len(line) > 3 else line.strip()
        if not entry:
            continue
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        paths.append(entry.strip())
    return paths


def _discover_package_configs(ctx: GateContext) -> list[Path]:
    return sorted(path for path in ctx.repo_root.glob(ctx.package_config_glob) if path.is_file())


def _repo_relative(path: Path, repo_root: Path) -> Path | None:
    """``path`` relative to the repo, or ``None`` when it lives outside it."""

    try:
        return path.resolve().relative_to(repo_root.resolve())
    except (ValueError, OSError):
        return None


def _materialise_committed_bytes(ctx: GateContext, scratch: Path, head: str) -> tuple[CommandResult, Path | None]:
    """``git archive HEAD`` -> tar file whose sha256 is the source digest."""

    archive = scratch / "source.tar"
    command = ctx.run(
        ("git", "-C", str(ctx.repo_root), "archive", "--format=tar", f"--output={archive}", head),
        discovery_source="git archive HEAD (the exact committed bytes)",
    )
    return command, archive if command.exit_code == 0 else None


def check_package_from_committed_bytes(ctx: GateContext) -> CheckResult:
    check_id = "e.package_from_committed_bytes"
    title = "package built from the exact committed bytes with digests recorded"
    commands: list[CommandResult] = []
    reasons: list[str] = []

    status_result = ctx.git("status", "--porcelain", discovery_source="git status --porcelain")
    commands.append(status_result)
    head_result = ctx.git("rev-parse", "HEAD", discovery_source="git rev-parse HEAD")
    commands.append(head_result)
    tree_result = ctx.git("rev-parse", "HEAD^{tree}", discovery_source="git rev-parse HEAD^{tree}")
    commands.append(tree_result)
    branch_result = ctx.git("rev-parse", "--abbrev-ref", "HEAD", discovery_source="git branch")
    commands.append(branch_result)

    head = head_result.stdout.strip()
    tree = tree_result.stdout.strip()
    branch = branch_result.stdout.strip()
    dirty = _dirty_paths(status_result.stdout) if status_result.exit_code == 0 else []

    record: dict[str, object] = {
        "protocol": PACKAGE_PROTOCOL,
        "run_id": ctx.run_id,
        "verdict": FAIL,
        "source": {
            "mode": ctx.package_source,
            "git_head": head or None,
            "git_tree": tree or None,
            "git_branch": branch or None,
            "archive_sha256": None,
            "archive_path": None,
        },
        "expect_head": ctx.expect_head,
        "working_tree": {"dirty": bool(dirty) or status_result.exit_code != 0, "dirty_paths": dirty},
        "artifacts": [],
        "package_digest": None,
        "reasons": reasons,
    }

    if status_result.exit_code != 0:
        reasons.append(f"git_status_failed:{ctx.repo_root}")
    elif dirty:
        reasons.append(f"working_tree_dirty:{len(dirty)}_paths")
    if not head:
        reasons.append("git_head_unreadable")
    if ctx.expect_head and head and ctx.expect_head != head:
        reasons.append(f"package_source_head_mismatch:{ctx.expect_head}!={head}")

    builder = ctx.package_builder or (ctx.repo_root / PACKAGE_BUILDER)
    configs = _discover_package_configs(ctx)
    missing: list[str] = []
    if not builder.is_file():
        missing.append(f"builder:{builder}")
    if not configs:
        missing.append(f"configs:{ctx.package_config_glob}")
    if missing and not dirty and status_result.exit_code == 0:
        reasons.append("package_build_step_missing:" + "|".join(missing))

    builder_relative = _repo_relative(builder, ctx.repo_root) if builder.is_file() else None
    if builder.is_file() and builder_relative is None:
        reasons.append(f"package_builder_outside_repo:{builder}")
    planned: list[tuple[str, Path, Path]] = []
    for config in configs:
        config_relative = _repo_relative(config, ctx.repo_root)
        if config_relative is None:
            reasons.append(f"package_config_outside_repo:{config}")
            continue
        planned.append((config.stem, config, config_relative))

    can_build = not reasons and bool(planned) and builder_relative is not None
    if can_build:
        assert builder_relative is not None  # narrowed by can_build, kept for the checker
        scratch = ctx.scratch_dir("source")
        source_root = ctx.repo_root
        if ctx.package_source == "committed":
            archive_command, archive_path = _materialise_committed_bytes(ctx, scratch, head)
            commands.append(archive_command)
            if archive_path is None:
                reasons.append("package_source_archive_failed:git_archive_HEAD")
                can_build = False
            else:
                record["source"]["archive_sha256"] = _sha256_file(archive_path)
                record["source"]["archive_path"] = str(archive_path)
                extracted = scratch / "source"
                extracted.mkdir(parents=True, exist_ok=True)
                try:
                    with tarfile.open(archive_path, "r") as tar:
                        tar.extractall(extracted, filter="data")
                except (tarfile.TarError, OSError, TypeError) as exc:
                    reasons.append(f"package_source_extract_failed:{type(exc).__name__}")
                    can_build = False
                else:
                    source_root = extracted
        else:
            reasons_note = "built in place from the verified-clean worktree"
            record["source"]["note"] = reasons_note

        if can_build:
            artifacts: list[dict[str, object]] = []
            artifact_root = ctx.run_dir / "package"
            record["build"] = {
                "artifact_root": str(artifact_root),
                "note": (
                    "the m22 service package embeds its absolute install root "
                    "(the plist / systemd unit point at the output root's "
                    "service-config.json), so the package digest is defined for "
                    "this deterministic output root and a rebuild must reuse it"
                ),
            }
            for role, config, config_relative in planned:
                output_root = artifact_root / role
                if output_root.exists():
                    shutil.rmtree(output_root)
                output_root.mkdir(parents=True, exist_ok=True)
                build_command = ctx.run(
                    ctx.python_command(
                        str(source_root / builder_relative),
                        "--config",
                        str(source_root / config_relative),
                        "--output-root",
                        str(output_root),
                    ),
                    discovery_source=f"{builder_relative.as_posix()} ({ctx.package_source} source)",
                    note=f"role={role}",
                )
                commands.append(build_command)
                if build_command.exit_code != 0:
                    reasons.append(f"package_build_failed:{role}")
                    continue
                files: list[dict[str, object]] = []
                for produced in sorted(output_root.rglob("*")):
                    if not produced.is_file():
                        continue
                    files.append(
                        {
                            "path": produced.relative_to(output_root).as_posix(),
                            "sha256": _sha256_file(produced),
                            "bytes": produced.stat().st_size,
                        }
                    )
                if not files:
                    reasons.append(f"package_build_produced_nothing:{role}")
                    continue
                artifacts.append(
                    {
                        "role": role,
                        "config": config_relative.as_posix(),
                        "output_root": str(output_root),
                        "files": files,
                    }
                )
            if artifacts:
                record["artifacts"] = artifacts
                record["package_digest"] = _package_digest(artifacts)
            if not ctx.keep_scratch:
                for directory in list(ctx.scratch_dirs):
                    shutil.rmtree(directory, ignore_errors=True)
                ctx.scratch_dirs = [item for item in ctx.scratch_dirs if not item.exists()]

    if not reasons:
        prior = _read_prior_package_record(ctx)
        if prior is not None:
            prior_head = str((prior.get("source") or {}).get("git_head") or "")
            prior_digest = prior.get("package_digest")
            record["prior_record"] = {"path": str(ctx.package_record_path), "git_head": prior_head, "package_digest": prior_digest}
            if prior_head == head and prior_digest and prior_digest != record["package_digest"]:
                reasons.append("package_digest_drift_for_recorded_head")

    record["reasons"] = reasons
    record["verdict"] = PASS if not reasons and record["package_digest"] else FAIL
    if not reasons and not record["package_digest"]:
        reasons.append("package_digest_not_recorded")
        record["verdict"] = FAIL
        record["reasons"] = reasons
    _write_json(ctx.package_record_path, record)

    detail = {
        "package_record": str(ctx.package_record_path),
        "package_digest": record["package_digest"],
        "git_head": head or None,
        "git_tree": tree or None,
        "dirty_paths": dirty,
        "source_mode": ctx.package_source,
        "archive_sha256": record["source"]["archive_sha256"],
        "artifacts": [item["role"] for item in record["artifacts"]],
    }
    return CheckResult(
        id=check_id,
        title=title,
        status=PASS if record["verdict"] == PASS else FAIL,
        reasons=tuple(reasons),
        commands=tuple(commands),
        detail=detail,
    )


def _package_digest(artifacts: Sequence[Mapping[str, object]]) -> str:
    digest = hashlib.sha256()
    for artifact in sorted(artifacts, key=lambda item: str(item["role"])):
        digest.update(str(artifact["role"]).encode("utf-8"))
        digest.update(b"\0")
        files = artifact.get("files") or []
        for entry in sorted(files, key=lambda item: str(item["path"])):  # type: ignore[union-attr]
            digest.update(str(entry["path"]).encode("utf-8"))  # type: ignore[index]
            digest.update(b"\0")
            digest.update(str(entry["sha256"]).encode("utf-8"))  # type: ignore[index]
            digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _read_prior_package_record(ctx: GateContext) -> dict[str, object] | None:
    path = ctx.package_record_path
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


# --------------------------------------------------------------------------- #
# check f: the pre-registered hypothesis
# --------------------------------------------------------------------------- #


def check_hypothesis(ctx: GateContext) -> CheckResult:
    check_id = "f.hypothesis_pre_registration"
    title = "pre-registered runs/<id>/HYPOTHESIS.md exists"
    path = ctx.run_dir / HYPOTHESIS_FILENAME
    detail: dict[str, object] = {"path": str(path), "run_id": ctx.run_id}
    if not path.is_file():
        return CheckResult(
            id=check_id,
            title=title,
            status=FAIL,
            reasons=(f"hypothesis_file_missing:{path}",),
            detail=detail,
        )
    text = path.read_text("utf-8", errors="replace")
    if not text.strip():
        return CheckResult(
            id=check_id,
            title=title,
            status=FAIL,
            reasons=(f"hypothesis_file_empty:{path}",),
            detail=detail,
        )
    lowered = text.lower()
    detail |= {
        "sha256": _sha256_file(path),
        "bytes": path.stat().st_size,
        "states_what_it_proves": "prove" in lowered,
        "states_expected_result": "expect" in lowered,
        "states_deciding_evidence": "evidence" in lowered,
    }
    return CheckResult(id=check_id, title=title, status=PASS, detail=detail)


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #

CHECKS: tuple[Check, ...] = (
    Check("a.canonical_suites_and_audits", "canonical suites + audits green", check_canonical_suites_and_audits),
    Check("b.budget_invariants", "budget invariants green (mycelium_deadlines.check_composition)", check_budget_invariants),
    Check("c.soak", "seeded soak over N seeds green", check_soak),
    Check("d.emulated_terminal", "emulated chain reaches a successful terminal", check_emulated_terminal),
    Check(
        "e.package_from_committed_bytes",
        "package built from the exact committed bytes with digests recorded",
        check_package_from_committed_bytes,
    ),
    Check("f.hypothesis_pre_registration", "pre-registered runs/<id>/HYPOTHESIS.md exists", check_hypothesis),
)


def run_gate(ctx: GateContext, *, checks: Sequence[Check] | None = None) -> dict[str, object]:
    """Run every check, in order, and build the report (never short-circuits)."""

    selected = list(CHECKS if checks is None else checks)
    started = time.monotonic()
    results: list[CheckResult] = []
    for check in selected:
        results.append(check.run(ctx))
    verdict = "GO" if results and all(item.status == PASS for item in results) else "NO-GO"
    report: dict[str, object] = {
        "protocol": PROTOCOL,
        "verdict": verdict,
        "run_id": ctx.run_id,
        "repo_root": str(ctx.repo_root),
        "runs_root": str(ctx.runs_root),
        "run_dir": str(ctx.run_dir),
        "seeds": ctx.seeds,
        "events": ctx.events,
        "extended": ctx.extended,
        "package_source": ctx.package_source,
        "duration_seconds": round(time.monotonic() - started, 3),
        "checks": [item.record() for item in results],
        "failed_checks": [item.id for item in results if item.status == FAIL],
        "skipped_checks": [item.id for item in results if item.status == SKIPPED],
        "reasons": [
            f"{item.id}:{reason}"
            for item in results
            if item.status in {FAIL, SKIPPED}
            for reason in item.reasons
        ],
        "evidence_paths": [],
    }
    return report


def render_report(report: Mapping[str, object]) -> str:
    checks = report.get("checks") or []
    lines: list[str] = []
    lines.append(f"pilot readiness gate [{report.get('protocol')}]")
    lines.append(f"run id        : {report.get('run_id')}")
    lines.append(f"repo root     : {report.get('repo_root')}")
    lines.append(f"evidence      : {report.get('run_dir')}")
    lines.append(f"seed sweep    : {report.get('seeds')} seeds x {report.get('events')} events")
    lines.append("")
    for index, raw in enumerate(checks, start=1):  # type: ignore[union-attr]
        check = dict(raw)
        status = str(check.get("status"))
        marker = {PASS: "PASS", FAIL: "FAIL", SKIPPED: "SKIP"}.get(status, status)
        lines.append(f"[{index}/{len(checks)}] {check.get('title')} ... {marker}")
        detail = check.get("detail") or {}
        if isinstance(detail, dict) and detail.get("dirty_paths"):
            lines.append(f"      dirty paths ({len(detail['dirty_paths'])}):")
            for path in list(detail["dirty_paths"])[:REASON_LINES]:
                lines.append(f"        - {path}")
        if isinstance(detail, dict):
            for entry in list(detail.get("not_executed_prerequisites") or [])[:REASON_LINES]:
                lines.append(f"      not executed (prerequisite): {entry}")
            for entry in list(detail.get("excluded_non_host_jobs") or [])[:REASON_LINES]:
                lines.append(f"      not discovered (non-host runner): {entry}")
        for command in check.get("commands") or []:  # type: ignore[union-attr]
            entry = dict(command)
            lines.append(
                f"      cmd: {entry.get('cmd')} | exit {entry.get('exit_code')} | {entry.get('duration_seconds')}s"
            )
        for reason in check.get("reasons") or []:  # type: ignore[union-attr]
            lines.append(f"      reason: {reason}")
        if status == FAIL:
            tails = [dict(item) for item in (check.get("commands") or [])]
            failing = [item for item in tails if item.get("exit_code") not in (0, None)] or tails
            if failing:
                tail = str(failing[-1].get("tail") or "").strip()
                if tail:
                    for line in tail.splitlines()[-REASON_LINES:]:
                        lines.append(f"      | {line}")
    lines.append("")
    failed = report.get("failed_checks") or []
    skipped = report.get("skipped_checks") or []
    lines.append(f"checks: {len(checks)} | failed: {len(failed)} | skipped-with-reason: {len(skipped)}")
    if failed:
        lines.append("failing checks: " + ", ".join(str(item) for item in failed))
    if skipped:
        lines.append("skipped checks: " + ", ".join(str(item) for item in skipped))
    lines.append(f"verdict: {report.get('verdict')}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# evidence
# --------------------------------------------------------------------------- #


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n", "utf-8")


def write_evidence(ctx: GateContext, report: dict[str, object]) -> list[str]:
    run_dir = ctx.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    json_path = run_dir / "readiness-gate.json"
    text_path = run_dir / "readiness-gate.txt"
    _write_json(json_path, report)
    text_path.write_text(render_report(report) + "\n", "utf-8")
    paths = [str(json_path), str(text_path)]
    if ctx.package_record_path.is_file():
        paths.append(str(ctx.package_record_path))
    report["evidence_paths"] = paths
    _write_json(json_path, report)
    return paths


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    """The gate's CLI.

    There is deliberately **no** flag that skips, replaces, only-runs or disables
    a check: check ``e`` (committed bytes + digests) is mandatory, and so are the
    other five.  The flags tune inputs (seeds, timeout, paths), never the set of
    mandatory checks.
    """

    parser = argparse.ArgumentParser(
        prog="pilot_readiness_gate.py",
        description="Print GO/NO-GO for the Mycelium core pilot physical readiness gate (PLAN 5.2 item 5).",
    )
    parser.add_argument("--run-id", required=True, help="run id whose runs/<id>/HYPOTHESIS.md must exist (PLAN 5.3)")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="integrated worktree (default: cwd)")
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT, help="evidence root OUTSIDE the repo")
    parser.add_argument("--python", default=sys.executable, help="interpreter used for every python child")
    parser.add_argument("--seeds", type=int, default=DEFAULT_SEEDS, help="soak seeds (N)")
    parser.add_argument("--events", type=int, default=DEFAULT_EVENTS, help="soak events per seed, when the CLI takes it")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="per-command timeout in seconds")
    parser.add_argument("--json", action="store_true", help="machine-readable report on stdout (human report on stderr)")
    parser.add_argument("--extended", action="store_true", help="add the release-runbook commands (additive only)")
    parser.add_argument("--expect-head", default=None, help="require this git HEAD for the package source")
    parser.add_argument(
        "--package-source",
        choices=("committed", "worktree"),
        default="committed",
        help="build the package from `git archive HEAD` (default) or from the verified-clean worktree",
    )
    parser.add_argument("--package-builder", type=Path, default=None, help="override the discovered packager script")
    parser.add_argument("--package-configs", default=PACKAGE_CONFIG_GLOB, help="glob of service configs to package")
    parser.add_argument("--keep-scratch", action="store_true", help="keep the scratch build directories")
    return parser


def context_from_args(args: argparse.Namespace) -> GateContext:
    return GateContext(
        repo_root=Path(args.repo_root).resolve(),
        runs_root=Path(args.runs_root).resolve(),
        run_id=str(args.run_id),
        python=str(args.python),
        seeds=int(args.seeds),
        events=int(args.events),
        timeout=float(args.timeout),
        extended=bool(args.extended),
        expect_head=args.expect_head,
        package_source=str(args.package_source),
        package_builder=Path(args.package_builder).resolve() if args.package_builder else None,
        package_config_glob=str(args.package_configs),
        keep_scratch=bool(args.keep_scratch),
    )


def main(argv: Sequence[str] | None = None, *, checks: Sequence[Check] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    ctx = context_from_args(args)
    if ctx.seeds < 1:
        parser.error("--seeds must be >= 1")
    try:
        report = run_gate(ctx, checks=checks)
    finally:
        # a crash must not leave a scratch tree (or an extracted archive) behind
        if not ctx.keep_scratch:
            for directory in list(ctx.scratch_dirs):
                shutil.rmtree(directory, ignore_errors=True)
            ctx.scratch_dirs = [item for item in ctx.scratch_dirs if not item.exists()]
    paths = write_evidence(ctx, report)
    human = render_report(report)
    if args.json:
        print(json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        print(human, file=sys.stderr)
    else:
        print(human)
        print("evidence: " + ", ".join(paths), file=sys.stderr)
    return 0 if report["verdict"] == "GO" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CHECKS",
    "FAIL",
    "PASS",
    "PINNED_ROUND_50_CODE",
    "PROTOCOL",
    "SKIPPED",
    "Check",
    "CheckResult",
    "CommandResult",
    "CommandRunner",
    "GateContext",
    "aggregate",
    "build_parser",
    "check_budget_invariants",
    "check_canonical_suites_and_audits",
    "check_emulated_terminal",
    "check_hypothesis",
    "check_package_from_committed_bytes",
    "check_soak",
    "context_from_args",
    "discover_canonical_commands",
    "main",
    "render_report",
    "run_gate",
    "subprocess_runner",
    "write_evidence",
]
