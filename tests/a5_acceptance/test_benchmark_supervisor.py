from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import time

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR = REPO_ROOT / "scripts/run_a5_benchmark_supervisor.py"
TREE = "1" * 40


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _fixture(tmp_path: Path, child_source: str):
    manifest = tmp_path / "source-manifest.v1.json"
    manifest.write_bytes(b'{"protocol":"test.source_manifest.v1"}\n')
    token = "t" * 44
    child = tmp_path / "child.py"
    child.write_text(child_source, encoding="utf-8")
    output = tmp_path / "benchmark.v1.json"
    failure = tmp_path / "benchmark.failure.v1.json"
    receipt = tmp_path / "benchmark.supervisor-receipt.v1.json"
    return manifest, token, child, output, failure, receipt


def _command(
    manifest,
    token,
    child,
    output,
    failure,
    receipt,
    expected,
    *,
    detach: bool = False,
    cycle_id: str | None = None,
    authorization_digest: str | None = None,
):
    child_argv = [
        sys.executable,
        str(child),
        "--source-manifest",
        str(manifest),
        "--source-manifest-digest",
        expected,
        "--output",
        str(output),
        "--failure-output",
        str(failure),
    ]
    command = [
        sys.executable,
        str(SUPERVISOR),
        "--candidate-tree",
        TREE,
        "--source-manifest",
        str(manifest),
        "--expected-source-manifest-digest",
        expected,
        "--receipt",
        str(receipt),
        "--output",
        str(output),
        "--failure-output",
        str(failure),
    ]
    if (cycle_id is None) != (authorization_digest is None):
        raise ValueError("cycle and authorization binding must be supplied together")
    if cycle_id is not None:
        command[4:4] = [
            "--cycle-id",
            cycle_id,
            "--authorization-digest",
            authorization_digest,
        ]
    if detach:
        command.append("--detach")
    command.extend(["--", *child_argv])
    return command, child_argv


def _environment(token: str) -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        environment.pop(name, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["MYCELIUM_A5_OPERATOR_TOKEN"] = token
    return environment


def _wait_for_file(path: Path, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists()


def test_supervisor_hashes_manifest_and_writes_success_receipt(tmp_path) -> None:
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path,
        """import argparse, json, os
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--source-manifest'); p.add_argument('--source-manifest-digest'); p.add_argument('--output'); p.add_argument('--failure-output'); a=p.parse_args()
assert len(os.environ['MYCELIUM_A5_OPERATOR_TOKEN']) >= 32
Path(a.output).write_text(json.dumps({'protocol':'test.success.v1'})+'\\n')
""",
    )
    expected = _sha(manifest.read_bytes())
    command, child_argv = _command(
        manifest, token, child, output, failure, receipt, expected
    )

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        env=_environment(token),
    )

    assert result.returncode == 0
    document = json.loads(receipt.read_text("utf-8"))
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert document["protocol"] == "mycelium.a5_benchmark_supervisor_receipt.v1"
    assert document["candidate_tree"] == TREE
    assert document["source_manifest_digest"] == expected
    assert document["child_argv_digest"] == _sha(
        json.dumps(child_argv, sort_keys=True, separators=(",", ":")).encode()
    )
    assert document["child_started"] is True
    assert document["child_returncode"] == 0
    assert document["success_artifact"]["exists"] is True
    assert document["failure_artifact"]["exists"] is False
    assert document["terminal_valid"] is True
    assert document["reason_code"] == "success_artifact_present"


def test_supervisor_rejects_manifest_digest_mismatch_before_child(tmp_path) -> None:
    marker = tmp_path / "child-started"
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path,
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('started')\n",
    )
    wrong = "sha256:" + "0" * 64
    command, _ = _command(manifest, token, child, output, failure, receipt, wrong)

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        env=_environment(token),
    )

    assert result.returncode == 2
    assert not marker.exists()
    document = json.loads(receipt.read_text("utf-8"))
    assert document["child_started"] is False
    assert document["terminal_valid"] is False
    assert document["reason_code"] == "source_manifest_digest_mismatch"
    assert document["source_manifest_digest"] == _sha(manifest.read_bytes())


def test_supervisor_receipt_exists_when_child_has_no_terminal_artifact(tmp_path) -> None:
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path,
        "import sys\nraise SystemExit(9)\n",
    )
    expected = _sha(manifest.read_bytes())
    command, _ = _command(manifest, token, child, output, failure, receipt, expected)

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        env=_environment(token),
    )

    assert result.returncode == 9
    document = json.loads(receipt.read_text("utf-8"))
    assert document["child_started"] is True
    assert document["child_returncode"] == 9
    assert document["success_artifact"]["exists"] is False
    assert document["failure_artifact"]["exists"] is False
    assert document["terminal_valid"] is False
    assert document["reason_code"] == "child_terminal_artifact_missing"


