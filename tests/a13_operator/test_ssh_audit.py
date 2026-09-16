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
    out = subprocess.run(['ssh', 'mycelium-laptop', 'true'],
                         env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    # It behaved as the real binary: /bin/echo printed its arguments.
    assert 'mycelium-laptop' in out.stdout
    recorded = ssh_audit.calls(log)
    assert len(recorded) == 1
    assert 'mycelium-laptop true' in recorded[0]
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
