from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
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


def test_detached_owner_survives_launcher_sigkill_before_ack(tmp_path) -> None:
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
    harness_command = _detach_harness(
        tmp_path, command, startup_delay_seconds=0.4, ack_timeout_seconds=5.0
    )
    launcher = subprocess.Popen(
        harness_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_environment(token),
    )
    claim, started = _attempt_state_paths(receipt)
    _wait_for_file(claim)
    launcher.kill()
    launcher.communicate(timeout=5)

    _wait_for_file(receipt)
    assert claim.exists()
    assert started.exists()
    document = json.loads(receipt.read_text("utf-8"))
    assert document["child_started"] is True
    assert document["terminal_valid"] is True


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
