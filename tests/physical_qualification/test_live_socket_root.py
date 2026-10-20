# SPDX-License-Identifier: AGPL-3.0-only
"""The live-route socket root stays inside the Unix-domain socket path limit for any run root.

macOS ``sun_path`` holds 104 bytes including the NUL; the node refuses a sidecar socket path of 100 bytes or
more (``sidecar_socket_path_too_long``).  A deep staging root used to push ``<staging>/socket/i.sock`` past it.
"""
from __future__ import annotations

import ast
import os
import shutil
import socket
import tempfile
from pathlib import Path, PurePosixPath

import pytest

from mycelium_live.socket_root import (
    SOCKET_FILE_NAME,
    SOCKET_PATH_LIMIT,
    live_socket_root,
)

REPO = Path(__file__).resolve().parents[2]


def _socket_path(root: str) -> bytes:
    return os.fsencode(f"{root}/{SOCKET_FILE_NAME}")


def _staging_for_legacy_length(total: int) -> str:
    """A staging root whose legacy ``<staging>/socket/i.sock`` is exactly ``total`` bytes."""
    fixed = len("/socket/") + len(SOCKET_FILE_NAME)
    base = "/private/tmp/mycelium-physical-run/"
    return base + "d" * (total - fixed - len(base))


def test_the_limit_matches_the_node_check():
    import re
    source = (REPO / "physical_inference_node.py").read_text()
    match = re.search(r"len\(\s*os\.fsencode\(self\.socket_path\)\s*\)\s*<\s*(\d+),\s*\"sidecar_socket_path_too_long\"", source)
    assert match and int(match.group(1)) == SOCKET_PATH_LIMIT


def test_a_root_that_fits_keeps_the_legacy_path_up_to_the_limit():
    staging = _staging_for_legacy_length(SOCKET_PATH_LIMIT - 1)
    root = live_socket_root(staging)
    assert root == f"{staging}/socket"
    assert len(_socket_path(root)) == SOCKET_PATH_LIMIT - 1


def test_one_byte_over_the_limit_moves_to_a_short_hashed_root():
    staging = _staging_for_legacy_length(SOCKET_PATH_LIMIT)
    root = live_socket_root(staging)
    assert root != f"{staging}/socket"
    assert len(_socket_path(root)) < SOCKET_PATH_LIMIT
    assert len(_socket_path(root)) < 60  # short, with room for any base


def test_deep_roots_are_deterministic_distinct_and_pass_the_run_plan_path_rules():
    deep_a = "/private/tmp/mycelium-physical-run/" + "run-with-a-very-long-name/" * 6 + "node-0"
    deep_b = deep_a[:-1] + "1"
    assert live_socket_root(deep_a) == live_socket_root(deep_a)
    assert live_socket_root(deep_a) != live_socket_root(deep_b)
    path = PurePosixPath(live_socket_root(deep_a))
    assert path.is_absolute() and str(path) == live_socket_root(deep_a) and len(path.parts) >= 3
    assert any(part.startswith("mycelium") for part in path.parts)  # run-plan socket_root marker rule
    assert not any(part in {"", ".", ".."} for part in path.parts)


def test_multibyte_staging_roots_are_measured_in_bytes_not_characters():
    staging = "/private/tmp/mycelium-physical-run/" + "é" * 24  # 24 characters, 48 bytes: legacy path is 98 + 9 bytes
    assert len(f"{staging}/socket/{SOCKET_FILE_NAME}") < SOCKET_PATH_LIMIT <= len(_socket_path(f"{staging}/socket"))
    assert len(_socket_path(live_socket_root(staging))) < SOCKET_PATH_LIMIT


def test_a_unix_socket_really_binds_at_the_resulting_path_for_a_deep_run_root():
    base = tempfile.mkdtemp(prefix="myc-", dir="/tmp")
    try:
        deep = "/private/tmp/mycelium-physical-run/" + "x" * 150
        root = Path(live_socket_root(deep, base=base))
        root.mkdir(parents=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(root / SOCKET_FILE_NAME))
        finally:
            listener.close()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_the_route_builder_derives_every_node_socket_root_through_the_helper():
    tree = ast.parse((REPO / "scripts" / "build_qwen_live_route.py").read_text())
    literal = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "/socket"]
    fstrings = [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
                and any(isinstance(v, ast.Constant) and v.value == "/socket" for v in n.values)]
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "live_socket_root"]
    assert not literal and not fstrings
    assert calls