def test_supervisor_sigterm_relays_to_child_and_seals_failure_receipt(tmp_path) -> None:
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path,
        """import argparse, json, os, signal, time
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--source-manifest'); p.add_argument('--source-manifest-digest'); p.add_argument('--output'); p.add_argument('--failure-output'); a=p.parse_args()
started=Path(a.failure_output + '.started'); started.write_text('ready')
def terminate(signum, _frame):
    failure=Path(a.failure_output)
    descriptor=os.open(failure, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump({'protocol':'test.failure.v1','reason_code':'external_sigterm'}, stream)
        stream.write('\\n'); stream.flush(); os.fsync(stream.fileno())
    raise SystemExit(128 + signum)
signal.signal(signal.SIGTERM, terminate)
while True: time.sleep(0.05)
""",
    )
    expected = _sha(manifest.read_bytes())
    command, _ = _command(manifest, token, child, output, failure, receipt, expected)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_environment(token),
    )
    started = Path(str(failure) + ".started")
    deadline = time.monotonic() + 10
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert started.exists()

    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 143, (stdout, stderr)
    document = json.loads(receipt.read_text("utf-8"))
    assert document["supervisor_termination_signal"] == "SIGTERM"
    assert document["child_returncode"] == 143
    assert document["success_artifact"]["exists"] is False
    assert document["failure_artifact"]["exists"] is True
    assert document["terminal_valid"] is True
    assert document["reason_code"] == "failure_artifact_present"


def test_supervisor_rejects_missing_environment_token_before_child(tmp_path) -> None:
    marker = tmp_path / "child-started"
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path,
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('started')\n",
    )
    expected = _sha(manifest.read_bytes())
    command, _ = _command(manifest, token, child, output, failure, receipt, expected)
    environment = os.environ.copy()
    environment.pop("MYCELIUM_A5_OPERATOR_TOKEN", None)

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )

    assert result.returncode == 2
    assert not marker.exists()
    document = json.loads(receipt.read_text("utf-8"))
    assert document["child_started"] is False
    assert document["terminal_valid"] is False
    assert document["reason_code"] == "operator_token_environment_invalid"


def test_supervisor_rejects_preexisting_receipt_before_child(tmp_path) -> None:
    marker = tmp_path / "child-started"
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path,
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('started')\n",
    )
    original = b'{"preserved":true}\n'
    receipt.write_bytes(original)
    expected = _sha(manifest.read_bytes())
    command, _ = _command(manifest, token, child, output, failure, receipt, expected)

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        env=_environment(token),
    )

    assert result.returncode == 2
    assert not marker.exists()
    assert receipt.read_bytes() == original
    assert json.loads(result.stdout) == {
        "reason_code": "terminal_receipt_exists",
        "terminal_valid": False,
    }


@pytest.mark.parametrize("existing_name", ["output", "failure"])
def test_supervisor_rejects_preexisting_terminal_artifact_before_child(
    tmp_path, existing_name: str
) -> None:
    marker = tmp_path / "child-started"
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path,
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('started')\n",
    )
    existing = output if existing_name == "output" else failure
    existing.write_text("preserve\n", encoding="utf-8")
    expected = _sha(manifest.read_bytes())
    command, _ = _command(manifest, token, child, output, failure, receipt, expected)

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        env=_environment(token),
    )

    assert result.returncode == 2
    assert not marker.exists()
    assert existing.read_text("utf-8") == "preserve\n"
    document = json.loads(receipt.read_text("utf-8"))
    assert document["child_started"] is False
    assert document["reason_code"] == "terminal_artifact_preexists"


@pytest.mark.parametrize("existing_name", ["receipt", "output", "failure"])
def test_supervisor_rejects_broken_symlink_artifact_name_before_child(
    tmp_path, existing_name: str
) -> None:
    marker = tmp_path / "child-started"
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path,
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('started')\n",
    )
    paths = {"receipt": receipt, "output": output, "failure": failure}
    existing = paths[existing_name]
    existing.symlink_to(tmp_path / f"missing-{existing_name}")
    expected = _sha(manifest.read_bytes())
    command, _ = _command(manifest, token, child, output, failure, receipt, expected)

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        env=_environment(token),
    )

    assert result.returncode == 2
    assert not marker.exists()
    assert existing.is_symlink()
    if existing_name == "receipt":
        assert json.loads(result.stdout)["reason_code"] == "terminal_receipt_exists"
    else:
        document = json.loads(receipt.read_text("utf-8"))
        assert document["child_started"] is False
        assert document["reason_code"] == "terminal_artifact_preexists"


