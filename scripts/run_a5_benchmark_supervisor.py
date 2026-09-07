#!/usr/bin/env python3
"""Supervise one A5 benchmark and seal a wrapper-level terminal receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import tempfile
import time
from typing import Any


class SupervisorError(RuntimeError):
    """Stable pre-launch supervisor rejection."""


def _sha_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _absolute_name(path: Path) -> Path:
    """Normalize a name without following a final pre-existing symlink."""

    return Path(os.path.abspath(os.fspath(path)))


def _sha_file(path: Path, *, maximum_bytes: int = 16 * 1024 * 1024) -> str:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise SupervisorError("source_manifest_invalid")
        if metadata.st_size <= 0 or metadata.st_size > maximum_bytes:
            raise SupervisorError("source_manifest_invalid")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
    except SupervisorError:
        raise
    except OSError as error:
        raise SupervisorError("source_manifest_invalid") from error
    return "sha256:" + digest.hexdigest()


def _artifact(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "sha256": None, "size_bytes": 0}
    metadata = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        return {"exists": True, "sha256": None, "size_bytes": metadata.st_size}
    return {
        "exists": True,
        "sha256": _sha_file(path, maximum_bytes=64 * 1024 * 1024),
        "size_bytes": metadata.st_size,
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
    }


def _exclusive_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise SupervisorError("terminal_receipt_exists")
    payload = json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.link(temporary, path)
        temporary.unlink()
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _one_value(argv: list[str], flag: str) -> str:
    positions = [index for index, value in enumerate(argv) if value == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        raise SupervisorError("child_argv_binding_invalid")
    return argv[positions[0] + 1]


def _environment_token() -> str:
    value = os.environ.get("MYCELIUM_A5_OPERATOR_TOKEN")
    if (
        value is None
        or not 32 <= len(value) <= 4096
        or value != value.strip()
    ):
        raise SupervisorError("operator_token_environment_invalid")
    return value


def _process_identity(pid: int) -> dict[str, Any]:
    """Return an OS-observed PID/start tuple without retaining command or env."""

    observed_at_unix_ns = time.time_ns()
    try:
        completed = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "ppid=,lstart="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        fields = completed.stdout.strip().split(maxsplit=1)
        if completed.returncode != 0 or len(fields) != 2:
            raise ValueError("process identity unavailable")
        parent_pid = int(fields[0])
        session_id = os.getsid(pid)
        start_identity = fields[1]
        source = "os_ps_lstart"
    except (OSError, subprocess.SubprocessError, ValueError):
        # A very short child may exit before ps samples it. Keep the exact PID
        # and supervisor observation boundary, but never use this fallback to
        # authorize an escalation signal.
        parent_pid = None
        session_id = None
        start_identity = f"supervisor_observed:{pid}:{observed_at_unix_ns}"
        source = "supervisor_observation_only"
    return {
        "pid": pid,
        "parent_pid": parent_pid,
        "session_id": session_id,
        "start_identity": start_identity,
        "start_identity_source": source,
        "observed_at_unix_ns": observed_at_unix_ns,
    }


def _detach_from_launcher() -> dict[str, Any] | None:
    """Double-fork; parent returns launch identity, OS-owned child returns None."""

    read_fd, write_fd = os.pipe()
    first_pid = os.fork()
    if first_pid != 0:
        os.close(write_fd)
        try:
            ready, _, _ = select.select([read_fd], [], [], 10.0)
            if not ready:
                raise SupervisorError("detached_supervisor_start_timeout")
            payload = os.read(read_fd, 8192)
        finally:
            os.close(read_fd)
            os.waitpid(first_pid, 0)
        try:
            launch = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise SupervisorError("detached_supervisor_start_failed") from error
        if not isinstance(launch, dict) or launch.get("detached") is not True:
            raise SupervisorError("detached_supervisor_start_failed")
        return launch

    os.close(read_fd)
    try:
        os.setsid()
        second_pid = os.fork()
        if second_pid != 0:
            os._exit(0)
        deadline = time.monotonic() + 5.0
        while os.getppid() != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        identity = _process_identity(os.getpid())
        launch = {
            "detached": True,
            "supervisor_pid": identity["pid"],
            "supervisor_parent_pid": identity["parent_pid"],
            "supervisor_session_id": identity["session_id"],
            "supervisor_start_identity": identity["start_identity"],
            "supervisor_start_identity_source": identity["start_identity_source"],
        }
        os.write(
            write_fd,
            (json.dumps(launch, sort_keys=True) + "\n").encode("utf-8"),
        )
        os.close(write_fd)
        null_fd = os.open(os.devnull, os.O_RDWR)
        try:
            for descriptor in (0, 1, 2):
                os.dup2(null_fd, descriptor)
        finally:
            if null_fd > 2:
                os.close(null_fd)
        return None
    except BaseException:
        try:
            os.close(write_fd)
        except OSError:
            pass
        os._exit(125)


def _base_receipt(args: argparse.Namespace, child_argv: list[str]) -> dict[str, Any]:
    return {
        "protocol": "mycelium.a5_benchmark_supervisor_receipt.v1",
        "qualification_claim": False,
        "promotion_authorized": False,
        "candidate_tree": args.candidate_tree,
        "expected_source_manifest_digest": args.expected_source_manifest_digest,
        "source_manifest_digest": None,
        "child_argv_digest": _sha_bytes(
            json.dumps(child_argv, sort_keys=True, separators=(",", ":")).encode()
        ),
        "detached": bool(args.detach),
        "supervisor_identity": _process_identity(os.getpid()),
        "child_started": False,
        "child_pid": None,
        "child_identity": None,
        "child_returncode": None,
        "supervisor_termination_signal": None,
        "child_termination_escalated_to_sigkill": False,
        "child_sigkill_identity_revalidated": False,
        "started_at_unix_ms": int(time.time() * 1000),
        "completed_at_unix_ms": None,
        "success_artifact": {"exists": False, "sha256": None, "size_bytes": 0},
        "failure_artifact": {"exists": False, "sha256": None, "size_bytes": 0},
        "terminal_valid": False,
        "reason_code": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--candidate-tree", required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--expected-source-manifest-digest", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--failure-output", type=Path, required=True)
    parser.add_argument(
        "--detach",
        action="store_true",
        help="double-fork before workload launch so the OS owns supervision",
    )
    parser.add_argument("child_argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    child_argv = list(args.child_argv)
    if child_argv and child_argv[0] == "--":
        child_argv.pop(0)
    receipt_path = _absolute_name(args.receipt)
    if os.path.lexists(receipt_path):
        print(
            json.dumps(
                {"reason_code": "terminal_receipt_exists", "terminal_valid": False},
                sort_keys=True,
            )
        )
        return 2
    document = _base_receipt(args, child_argv)

    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.candidate_tree):
            raise SupervisorError("candidate_tree_invalid")
        if not child_argv:
            raise SupervisorError("child_argv_binding_invalid")
        source_path = args.source_manifest.resolve(strict=True)
        output_path = _absolute_name(args.output)
        failure_path = _absolute_name(args.failure_output)
        if receipt_path in (output_path, failure_path) or output_path == failure_path:
            raise SupervisorError("artifact_path_collision")
        if os.path.lexists(output_path) or os.path.lexists(failure_path):
            raise SupervisorError("terminal_artifact_preexists")
        source_digest = _sha_file(source_path)
        document["source_manifest_digest"] = source_digest
        if source_digest != args.expected_source_manifest_digest:
            raise SupervisorError("source_manifest_digest_mismatch")
        if Path(_one_value(child_argv, "--source-manifest")).resolve(strict=True) != source_path:
            raise SupervisorError("child_argv_binding_invalid")
        if _one_value(child_argv, "--source-manifest-digest") != source_digest:
            raise SupervisorError("child_argv_binding_invalid")
        if Path(_one_value(child_argv, "--output")).resolve() != output_path:
            raise SupervisorError("child_argv_binding_invalid")
        if Path(_one_value(child_argv, "--failure-output")).resolve() != failure_path:
            raise SupervisorError("child_argv_binding_invalid")
        operator_token = _environment_token()
    except (SupervisorError, OSError) as error:
        document["completed_at_unix_ms"] = int(time.time() * 1000)
        document["reason_code"] = (
            str(error) if isinstance(error, SupervisorError) else "source_manifest_invalid"
        )
        _exclusive_json(receipt_path, document)
        print(json.dumps({"reason_code": document["reason_code"], "terminal_valid": False}, sort_keys=True))
        return 2

    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        environment.pop(name, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["MYCELIUM_A5_OPERATOR_TOKEN"] = operator_token
    os.umask(0o077)
    if args.detach:
        try:
            launch = _detach_from_launcher()
        except SupervisorError as error:
            document["completed_at_unix_ms"] = int(time.time() * 1000)
            document["reason_code"] = str(error)
            _exclusive_json(receipt_path, document)
            print(
                json.dumps(
                    {"reason_code": document["reason_code"], "terminal_valid": False},
                    sort_keys=True,
                )
            )
            return 125
        if launch is not None:
            print(json.dumps(launch, sort_keys=True))
            return 0
        document["supervisor_identity"] = _process_identity(os.getpid())
    child_holder: list[subprocess.Popen | None] = [None]
    received_signal: list[int] = []
    termination_deadline: list[float | None] = [None]

    def relay_signal(signum: int, _frame: Any) -> None:
        if not received_signal:
            received_signal.append(signum)
            termination_deadline[0] = time.monotonic() + 30.0
        child_process = child_holder[0]
        if child_process is not None and child_process.poll() is None:
            try:
                child_process.send_signal(signum)
            except ProcessLookupError:
                pass

    watched_signals = (signal.SIGTERM, signal.SIGINT)
    previous_handlers = {
        signum: signal.signal(signum, relay_signal) for signum in watched_signals
    }
    try:
        child = subprocess.Popen(
            child_argv,
            env=environment,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
        child_holder[0] = child
        child_identity = _process_identity(child.pid)
        document["child_identity"] = child_identity
        if received_signal and child.poll() is None:
            child.send_signal(received_signal[0])
    except OSError:
        document["completed_at_unix_ms"] = int(time.time() * 1000)
        document["reason_code"] = "child_start_failed"
        if received_signal:
            document["supervisor_termination_signal"] = signal.Signals(
                received_signal[0]
            ).name
        _exclusive_json(receipt_path, document)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        print(json.dumps({"reason_code": document["reason_code"], "terminal_valid": False}, sort_keys=True))
        return 125

    document["child_started"] = True
    document["child_pid"] = child.pid
    while True:
        try:
            returncode = child.wait(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            deadline = termination_deadline[0]
            if deadline is not None and time.monotonic() >= deadline:
                current_identity = _process_identity(child.pid)
                if (
                    child_identity["start_identity_source"] != "os_ps_lstart"
                    or current_identity["start_identity_source"] != "os_ps_lstart"
                    or current_identity["start_identity"]
                    != child_identity["start_identity"]
                ):
                    # Never escalate against a PID that cannot be re-bound to
                    # the freshly launched child. Keep waiting for authorized
                    # external cleanup rather than signal an unproven process.
                    termination_deadline[0] = None
                    continue
                child.kill()
                document["child_termination_escalated_to_sigkill"] = True
                document["child_sigkill_identity_revalidated"] = True
                returncode = child.wait()
                break
    document["child_returncode"] = returncode
    if received_signal:
        document["supervisor_termination_signal"] = signal.Signals(
            received_signal[0]
        ).name
    document["completed_at_unix_ms"] = int(time.time() * 1000)
    document["success_artifact"] = _artifact(output_path)
    document["failure_artifact"] = _artifact(failure_path)
    success_exists = document["success_artifact"]["exists"] is True
    failure_exists = document["failure_artifact"]["exists"] is True
    if returncode == 0 and success_exists and not failure_exists:
        document["terminal_valid"] = True
        document["reason_code"] = "success_artifact_present"
    elif returncode != 0 and failure_exists and not success_exists:
        document["terminal_valid"] = True
        document["reason_code"] = "failure_artifact_present"
    elif not success_exists and not failure_exists:
        document["reason_code"] = "child_terminal_artifact_missing"
    else:
        document["reason_code"] = "child_terminal_artifact_inconsistent"
    _exclusive_json(receipt_path, document)
    for signum, handler in previous_handlers.items():
        signal.signal(signum, handler)
    print(
        json.dumps(
            {
                "child_returncode": returncode,
                "reason_code": document["reason_code"],
                "terminal_valid": document["terminal_valid"],
            },
            sort_keys=True,
        )
    )
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
