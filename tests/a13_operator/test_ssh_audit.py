# SPDX-License-Identifier: AGPL-3.0-only
"""The no-SSH audit must actually count ssh, and must not silently pass.

Criterion 3 of PLAN.md section 3 requires an acceptance run with no SSH. An unmeasured
"we didn't ssh" claim is exactly the kind of unfalsifiable statement that cost this
project hours (64r1's `returncode: null`), so the counting mechanism is pinned here:
the shim must record the call AND still execute the real binary, and an absent log must
read as zero calls rather than as an error.
"""
import importlib.util
import os
import pathlib
import subprocess

POLICY = pathlib.Path(__file__).resolve().parents[2] / 'scripts/a13_operator/ssh_audit.py'
spec = importlib.util.spec_from_file_location('ssh_audit', POLICY)
ssh_audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ssh_audit)


def _shimmed(tmp_path, real_binary='/bin/echo'):
    shim_dir = tmp_path / 'shim'
    log = tmp_path / 'ssh-calls.log'
    env_overrides = ssh_audit.install(shim_dir=shim_dir, log=log)
    env = dict(os.environ, **env_overrides)
    env[ssh_audit.REAL_SSH_ENV] = real_binary
    return env, log, shim_dir


def test_an_absent_log_reads_as_zero_calls_not_an_error(tmp_path):
    missing = tmp_path / 'never-written.log'
    assert ssh_audit.calls(missing) == []
    assert ssh_audit.report(missing)['verdict'] == 'NO_SSH'
    assert ssh_audit.report(missing)['ssh_calls'] == 0


def test_the_shim_records_the_call_and_still_runs_the_real_binary(tmp_path):
    env, log, shim_dir = _shimmed(tmp_path)
    out = subprocess.run(['ssh', 'member-mac', 'true'],
                         env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    # It behaved as the real binary: /bin/echo printed its arguments.
    assert 'member-mac' in out.stdout
    recorded = ssh_audit.calls(log)
    assert len(recorded) == 1
    assert 'member-mac true' in recorded[0]
    assert ssh_audit.report(log)['verdict'] == 'SSH_USED'


def test_calls_are_counted_in_order(tmp_path):
    env, log, shim_dir = _shimmed(tmp_path)
    for host in ('first-host', 'second-host', 'third-host'):
        subprocess.run(['ssh', host, 'true'], env=env, capture_output=True, text=True)
    recorded = ssh_audit.calls(log)
    assert len(recorded) == 3
    assert 'first-host' in recorded[0]
    assert 'third-host' in recorded[2]
    assert ssh_audit.report(log)['ssh_calls'] == 3


def test_reset_clears_a_previous_run(tmp_path):
    env, log, shim_dir = _shimmed(tmp_path)
    subprocess.run(['ssh', 'someone', 'true'], env=env, capture_output=True, text=True)
    assert ssh_audit.report(log)['ssh_calls'] == 1
    ssh_audit.reset(log)
    assert ssh_audit.report(log)['verdict'] == 'NO_SSH'


def test_the_shim_is_executable_and_first_on_path(tmp_path):
    env, log, shim_dir = _shimmed(tmp_path)
    shim = shim_dir / 'ssh'
    assert shim.exists()
    assert os.access(shim, os.X_OK)
    assert env['PATH'].split(os.pathsep)[0] == str(shim_dir)


def test_a_multiline_command_counts_as_one_invocation(tmp_path):
    """Measured 2026-09-16: the sweep's 2 ssh calls logged as 10 lines and read as 10 calls."""
    env, log, shim_dir = _shimmed(tmp_path)
    subprocess.run(['ssh', 'some-host', 'line one\nline two\nline three'],
                   env=env, capture_output=True, text=True)
    assert ssh_audit.report(log)['ssh_calls'] == 1
    # And the log itself is one physical line per invocation.
    assert len(ssh_audit.calls(log)) == 1


def test_an_already_written_inflated_log_still_reports_the_true_count(tmp_path):
    """A log collected before the fix must still be readable honestly, without a re-run."""
    log = tmp_path / 'old.log'
    log.write_text(
        '2026-09-16T23:21:00Z -T -o BatchMode=yes member-mac /bin/sh -c "ps -Ao pid=,args="\n'
        'from pathlib import Path\n'
        'p=Path(sys.argv[1])\n'
        "printf('%s')\n"
        '2026-09-16T23:21:01Z -T member-mac /usr/bin/python3 -c \'import json\'\n'
        'more continuation\n'
    )
    # The point is the RELATION, not a hand-counted fixture total: the old log carries more
    # physical lines than invocations, and the report must see through that.
    assert len(ssh_audit.calls(log)) > ssh_audit.report(log)['ssh_calls']
    assert ssh_audit.report(log)['ssh_calls'] == 2       # the truthful invocation count
    assert ssh_audit.report(log)['verdict'] == 'SSH_USED'
    # Every counted entry is a real invocation line, not a continuation.
    for entry in ssh_audit.invocations(log):
        assert ssh_audit._is_invocation(entry)


def test_an_empty_log_is_no_ssh_but_a_whitespace_log_is_not(tmp_path):
    log = tmp_path / 'ws.log'
    log.write_text('\n   \n')
    assert ssh_audit.report(log)['verdict'] == 'NO_SSH'
    log.write_text('\n2026-09-16T23:21:00Z -T host true\n')
    assert ssh_audit.report(log)['verdict'] == 'SSH_USED'
