# SPDX-License-Identifier: AGPL-3.0-only
"""Loopback end-to-end proof for the gateway + control-origin runtime.

Scope: this module proves the *composition* -- an origin actually listens, the
gateway admits a prompt, token events leave the socket as SSE, the control-origin
mailbox answers on its own port, and a dead route returns the named cold-route
error instead of hanging.  It uses the stub/echo backend only: no model and no
physical qualification are involved, and nothing outside loopback is touched.
"""

from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
import os
import re
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterator

import pytest

from mycelium_demo import gateway_control_runtime as runtime_module
from mycelium_demo.gateway_control_runtime import (
    DEFAULT_PUBLIC_ORIGIN_PORT,
    ENV_PUBLIC_ORIGIN_PORT,
    ENV_REQUEST_BEARER_TOKEN,
    GATEWAY_INFERENCE_PATH,
    PUBLIC_INFERENCE_PATH,
    EchoStubBackend,
    GatewayControlRuntime,
    GatewayControlRuntimeError,
    bearer_token_from_environment,
    build_parser,
    map_public_path_to_gateway_path,
)
from mycelium_live.supervisor import COLD_ROUTE_RESTART_ERROR_CODE
from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT = "alpha beta gamma"
CAPABILITY = "b" * 64
DIAGNOSTIC_TOKEN = "loopback-diagnostic-token"

pytestmark = pytest.mark.integration


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def _runtime(**overrides: Any) -> Iterator[GatewayControlRuntime]:
    # Never let a test write runtime state into the developer's $HOME: the
    # control origin creates a key file and a SQLite mailbox under `state_root`.
    private_state = "state_root" not in overrides
    state_root = overrides.get("state_root")
    if private_state:
        state_root = Path(tempfile.mkdtemp(prefix="m1-gateway-runtime-test-"))
        overrides["state_root"] = state_root
    arguments: dict[str, Any] = {
        "public_origin_port": _free_loopback_port(),
        "gateway_upstream_port": 0,
        "control_origin_port": 0,
        "capability": CAPABILITY,
        "request_bearer_token": DIAGNOSTIC_TOKEN,
    }
    arguments.update(overrides)
    runtime = GatewayControlRuntime(**arguments)
    runtime.start()
    try:
        yield runtime
    finally:
        runtime.stop()
        if private_state and state_root is not None:
            shutil.rmtree(state_root, ignore_errors=True)


def _request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        headers_out = {
            name.lower(): value for name, value in response.getheaders()
        }
        return response.status, headers_out, response.read()
    finally:
        connection.close()


def _submission_body(runtime: GatewayControlRuntime, *, max_new_tokens: int = 3) -> bytes:
    submission = InferenceSubmission(
        prompt=PROMPT,
        max_new_tokens=max_new_tokens,
        qualification=qualification_binding(runtime.qualification),
    )
    return json.dumps(submission.to_dict(), sort_keys=True).encode("utf-8")


def _submit_through_public_origin(
    runtime: GatewayControlRuntime, *, max_new_tokens: int = 3
) -> tuple[int, dict[str, Any]]:
    status, _headers, body = _request(
        runtime.public_origin_port,
        "POST",
        "/api/v1/inference",
        body=_submission_body(runtime, max_new_tokens=max_new_tokens),
        headers={
            "Host": runtime.public_host,
            "Content-Type": "application/json",
        },
    )
    return status, json.loads(body.decode("utf-8"))


