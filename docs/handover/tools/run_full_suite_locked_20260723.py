#!/usr/bin/env python3
"""Serialize Mycelium's unrestricted full pytest gate across parallel worktrees."""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=900.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = args.repo.expanduser().resolve()
    if not repo.is_dir() or not (repo / ".git").exists():
        print(f"invalid Mycelium worktree: {repo}", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("timeout must be positive", file=sys.stderr)
        return 2

    lock_path = Path("/Users/evinova/Projects/.mycelium-plan/full-suite.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        queued_at = time.monotonic()
        print(f"waiting for unrestricted full-suite lock: {repo}", flush=True)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        waited = time.monotonic() - queued_at
        print(f"acquired full-suite lock after {waited:.1f}s: {repo}", flush=True)
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=repo,
                check=False,
                timeout=args.timeout,
            )
            return completed.returncode
        except subprocess.TimeoutExpired:
            print(
                f"unrestricted full suite exceeded {args.timeout:.0f}s",
                file=sys.stderr,
            )
            return 124
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            print(f"released full-suite lock: {repo}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
