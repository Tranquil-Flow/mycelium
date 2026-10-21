# SPDX-License-Identifier: AGPL-3.0-only
"""wp2-5: the pilot readiness gate (``scripts/pilot_readiness_gate.py``).

The gate exists to say ``NO-GO`` out loud, so these tests cover:

* a **GO** built from stub checks - the aggregation, the exit code and the
  report shape, with no canonical suite, soak or emulation run;
* two **NO-GO** cases that violate reality rather than a stub: a missing
  pre-registered ``runs/<id>/HYPOTHESIS.md`` and a dirty working tree;
* that check ``e`` (package built from the exact committed bytes, digests
  recorded) can really build, from ``git archive HEAD``, with the repo's own
  packager, and that no CLI flag can disable it;
* named-reason failures for an absent/incomplete soak CLI, an absent emulation
  suite, an undiscoverable canonical pytest step and an unbuildable package -
  every check the gate could not observe is ``FAIL``/``SKIPPED``, never a green.

Every check is driven through the ``CommandRunner`` seam (``StubRunner`` /
``HybridRunner``), so the file passes independently of ``mycelium_testbed.soak``,
``mycelium_testbed.emulation`` and ``mycelium_testbed.faults`` - which other
packets own and were still being written when this gate was delivered.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pytest

from scripts import pilot_readiness_gate as gate

ROOT = Path(__file__).resolve().parents[2]

PINNED_CODE = "peer_advertised_cleanup_bound_exceeds_route_fanout_window"
SOAK_CODE = "nonterminal_row_not_retired_within_bound"


# --------------------------------------------------------------------------- #
# runner stubs
# --------------------------------------------------------------------------- #


def canned(
    command: Sequence[str],
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
) -> gate.CommandResult:
    return gate.CommandResult(
        command=tuple(command),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.0,
        timed_out=timed_out,
    )


class Rule:
    """One canned response, matched by substring, exact argv or a callback."""

    def __init__(
        self,
        *,
        contains: str | None = None,
        exact: Sequence[str] | None = None,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
        stdout_from: Callable[[tuple[str, ...]], str] | None = None,
        exit_code_from: Callable[[tuple[str, ...]], int] | None = None,
    ) -> None:
        self.contains = contains
        self.exact = tuple(exact) if exact is not None else None
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.stdout_from = stdout_from
        self.exit_code_from = exit_code_from

    def matches(self, command: tuple[str, ...]) -> bool:
        if self.exact is not None:
            return command == self.exact
        if self.contains is not None:
            return self.contains in " ".join(command)
        return False

    def respond(self, command: tuple[str, ...]) -> gate.CommandResult:
        return canned(
            command,
            exit_code=self.exit_code_from(command) if self.exit_code_from else self.exit_code,
            stdout=self.stdout_from(command) if self.stdout_from else self.stdout,
            stderr=self.stderr,
            timed_out=self.timed_out,
        )


class StubRunner:
    """Canned responses only: an unmatched command is a loud, named failure."""

    def __init__(self, rules: Sequence[Rule] = ()) -> None:
        self.rules = list(rules)
        self.calls: list[tuple[str, ...]] = []

    def add(self, rule: Rule) -> "StubRunner":
        self.rules.append(rule)
        return self

    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
    ) -> gate.CommandResult:
        self.calls.append(tuple(command))
        for rule in self.rules:
            if rule.matches(tuple(command)):
                return rule.respond(tuple(command))
        return canned(
            command,
            exit_code=127,
            stderr=f"unmatched_stub_command: {' '.join(command)}",
        )


class HybridRunner:
    """Real subprocesses, except for the commands a rule intercepts.

    Used where a test must observe the *real* packager / *real* git while
    pretending the working tree is clean.
    """

    def __init__(self, rules: Sequence[Rule] = ()) -> None:
        self.rules = list(rules)
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
    ) -> gate.CommandResult:
        self.calls.append(tuple(command))
        for rule in self.rules:
            if rule.matches(tuple(command)):
                return rule.respond(tuple(command))
        return gate.subprocess_runner(command, cwd=cwd, env=env, timeout=timeout)


# --------------------------------------------------------------------------- #
# contexts and stub checks
# --------------------------------------------------------------------------- #


def context(
    tmp_path: Path,
    *,
    run_id: str = "wp2-5-unit",
    runner: gate.CommandRunner | None = None,
    repo_root: Path | None = None,
    seeds: int = 3,
    events: int = 25,
    timeout: float = 120.0,
    extended: bool = False,
    expect_head: str | None = None,
    package_source: str = "committed",
    soak_module: str = gate.SOAK_MODULE,
    emulation_glob: str = gate.EMULATION_GLOB,
    package_builder: Path | None = None,
) -> gate.GateContext:
    return gate.GateContext(
        repo_root=repo_root if repo_root is not None else ROOT,
        runs_root=tmp_path / "runs",
        run_id=run_id,
        seeds=seeds,
        events=events,
        timeout=timeout,
        runner=runner if runner is not None else StubRunner(),
        extended=extended,
        expect_head=expect_head,
        package_source=package_source,
        soak_module=soak_module,
        emulation_glob=emulation_glob,
        package_builder=package_builder,
    )


def stub_check(check_id: str, status: str, reasons: Sequence[str] = ()) -> gate.Check:
    def run(_ctx: gate.GateContext) -> gate.CheckResult:
        return gate.CheckResult(id=check_id, title=f"stub {check_id}", status=status, reasons=tuple(reasons))

    return gate.Check(id=check_id, title=f"stub {check_id}", run=run)


def all_pass_checks() -> list[gate.Check]:
    return [stub_check(item.id, gate.PASS) for item in gate.CHECKS]


def git_status_response(head: str) -> Rule:
    return Rule(exact=("git", "-C", str(ROOT), "status", "--porcelain"), stdout="")


def real_head() -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


# --------------------------------------------------------------------------- #
# GO from stub checks
# --------------------------------------------------------------------------- #


def test_go_when_all_six_checks_pass(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    checks = all_pass_checks()
    assert [item.id for item in checks] == [item.id for item in gate.CHECKS]

    ctx = context(tmp_path)
    report = gate.run_gate(ctx, checks=checks)

    assert report["verdict"] == "GO"
    assert report["failed_checks"] == [] and report["skipped_checks"] == []
    assert report["reasons"] == []
    assert len(report["checks"]) == 6

    exit_code = gate.main(
        ["--run-id", "wp2-5-unit", "--runs-root", str(tmp_path / "runs"), "--json"],
        checks=checks,
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out.strip().splitlines()[-1])
    assert payload["verdict"] == "GO"
    assert "verdict: GO" in captured.err


def test_any_failing_check_is_no_go_and_names_the_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checks = all_pass_checks()
    checks[4] = stub_check(
        "e.package_from_committed_bytes", gate.FAIL, ("working_tree_dirty:2_paths",)
    )
    report = gate.run_gate(context(tmp_path), checks=checks)

    assert report["verdict"] == "NO-GO"
    assert report["failed_checks"] == ["e.package_from_committed_bytes"]
    assert any("working_tree_dirty" in reason for reason in report["reasons"])

    exit_code = gate.main(
        ["--run-id", "wp2-5-unit", "--runs-root", str(tmp_path / "runs"), "--json"], checks=checks
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.out.strip().splitlines()[-1])["verdict"] == "NO-GO"
    assert "e.package_from_committed_bytes" in captured.err


def test_a_skipped_check_is_never_go(tmp_path: Path) -> None:
    checks = all_pass_checks()
    checks[0] = stub_check(
        "a.canonical_suites_and_audits",
        gate.SKIPPED,
        ("canonical_pytest_step_not_discoverable",),
    )
    report = gate.run_gate(context(tmp_path), checks=checks)
    assert report["verdict"] == "NO-GO"
    assert report["skipped_checks"] == ["a.canonical_suites_and_audits"]

    assert gate.aggregate([gate.PASS, gate.PASS]) == gate.PASS
    assert gate.aggregate([gate.PASS, gate.SKIPPED]) == gate.SKIPPED
    assert gate.aggregate([gate.PASS, gate.FAIL]) == gate.FAIL
    assert gate.aggregate([]) == gate.SKIPPED


# --------------------------------------------------------------------------- #
# check f: the pre-registered hypothesis (a real NO-GO)
# --------------------------------------------------------------------------- #


def test_missing_hypothesis_file_is_a_named_no_go(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    result = gate.check_hypothesis(ctx)

    assert result.status == gate.FAIL
    assert result.reasons == (f"hypothesis_file_missing:{tmp_path / 'runs' / 'wp2-5-unit' / 'HYPOTHESIS.md'}",)
    assert "HYPOTHESIS.md" in result.reasons[0]


def test_empty_hypothesis_file_is_a_named_no_go(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    (ctx.run_dir / "HYPOTHESIS.md").write_text("   \n\n", "utf-8")

    result = gate.check_hypothesis(ctx)
    assert result.status == gate.FAIL
    assert result.reasons[0].startswith("hypothesis_file_empty:")


def test_present_hypothesis_file_passes_and_records_the_declaration(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    ctx.run_dir.mkdir(parents=True, exist_ok=True)
    (ctx.run_dir / "HYPOTHESIS.md").write_text(
        "This run proves two-Mac C5 terminal. Expected result: streamed tokens. "
        "Deciding evidence: the traces under this run directory.\n",
        "utf-8",
    )

    result = gate.check_hypothesis(ctx)
    assert result.status == gate.PASS
    assert result.detail["states_what_it_proves"] is True
    assert result.detail["states_expected_result"] is True
    assert result.detail["states_deciding_evidence"] is True
    assert str(result.detail["sha256"]).startswith("sha256:")


# --------------------------------------------------------------------------- #
# check e: dirty tree and committed bytes
# --------------------------------------------------------------------------- #


def test_dirty_tree_fails_check_e_with_the_dirty_paths_listed(tmp_path: Path) -> None:
    dirty = " M mycelium_live/member_transport.py\n?? mycelium_deadlines.py\n"
    runner = StubRunner(
        [
            Rule(exact=("git", "-C", str(ROOT), "status", "--porcelain"), stdout=dirty),
            Rule(contains="rev-parse HEAD^{tree}", stdout="b" * 40),
            Rule(contains="rev-parse --abbrev-ref HEAD", stdout="integration/core-pilot"),
            Rule(contains="rev-parse HEAD", stdout="a" * 40),
        ]
    )
    ctx = context(tmp_path, runner=runner)

    result = gate.check_package_from_committed_bytes(ctx)

    assert result.status == gate.FAIL
    assert "working_tree_dirty:2_paths" in result.reasons
    assert result.detail["dirty_paths"] == [
        "mycelium_live/member_transport.py",
        "mycelium_deadlines.py",
    ]
    # a dirty tree must not be built from: only the four git observations ran
    assert len(result.commands) == 4
    assert not any("archive" in " ".join(item.command) for item in result.commands)

    record = json.loads((tmp_path / "runs" / "wp2-5-unit" / "PACKAGE.json").read_text("utf-8"))
    assert record["verdict"] == "FAIL"
    assert record["working_tree"]["dirty_paths"] == [
        "mycelium_live/member_transport.py",
        "mycelium_deadlines.py",
    ]
    assert record["protocol"] == gate.PACKAGE_PROTOCOL


def test_check_e_builds_from_committed_bytes_and_records_digests(tmp_path: Path) -> None:
    """The real packager, the real ``git archive HEAD``, a stubbed clean tree."""

    runner = HybridRunner([Rule(exact=("git", "-C", str(ROOT), "status", "--porcelain"), stdout="")])
    ctx = context(tmp_path, runner=runner)

    result = gate.check_package_from_committed_bytes(ctx)

    assert result.status == gate.PASS, result.reasons
    assert result.reasons == ()
    assert str(result.detail["git_head"]) == real_head()
    assert str(result.detail["package_digest"]).startswith("sha256:")
    assert str(result.detail["archive_sha256"]).startswith("sha256:")
    assert sorted(result.detail["artifacts"]) == ["node", "seed", "supervisor"]

    record = json.loads((tmp_path / "runs" / "wp2-5-unit" / "PACKAGE.json").read_text("utf-8"))
    assert record["verdict"] == "PASS"
    assert record["source"]["mode"] == "committed"
    assert record["source"]["git_head"] == real_head()
    assert record["package_digest"] == result.detail["package_digest"]
    assert record["build"]["artifact_root"] == str(tmp_path / "runs" / "wp2-5-unit" / "package")
    for artifact in record["artifacts"]:
        assert artifact["files"], artifact
        assert artifact["output_root"] == str(
            tmp_path / "runs" / "wp2-5-unit" / "package" / artifact["role"]
        )
        for entry in artifact["files"]:
            assert str(entry["sha256"]).startswith("sha256:")
            durable = Path(artifact["output_root"]) / str(entry["path"])
            assert durable.is_file(), durable


def test_check_e_rebuilding_the_same_head_is_digest_stable(tmp_path: Path) -> None:
    runner = HybridRunner([Rule(exact=("git", "-C", str(ROOT), "status", "--porcelain"), stdout="")])
    ctx = context(tmp_path, runner=runner)

    first = gate.check_package_from_committed_bytes(ctx)
    second = gate.check_package_from_committed_bytes(ctx)

    assert first.status == gate.PASS and second.status == gate.PASS, second.reasons
    assert first.detail["package_digest"] == second.detail["package_digest"]
    assert "package_digest_drift_for_recorded_head" not in second.reasons


def test_check_e_head_mismatch_is_named(tmp_path: Path) -> None:
    runner = HybridRunner([Rule(exact=("git", "-C", str(ROOT), "status", "--porcelain"), stdout="")])
    ctx = context(tmp_path, runner=runner, expect_head="c" * 40)

    result = gate.check_package_from_committed_bytes(ctx)
    assert result.status == gate.FAIL
    assert any(reason.startswith("package_source_head_mismatch:") for reason in result.reasons)


def test_check_e_names_the_missing_build_step(tmp_path: Path) -> None:
    empty_repo = tmp_path / "repo"
    empty_repo.mkdir()
    runner = StubRunner(
        [
            Rule(contains="status --porcelain", stdout=""),
            Rule(contains="rev-parse HEAD^{tree}", stdout="b" * 40),
            Rule(contains="rev-parse --abbrev-ref HEAD", stdout="main"),
            Rule(contains="rev-parse HEAD", stdout="a" * 40),
        ]
    )
    ctx = context(tmp_path, runner=runner, repo_root=empty_repo)

    result = gate.check_package_from_committed_bytes(ctx)
    assert result.status == gate.FAIL
    missing = [reason for reason in result.reasons if reason.startswith("package_build_step_missing:")]
    assert missing, result.reasons
    assert "scripts/package_m22_service.py" in missing[0]
    assert "release/service-configs/*.json" in missing[0]


# --------------------------------------------------------------------------- #
# check e is mandatory: no flag may disable a check
# --------------------------------------------------------------------------- #


def test_no_cli_flag_can_disable_a_mandatory_check() -> None:
    parser = gate.build_parser()
    options = {option for action in parser._actions for option in action.option_strings}
    forbidden = (
        "--skip-check",
        "--only",
        "--disable-check",
        "--no-package",
        "--skip-package",
        "--ignore",
        "--fail-fast-skip",
        "--checks",
    )
    for flag in forbidden:
        assert flag not in options, flag
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args(["--run-id", "x", flag])
        assert excinfo.value.code == 2, flag

    # inputs may be tuned, the check set may not: a package source value that
    # skipped the build is not a legal input
    args = parser.parse_args(["--run-id", "x", "--package-source", "worktree", "--seeds", "4"])
    assert args.package_source == "worktree"
    assert args.seeds == 4
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--run-id", "x", "--package-source", "skip"])
    assert excinfo.value.code == 2

    assert len(gate.CHECKS) == 6
    assert "e.package_from_committed_bytes" in {item.id for item in gate.CHECKS}


def test_every_mandatory_check_runs_even_when_an_earlier_one_fails(tmp_path: Path) -> None:
    runner = StubRunner(
        [
            Rule(exact=("git", "-C", str(ROOT), "status", "--porcelain"), stdout=" M dirty.py\n"),
            Rule(contains="rev-parse HEAD^{tree}", stdout="b" * 40),
            Rule(contains="rev-parse --abbrev-ref HEAD", stdout="main"),
            Rule(contains="rev-parse HEAD", stdout="a" * 40),
        ]
    )
    ctx = context(tmp_path, runner=runner, repo_root=ROOT)

    report = gate.run_gate(ctx)  # the real registry: no injection

    assert len(report["checks"]) == 6
    assert [item["id"] for item in report["checks"]] == [item.id for item in gate.CHECKS]
    assert report["verdict"] == "NO-GO"
    assert "e.package_from_committed_bytes" in report["failed_checks"]
    assert "f.hypothesis_pre_registration" in report["failed_checks"]


# --------------------------------------------------------------------------- #
# check a: discovered canonical commands
# --------------------------------------------------------------------------- #


def test_check_a_discovers_the_ci_pytest_step_audits_and_ui_scripts(
    tmp_path: Path, ui_web_toolchain: None
) -> None:
    # `ui_web_toolchain` models the gitignored `ui/web/node_modules` prerequisite
    # the gate reads before it will discover the declared npm scripts. The
    # assertions below are unchanged: discovery of the *declared* scripts is what
    # this test is about, and no npm/ui command is executed (commands are stubbed).
    ctx = context(tmp_path)
    discovered = gate.discover_canonical_commands(ctx)
    runnable = [item for item in discovered if item.kind == "run"]

    pytest_steps = [
        item
        for item in runnable
        if any("pytest" in token for token in item.argv)
        and item.source == gate.WORKFLOW.as_posix()
    ]
    assert len(pytest_steps) == 1, discovered
    workflow_pytest = pytest_steps[0].argv
    assert {"tests/governance", "tests/claims", "tests/contracts"} <= set(workflow_pytest)
    assert "-p" in workflow_pytest and "no:cacheprovider" in workflow_pytest

    for relative in (
        "scripts/governance_gate.py",
        "scripts/contract_audit.py",
        "scripts/claim_boundary_audit.py",
        "scripts/mycelium_completion_audit.py",
    ):
        assert any(relative in " ".join(item.argv) for item in runnable), relative

    for name in ("test", "test:contracts", "typecheck"):
        matches = [item for item in runnable if item.argv[-1] == name and item.cwd is not None]
        assert len(matches) == 1, name
        assert matches[0].cwd == ROOT / "ui" / "web"
        assert matches[0].source.endswith(f"#scripts.{name}")

    extended = gate.discover_canonical_commands(context(tmp_path, extended=True))
    extended_argv = {" ".join(item.argv) for item in extended if item.kind == "run"}
    assert any("pytest" in value and "-q" in value for value in extended_argv), extended_argv
    assert any(value.endswith("npm run build") for value in extended_argv), extended_argv
    assert extended_argv > {item for item in (" ".join(item.argv) for item in runnable)}


def test_check_a_skips_an_undiscoverable_pytest_step_with_a_reason(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "governance.yml").write_text(
        "on: [push]\njobs:\n  g:\n    steps:\n      - name: install\n"
        "        run: python -m pip install --requirement tests/governance-requirements.txt\n",
        "utf-8",
    )
    ctx = context(tmp_path, repo_root=repo)

    result = gate.check_canonical_suites_and_audits(ctx)

    assert result.status == gate.SKIPPED
    assert any("canonical_pytest_step_not_discoverable" in reason for reason in result.reasons)
    # the dependency install is an environment prerequisite: recorded, never
    # silently dropped, and never turned into a green or a failure by itself
    assert any(
        "workflow_dependency_install_not_run" in entry
        for entry in result.detail["not_executed_prerequisites"]
    )
    assert not any("dependency_install" in reason for reason in result.reasons)


def test_check_a_records_the_dependency_prerequisite_without_failing(
    tmp_path: Path, ui_web_toolchain: None
) -> None:
    """A green (a) with a recorded, not-executed workflow prerequisite stays green.

    ``ui_web_toolchain`` supplies the gitignored ``ui/web/node_modules``
    prerequisite so that the only *unexecuted* prerequisite this test observes is
    the workflow's dependency install; the assertion is unchanged.
    """

    runner = StubRunner(
        [
            Rule(contains="pytest", stdout="58 passed in 6.35s"),
            Rule(contains="governance_gate.py", stdout="{}"),
            Rule(contains="contract_audit.py", stdout="{}"),
            Rule(contains="claim_boundary_audit.py", stdout="{}"),
            Rule(contains="mycelium_completion_audit.py", stdout="{}"),
            Rule(contains="run typecheck", stdout="ok"),
            Rule(contains="run test:contracts", stdout="ok"),
            Rule(contains="run test", stdout="ok"),
        ]
    )
    result = gate.check_canonical_suites_and_audits(context(tmp_path, runner=runner))

    assert result.status == gate.PASS
    assert result.reasons == ()
    assert any(
        "workflow_dependency_install_not_run" in entry
        for entry in result.detail["not_executed_prerequisites"]
    )
    assert "not executed (prerequisite)" in gate.render_report(
        {
            "protocol": gate.PROTOCOL,
            "run_id": "x",
            "checks": [result.record()],
            "failed_checks": [],
            "skipped_checks": [],
            "verdict": "GO",
        }
    )


def test_check_a_fails_when_a_canonical_command_fails(tmp_path: Path) -> None:
    runner = StubRunner(
        [
            Rule(contains="pytest", exit_code=1, stdout="2 failed, 10 passed"),
            Rule(contains="governance_gate.py", stdout="{}"),
            Rule(contains="contract_audit.py", stdout="{}"),
            Rule(contains="claim_boundary_audit.py", stdout="{}"),
            Rule(contains="mycelium_completion_audit.py", stdout="{}"),
            Rule(contains="run typecheck", stdout="ok"),
            Rule(contains="run test:contracts", stdout="ok"),
            Rule(contains="run test", stdout="ok"),
        ]
    )
    ctx = context(tmp_path, runner=runner)

    result = gate.check_canonical_suites_and_audits(ctx)

    assert result.status == gate.FAIL
    assert any(reason.startswith("canonical_command_failed:") for reason in result.reasons)
    failing = [item for item in result.commands if item.exit_code == 1]
    assert len(failing) == 1
    assert "pytest" in " ".join(failing[0].command)
    assert failing[0].tail()


def test_check_a_scopes_discovery_to_host_executable_jobs(tmp_path: Path) -> None:
    """A job on another OS family is not a canonical command for this host.

    ``.github/workflows/governance.yml`` grew a ``windows-latest`` portability
    job whose ``run:`` steps cannot execute here.  Discovery has to scope itself
    to the jobs this host can run *and* record the non-host job: neither leaking
    its steps into the canonical set (the regression) nor dropping the coverage
    decision in silence.
    """

    repo = tmp_path / "repo"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "governance.yml").write_text(
        "name: Governance gates\n"
        "on: [push]\n"
        "jobs:\n"
        "  posix:\n"
        "    runs-on: macos-15\n"
        "    steps:\n"
        "      - name: install\n"
        "        run: python -m pip install --requirement tests/governance-requirements.txt\n"
        "      - name: host suites\n"
        "        run: python -m pytest -q tests/governance\n"
        "  windows-portability:\n"
        "    runs-on: windows-latest\n"
        "    steps:\n"
        "      - name: install\n"
        "        run: python -m pip install --requirement tests/governance-requirements.txt\n"
        "      - name: non-POSIX suites\n"
        "        run: python -m pytest -q tests/a13_acceptance\n",
        "utf-8",
    )
    runner = StubRunner([Rule(contains="pytest", stdout="7 passed in 1.0s")])
    ctx = context(tmp_path, repo_root=repo, runner=runner)

    discovered = gate.discover_canonical_commands(ctx)
    runnable = [item for item in discovered if item.kind == "run"]
    pytest_steps = [item for item in runnable if any("pytest" in token for token in item.argv)]

    assert len(pytest_steps) == 1, discovered
    assert "tests/governance" in pytest_steps[0].argv
    assert not any("tests/a13_acceptance" in " ".join(item.argv) for item in discovered), discovered
    # an empty argv is never a runnable canonical command
    assert all(item.argv for item in runnable), discovered
    assert any(
        item.kind == "excluded" and "windows-latest" in str(item.note) for item in discovered
    ), discovered

    result = gate.check_canonical_suites_and_audits(ctx)

    pytest_calls = [call for call in runner.calls if any("pytest" in token for token in call)]
    assert len(pytest_calls) == 1, runner.calls
    assert all("a13_acceptance" not in " ".join(item.command) for item in result.commands)
    assert "windows-latest" in str(result.detail["excluded_non_host_jobs"]), result.detail


def test_check_a_is_never_green_when_the_only_pytest_step_is_non_host(tmp_path: Path) -> None:
    """Excluding a non-host job must not manufacture a green.

    If the only canonical pytest step belongs to a job this host cannot execute,
    check ``a`` has verified nothing: it stays ``SKIPPED`` with the named reason,
    which is a ``NO-GO``.
    """

    repo = tmp_path / "repo"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "governance.yml").write_text(
        "on: [push]\n"
        "jobs:\n"
        "  windows-portability:\n"
        "    runs-on: windows-latest\n"
        "    steps:\n"
        "      - name: non-POSIX suites\n"
        "        run: python -m pytest -q tests/a13_acceptance\n",
        "utf-8",
    )
    ctx = context(tmp_path, repo_root=repo)

    result = gate.check_canonical_suites_and_audits(ctx)

    assert result.status == gate.SKIPPED
    assert any("canonical_pytest_step_not_discoverable" in reason for reason in result.reasons)
    assert result.commands == ()


def test_discovery_records_an_empty_argv_only_when_it_is_not_runnable(tmp_path: Path) -> None:
    """The real workflow: every ``run`` record carries a real argv."""

    discovered = gate.discover_canonical_commands(context(tmp_path))
    runnable = [item for item in discovered if item.kind == "run"]

    assert runnable, discovered
    assert all(item.argv for item in runnable), discovered
    assert {item.kind for item in discovered} <= {"run", "prereq", "skip", "excluded"}, discovered
    for item in discovered:
        if not item.argv:
            assert item.kind in {"prereq", "skip", "excluded"}, item


# --------------------------------------------------------------------------- #
# check b: budget invariants
# --------------------------------------------------------------------------- #


def test_check_b_surfaces_the_pinned_round_50_violation_by_name(tmp_path: Path) -> None:
    document = json.dumps(
        {
            "violations": [PINNED_CODE],
            "advertised_maximum_total_cleanup_ms": 2000,
            "route_cleanup_fanout_window_ms": 1500,
        }
    )
    runner = StubRunner([Rule(contains="check_composition", stdout=document)])
    result = gate.check_budget_invariants(context(tmp_path, runner=runner))

    assert result.status == gate.FAIL
    assert result.reasons == (f"budget_invariant_violated:{PINNED_CODE}",)
    assert result.detail["pinned_round_50_defect"] is True
    assert result.detail["advertised_maximum_total_cleanup_ms"] == 2000
    assert result.detail["route_cleanup_fanout_window_ms"] == 1500


def test_check_b_is_truthful_against_the_real_registry(tmp_path: Path) -> None:
    """Run the real ``check_composition`` and require the gate to report it."""

    result = gate.check_budget_invariants(context(tmp_path, runner=gate.subprocess_runner))
    observed = [reason.split(":", 1)[1] for reason in result.reasons if reason.startswith("budget_invariant_violated:")]

    if observed:
        assert result.status == gate.FAIL
        assert result.detail["violations"] == observed
    else:
        assert result.status == gate.PASS
        assert result.detail["violations"] == []


def test_check_b_fails_when_the_registry_module_is_absent(tmp_path: Path) -> None:
    runner = StubRunner(
        [
            Rule(
                contains="check_composition",
                exit_code=1,
                stderr="ModuleNotFoundError: No module named 'mycelium_deadlines'",
            )
        ]
    )
    result = gate.check_budget_invariants(context(tmp_path, runner=runner))
    assert result.status == gate.FAIL
    assert result.reasons == ("budget_registry_module_absent:mycelium_deadlines",)


# --------------------------------------------------------------------------- #
# check c: the soak, through its own interface
# --------------------------------------------------------------------------- #


def test_check_c_fails_with_a_named_reason_when_the_soak_module_is_absent(tmp_path: Path) -> None:
    ctx = context(tmp_path, runner=gate.subprocess_runner, soak_module="mycelium_testbed.no_such_soak")

    result = gate.check_soak(ctx)

    assert result.status == gate.FAIL
    assert result.reasons[0] == "soak_module_absent:mycelium_testbed.no_such_soak"
    assert any(reason.startswith("probe_error:") for reason in result.reasons)
    assert result.commands and result.commands[0].tail()


def test_check_c_fails_when_the_cli_has_no_seed_count_flag(tmp_path: Path) -> None:
    runner = StubRunner(
        [
            Rule(contains="importlib, inspect, json, sys", stdout=json.dumps({"interface": "cli"})),
            Rule(contains="--help", stdout="usage: soak [--events N] [--run-id ID]\n"),
        ]
    )
    result = gate.check_soak(context(tmp_path, runner=runner))

    assert result.status == gate.FAIL
    assert result.reasons[0].startswith("soak_cli_interface_incomplete:")
    assert any(reason.startswith("flags_seen:") for reason in result.reasons)


def test_check_c_fails_when_the_module_has_no_cli_and_no_sweep_entry_point(tmp_path: Path) -> None:
    runner = StubRunner(
        [Rule(contains="importlib, inspect, json, sys", stdout=json.dumps({"interface": "module"}))]
    )
    result = gate.check_soak(context(tmp_path, runner=runner))
    assert result.status == gate.FAIL
    assert result.reasons[0].startswith("soak_interface_incomplete:")
    assert result.reasons[0].endswith("no_cli_and_no_sweep_entry_point")


def _soak_rules(
    *,
    seeds_run: int,
    codes: Sequence[str],
    exit_code: int,
    replay_identical: bool = True,
    diverges: bool = True,
    gate_failures: Sequence[str] = (),
) -> list[Rule]:
    document = json.dumps(
        {
            "schema": "mycelium.testbed.soak.v1",
            "green": not codes,
            "seeds_run": seeds_run,
            "events_per_seed": 25,
            "wall_seconds": 1.5,
            "replay_check": {
                "performed": True,
                "identical": replay_identical,
                "first_digest": "sha256:first",
                "replay_digest": "sha256:replay",
                # Design-review decision 2' (2026-09-15): the digests above are recorded
                # observations; what gates is this structural list, plus the exit code.
                "gated_property": "invariants_and_terminal_coverage",
                "gate_failures": list(gate_failures),
            },
            "divergence_check": {"performed": True, "differs_from_first": diverges, "seed": 1000004},
            "seeds": [
                {"seed": index + 1, "green": not codes, "invariant_codes_fired": list(codes)}
                for index in range(seeds_run)
            ],
        }
    )
    return [
        Rule(contains="importlib, inspect, json, sys", stdout=json.dumps({"interface": "cli"})),
        Rule(contains="--help", stdout="usage: soak [--seeds N] [--events N] [--run-id ID] [--out DIR] [--json]\n"),
        Rule(contains=" -m mycelium_testbed.soak ", exit_code=exit_code, stdout=document),
    ]


def test_check_c_invokes_the_discovered_cli_with_n_seeds_and_json(tmp_path: Path) -> None:
    runner = StubRunner(_soak_rules(seeds_run=3, codes=(), exit_code=0))
    result = gate.check_soak(context(tmp_path, runner=runner))

    assert result.status == gate.PASS, result.reasons
    sweep = result.commands[-1].command
    assert "--seeds" in sweep and sweep[sweep.index("--seeds") + 1] == "3"
    assert "--events" in sweep and sweep[sweep.index("--events") + 1] == "25"
    assert "--json" in sweep
    assert "--run-id" in sweep and sweep[sweep.index("--run-id") + 1] == "wp2-5-unit-soak"
    assert "--out" in sweep and sweep[sweep.index("--out") + 1] == str(tmp_path / "runs")
    assert result.detail["seeds_run"] == 3
    assert result.detail["sweep_green"] is True


def test_check_c_reports_a_seed_violation_as_a_named_no_go(tmp_path: Path) -> None:
    runner = StubRunner(_soak_rules(seeds_run=3, codes=(SOAK_CODE,), exit_code=1))
    result = gate.check_soak(context(tmp_path, runner=runner))

    assert result.status == gate.FAIL
    assert f"soak_invariant_violation:{SOAK_CODE}" in result.reasons
    assert result.detail["invariant_codes_fired"] == [SOAK_CODE]


def test_check_c_reports_a_seed_count_mismatch(tmp_path: Path) -> None:
    runner = StubRunner(_soak_rules(seeds_run=1, codes=(), exit_code=0))
    result = gate.check_soak(context(tmp_path, runner=runner))

    assert result.status == gate.FAIL
    assert result.reasons[0] == "soak_seed_count_mismatch:requested=3,ran=1"


def test_check_c_does_not_fail_on_a_non_identical_replay_digest(tmp_path: Path) -> None:
    """Decision 2': the replay digests are recorded, never gated.

    The digest identity is an observation about a variable work budget - product-digest identity
    held 5/20 canonical runs, event-stream identity 29/30 - so failing on it would refuse the
    pilot ~15 times in 20 for a non-defect reason.  This test pinned the superseded rule until
    2026-09-15, so it now pins the correction.
    """

    runner = StubRunner(
        _soak_rules(seeds_run=3, codes=(), exit_code=0, replay_identical=False)
    )
    result = gate.check_soak(context(tmp_path, runner=runner))

    assert result.status == gate.PASS, result.reasons
    assert not any(reason.startswith("soak_replay_not_identical") for reason in result.reasons)


def test_check_c_fails_on_the_soaks_structural_gate_failures(tmp_path: Path) -> None:
    """What gates is structural: the sweep's own gate_failures list, named by the gate."""

    runner = StubRunner(
        _soak_rules(
            seeds_run=3,
            codes=(),
            exit_code=0,
            gate_failures=("invariant_violated:at_most_one_native_execution_per_command:x:in_seed-1",),
        )
    )
    result = gate.check_soak(context(tmp_path, runner=runner))

    assert result.status == gate.FAIL
    assert any(
        reason.startswith("soak_gate_failures:") and "at_most_one_native_execution" in reason
        for reason in result.reasons
    )