class _SSEReader:
    """Reads SSE blocks straight off a loopback socket, one block at a time."""

    def __init__(
        self,
        port: int,
        request_id: str,
        *,
        host: str,
        session_token: str,
        authorization: str | None = None,
        inference_prefix: str = PUBLIC_INFERENCE_PATH,
    ) -> None:
        self._socket = socket.create_connection(("127.0.0.1", port), timeout=15.0)
        self._socket.settimeout(15.0)
        authorization_line = (
            "" if authorization is None else f"Authorization: {authorization}\r\n"
        )
        request = (
            f"GET {inference_prefix}/{request_id}/events HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Accept: text/event-stream\r\n"
            f"X-Mycelium-Session: {session_token}\r\n"
            f"{authorization_line}"
            "\r\n"
        )
        self._socket.sendall(request.encode("ascii"))
        self._buffer = b""
        self.status_line = ""
        self.headers: dict[str, str] = {}

    def __enter__(self) -> "_SSEReader":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass

    def read_headers(self, *, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while b"\r\n\r\n" not in self._buffer:
            assert time.monotonic() < deadline, "no response headers from the origin"
            chunk = self._socket.recv(4096)
            assert chunk, "origin closed before response headers"
            self._buffer += chunk
        head, _, rest = self._buffer.partition(b"\r\n\r\n")
        self._buffer = rest
        lines = head.decode("latin-1").split("\r\n")
        self.status_line = lines[0]
        for line in lines[1:]:
            name, _, value = line.partition(":")
            self.headers[name.strip().lower()] = value.strip()

    def next_block(self, *, timeout: float = 10.0) -> str | None:
        deadline = time.monotonic() + timeout
        while True:
            index = self._buffer.find(b"\n\n")
            if index == -1:
                if time.monotonic() > deadline:
                    return None
                try:
                    chunk = self._socket.recv(4096)
                except (socket.timeout, TimeoutError):
                    return None
                if not chunk:
                    return None
                self._buffer += chunk
                continue
            raw = self._buffer[:index].decode("utf-8")
            self._buffer = self._buffer[index + 2 :]
            if raw.strip():
                return raw

    def next_event(self, *, timeout: float = 10.0) -> dict[str, Any] | None:
        while True:
            block = self.next_block(timeout=timeout)
            if block is None:
                return None
            event: dict[str, Any] = {"raw": block}
            for line in block.splitlines():
                if line.startswith("id:"):
                    event["id"] = line[3:].lstrip()
                elif line.startswith("event:"):
                    event["event"] = line[6:].lstrip()
                elif line.startswith("data:"):
                    event["data"] = json.loads(line[5:].lstrip())
            if "event" in event:
                return event


def test_public_origin_serves_a_prompt_and_streams_sse_token_events() -> None:
    hold_release = threading.Event()
    backend = EchoStubBackend(hold_after_tokens=1, hold_release=hold_release)
    with _runtime(backend=backend) as runtime:
        status, accepted = _submit_through_public_origin(runtime)
        assert status == 202
        request_id = accepted["request_id"]
        assert accepted["stream_path"] == f"/v1/inference/{request_id}/events"
        assert accepted["session_token"]

        with _SSEReader(
            runtime.public_origin_port,
            request_id,
            host=runtime.public_host,
            session_token=accepted["session_token"],
        ) as reader:
            reader.read_headers()
            assert "200" in reader.status_line
            assert reader.headers["content-type"] == "text/event-stream; charset=utf-8"

            first = reader.next_event()
            assert first is not None and first["event"] == "accepted"

            # The stream is live: a token event arrives while the backend is
            # still held, so the origin is not buffering the response.
            assert backend.started.wait(timeout=5.0)
            token = reader.next_event()
            assert token is not None and token["event"] == "token"
            assert token["data"]["text"] == "alpha "
            assert backend.emitted_tokens == 1
            assert not hold_release.is_set()

            hold_release.set()
            rest = []
            while True:
                event = reader.next_event(timeout=10.0)
                if event is None:
                    break
                rest.append(event)
                if event["event"] == "completed":
                    break
            assert [item["event"] for item in rest] == ["token", "token", "completed"]
            assert [item["data"]["text"] for item in rest[:2]] == ["beta ", "gamma"]
            assert [item["data"]["sequence"] for item in [first, token, *rest]] == [0, 1, 2, 3, 4]


def test_gateway_upstream_session_api_requires_authentication_and_streams_tokens() -> None:
    """The gateway's own ``/v1`` session-API wire surface, on its loopback port."""

    with _runtime() as runtime:
        gateway = runtime.gateway_server.port
        assert _request(gateway, "GET", "/v1/qualification/current")[0] == 401

        status, _headers, body = _request(
            gateway,
            "GET",
            "/v1/qualification/current",
            headers={"Authorization": f"Bearer {DIAGNOSTIC_TOKEN}"},
        )
        assert status == 200
        projection = json.loads(body.decode("utf-8"))
        assert projection["route_ready"] is True
        assert projection["binding"]["qualification_id"] == runtime.qualification.qualification_id

        status, _headers, body = _request(
            gateway,
            "POST",
            "/v1/inference",
            body=_submission_body(runtime, max_new_tokens=2),
            headers={
                "Authorization": f"Bearer {DIAGNOSTIC_TOKEN}",
                "Content-Type": "application/json",
            },
        )
        assert status == 202
        accepted = json.loads(body.decode("utf-8"))

        with _SSEReader(
            gateway,
            accepted["request_id"],
            host="127.0.0.1",
            session_token=accepted["session_token"],
            authorization=f"Bearer {DIAGNOSTIC_TOKEN}",
            inference_prefix=GATEWAY_INFERENCE_PATH,
        ) as reader:
            reader.read_headers()
            assert reader.status_line.startswith("HTTP/1.1 200")
            kinds: list[str] = []
            texts: list[str] = []
            while True:
                event = reader.next_event()
                if event is None:
                    break
                kinds.append(event["event"])
                if event["event"] == "token":
                    texts.append(event["data"]["text"])
                if event["event"] == "completed":
                    break
        assert kinds == ["accepted", "token", "token", "completed"]
        assert texts == ["alpha ", "beta "]


def test_repo_http_client_streams_an_owner_locked_session() -> None:
    """The CLI client can now use a locked submit->stream pair end to end.

    ``mycelium_request_gateway.service`` locks each session to the token its
    ``session_token_source`` minted (``_require_session_owner``, reached from both
    ``subscribe()`` and ``cancel()``), and ``asgi.py`` requires that token on the
    ``/events`` subscription and on ``DELETE /v1/inference/{request_id}`` --
    ``_session_token`` reads exactly the ``x-mycelium-session`` header.
    ``HTTPGatewayClient`` captures the ``session_token`` the 202 acceptance document
    carries and echoes it as ``X-Mycelium-Session`` on both follow-up requests,
    mirroring ``mycelium_ui_gateway/app.py``.

    The ``stranger`` arm is the negative control, and the reason this test is RED
    whenever the client stops sending the header: the same request streamed (and
    cancelled) by a client that never saw the acceptance document is refused with
    HTTP 409 ``session_owner_mismatch`` -- the exact response the unrepaired client
    produced.
    """

    from mycelium_request_gateway.client import GatewayClientError, HTTPGatewayClient

    with _runtime() as runtime:
        client = HTTPGatewayClient(
            base_url=runtime.gateway_base_url,
            bearer_token=DIAGNOSTIC_TOKEN,
        )
        request_id = client.submit(
            InferenceSubmission(
                prompt=PROMPT,
                max_new_tokens=2,
                qualification=client.current_qualification(),
            )
        )
        kinds: list[str] = []
        texts: list[str] = []
        for event in client.events(request_id):
            kinds.append(event.kind)
            if event.kind == "token":
                assert event.text is not None
                texts.append(event.text)
            if event.kind == "completed":
                break
        assert kinds == ["accepted", "token", "token", "completed"]
        assert texts == ["alpha ", "beta "]
        # The owner's DELETE is admitted (the session is already terminal, so
        # nothing is actually cancelled); the token-less client is refused.
        assert client.cancel(request_id) is False

        stranger = HTTPGatewayClient(
            base_url=runtime.gateway_base_url,
            bearer_token=DIAGNOSTIC_TOKEN,
        )
        with pytest.raises(GatewayClientError) as refused:
            list(stranger.events(request_id))
        assert refused.value.code == "session_owner_mismatch"
        with pytest.raises(GatewayClientError) as refused_cancel:
            stranger.cancel(request_id)
        assert refused_cancel.value.code == "session_owner_mismatch"


def test_owner_locked_session_still_rejects_a_stream_that_omits_the_session_token() -> None:
    """Gateway-side half of the former section 6 finding: owner scoping is not relaxed.

    The finding recorded here before the repair -- ``HTTPGatewayClient._headers``
    sending only ``Authorization`` -- was a defect report, not a contract, and the
    client now echoes the owner token.  What stays pinned is the property the repair
    must not touch: a subscriber that does not present the token minted for the
    session is refused before a single event is streamed.  It is asserted through the
    real client (a second instance that never saw the acceptance document), so it goes
    RED if the gateway stops enforcing the owner digest -- or if the client were ever
    "fixed" by inventing or skipping a token instead of echoing the one it was given.
    """

    from mycelium_request_gateway.client import GatewayClientError, HTTPGatewayClient

    with _runtime() as runtime:
        submitter = HTTPGatewayClient(
            base_url=runtime.gateway_base_url,
            bearer_token=DIAGNOSTIC_TOKEN,
        )
        request_id = submitter.submit(
            InferenceSubmission(
                prompt=PROMPT,
                max_new_tokens=2,
                qualification=submitter.current_qualification(),
            )
        )
        stranger = HTTPGatewayClient(
            base_url=runtime.gateway_base_url,
            bearer_token=DIAGNOSTIC_TOKEN,
        )
        with pytest.raises(GatewayClientError) as caught:
            list(stranger.events(request_id))
        assert caught.value.code == "session_owner_mismatch"


def test_control_origin_mailbox_listens_on_its_own_loopback_port(tmp_path: Path) -> None:
    with _runtime(state_root=tmp_path) as runtime:
        assert runtime.control_origin is not None
        assert runtime.control_origin.port != runtime.public_origin_port
        assert runtime.control_origin.port != runtime.gateway_server.port

        status, headers, body = _request(
            runtime.control_origin.port,
            "GET",
            "/healthz",
            headers={"Host": "127.0.0.1"},
        )
        assert status == 200
        document = json.loads(body.decode("utf-8"))
        assert document == {
            "ok": True,
            "protocol": "mycelium.managed_control.origin_health.v1",
            "service": "managed-control-origin",
        }
        assert headers["content-type"] == "application/json"

        status, _headers, body = _request(
            runtime.control_origin.port,
            "POST",
            "/managed-control/enqueue",
            body=b"{}",
            headers={"Host": runtime.control_public_host, "Content-Type": "application/json"},
        )
        assert status in {400, 401}
        assert json.loads(body.decode("utf-8"))["error"]["code"].startswith(
            "managed_control_"
        )


def test_dead_route_returns_the_named_cold_route_error_and_successor_serves() -> None:
    with _runtime() as runtime:
        assert runtime.cold_route_gate.route_ready() is True
        generation_before = runtime.route_owner.public_status()["cold_route_restart"][
            "generation"
        ]

        runtime.kill_current_route()
        started = time.monotonic()
        status, document = _submit_through_public_origin(runtime)
        elapsed = time.monotonic() - started

        assert status == 503
        assert document["error"] == COLD_ROUTE_RESTART_ERROR_CODE
        assert document["code"] == COLD_ROUTE_RESTART_ERROR_CODE
        assert document["retryable"] is True
        assert elapsed < 5.0, "a dead route must not hang the request"
        assert document["cold_route_restart"]["attempts"] == 1

        projection = runtime.route_owner.public_status()
        assert projection["cold_route_restart"]["generation"] == generation_before + 1
        assert runtime.cold_route_gate.route_ready() is True
        assert len(runtime.routes) == 2
        assert runtime.routes[0].closed == 1

        # The replacement route serves the very next request.
        status, accepted = _submit_through_public_origin(runtime, max_new_tokens=2)
        assert status == 202
        with _SSEReader(
            runtime.public_origin_port,
            accepted["request_id"],
            host=runtime.public_host,
            session_token=accepted["session_token"],
        ) as reader:
            reader.read_headers()
            kinds = []
            while True:
                event = reader.next_event()
                if event is None:
                    break
                kinds.append(event["event"])
                if event["event"] == "completed":
                    break
        assert kinds == ["accepted", "token", "token", "completed"]


def test_ports_are_arguments_and_no_live_host_port_is_touched() -> None:
    public_port = _free_loopback_port()
    control_port = _free_loopback_port()
    with _runtime(public_origin_port=public_port, control_origin_port=control_port) as runtime:
        status = runtime.status()
        assert status["ports"] == {
            "public_origin": public_port,
            "gateway_upstream": runtime.gateway_server.port,
            "control_origin": control_port,
        }
        assert status["live_host_mutations"] == 0
        # 4351 is only a default: the caller's port is what is bound.
        assert public_port != DEFAULT_PUBLIC_ORIGIN_PORT or runtime.public_origin_port == public_port
        assert runtime.control_origin is not None
        assert runtime.control_origin.port == control_port
        assert runtime.public_origin_port == public_port
        assert _request(public_port, "GET", "/api/v1/qualification/current", headers={"Host": runtime.public_host})[0] == 200


def test_public_path_mapping_is_explicit() -> None:
    assert map_public_path_to_gateway_path("/api/v1/inference") == "/v1/inference"
    assert (
        map_public_path_to_gateway_path("/api/v1/inference/abc/events")
        == "/v1/inference/abc/events"
    )
    assert (
        map_public_path_to_gateway_path("/api/v1/inference/abc/cancel")
        == "/v1/inference/abc"
    )
    assert (
        map_public_path_to_gateway_path("/api/v1/qualification/current")
        == "/v1/qualification/current"
    )
    assert map_public_path_to_gateway_path("/api/v1/swarm/status") is None
    assert map_public_path_to_gateway_path("/healthz") is None


def test_runtime_rejects_a_non_loopback_or_unusable_configuration(tmp_path: Path) -> None:
    with pytest.raises(GatewayControlRuntimeError) as caught:
        GatewayControlRuntime(public_origin_port=70000, capability=CAPABILITY)
    assert caught.value.code == "invalid_public_origin_port"

    with pytest.raises(GatewayControlRuntimeError) as caught:
        GatewayControlRuntime(
            public_origin_port=_free_loopback_port(),
            capability=CAPABILITY,
            capability_file=tmp_path / "also-a-file",
        )
    assert caught.value.code == "capability_ambiguous"


def test_cli_defaults_read_the_port_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_PUBLIC_ORIGIN_PORT, "5123")
    parser = build_parser()
    assert parser.parse_args([]).public_port == 5123
    assert parser.parse_args(["--public-port", "6123"]).public_port == 6123
    monkeypatch.setenv(ENV_PUBLIC_ORIGIN_PORT, "not-a-port")
    with pytest.raises(GatewayControlRuntimeError) as caught:
        build_parser()
    assert caught.value.code == f"invalid_env_port:{ENV_PUBLIC_ORIGIN_PORT}"