def test_supervisor_seals_normal_child_failure_without_restart(tmp_path) -> None:
    run_count = tmp_path / "run-count"
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path,
        f"""import argparse, json, os
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--source-manifest'); p.add_argument('--source-manifest-digest'); p.add_argument('--output'); p.add_argument('--failure-output'); a=p.parse_args()
count=Path({str(run_count)!r}); count.write_text((count.read_text() if count.exists() else '') + 'run\\n')
descriptor=os.open(a.failure_output, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
with os.fdopen(descriptor, 'w') as stream:
    json.dump({{'protocol':'test.failure.v1','reason_code':'controlled_failure'}}, stream)
    stream.write('\\n'); stream.flush(); os.fsync(stream.fileno())
raise SystemExit(7)
""",
    )
    expected = _sha(manifest.read_bytes())
    command, _ = _command(manifest, token, child, output, failure, receipt, expected)

    first = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        env=_environment(token),
    )
    second = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        env=_environment(token),
    )

    assert first.returncode == 7
    assert second.returncode == 2
    assert run_count.read_text("utf-8") == "run\n"
    document = json.loads(receipt.read_text("utf-8"))
    assert document["child_returncode"] == 7
    assert document["failure_artifact"]["exists"] is True
    assert document["terminal_valid"] is True
    assert document["reason_code"] == "failure_artifact_present"


def test_supervisor_records_exact_process_identity_and_persists_no_token(
    tmp_path,
) -> None:
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path,
        """import argparse, json, os, time
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--source-manifest'); p.add_argument('--source-manifest-digest'); p.add_argument('--output'); p.add_argument('--failure-output'); a=p.parse_args()
report={'protocol':'test.success.v1','operator_capability_present':len(os.environ['MYCELIUM_A5_OPERATOR_TOKEN']) >= 32,'python_environment_scrubbed':all(name not in os.environ for name in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV')) and os.environ.get('PYTHONDONTWRITEBYTECODE') == '1'}
time.sleep(0.2)
Path(a.output).write_text(json.dumps(report)+'\\n')
""",
    )
    test_token = "P01_LOCAL_TEST_CAPABILITY_" + "x" * 32
    expected = _sha(manifest.read_bytes())
    command, _ = _command(
        manifest, test_token, child, output, failure, receipt, expected
    )

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        env=_environment(test_token),
    )

    assert result.returncode == 0
    document = json.loads(receipt.read_text("utf-8"))
    assert document["supervisor_identity"]["pid"] > 1
    assert document["supervisor_identity"]["start_identity"]
    assert document["child_identity"]["pid"] == document["child_pid"]
    assert document["child_identity"]["start_identity"]
    child_report = json.loads(output.read_text("utf-8"))
    assert child_report["operator_capability_present"] is True
    assert child_report["python_environment_scrubbed"] is True
    persisted = receipt.read_text("utf-8") + output.read_text("utf-8")
    persisted += result.stdout + result.stderr
    assert test_token not in persisted


def test_detached_supervisor_survives_launcher_exit_and_seals_receipt(
    tmp_path,
) -> None:
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path,
        """import argparse, json, time
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--source-manifest'); p.add_argument('--source-manifest-digest'); p.add_argument('--output'); p.add_argument('--failure-output'); a=p.parse_args()
time.sleep(0.4)
Path(a.output).write_text(json.dumps({'protocol':'test.success.v1'})+'\\n')
""",
    )
    expected = _sha(manifest.read_bytes())
    command, _ = _command(
        manifest, token, child, output, failure, receipt, expected, detach=True
    )
    launcher = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_environment(token),
    )
    launcher_pid = launcher.pid

    stdout, stderr = launcher.communicate(timeout=10)
    assert launcher.returncode == 0, (stdout, stderr)
    launch = json.loads(stdout)
    assert launch["detached"] is True
    assert launch["supervisor_pid"] != launcher_pid
    assert launch["supervisor_parent_pid"] == 1
    _wait_for_file(receipt)

    document = json.loads(receipt.read_text("utf-8"))
    assert document["detached"] is True
    assert document["supervisor_identity"]["pid"] == launch["supervisor_pid"]
    assert document["supervisor_identity"]["start_identity"] == launch[
        "supervisor_start_identity"
    ]
    assert document["terminal_valid"] is True
    assert document["reason_code"] == "success_artifact_present"


def _attempt_state_paths(receipt: Path) -> tuple[Path, Path]:
    return (
        receipt.with_name(receipt.name + ".attempt-claim.v1.json"),
        receipt.with_name(receipt.name + ".attempt-started.v1.json"),
    )


