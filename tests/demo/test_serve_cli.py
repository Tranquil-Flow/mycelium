from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mycelium_demo import cli


def test_fixture_serve_selects_bundled_data_and_never_starts_live_runtime() -> None:
    calls: list[dict[str, Any]] = []
    live_calls: list[list[str]] = []

    def runner(command, *, cwd, env, check):
        calls.append(
            {
                "command": list(command),
                "cwd": cwd,
                "env": dict(env),
                "check": check,
            }
        )
        return SimpleNamespace(returncode=0)

    exit_code = cli.main(
        ["serve", "--mode", "fixture", "--host", "127.0.0.1", "--port", "4173"],
        process_runner=runner,
        live_server_main=lambda argv: live_calls.append(list(argv)) or 0,
        environ={"PATH": "/usr/bin"},
        fixture_runtime_available=lambda _path: True,
    )

    assert exit_code == 0
    assert live_calls == []
    assert calls == [
        {
            "command": [
                "npm",
                "run",
                "dev",
                "--",
                "--host",
                "127.0.0.1",
                "--port",
                "4173",
                "--strictPort",
            ],
            "cwd": cli.ROOT / "ui" / "web",
            "env": {
                "PATH": "/usr/bin",
                "VITE_OBSERVATORY_SOURCE_MODE": "fixture",
            },
            "check": False,
        }
    ]


def test_fixture_serve_bootstraps_locked_dependencies_before_start() -> None:
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(list(command))
        return SimpleNamespace(returncode=0)

    exit_code = cli.main(
        ["serve", "--mode", "fixture"],
        process_runner=runner,
        fixture_runtime_available=lambda _path: False,
        environ={},
    )

    assert exit_code == 0
    assert calls == [
        ["npm", "ci"],
        [
            "npm",
            "run",
            "dev",
            "--",
            "--host",
            "127.0.0.1",
            "--port",
            "5173",
            "--strictPort",
        ],
    ]


def test_fixture_serve_stops_when_dependency_bootstrap_fails() -> None:
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(list(command))
        return SimpleNamespace(returncode=23)

    exit_code = cli.main(
        ["serve", "--mode", "fixture"],
        process_runner=runner,
        fixture_runtime_available=lambda _path: False,
        environ={},
    )

    assert exit_code == 23
    assert calls == [["npm", "ci"]]


def test_live_serve_delegates_to_same_origin_browser_mlx_runtime(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    token_file = tmp_path / "operator-token"
    state_root = tmp_path / "state"

    exit_code = cli.main(
        [
            "serve",
            "--mode",
            "live",
            "--host",
            "127.0.0.1",
            "--port",
            "8799",
            "--state-root",
            str(state_root),
            "--operator-token-file",
            str(token_file),
        ],
        live_server_main=lambda argv: calls.append(list(argv)) or 17,
        process_runner=lambda *args, **kwargs: pytest.fail("fixture process must not start"),
        environ={},
    )

    assert exit_code == 17
    assert calls == [
        [
            "--host",
            "127.0.0.1",
            "--port",
            "8799",
            "--state-root",
            str(state_root),
            "--operator-token-file",
            str(token_file),
        ]
    ]


def test_live_serve_forwards_optional_https_and_static_arguments(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    exit_code = cli.main(
        [
            "serve",
            "--mode",
            "live",
            "--public-origin",
            "https://demo.example",
            "--static-root",
            str(tmp_path / "static"),
            "--worker-static-root",
            str(tmp_path / "worker-static"),
            "--tls-cert",
            str(tmp_path / "cert.pem"),
            "--tls-key",
            str(tmp_path / "key.pem"),
        ],
        live_server_main=lambda argv: calls.append(list(argv)) or 0,
        environ={},
    )

    assert exit_code == 0
    assert calls == [
        [
            "--host",
            "127.0.0.1",
            "--port",
            "8787",
            "--public-origin",
            "https://demo.example",
            "--static-root",
            str(tmp_path / "static"),
            "--worker-static-root",
            str(tmp_path / "worker-static"),
            "--tls-cert",
            str(tmp_path / "cert.pem"),
            "--tls-key",
            str(tmp_path / "key.pem"),
        ]
    ]


def test_fixture_serve_rejects_live_only_arguments(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "serve",
                "--mode",
                "fixture",
                "--state-root",
                str(tmp_path / "state"),
            ],
            environ={},
        )

    assert exc.value.code == 2


def test_serve_rejects_unknown_mode() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["serve", "--mode", "synthetic-live"], environ={})

    assert exc.value.code == 2