def test_cli_entry_point_serves_end_to_end_on_loopback(tmp_path: Path) -> None:
    public_port = _free_loopback_port()
    control_port = _free_loopback_port()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mycelium_demo.gateway_control_runtime",
            "--public-port",
            str(public_port),
            "--control-port",
            str(control_port),
            "--state-root",
            str(tmp_path),
            "--public-host",
            "mycelium.now",
        ],
        cwd=str(REPO_ROOT),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready_line = process.stdout.readline() if process.stdout else ""
        assert ready_line, "the entry point did not report its ports"
        ready = json.loads(ready_line)
        assert ready["protocol"] == "mycelium.gateway_control_runtime.ready.v1"
        assert ready["ports"]["public_origin"] == public_port
        assert ready["ports"]["control_origin"] == control_port
        assert ready["live_host_mutations"] == 0

        # The child process is the server; the test only needs the same stub
        # qualification to build one admissible submission body.
        qualification = runtime_module.load_stub_qualification()
        body = json.dumps(
            InferenceSubmission(
                prompt=PROMPT,
                max_new_tokens=3,
                qualification=qualification_binding(qualification),
            ).to_dict(),
            sort_keys=True,
        ).encode("utf-8")
        status, _headers, payload = _request(
            public_port,
            "POST",
            "/api/v1/inference",
            body=body,
            headers={"Host": "mycelium.now", "Content-Type": "application/json"},
        )
        assert status == 202
        accepted = json.loads(payload.decode("utf-8"))
        with _SSEReader(
            public_port,
            accepted["request_id"],
            host="mycelium.now",
            session_token=accepted["session_token"],
        ) as reader:
            reader.read_headers()
            kinds = []
            while True:
                event = reader.next_event()
                if event is None:
                    break
                kinds.append(event["event"])
                if event["event"] == "completed":
                    break
        assert kinds == ["accepted", "token", "token", "token", "completed"]
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()
            process.wait(timeout=10)


