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


def _command(manifest, token, child, output, failure, receipt, expected):
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
        "--",
        *child_argv,
    ]
    return command, child_argv


def _environment(token: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment["MYCELIUM_A5_OPERATOR_TOKEN"] = token
    return environment


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
