# SPDX-License-Identifier: AGPL-3.0-only
"""The seed behind a trusted HTTPS proxy (Cloudflare tunnel) advertises https.

An https advertised URL is accepted only with an explicit
``--trusted-https-proxy`` and a loopback bind, mirroring the demo's
``--public-origin``/``--trusted-https-proxy`` pair. The plain-http behaviour is
unchanged.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import selectors
import subprocess
import sys
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest


def _run(tmp_path: Path, name: str, arguments: list[str]) -> int:
    seed_main = importlib.import_module("mycelium_seed.__main__")
    data_dir = tmp_path / name
    data_dir.mkdir(mode=0o700)
    return seed_main.run([*arguments, "--data-dir", str(data_dir), "--dry-run"])


def _refused(tmp_path: Path, name: str, arguments: list[str]) -> None:
    seed_main = importlib.import_module("mycelium_seed.__main__")
    with pytest.raises(seed_main._EntrypointFailure) as caught:
        _run(tmp_path, name, arguments)
    assert (caught.value.code, caught.value.exit_status) == (
        "seed_preflight_failed",
        2,
    )


@pytest.mark.parametrize("bind", ["127.0.0.1", "::1", "localhost"])
def test_https_advertised_url_with_trusted_proxy_and_loopback_bind_is_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], bind: str
) -> None:
    arguments = [
        "--bind",
        bind,
        "--port",
        "18865",
        "--advertised-url",
        "https://seed.mycelium.now",
        "--trusted-https-proxy",
    ]
    assert _run(tmp_path, bind.replace(":", "v6"), arguments) == 0
    capsys.readouterr()


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        (
            "https-without-flag",
            ["--bind", "127.0.0.1", "--port", "18865", "--advertised-url", "https://seed.mycelium.now"],
        ),
        (
            "flag-with-http",
            [
                "--bind",
                "127.0.0.1",
                "--port",
                "18865",
                "--advertised-url",
                "http://127.0.0.1:18865",
                "--trusted-https-proxy",
            ],
        ),
        (
            "flag-without-url",
            ["--bind", "127.0.0.1", "--port", "18865", "--trusted-https-proxy"],
        ),
        (
            "flag-wildcard-bind",
            [
                "--bind",
                "0.0.0.0",
                "--port",
                "18865",
                "--advertised-url",
                "https://seed.mycelium.now",
                "--trusted-https-proxy",
            ],
        ),
        (
            "flag-tailnet-bind",
            [
                "--bind",
                "203.0.113.13",
                "--port",
                "18865",
                "--advertised-url",
                "https://seed.mycelium.now",
                "--trusted-https-proxy",
            ],
        ),
        (
            "https-with-path",
            [
                "--bind",
                "127.0.0.1",
                "--port",
                "18865",
                "--advertised-url",
                "https://seed.mycelium.now/seed",
                "--trusted-https-proxy",
            ],
        ),
        (
            "https-port-zero",
            [
                "--bind",
                "127.0.0.1",
                "--port",
                "18865",
                "--advertised-url",
                "https://seed.mycelium.now:0",
                "--trusted-https-proxy",
            ],
        ),
    ],
)
def test_https_and_trusted_proxy_refusals(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    arguments: list[str],
) -> None:
    _refused(tmp_path, name, arguments)
    capsys.readouterr()


def test_plain_http_advertised_url_is_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = [
        "--bind",
        "127.0.0.1",
        "--port",
        "18865",
        "--advertised-url",
        "http://127.0.0.1:18865",
    ]
    assert _run(tmp_path, "plain", arguments) == 0
    _refused(
        tmp_path,
        "plain-mismatch",
        ["--bind", "127.0.0.1", "--port", "18865", "--advertised-url", "http://127.0.0.1:18866"],
    )
    capsys.readouterr()


def _read_line(process: subprocess.Popen[str]) -> dict[str, Any]:
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        assert selector.select(timeout=10), f"no status; returncode={process.poll()}"
        line = process.stdout.readline()
    finally:
        selector.close()
    assert line
    return json.loads(line)


def test_trusted_proxy_seed_binds_https_url_and_enforces_public_boundary(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "seed"
    base = [sys.executable, "-m", "mycelium_seed", "--bind", "127.0.0.1", "--port", "0", "--data-dir", str(data_dir)]
    initialized = subprocess.run(
        [*base, "--init-only"], capture_output=True, text=True, timeout=10, check=False
    )
    assert initialized.returncode == 0, initialized.stderr
    process = subprocess.Popen(
        [*base, "--advertised-url", "https://seed.example.test", "--trusted-https-proxy"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        status = _read_line(process)
        assert status["event"] == "seed_started"
        assert status["seed_url"] == "https://seed.example.test"
        local = status["local_url"]
        assert local.startswith("http://127.0.0.1:")

        request = Request(local + "/seed/identity", headers={"X-Forwarded-Proto": "https"})
        with urlopen(request, timeout=2) as response:
            identity = json.loads(response.read())
        assert identity["statement"]["seed_endpoint_id"] == status["seed_endpoint_id"]
        assert identity["statement"]["seed_url"] == "https://seed.example.test"

        downgraded = Request(local + "/seed/identity", headers={"X-Forwarded-Proto": "http"})
        with pytest.raises(HTTPError) as caught:
            urlopen(downgraded, timeout=2)
        assert caught.value.code >= 400
    finally:
        process.terminate()
        process.communicate(timeout=5)
    assert process.returncode == 0