def test_check_c_surfaces_seeds_that_do_not_diverge(tmp_path: Path) -> None:
    runner = StubRunner(_soak_rules(seeds_run=3, codes=(), exit_code=0, diverges=False))
    result = gate.check_soak(context(tmp_path, runner=runner))

    assert result.status == gate.FAIL
    assert any(reason.startswith("soak_seeds_do_not_diverge:") for reason in result.reasons)


# --------------------------------------------------------------------------- #
# check d: the emulated chain
# --------------------------------------------------------------------------- #


def test_check_d_fails_when_the_emulation_suite_is_absent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "tests" / "testbed").mkdir(parents=True)
    result = gate.check_emulated_terminal(context(tmp_path, repo_root=repo))

    assert result.status == gate.FAIL
    assert result.reasons == (f"emulation_suite_missing:{gate.EMULATION_GLOB}",)
    assert result.commands == ()


def _emulation_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    target = repo / "tests" / "testbed"
    target.mkdir(parents=True)
    (target / "test_emulation_terminal.py").write_text(
        "def test_terminal():\n    assert True\n", "utf-8"
    )
    return repo


def test_check_d_fails_on_a_failing_suite(tmp_path: Path) -> None:
    repo = _emulation_repo(tmp_path)
    runner = StubRunner([Rule(contains="test_emulation_terminal.py", exit_code=1, stdout="2 failed, 1 passed")])
    result = gate.check_emulated_terminal(context(tmp_path, repo_root=repo, runner=runner))

    assert result.status == gate.FAIL
    assert "emulation_suite_failed_tests:2" in result.reasons
    assert result.detail["failed"] == 2


