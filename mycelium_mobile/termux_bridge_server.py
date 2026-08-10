# SPDX-License-Identifier: AGPL-3.0-or-later
"""Small authenticated argv-only bridge for a generic Android/Termux peer."""

from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
from typing import Any, Mapping, Sequence


TOKEN_HEADER = "x-termux-bridge-token"
TERMUX_ROOT = PurePosixPath("/data/data/com.termux/files")
MAX_REQUEST_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 512 * 1024
MAX_ARGV_ITEMS = 256
MAX_ARGUMENT_BYTES = 32 * 1024
FORBIDDEN_EXECUTABLES = frozenset(
    {"ash", "bash", "busybox", "dash", "fish", "ksh", "sh", "toybox", "zsh"}
)


class BridgeRequestError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _termux_path(value: object, *, executable: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 4_096:
        raise BridgeRequestError("path_invalid")
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or path == TERMUX_ROOT
        or not path.is_relative_to(TERMUX_ROOT)
        or ".." in path.parts
    ):
        raise BridgeRequestError("path_invalid")
    if executable and path.name.lower() in FORBIDDEN_EXECUTABLES:
        raise BridgeRequestError("executable_forbidden")
    return str(path)


def decode_run_request(value: object) -> tuple[list[str], str | None, float, bool]:
    if not isinstance(value, Mapping) or set(value) not in (
        {"argv", "timeout_seconds", "detach"},
        {"argv", "cwd", "timeout_seconds", "detach"},
    ):
        raise BridgeRequestError("request_invalid")
    argv_value = value["argv"]
    if (
        not isinstance(argv_value, list)
        or not 1 <= len(argv_value) <= MAX_ARGV_ITEMS
        or any(not isinstance(item, str) or not item for item in argv_value)
        or sum(len(item.encode("utf-8")) for item in argv_value) > MAX_ARGUMENT_BYTES
    ):
        raise BridgeRequestError("argv_invalid")
    argv = list(argv_value)
    argv[0] = _termux_path(argv[0], executable=True)
    cwd_value = value.get("cwd")
    cwd = None if cwd_value is None else _termux_path(cwd_value)
    timeout = value["timeout_seconds"]
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(float(timeout))
        or not 0.1 <= float(timeout) <= 65.0
    ):
        raise BridgeRequestError("timeout_invalid")
    detach = value["detach"]
    if type(detach) is not bool:
        raise BridgeRequestError("detach_invalid")
    return argv, cwd, float(timeout), detach


def load_token(path: Path) -> str:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise BridgeRequestError("token_file_invalid")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            raw = os.read(descriptor, 513)
        finally:
            os.close(descriptor)
        token = raw.decode("ascii").strip()
    except BridgeRequestError:
        raise
    except (OSError, UnicodeError) as exc:
        raise BridgeRequestError("token_file_invalid") from exc
    if not 32 <= len(token) <= 512 or any(character.isspace() for character in token):
        raise BridgeRequestError("token_file_invalid")
    return token


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def create_server(*, host: str, port: int, token: str) -> HTTPServer:
    expected_token = token

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _send(self, status: int, value: Mapping[str, Any]) -> None:
            body = _json_bytes(value)
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.send_header("x-content-type-options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/health":
                self._send(404, {"error": "not_found"})
                return
            self._send(
                200,
                {
                    "allow_shell": False,
                    "claim": "authenticated argv command bridge for Termux",
                    "status": "ok",
                },
            )

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/run":
                self._send(404, {"error": "not_found"})
                return
            supplied = self.headers.get(TOKEN_HEADER, "")
            if not hmac.compare_digest(supplied, expected_token):
                self._send(401, {"error": "unauthorized"})
                return
            raw_length = self.headers.get("content-length")
            if raw_length is None or not raw_length.isdecimal():
                self._send(400, {"error": "request_invalid"})
                return
            length = int(raw_length)
            if not 1 <= length <= MAX_REQUEST_BYTES:
                self._send(413, {"error": "request_too_large"})
                return
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
                argv, cwd, timeout, detach = decode_run_request(value)
            except (UnicodeError, ValueError, RecursionError) as exc:
                code = exc.code if isinstance(exc, BridgeRequestError) else "request_invalid"
                self._send(400, {"error": code})
                return
            try:
                if detach:
                    process = subprocess.Popen(
                        argv,
                        cwd=cwd,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        shell=False,
                        start_new_session=True,
                        close_fds=True,
                    )
                    self._send(
                        200,
                        {"detached": True, "pid": process.pid, "shell": False},
                    )
                    return
                completed = subprocess.run(
                    argv,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    check=False,
                    shell=False,
                )
                stdout = completed.stdout[:MAX_OUTPUT_BYTES]
                remaining = MAX_OUTPUT_BYTES - len(stdout)
                stderr = completed.stderr[:remaining]
                self._send(
                    200,
                    {
                        "exit_code": completed.returncode,
                        "shell": False,
                        "stderr": stderr.decode("utf-8", errors="replace"),
                        "stdout": stdout.decode("utf-8", errors="replace"),
                    },
                )
            except subprocess.TimeoutExpired:
                self._send(408, {"error": "command_timeout"})
            except OSError:
                self._send(500, {"error": "command_failed"})

    return HTTPServer((host, port), Handler)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mycelium-termux-argv-bridge")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9020)
    parser.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.host != "0.0.0.0" or args.port != 9020:
        raise SystemExit("bridge binding must be 0.0.0.0:9020")
    token = load_token(args.token_file)
    server = create_server(host=args.host, port=args.port, token=token)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BridgeRequestError",
    "create_server",
    "decode_run_request",
    "load_token",
    "main",
]