# --------------------------------------------------------------------------- #
# Credential provisioning: no secret value may cross the argv boundary
# --------------------------------------------------------------------------- #
def _child_environment(**overrides: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.update(overrides)
    return environment


def _secret_bearing_options() -> list[str]:
    """Option strings the release security audit itself would flag.

    The rule is imported, not re-implemented, so this test cannot drift from
    ``scripts/release_security_audit.py``: an option is secret-bearing unless it
    references its value (``-env``/``-file``/``-path``/``-ref``/``-fd``).
    """

    from scripts.release_security_audit import _secret_option

    return [
        option
        for action in build_parser()._actions
        for option in action.option_strings
        if _secret_option(option)
    ]


def test_cli_declares_no_secret_bearing_argument() -> None:
    parser = build_parser()

    assert _secret_bearing_options() == []
    assert parser.parse_args([]).request_bearer_token_env == ENV_REQUEST_BEARER_TOKEN
    assert (
        parser.parse_args(["--request-bearer-token-env", "OTHER_TOKEN_VAR"]).request_bearer_token_env
        == "OTHER_TOKEN_VAR"
    )
    # The removed shape is not silently re-added under the same name.
    with pytest.raises(SystemExit):
        parser.parse_args(["--request-bearer-token", DIAGNOSTIC_TOKEN])


def test_bearer_credential_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_REQUEST_BEARER_TOKEN, raising=False)
    assert bearer_token_from_environment() is None
    assert bearer_token_from_environment("SOME_UNSET_VARIABLE") is None

    monkeypatch.setenv(ENV_REQUEST_BEARER_TOKEN, DIAGNOSTIC_TOKEN)
    assert bearer_token_from_environment() == DIAGNOSTIC_TOKEN

    monkeypatch.setenv("ALTERNATE_TOKEN_VARIABLE", "alternate-credential")
    assert bearer_token_from_environment("ALTERNATE_TOKEN_VARIABLE") == "alternate-credential"