def test_check_d_fails_on_a_vacuous_suite(tmp_path: Path) -> None:
    repo = _emulation_repo(tmp_path)
    runner = StubRunner([Rule(contains="test_emulation_terminal.py", exit_code=0, stdout="no tests ran")])
    result = gate.check_emulated_terminal(context(tmp_path, repo_root=repo, runner=runner))

    assert result.status == gate.FAIL
    assert result.reasons[0].startswith("emulation_suite_vacuous:")


def test_check_d_passes_only_on_a_real_passing_terminal_test(tmp_path: Path) -> None:
    repo = _emulation_repo(tmp_path)
    runner = StubRunner(
        [Rule(contains="test_emulation_terminal.py", exit_code=0, stdout="1 passed in 0.10s")]
    )
    result = gate.check_emulated_terminal(context(tmp_path, repo_root=repo, runner=runner))

    assert result.status == gate.PASS
    assert result.detail["passed"] == 1
    assert result.detail["files"] == ["tests/testbed/test_emulation_terminal.py"]


# --------------------------------------------------------------------------- #
# report shape, evidence and exit codes
# --------------------------------------------------------------------------- #


def test_json_report_schema_and_human_render(tmp_path: Path) -> None:
    ctx = context(tmp_path)
    report = gate.run_gate(ctx, checks=all_pass_checks())

    for key in (
        "protocol",
        "verdict",
        "run_id",
        "repo_root",
        "runs_root",
        "run_dir",
        "seeds",
        "events",
        "checks",
        "failed_checks",
        "skipped_checks",
        "reasons",
        "evidence_paths",
        "duration_seconds",
    ):
        assert key in report, key
    assert report["protocol"] == gate.PROTOCOL

    for check in report["checks"]:
        for key in ("id", "title", "status", "reasons", "commands", "detail"):
            assert key in check, key
        assert check["status"] == gate.PASS

    json.dumps(report)  # machine consumption must not raise
    human = gate.render_report(report)
    assert human.count(" ... PASS") == 6
    assert "verdict: GO" in human

    paths = gate.write_evidence(ctx, report)
    assert len(paths) == 2
    assert all(Path(item).is_file() for item in paths)
    assert report["evidence_paths"] == paths
    written = json.loads(Path(paths[0]).read_text("utf-8"))
    assert written["verdict"] == "GO"
    assert "verdict: GO" in Path(paths[1]).read_text("utf-8")