def _detach_harness(
    tmp_path: Path,
    command: list[str],
    *,
    startup_delay_seconds: float = 0.0,
    fail_ack: bool = False,
    ack_timeout_seconds: float = 0.05,
) -> list[str]:
    harness = tmp_path / (
        f"detach-harness-{startup_delay_seconds}-{int(fail_ack)}.py"
    )
    harness.write_text(
        f"""import importlib.util, sys, time
spec=importlib.util.spec_from_file_location('p01r_supervisor', {str(SUPERVISOR)!r})
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
module.DETACH_ACK_TIMEOUT_SECONDS={ack_timeout_seconds!r}
module._before_detached_startup_ack=lambda: time.sleep({startup_delay_seconds!r})
if {fail_ack!r}:
    module._send_detached_startup_ack=lambda _fd, _launch: (_ for _ in ()).throw(OSError('controlled_ack_failure'))
sys.argv={[str(SUPERVISOR), *command[2:]]!r}
raise SystemExit(module.main())
""",
        encoding="utf-8",
    )
    return [sys.executable, str(harness)]


def test_identical_concurrent_attempt_paths_launch_exactly_one_child(tmp_path) -> None:
    starts = tmp_path / "starts.txt"
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path,
        f"""import argparse, json, os, time
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--source-manifest'); p.add_argument('--source-manifest-digest'); p.add_argument('--output'); p.add_argument('--failure-output'); a=p.parse_args()
starts=Path({str(starts)!r})
fd=os.open(starts, os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o600)
os.write(fd, b'started\\n'); os.fsync(fd); os.close(fd)
deadline=time.monotonic()+1.0
while time.monotonic()<deadline and starts.read_text().count('started')<2: time.sleep(0.01)
time.sleep(0.1)
fd=os.open(a.output, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
os.write(fd, (json.dumps({{'protocol':'test.success.v1'}})+'\\n').encode()); os.fsync(fd); os.close(fd)
""",
    )
    expected = _sha(manifest.read_bytes())
    command, _ = _command(manifest, token, child, output, failure, receipt, expected)
    contenders = [
        subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_environment(token),
        )
        for _ in range(2)
    ]

    results = []
    for process in contenders:
        stdout, stderr = process.communicate(timeout=10)
        results.append((process.returncode, stdout, stderr))

    claim, started = _attempt_state_paths(receipt)
    assert starts.read_text("utf-8") == "started\n"
    assert sorted(result[0] for result in results) == [0, 2]
    rejected = next(result for result in results if result[0] == 2)
    assert json.loads(rejected[1]) == {
        "reason_code": "attempt_already_claimed",
        "terminal_valid": False,
    }
    assert claim.exists()
    assert started.exists()
    assert json.loads(receipt.read_text("utf-8"))["terminal_valid"] is True


def test_owner_death_after_child_start_consumes_attempt_without_replay(tmp_path) -> None:
    starts = tmp_path / "starts.txt"
    marker = tmp_path / "child-started"
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path,
        f"""import argparse, os, time
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--source-manifest'); p.add_argument('--source-manifest-digest'); p.add_argument('--output'); p.add_argument('--failure-output'); p.parse_args()
starts=Path({str(starts)!r}); fd=os.open(starts, os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o600); os.write(fd,b'started\\n'); os.fsync(fd); os.close(fd)
Path({str(marker)!r}).write_text('started')
time.sleep(0.4)
raise SystemExit(9)
""",
    )
    expected = _sha(manifest.read_bytes())
    command, _ = _command(manifest, token, child, output, failure, receipt, expected)
    owner = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_environment(token),
    )
    _wait_for_file(marker)
    owner.kill()
    owner.communicate(timeout=5)
    time.sleep(0.6)

    restarted = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=10,
        env=_environment(token),
    )

    claim, started = _attempt_state_paths(receipt)
    assert restarted.returncode == 2
    assert json.loads(restarted.stdout) == {
        "reason_code": "attempt_already_claimed",
        "terminal_valid": False,
    }
    assert starts.read_text("utf-8") == "started\n"
    assert claim.exists()
    assert started.exists()
    assert not receipt.exists()


def test_detached_ack_timeout_never_seals_false_no_child_terminal(tmp_path) -> None:
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path,
        """import argparse, json, time
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--source-manifest'); p.add_argument('--source-manifest-digest'); p.add_argument('--output'); p.add_argument('--failure-output'); a=p.parse_args()
time.sleep(0.1)
Path(a.output).write_text(json.dumps({'protocol':'test.success.v1'})+'\\n')
""",
    )
    expected = _sha(manifest.read_bytes())
    command, _ = _command(
        manifest, token, child, output, failure, receipt, expected, detach=True
    )
    harness_command = _detach_harness(
        tmp_path, command, startup_delay_seconds=0.25, ack_timeout_seconds=0.05
    )

    launcher = subprocess.run(
        harness_command,
        capture_output=True,
        text=True,
        timeout=5,
        env=_environment(token),
    )

    assert launcher.returncode == 125
    assert json.loads(launcher.stdout) == {
        "reason_code": "detached_supervisor_start_timeout",
        "terminal_valid": False,
    }
    _wait_for_file(receipt)
    document = json.loads(receipt.read_text("utf-8"))
    assert document["child_started"] is True
    assert document["terminal_valid"] is True
    assert document["reason_code"] == "success_artifact_present"