def test_module_help_exposes_no_secret_bearing_argument() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "mycelium_demo.gateway_control_runtime", "--help"],
        cwd=str(REPO_ROOT),
        env=_child_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--request-bearer-token-env" in completed.stdout
    for option in re.findall(r"--[a-z0-9-]+", completed.stdout):
        assert option.endswith("-env") or not option.startswith(
            "--request-bearer-token"
        ), f"the help still advertises a secret-bearing argument: {option}"

    from scripts.release_security_audit import audit_repository

    result = audit_repository(REPO_ROOT)
    assert [
        finding
        for finding in result["findings"]
        if finding["code"] == "secret_cli_argument"
        and finding["path"] == "mycelium_demo/gateway_control_runtime.py"
    ] == []


def test_cli_provisions_the_bearer_credential_from_the_named_environment_variable(
    tmp_path: Path,
) -> None:
    """The credential works end to end while never appearing in argv."""

    public_port = _free_loopback_port()
    control_port = _free_loopback_port()
    credential = "env-provisioned-credential"
    argv = [
        sys.executable,
        "-m",
        "mycelium_demo.gateway_control_runtime",
        "--public-port",
        str(public_port),
        "--control-port",
        str(control_port),
        "--state-root",
        str(tmp_path),
        "--request-bearer-token-env",
        "M1_TEST_GATEWAY_TOKEN",
    ]
    process = subprocess.Popen(
        argv,
        cwd=str(REPO_ROOT),
        env=_child_environment(M1_TEST_GATEWAY_TOKEN=credential),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready_line = process.stdout.readline() if process.stdout else ""
        assert ready_line, "the entry point did not report its ports"
        gateway = json.loads(ready_line)["ports"]["gateway_upstream"]

        # The credential itself never crossed the argv boundary.
        assert credential not in " ".join(argv)

        # Without the credential the gateway's own surface denies (401)...
        assert _request(gateway, "GET", "/v1/qualification/current")[0] == 401

        # ...and the credential provisioned through the environment admits it.
        status, _headers, body = _request(
            gateway,
            "GET",
            "/v1/qualification/current",
            headers={"Authorization": f"Bearer {credential}"},
        )
        assert status == 200
        assert json.loads(body.decode("utf-8"))["route_ready"] is True
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()
            process.wait(timeout=10)
