"""Finite local subprocess checks, not physical/benchmark evidence."""
import json
import subprocess
import pytest
from tests.a5_acceptance.test_benchmark_supervisor import _fixture, _command, _environment, _sha
from pathlib import Path
import sys
import time


def test_total_lifetime_terminates_owned_child_and_seals(tmp_path):
    inputs = _fixture(tmp_path, "import time\nwhile True: time.sleep(0.02)\n")
    manifest, token, child, output, failure, receipt = inputs
    command, _ = _command(*inputs, _sha(manifest.read_bytes()))
    command[2:2] = ['--maximum-lifetime-seconds', '0.5']
    result = subprocess.run(command, env=_environment(token), capture_output=True, text=True, timeout=10)
    assert receipt.exists(), result.stderr
    document = json.loads(receipt.read_text())
    assert document['reason_code'] == 'maximum_lifetime_exceeded'
    assert document['terminal_valid'] is False
    assert document['child_started'] is True
    assert document['child_returncode'] is not None
    assert document['maximum_lifetime_seconds'] == 0.5
    assert document['cleanup_blocked'] is False
    assert result.returncode != 0


@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf', '86401'])
def test_rejects_invalid_lifetime_before_launch(tmp_path, value):
    marker = tmp_path / 'started'
    inputs = _fixture(tmp_path, f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    command, _ = _command(*inputs, _sha(inputs[0].read_bytes()))
    command[2:2] = ['--maximum-lifetime-seconds', value]
    result = subprocess.run(command, env=_environment(inputs[1]), capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert not marker.exists()
    assert json.loads(inputs[-1].read_text())['reason_code'] == 'maximum_lifetime_invalid'


@pytest.mark.parametrize('lose_identity', [False, True])
def test_escalation_requires_original_identity(tmp_path, lose_identity):
    # Finite test child ignores TERM. In identity-loss case it exits naturally;
    # no external guessed-PID cleanup is needed or permitted.
    ended = tmp_path / 'ended'
    inputs = _fixture(tmp_path, f"import signal,time\nfrom pathlib import Path\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(2)\nPath({str(ended)!r}).touch()\n")
    command, _ = _command(*inputs, _sha(inputs[0].read_bytes()))
    command[2:2] = ['--maximum-lifetime-seconds', '0.3']
    wrapper = tmp_path / 'wrapper.py'
    wrapper.write_text(f'''import importlib.util,sys,os
spec=importlib.util.spec_from_file_location('supervisor', {command[1]!r})
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.TERMINATION_GRACE_SECONDS=0.1
original=m._process_identity
calls={{}}
def identity(pid):
    result=original(pid)
    calls[pid]=calls.get(pid,0)+1
    if {lose_identity!r} and pid!=os.getpid() and calls[pid]>1:
        result['start_identity']='changed-identity'
    return result
m._process_identity=identity
sys.argv={command[1:]!r}
raise SystemExit(m.main())
''')
    result = subprocess.run([sys.executable, str(wrapper)], env=_environment(inputs[1]), capture_output=True, text=True, timeout=5)
    assert result.returncode == 125, result.stderr
    doc = json.loads(inputs[-1].read_text())
    assert doc['cleanup_blocked'] is lose_identity
    assert doc['child_termination_escalated_to_sigkill'] is (not lose_identity)
    assert doc['child_sigkill_identity_revalidated'] is (not lose_identity)
    if lose_identity:
        assert doc['child_returncode'] is None
        deadline = time.monotonic()+3
        while not ended.exists() and time.monotonic()<deadline:
            time.sleep(0.02)
        assert ended.exists()


def test_new_claim_roundtrips_through_recovery(tmp_path):
    import importlib.util
    from tests.a5_acceptance.test_interrupted_attempt_recovery import _case, _artifact_input, _write_json, _sha_bytes
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('recovery', root/'scripts/reconcile_a5_interrupted_attempt.py')
    assert spec is not None and spec.loader is not None
    recovery = importlib.util.module_from_spec(spec); spec.loader.exec_module(recovery)
    _case(tmp_path, with_start=True)
    claim_path = tmp_path/'attempt-claim.json'
    started_path = tmp_path/'attempt-started.json'
    claim = json.loads(claim_path.read_text())
    claim['protocol'] = 'mycelium.a5_benchmark_attempt_claim.v2'
    claim['attempt_binding']['maximum_lifetime_seconds'] = 300.0
    claim['attempt_id'] = _sha_bytes(json.dumps(claim['attempt_binding'], sort_keys=True, separators=(',', ':')).encode())
    _write_json(claim_path, claim)
    started = json.loads(started_path.read_text())
    started['attempt_id'] = claim['attempt_id']
    started['attempt_claim_sha256'] = _artifact_input(claim_path)['sha256']
    _write_json(started_path, started)
    authorization = json.loads((tmp_path/'authorization.json').read_text())
    candidate = {k: authorization[k] for k in ('cycle_id', 'candidate_commit', 'candidate_tree')}
    candidate['source_manifest_sha256'] = authorization['source_manifest']['sha256']
    result = recovery._validate_start_observation({'claim': _artifact_input(claim_path), 'started': _artifact_input(started_path)}, candidate=candidate, authorization_sha256=_artifact_input(tmp_path/'authorization.json')['sha256'])
    assert result['status'] == 'source_bound_observed'