def test_detached_failed_ack_does_not_abort_owned_descendant(tmp_path) -> None:
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path,
        """import argparse, json
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--source-manifest'); p.add_argument('--source-manifest-digest'); p.add_argument('--output'); p.add_argument('--failure-output'); a=p.parse_args()
Path(a.output).write_text(json.dumps({'protocol':'test.success.v1'})+'\\n')
""",
    )
    expected = _sha(manifest.read_bytes())
    command, _ = _command(
        manifest, token, child, output, failure, receipt, expected, detach=True
    )
    harness_command = _detach_harness(tmp_path, command, fail_ack=True)

    launcher = subprocess.run(
        harness_command,
        capture_output=True,
        text=True,
        timeout=5,
        env=_environment(token),
    )

    assert launcher.returncode == 125
    assert json.loads(launcher.stdout) == {
        "reason_code": "detached_supervisor_start_failed",
        "terminal_valid": False,
    }
    _wait_for_file(receipt)
    assert json.loads(receipt.read_text("utf-8"))["terminal_valid"] is True


def _interrupt_at_detach_phase(tmp_path: Path, phase: str):
    """Real process barrier: claim-before-fork or detached-before-ack."""
    starts = tmp_path / "starts.txt"
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path,
        f"""import argparse, json, os
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--source-manifest'); p.add_argument('--source-manifest-digest'); p.add_argument('--output'); p.add_argument('--failure-output'); a=p.parse_args()
fd=os.open({str(starts)!r}, os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o600)
os.write(fd,b'started\\n'); os.close(fd)
Path(a.output).write_text(json.dumps({{'protocol':'test.success.v1'}})+'\\n')
""",
    )
    command, child_argv = _command(
        manifest, token, child, output, failure, receipt,
        _sha(manifest.read_bytes()), detach=True,
    )
    controller, endpoint = socket.socketpair()
    controller.settimeout(5)
    harness = tmp_path / "phase-harness.py"
    harness.write_text(
        f"""import importlib.util, json, os, socket, sys
spec=importlib.util.spec_from_file_location('phase_supervisor', {str(SUPERVISOR)!r})
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
channel=socket.socket(fileno={endpoint.fileno()}); channel.settimeout(5)
def barrier():
    identity=module._process_identity(os.getpid())
    channel.sendall((json.dumps(identity)+'\\n').encode())
    if channel.recv(1)!=b'G': raise RuntimeError('barrier_not_released')
    channel.close()
original_detach=module._detach_from_launcher
def before_fork():
    barrier()
    return original_detach()
if {phase!r}=='before_fork': module._detach_from_launcher=before_fork
else: module._before_detached_startup_ack=barrier
sys.argv={[str(SUPERVISOR), *command[2:]]!r}
raise SystemExit(module.main())
""", encoding="utf-8",
    )
    launcher = subprocess.Popen(
        [sys.executable, str(harness)], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=_environment(token),
        pass_fds=(endpoint.fileno(),),
    )
    endpoint.close()
    claim, started = _attempt_state_paths(receipt)
    try:
        payload = b""
        while not payload.endswith(b"\n"):
            chunk = controller.recv(4096)
            assert chunk, "phase owner disappeared before handshake"
            payload += chunk
        identity = json.loads(payload)
        observed = subprocess.run(
            ["/bin/ps", "-p", str(identity["pid"]), "-o", "ppid=,lstart="],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip().split(maxsplit=1)
        assert identity["start_identity_source"] == "os_ps_lstart"
        assert observed == [str(identity["parent_pid"]), identity["start_identity"]]
        assert os.getsid(identity["pid"]) == identity["session_id"]
        assert claim.exists()
        claim_bytes = claim.read_bytes()
        assert not started.exists() and not receipt.exists() and not starts.exists()
        if phase == "before_fork":
            assert identity["pid"] == launcher.pid
        else:
            assert identity["pid"] != launcher.pid
            assert identity["parent_pid"] == 1
        launcher.kill()
        assert launcher.wait(timeout=5) == -signal.SIGKILL
        if phase != "before_fork":
            controller.sendall(b"G")
            _wait_for_file(receipt)
        launcher.communicate(timeout=5)
        terminal_bytes = receipt.read_bytes() if receipt.exists() else None
        restarted = subprocess.run(
            command, capture_output=True, text=True, timeout=10,
            env=_environment(token),
        )
        assert restarted.returncode == 2
        assert json.loads(restarted.stdout) == {
            "reason_code": (
                "attempt_already_claimed" if phase == "before_fork"
                else "terminal_receipt_exists"
            ),
            "terminal_valid": False,
        }
        assert (receipt.read_bytes() if receipt.exists() else None) == terminal_bytes
        assert claim.read_bytes() == claim_bytes
        evidence = {
            "phase": phase, "barrier_identity": identity,
            "independent_ps_identity": observed, "launcher_pid": launcher.pid,
            "launcher_returncode": launcher.returncode,
            "restart_rejected": True, "claim_sha256": _sha(claim_bytes),
            "started_exists": started.exists(), "receipt_exists": receipt.exists(),
            "workload_starts": starts.read_text().count("started\n") if starts.exists() else 0,
        }
        (tmp_path / "phase-evidence.json").write_text(json.dumps(evidence, indent=2))
        return manifest, child_argv, starts, output, failure, receipt, identity
    finally:
        controller.close()
        if launcher.poll() is None:
            launcher.kill()
        launcher.communicate(timeout=5)


def test_claim_before_fork_interruption_never_launches_or_replays(tmp_path) -> None:
    _, _, starts, output, failure, receipt, _ = _interrupt_at_detach_phase(
        tmp_path, "before_fork",
    )
    claim, started = _attempt_state_paths(receipt)
    assert claim.exists()
    assert not started.exists()
    assert not starts.exists() and not output.exists() and not failure.exists()
    assert not receipt.exists()


def test_detached_owner_survives_launcher_sigkill_before_ack(tmp_path) -> None:
    manifest, child_argv, starts, output, failure, receipt, identity = (
        _interrupt_at_detach_phase(tmp_path, "before_ack")
    )
    claim, started = _attempt_state_paths(receipt)
    assert claim.exists() and started.exists()
    assert starts.read_text() == "started\n"
    assert output.exists() and not failure.exists()
    document = json.loads(receipt.read_text("utf-8"))
    assert document["child_started"] is True
    assert document["child_returncode"] == 0
    assert document["terminal_valid"] is True
    assert document["candidate_tree"] == TREE
    assert document["source_manifest_digest"] == _sha(manifest.read_bytes())
    assert document["child_argv_digest"] == _sha(
        json.dumps(child_argv, sort_keys=True, separators=(",", ":")).encode()
    )
    assert document["supervisor_identity"]["pid"] == identity["pid"]
    assert document["supervisor_identity"]["start_identity"] == identity["start_identity"]
    assert document["attempt_claim_artifact"]["sha256"] == _sha(claim.read_bytes())
    assert len(list(tmp_path.glob("benchmark.supervisor-receipt.v1.json"))) == 1


def test_attempt_claim_and_start_bind_cycle_authorization_before_launch(tmp_path) -> None:
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path,
        """import argparse, json
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--source-manifest'); p.add_argument('--source-manifest-digest'); p.add_argument('--output'); p.add_argument('--failure-output'); a=p.parse_args()
Path(a.output).write_text(json.dumps({'protocol':'test.success.v1'})+'\\n')
""",
    )
    expected = _sha(manifest.read_bytes())
    cycle_id = "a5-controlled-cycle"
    authorization_digest = "sha256:" + "a" * 64
    command, _ = _command(
        manifest,
        token,
        child,
        output,
        failure,
        receipt,
        expected,
        cycle_id=cycle_id,
        authorization_digest=authorization_digest,
    )

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=10,
        env=_environment(token),
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    claim_path, started_path = _attempt_state_paths(receipt)
    claim = json.loads(claim_path.read_text("utf-8"))
    started_record = json.loads(started_path.read_text("utf-8"))
    terminal = json.loads(receipt.read_text("utf-8"))
    assert stat.S_IMODE(claim_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(started_path.stat().st_mode) == 0o600
    assert claim["attempt_binding"]["cycle_id"] == cycle_id
    assert claim["attempt_binding"]["authorization_sha256"] == authorization_digest
    assert claim["attempt_binding"]["candidate_tree"] == TREE
    assert claim["attempt_binding"]["source_manifest_digest"] == expected
    assert claim["attempt_id"] == _sha(
        json.dumps(
            claim["attempt_binding"],
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    assert started_record["attempt_id"] == claim["attempt_id"]
    assert started_record["attempt_claim_sha256"] == _sha(claim_path.read_bytes())
    assert terminal["attempt_id"] == claim["attempt_id"]
    assert terminal["attempt_claim_artifact"]["sha256"] == _sha(
        claim_path.read_bytes()
    )
    assert terminal["attempt_started_artifact"]["sha256"] == _sha(
        started_path.read_bytes()
    )


def _postspawn_fault_harness(
    tmp_path: Path,
    command: list[str],
    *,
    mode: str,
    starts: Path | None = None,
    receipt: Path | None = None,
) -> list[str]:
    harness = tmp_path / f"postspawn-fault-{mode}.py"
    wait_for_child = ""
    if starts is not None:
        wait_for_child = f"""
        deadline=time.monotonic()+3.0
        while not pathlib.Path({str(starts)!r}).exists() and time.monotonic()<deadline:
            time.sleep(0.01)
        if not pathlib.Path({str(starts)!r}).exists():
            raise RuntimeError('controlled child did not start')
"""
    injection = {
        "started_oserror": f"""
original=module._exclusive_json
def injected(path, document, **kwargs):
    if str(path).endswith('.attempt-started.v1.json'):
{wait_for_child.rstrip()}
        raise OSError('controlled local started-record persistence failure')
    return original(path, document, **kwargs)
module._exclusive_json=injected
""",
        "started_conflict": f"""
original=module._exclusive_json
def injected(path, document, **kwargs):
    if str(path).endswith('.attempt-started.v1.json'):
{wait_for_child.rstrip()}
        fd=os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        os.write(fd, b'controlled-conflict\\n'); os.fsync(fd); os.close(fd)
    return original(path, document, **kwargs)
module._exclusive_json=injected
""",
        "identity_once": f"""
original_identity=module._process_identity
failed=[False]
def injected_identity(pid):
    if pid != os.getpid() and not failed[0]:
{wait_for_child.rstrip()}
        failed[0]=True
        raise module.SupervisorError('controlled_child_identity_failure')
    return original_identity(pid)
module._process_identity=injected_identity
""",
        "receipt_oserror": f"""
original=module._exclusive_json
def injected(path, document, **kwargs):
    if str(path) == {str(receipt)!r}:
        raise OSError('controlled terminal-receipt persistence failure')
    return original(path, document, **kwargs)
module._exclusive_json=injected
""",
    }[mode]
    harness.write_text(
        f"""import importlib.util, os, pathlib, sys, time
spec=importlib.util.spec_from_file_location('p01s_supervisor', {str(SUPERVISOR)!r})
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
{injection}
sys.argv={[str(SUPERVISOR), *command[2:]]!r}
raise SystemExit(module.main())
""",
        encoding="utf-8",
    )
    return [sys.executable, str(harness)]


def _terminating_child_source(starts: Path, terminated: Path) -> str:
    return f"""import argparse, os, signal, time
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--source-manifest'); p.add_argument('--source-manifest-digest'); p.add_argument('--output'); p.add_argument('--failure-output'); p.parse_args()
def terminate(_signum, _frame):
    Path({str(terminated)!r}).write_text('terminated')
    raise SystemExit(77)
signal.signal(signal.SIGTERM, terminate)
fd=os.open({str(starts)!r}, os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o600)
os.write(fd, (str(os.getpid())+'\\n').encode()); os.fsync(fd); os.close(fd)
deadline=time.monotonic()+5.0
while time.monotonic()<deadline: time.sleep(0.02)
"""


def _process_exists(pid: int) -> bool:
    return bool(
        subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "pid="],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    )


def test_started_record_oserror_supervises_real_child_and_records_truth(tmp_path) -> None:
    starts = tmp_path / "starts"
    terminated = tmp_path / "terminated"
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path, _terminating_child_source(starts, terminated)
    )
    expected = _sha(manifest.read_bytes())
    command, _ = _command(manifest, token, child, output, failure, receipt, expected)
    fault_command = _postspawn_fault_harness(
        tmp_path, command, mode="started_oserror", starts=starts
    )

    result = subprocess.run(
        fault_command,
        capture_output=True,
        text=True,
        timeout=10,
        env=_environment(token),
    )

    assert result.returncode == 125, (result.stdout, result.stderr)
    pid = int(starts.read_text("utf-8").strip())
    document = json.loads(receipt.read_text("utf-8"))
    assert document["child_started"] is True
    assert document["child_pid"] == pid
    assert document["child_identity"]["pid"] == pid
    assert document["child_returncode"] == 77
    assert document["attempt_started_artifact"]["exists"] is False
    assert document["reason_code"] == "attempt_started_record_write_failed"
    assert document["terminal_valid"] is False
    assert terminated.read_text("utf-8") == "terminated"
    assert not _process_exists(pid)


def test_started_record_exclusive_conflict_is_postspawn_failure_not_no_child(tmp_path) -> None:
    starts = tmp_path / "starts"
    terminated = tmp_path / "terminated"
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path, _terminating_child_source(starts, terminated)
    )
    expected = _sha(manifest.read_bytes())
    command, _ = _command(manifest, token, child, output, failure, receipt, expected)
    fault_command = _postspawn_fault_harness(
        tmp_path, command, mode="started_conflict", starts=starts
    )

    result = subprocess.run(
        fault_command,
        capture_output=True,
        text=True,
        timeout=10,
        env=_environment(token),
    )

    assert result.returncode == 125, (result.stdout, result.stderr)
    pid = int(starts.read_text("utf-8").strip())
    document = json.loads(receipt.read_text("utf-8"))
    started_path = _attempt_state_paths(receipt)[1]
    assert started_path.read_bytes() == b"controlled-conflict\n"
    assert document["child_started"] is True
    assert document["child_pid"] == pid
    assert document["child_returncode"] == 77
    assert document["attempt_started_artifact"]["exists"] is True
    assert document["attempt_started_artifact"]["sha256"] == _sha(
        started_path.read_bytes()
    )
    assert document["reason_code"] == "attempt_started_record_exists"
    assert document["terminal_valid"] is False
    assert terminated.exists()
    assert not _process_exists(pid)


