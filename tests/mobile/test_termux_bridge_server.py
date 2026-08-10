from __future__ import annotations

from pathlib import Path

import pytest

from mycelium_mobile.termux_bridge_server import (
    BridgeRequestError,
    decode_run_request,
    load_token,
)


TERMUX_BIN = "/data/data/com.termux/files/usr/bin"


def test_run_request_is_argv_only_and_termux_scoped() -> None:
    argv, cwd, timeout, detach = decode_run_request(
        {
            "argv": [f"{TERMUX_BIN}/python", "worker.py"],
            "cwd": "/data/data/com.termux/files/home/mycelium",
            "timeout_seconds": 10,
            "detach": False,
        }
    )
    assert argv == [f"{TERMUX_BIN}/python", "worker.py"]
    assert cwd == "/data/data/com.termux/files/home/mycelium"
    assert timeout == 10.0
    assert detach is False


@pytest.mark.parametrize(
    "argv",
    [
        ["/system/bin/id"],
        [f"{TERMUX_BIN}/sh", "-c", "id"],
        [f"{TERMUX_BIN}/bash", "-lc", "id"],
        ["relative-command"],
    ],
)
def test_run_request_rejects_non_termux_and_shell_executables(argv: list[str]) -> None:
    with pytest.raises(BridgeRequestError):
        decode_run_request(
            {
                "argv": argv,
                "timeout_seconds": 1,
                "detach": False,
            }
        )


def test_token_file_is_owner_only_regular_file(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("a" * 64)
    token.chmod(0o600)
    assert load_token(token) == "a" * 64

    token.chmod(0o644)
    with pytest.raises(BridgeRequestError, match="token_file_invalid"):
        load_token(token)