def test_command_record_is_bounded_and_names_the_exit_code() -> None:
    long_output = "x" * (gate.TAIL_CHARS * 2)
    result = canned(("false",), exit_code=1, stdout=long_output, stderr="boom")
    assert len(result.tail()) < len(long_output)
    assert result.tail().startswith("[...truncated...]")
    record = result.record()
    assert record["exit_code"] == 1
    assert record["cmd"] == "false"
    assert record["tail"].endswith("boom")
    assert result.status() == gate.FAIL
    assert canned(("true",)).status() == gate.PASS
    assert canned(("slow",), timed_out=True).status() == gate.FAIL


def test_run_id_is_required_and_the_gate_exits_non_zero_on_no_go(tmp_path: Path) -> None:
    parser = gate.build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args([])
    assert excinfo.value.code == 2

    checks = all_pass_checks()
    checks[5] = stub_check("f.hypothesis_pre_registration", gate.FAIL, ("hypothesis_file_missing:x",))
    exit_code = gate.main(
        ["--run-id", "wp2-5-unit", "--runs-root", str(tmp_path / "runs"), "--seeds", "1"],
        checks=checks,
    )
    assert exit_code == 1
    assert (tmp_path / "runs" / "wp2-5-unit" / "readiness-gate.json").is_file()
    assert not (tmp_path / "runs" / "wp2-5-unit" / "HYPOTHESIS.md").exists()