def test_child_identity_failure_retries_identity_for_bounded_teardown(tmp_path) -> None:
    starts = tmp_path / "starts"
    terminated = tmp_path / "terminated"
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path, _terminating_child_source(starts, terminated)
    )
    expected = _sha(manifest.read_bytes())
    command, _ = _command(manifest, token, child, output, failure, receipt, expected)
    fault_command = _postspawn_fault_harness(
        tmp_path, command, mode="identity_once", starts=starts
    )

    result = subprocess.run(
        fault_command,
        capture_output=True,
        text=True,
        timeout=10,
        env=_environment(token),
    )

    assert result.returncode == 125, (result.stdout, result.stderr)
    pid = int(starts.read_text("utf-8").strip())
    document = json.loads(receipt.read_text("utf-8"))
    assert document["child_started"] is True
    assert document["child_pid"] == pid
    assert document["child_identity"]["pid"] == pid
    assert document["child_returncode"] == 77
    assert document["attempt_started_artifact"]["exists"] is False
    assert document["reason_code"] == "child_identity_observation_failed"
    assert document["terminal_valid"] is False
    assert terminated.exists()
    assert not _process_exists(pid)


def test_true_popen_failure_remains_no_child_startup_failure(tmp_path) -> None:
    marker = tmp_path / "must-not-start"
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path, f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n"
    )
    expected = _sha(manifest.read_bytes())
    command, _ = _command(manifest, token, child, output, failure, receipt, expected)
    separator = command.index("--")
    command[separator + 1] = str(tmp_path / "executable-that-does-not-exist")

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=10,
        env=_environment(token),
    )

    assert result.returncode == 125
    document = json.loads(receipt.read_text("utf-8"))
    assert document["child_started"] is False
    assert document["child_pid"] is None
    assert document["child_identity"] is None
    assert document["child_returncode"] is None
    assert document["reason_code"] == "child_start_failed"
    assert document["terminal_valid"] is False
    assert not marker.exists()


