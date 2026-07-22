# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail-closed supervision for the physical inference node JSONL service."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
import re
import signal
import subprocess
import threading
from typing import Any
import uuid

from mycelium_qualification.evidence import canonical_json_bytes


NODE_CONTROL_PROTOCOL = "mycelium.physical_node_control.v1"
MAX_CONTROL_FRAME_BYTES = 1024 * 1024
MAX_STDERR_BYTES = 16 * 1024
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EOF = object()


class NodeProcessError(RuntimeError):
    """Stable, fail-closed node-process failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class _RemoteNodeError(NodeProcessError):
    pass


@dataclass(frozen=True)
class _ReaderError:
    code: str


def _canonical_json_loads(raw: bytes) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise ValueError("duplicate JSON key")
            document[key] = value
        return document

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON value: {value}")

    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid canonical JSON") from exc
    if canonical_json_bytes(document) != raw:
        raise ValueError("non-canonical JSON")
    return document


def _required_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field} is invalid")
    return value


def _positive_seconds(value: Any, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field} must be finite and positive")
    return float(value)


def _existing_path(value: str | Path, field: str, *, directory: bool) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{field} is unavailable") from exc
    if directory and not path.is_dir():
        raise ValueError(f"{field} must be a directory")
    if not directory and not path.is_file():
        raise ValueError(f"{field} must be a regular file")
    return path


def build_physical_node_command(
    *,
    python_executable: str | Path,
    service_script: str | Path,
    run_id: str,
    deployment_id: str,
    node_id: str,
    artifact_root: str | Path,
    socket_root: str | Path,
    sidecar_binary: str | Path,
    sidecar_local_only: bool,
    command_timeout_seconds: float = 30.0,
) -> tuple[str, ...]:
    """Build an argv-only physical-node launch command with no secret material."""

    python_path = _existing_path(python_executable, "python_executable", directory=False)
    service_path = _existing_path(service_script, "service_script", directory=False)
    artifacts = _existing_path(artifact_root, "artifact_root", directory=True)
    sockets = _existing_path(socket_root, "socket_root", directory=True)
    sidecar = _existing_path(sidecar_binary, "sidecar_binary", directory=False)
    run_id = _required_identifier(run_id, "run_id")
    deployment_id = _required_identifier(deployment_id, "deployment_id")
    node_id = _required_identifier(node_id, "node_id")
    timeout = _positive_seconds(command_timeout_seconds, "command_timeout_seconds")
    if not isinstance(sidecar_local_only, bool):
        raise ValueError("sidecar_local_only must be a boolean")

    command = [
        str(python_path),
        str(service_path),
        "--run-id",
        run_id,
        "--deployment-id",
        deployment_id,
        "--node-id",
        node_id,
        "--artifact-root",
        str(artifacts),
        "--socket-root",
        str(sockets),
        "--sidecar-binary",
        str(sidecar),
        "--command-timeout",
        str(timeout),
    ]
    if sidecar_local_only:
        command.append("--sidecar-local-only")
    return tuple(command)


class PhysicalNodeProcess:
    """One strictly ordered request/response client for ``PhysicalNodeService``."""

    def __init__(
        self,
        *,
        command: Sequence[str],
        node_id: str,
        run_id: str,
        deployment_id: str,
        response_timeout_seconds: float = 35.0,
        shutdown_timeout_seconds: float = 5.0,
        max_frame_bytes: int = MAX_CONTROL_FRAME_BYTES,
    ) -> None:
        if (
            not isinstance(command, Sequence)
            or isinstance(command, (str, bytes))
            or not command
            or not all(isinstance(item, str) and item for item in command)
        ):
            raise ValueError("command must be a non-empty argv sequence")
        self.node_id = _required_identifier(node_id, "node_id")
        self.run_id = _required_identifier(run_id, "run_id")
        self.deployment_id = _required_identifier(deployment_id, "deployment_id")
        self.response_timeout_seconds = _positive_seconds(
            response_timeout_seconds, "response_timeout_seconds"
        )
        self.shutdown_timeout_seconds = _positive_seconds(
            shutdown_timeout_seconds, "shutdown_timeout_seconds"
        )
        if (
            not isinstance(max_frame_bytes, int)
            or isinstance(max_frame_bytes, bool)
            or max_frame_bytes <= 0
            or max_frame_bytes > MAX_CONTROL_FRAME_BYTES
        ):
            raise ValueError("max_frame_bytes is invalid")
        self.max_frame_bytes = max_frame_bytes
        self._command = tuple(command)
        self._exchange_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._closed = False
        self._responses: Queue[bytes | object | _ReaderError] = Queue(maxsize=8)
        self._stderr_lock = threading.Lock()
        self._stderr_chunks: deque[bytes] = deque()
        self._stderr_size = 0
        try:
            self._process = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                shell=False,
                start_new_session=True,
            )
        except OSError as exc:
            self._closed = True
            raise NodeProcessError("node_process_start_failed") from exc
        self._stdout_thread = threading.Thread(
            target=self._read_stdout,
            name=f"mycelium-node-stdout-{self.node_id}",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            name=f"mycelium-node-stderr-{self.node_id}",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def running(self) -> bool:
        with self._state_lock:
            return not self._closed and self._process.poll() is None

    @property
    def stderr_tail(self) -> str:
        with self._stderr_lock:
            return b"".join(self._stderr_chunks).decode("utf-8", errors="replace")

    def _enqueue_response(self, item: bytes | object | _ReaderError) -> None:
        try:
            self._responses.put_nowait(item)
        except Exception:
            self._abort()

    def _read_stdout(self) -> None:
        stream = self._process.stdout
        assert stream is not None
        try:
            while True:
                line = stream.readline(self.max_frame_bytes + 2)
                if not line:
                    self._enqueue_response(_EOF)
                    return
                if len(line) > self.max_frame_bytes + 1 or not line.endswith(b"\n"):
                    self._enqueue_response(_ReaderError("invalid_node_response_frame"))
                    return
                self._enqueue_response(line[:-1])
        except (OSError, ValueError):
            self._enqueue_response(_ReaderError("node_response_read_failed"))

    def _read_stderr(self) -> None:
        stream = self._process.stderr
        assert stream is not None
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                with self._stderr_lock:
                    self._stderr_chunks.append(chunk)
                    self._stderr_size += len(chunk)
                    while self._stderr_size > MAX_STDERR_BYTES and self._stderr_chunks:
                        excess = self._stderr_size - MAX_STDERR_BYTES
                        first = self._stderr_chunks[0]
                        if len(first) <= excess:
                            self._stderr_chunks.popleft()
                            self._stderr_size -= len(first)
                        else:
                            self._stderr_chunks[0] = first[excess:]
                            self._stderr_size -= excess
        except (OSError, ValueError):
            return

    def _terminate_process(self) -> None:
        process = self._process
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    process.terminate()
                except OSError:
                    pass
            try:
                process.wait(timeout=min(self.shutdown_timeout_seconds, 1.0))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    try:
                        process.kill()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=self.shutdown_timeout_seconds)
                except subprocess.TimeoutExpired:
                    pass

    def _abort(self) -> None:
        with self._state_lock:
            self._closed = True
        self._terminate_process()
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass

    def _validate_response(self, raw: bytes, command_id: str) -> Any:
        try:
            response = _canonical_json_loads(raw)
        except (TypeError, ValueError) as exc:
            raise NodeProcessError("invalid_node_response") from exc
        if not isinstance(response, dict):
            raise NodeProcessError("invalid_node_response")
        if response.get("command_id") != command_id:
            raise NodeProcessError("response_command_mismatch")
        if (
            response.get("protocol") != NODE_CONTROL_PROTOCOL
            or response.get("node_id") != self.node_id
            or response.get("route_ready") is not False
            or not isinstance(response.get("ok"), bool)
        ):
            raise NodeProcessError("invalid_node_response")
        if response["ok"]:
            if set(response) != {
                "protocol",
                "command_id",
                "node_id",
                "ok",
                "route_ready",
                "result",
            }:
                raise NodeProcessError("invalid_node_response")
            return response["result"]
        if set(response) != {
            "protocol",
            "command_id",
            "node_id",
            "ok",
            "route_ready",
            "error",
        }:
            raise NodeProcessError("invalid_node_response")
        error = response.get("error")
        if (
            not isinstance(error, dict)
            or set(error) != {"code"}
            or not isinstance(error.get("code"), str)
            or not _OPERATION_RE.fullmatch(error["code"])
        ):
            raise NodeProcessError("invalid_node_response")
        raise _RemoteNodeError(error["code"])

    def command(
        self, operation: str, payload: Mapping[str, Any] | None = None
    ) -> Any:
        if not isinstance(operation, str) or not _OPERATION_RE.fullmatch(operation):
            raise ValueError("operation is invalid")
        if payload is None:
            payload_document: dict[str, Any] = {}
        elif not isinstance(payload, Mapping) or not all(
            isinstance(key, str) for key in payload
        ):
            raise ValueError("payload must be a JSON object")
        else:
            payload_document = dict(payload)
        with self._exchange_lock:
            with self._state_lock:
                if self._closed:
                    raise NodeProcessError("node_process_closed")
                if self._process.poll() is not None:
                    self._closed = True
                    raise NodeProcessError("node_process_exited")
            command_id = str(uuid.uuid4())
            document = {
                "protocol": NODE_CONTROL_PROTOCOL,
                "command_id": command_id,
                "run_id": self.run_id,
                "deployment_id": self.deployment_id,
                "command": operation,
                "payload": payload_document,
            }
            try:
                frame = canonical_json_bytes(document)
            except (TypeError, ValueError) as exc:
                raise ValueError("command fields are not canonical JSON") from exc
            if len(frame) + 1 > self.max_frame_bytes:
                raise NodeProcessError("node_command_too_large")
            stdin = self._process.stdin
            assert stdin is not None
            try:
                stdin.write(frame + b"\n")
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                self._abort()
                raise NodeProcessError("node_process_unavailable") from exc
            try:
                item = self._responses.get(timeout=self.response_timeout_seconds)
            except Empty as exc:
                self._abort()
                raise NodeProcessError("node_response_timeout") from exc
            if item is _EOF:
                self._abort()
                raise NodeProcessError("node_process_exited")
            if isinstance(item, _ReaderError):
                self._abort()
                raise NodeProcessError(item.code)
            assert isinstance(item, bytes)
            try:
                return self._validate_response(item, command_id)
            except _RemoteNodeError:
                raise
            except NodeProcessError:
                self._abort()
                raise

    def close(self) -> None:
        with self._exchange_lock:
            with self._state_lock:
                if self._closed:
                    return
            try:
                self._command_stop()
            except NodeProcessError:
                pass
            with self._state_lock:
                self._closed = True
            stdin = self._process.stdin
            if stdin is not None and not stdin.closed:
                try:
                    stdin.close()
                except OSError:
                    pass
            try:
                self._process.wait(timeout=self.shutdown_timeout_seconds)
            except subprocess.TimeoutExpired:
                self._terminate_process()
            for stream in (self._process.stdout, self._process.stderr):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def _command_stop(self) -> None:
        if self._process.poll() is not None:
            return
        command_id = str(uuid.uuid4())
        frame = canonical_json_bytes(
            {
                "protocol": NODE_CONTROL_PROTOCOL,
                "command_id": command_id,
                "run_id": self.run_id,
                "deployment_id": self.deployment_id,
                "command": "stop",
                "payload": {},
            }
        )
        stdin = self._process.stdin
        assert stdin is not None
        try:
            stdin.write(frame + b"\n")
            stdin.flush()
            item = self._responses.get(timeout=self.response_timeout_seconds)
        except (BrokenPipeError, OSError, ValueError, Empty) as exc:
            raise NodeProcessError("node_stop_failed") from exc
        if not isinstance(item, bytes):
            raise NodeProcessError("node_stop_failed")
        self._validate_response(item, command_id)

    def __enter__(self) -> "PhysicalNodeProcess":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()
