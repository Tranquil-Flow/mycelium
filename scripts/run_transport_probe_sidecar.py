#!/usr/bin/env python3
"""Run one bounded physical transport-probe sidecar with private local control."""
# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import stat
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from physical_inference_node import NativeSidecarProcess


def _private_parent(path: Path) -> None:
    metadata = path.parent.lstat()
    if (
        not path.is_absolute()
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise RuntimeError("transport_probe_private_parent_invalid")


def _create_private(path: Path, payload: bytes) -> None:
    _private_parent(path)
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)


def _endpoint_secret(path: Path) -> Path:
    metadata = path.lstat()
    if (
        not path.is_absolute()
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size != 32
    ):
        raise RuntimeError("transport_probe_endpoint_secret_invalid")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--socket-root", type=Path, required=True)
    parser.add_argument("--bootstrap-secret-file", type=Path, required=True)
    parser.add_argument("--endpoint-secret-file", type=Path, required=True)
    parser.add_argument("--max-runtime-seconds", type=float, default=300.0)
    args = parser.parse_args()
    if not 1.0 <= args.max_runtime_seconds <= 3_600.0:
        raise RuntimeError("transport_probe_max_runtime_invalid")

    endpoint_secret = _endpoint_secret(args.endpoint_secret_file)
    process = NativeSidecarProcess(
        binary=args.binary,
        socket_root=args.socket_root,
        local_only=False,
        queue_capacity=128,
        startup_timeout=30.0,
        endpoint_secret_file=endpoint_secret,
    )
    stopping = threading.Event()
    prior = {
        signum: signal.signal(signum, lambda _signum, _frame: stopping.set())
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        ready = process.start()
        _create_private(args.bootstrap_secret_file, process.bootstrap_material)
        print(
            json.dumps(
                {
                    "protocol": "mycelium.transport_probe_sidecar_ready.v1",
                    "endpoint_id": ready["endpoint_id"],
                    "endpoint_addr": ready["endpoint_addr"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            flush=True,
        )
        deadline = time.monotonic() + args.max_runtime_seconds
        while not stopping.wait(timeout=min(1.0, max(0.0, deadline - time.monotonic()))):
            if time.monotonic() >= deadline:
                break
    finally:
        args.bootstrap_secret_file.unlink(missing_ok=True)
        process.close()
        for signum, handler in prior.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