def test_terminal_receipt_write_failure_consumes_attempt_without_replay(tmp_path) -> None:
    starts = tmp_path / "starts"
    manifest, token, child, output, failure, receipt = _fixture(
        tmp_path,
        f"""import argparse, json, os
from pathlib import Path
p=argparse.ArgumentParser(); p.add_argument('--source-manifest'); p.add_argument('--source-manifest-digest'); p.add_argument('--output'); p.add_argument('--failure-output'); a=p.parse_args()
fd=os.open({str(starts)!r}, os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o600); os.write(fd,b'started\\n'); os.fsync(fd); os.close(fd)
Path(a.output).write_text(json.dumps({{'protocol':'test.success.v1'}})+'\\n')
""",
    )
    expected = _sha(manifest.read_bytes())
    command, _ = _command(manifest, token, child, output, failure, receipt, expected)
    fault_command = _postspawn_fault_harness(
        tmp_path, command, mode="receipt_oserror", receipt=receipt
    )

    result = subprocess.run(
        fault_command,
        capture_output=True,
        text=True,
        timeout=10,
        env=_environment(token),
    )
    restarted = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=10,
        env=_environment(token),
    )

    claim, started = _attempt_state_paths(receipt)
    assert result.returncode == 125, (result.stdout, result.stderr)
    assert json.loads(result.stdout) == {
        "reason_code": "terminal_receipt_write_failed",
        "terminal_valid": False,
    }
    assert not receipt.exists()
    assert claim.exists()
    assert started.exists()
    assert starts.read_text("utf-8") == "started\n"
    assert restarted.returncode == 2
    assert json.loads(restarted.stdout)["reason_code"] == "attempt_already_claimed"
