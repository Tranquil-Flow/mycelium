# SPDX-License-Identifier: AGPL-3.0-only
"""RED->GREEN reachability proof for the two built-but-unlaunched product servers.

This module is deliberately written *before* the wiring exists: while either
server is unreachable from the demo stack it fails loudly (import/launch error),
and it passes only when a real demo-stack bring-up serves both:

* ``mycelium_openai_shim.server`` -- a real HTTP listener, proven with
  ``GET /v1/models`` (and one non-streaming ``POST /v1/chat/completions``);
* ``mycelium_gateway.asgi.ObservatoryASGIApplication`` -- mounted in the product
  stack's ``observatory_app`` slot, proven end-to-end over loopback HTTP at the
  browser route ``/api/v1/observatory/snapshot``.

Scope: loopback only, with the repository's own synthetic qualification fixture
and a simulated route.  No model, no device, no physical route is involved and
none is claimed.  Nothing here weakens an existing assertion: the module is
additive and the existing suites are untouched.
"""

from __future__ import annotations

from contextlib import contextmanager
import http.client
import importlib
import json
from pathlib import Path
import time
from typing import Any, Iterator

import pytest

from mycelium_demo import cli
from mycelium_demo import product_servers as servers_module
from mycelium_demo.product_servers import (
    DEMO_SERVERS_STATUS_PROTOCOL,
    DemoServerWiringError,
    bring_up_demo_product_servers,
    build_stub_demo_stack_inputs,
)
from mycelium_gateway.asgi import ObservatoryASGIApplication

REPO_ROOT = Path(__file__).resolve().parents[2]
OBSERVATORY_PROTOCOL = "mycelium.observatory_stream.v1"

pytestmark = pytest.mark.integration


def _request(
    port: int,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return (
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            response.read(),
        )
    finally:
        connection.close()


def _wait_for_ports(servers: Any) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            status, _headers, _body = _request(servers.openai_shim_port, "GET", "/healthz")
        except OSError:
            time.sleep(0.05)
            continue
        if status == 200:
            return
        time.sleep(0.05)
    raise AssertionError("openai shim listener never became reachable")


@contextmanager
def _servers(tmp_path: Path, **overrides: Any) -> Iterator[Any]:
    inputs = build_stub_demo_stack_inputs()
    inputs.update(overrides.pop("inputs", {}))
    servers = bring_up_demo_product_servers(
        state_root=tmp_path / "demo-state",
        product_port=0,
        openai_shim_port=0,
        **inputs,
        **overrides,
    )
    try:
        _wait_for_ports(servers)
        yield servers
    finally:
        servers.close()


def _browser_session(servers: Any) -> dict[str, str]:
    status, headers, _body = _request(servers.product_port, "GET", "/api/v1/bootstrap")
    assert status == 200
    set_cookie = headers["set-cookie"]
    return {"cookie": set_cookie.split(";", 1)[0]}


def test_demo_stack_serves_the_openai_shim_models_surface(tmp_path: Path) -> None:
    with _servers(tmp_path) as servers:
        status, headers, body = _request(servers.openai_shim_port, "GET", "/v1/models")
        assert status == 200
        assert headers["content-type"].startswith("application/json")
        models = json.loads(body)["data"]
        assert [entry["id"] for entry in models] == [servers.model_id]

        tags_status, _tags_headers, tags_body = _request(
            servers.openai_shim_port, "GET", "/api/tags"
        )
        assert tags_status == 200
        assert json.loads(tags_body)["models"][0]["name"] == servers.model_id


def test_demo_stack_shim_drives_the_route_inference_surface(tmp_path: Path) -> None:
    with _servers(tmp_path) as servers:
        payload = json.dumps(
            {"model": servers.model_id, "messages": [{"role": "user", "content": "alpha beta"}]}
        ).encode("utf-8")
        status, _headers, body = _request(
            servers.openai_shim_port,
            "POST",
            "/v1/chat/completions",
            headers={"content-type": "application/json"},
            body=payload,
        )
        assert status == 200
        document = json.loads(body)
        assert document["object"] == "chat.completion"
        assert document["choices"][0]["message"]["role"] == "assistant"
        assert len(servers.live_route.inference_calls) == 1


def test_demo_stack_serves_the_observatory_snapshot_through_the_product_gateway(
    tmp_path: Path,
) -> None:
    with _servers(tmp_path) as servers:
        assert isinstance(servers.observatory_app, ObservatoryASGIApplication)
        assert servers.observatory_app is servers.observatory_runtime.app

        session = _browser_session(servers)
        status, headers, body = _request(
            servers.product_port,
            "GET",
            "/api/v1/observatory/snapshot",
            headers=session,
        )
        assert status == 200
        assert headers["content-type"].startswith("application/json")
        envelope = json.loads(body)
        assert envelope["protocol"] == OBSERVATORY_PROTOCOL
        assert envelope["generation"] >= 1
        assert set(envelope["bundle"]) == {"snapshot", "incidents", "provisioning"}
        assert envelope["bundle"]["provisioning"]["route_ready"] is False


def test_demo_stack_observatory_slot_is_populated_by_the_real_gateway_application(
    tmp_path: Path,
) -> None:
    """No dead parameter: the composed product stack holds the real ASGI app."""

    module = importlib.import_module("mycelium_gateway.asgi")
    assert ObservatoryASGIApplication is getattr(module, "ObservatoryASGIApplication")

    with _servers(tmp_path) as servers:
        composed = servers.product_app
        assert composed is not None
        assert servers.product_observatory_slot() is servers.observatory_app
        # The endpoint is served by that application, not by a stub: a fresh
        # generation is allocated on every snapshot read from the publisher.
        first = json.loads(
            _request(
                servers.product_port,
                "GET",
                "/api/v1/observatory/snapshot",
                headers=_browser_session(servers),
            )[2]
        )
        server_direct = _request(
            servers.product_port, "GET", "/api/v1/observatory/snapshot"
        )
        assert first["generation"] >= 1
        # Without a browser session the product gateway refuses before upstream.
        assert server_direct[0] == 401


def test_demo_stack_fails_closed_when_the_observatory_module_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        servers_module,
        "_load_observatory_gateway",
        _raise_unreachable_observatory,
        raising=True,
    )
    with pytest.raises(ImportError, match="mycelium_gateway"):
        bring_up_demo_product_servers(
            state_root=tmp_path / "unreachable-observatory",
            **build_stub_demo_stack_inputs(),
        )


def test_demo_stack_fails_closed_when_the_openai_shim_module_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        servers_module,
        "_load_openai_shim_serve_in_thread",
        _raise_unreachable_shim,
        raising=True,
    )
    with pytest.raises(ImportError, match="mycelium_openai_shim"):
        bring_up_demo_product_servers(
            state_root=tmp_path / "unreachable-shim",
            **build_stub_demo_stack_inputs(),
        )


def _raise_unreachable_observatory(*_args: Any, **_kwargs: Any) -> Any:
    raise ImportError("mycelium_gateway.asgi is not reachable from the demo stack")


def _raise_unreachable_shim() -> Any:
    raise ImportError("mycelium_openai_shim.server is not reachable from the demo stack")


def test_demo_stack_cli_action_composes_both_servers(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    class _FakeServers:
        product_port = 8798
        openai_shim_port = 8791

        def status(self) -> dict[str, Any]:
            return {DEMO_SERVERS_STATUS_PROTOCOL: True, "ports": {"openai_shim": 8791}}

        def close(self) -> None:
            calls.append({"closed": True})

        def __enter__(self) -> "_FakeServers":
            return self

        def __exit__(self, *_exc: Any) -> None:
            self.close()

    def bringup(**kwargs: Any) -> _FakeServers:
        calls.append(dict(kwargs))
        return _FakeServers()

    def inputs_factory() -> dict[str, Any]:
        inputs = build_stub_demo_stack_inputs()
        inputs["request_bearer_token"] = "stub-token"
        return inputs

    exit_code = cli.main(
        [
            "demo-stack",
            "--port",
            "8798",
            "--openai-shim-port",
            "8791",
            "--state-root",
            str(tmp_path / "cli-state"),
            "--status-only",
        ],
        demo_stack_bringup=bringup,
        demo_stack_inputs_factory=inputs_factory,
        environ={"MYCELIUM_DEMO_REQUEST_BEARER_TOKEN": "env-token"},
    )

    assert exit_code == 0
    assert calls[0]["product_port"] == 8798
    assert calls[0]["openai_shim_port"] == 8791
    assert calls[0]["state_root"] == tmp_path / "cli-state"
    assert calls[0]["product_host"] == "127.0.0.1"
    assert calls[0]["openai_shim_host"] == "127.0.0.1"
    # The credential is only ever read from the environment, never from argv.
    assert calls[0]["request_bearer_token"] == "env-token"
    assert calls[0]["qualification_source"] is not None
    assert calls[0]["live_route"] is not None
    assert calls[0]["swarm_coordinator"] is not None
    assert calls[-1] == {"closed": True}


def test_demo_stack_status_document_names_both_endpoints(tmp_path: Path) -> None:
    with _servers(tmp_path) as servers:
        status = servers.status()
        assert status["protocol"] == DEMO_SERVERS_STATUS_PROTOCOL
        endpoints = "\n".join(status["endpoints"])
        assert f":{servers.openai_shim_port}/v1/models" in endpoints
        assert f":{servers.product_port}/api/v1/observatory/snapshot" in endpoints
        assert status["observatory"]["gateway_application"].endswith(
            "ObservatoryASGIApplication"
        )
        assert status["observatory"]["upstream_snapshot_path"] == "/v1/observatory/snapshot"
        assert status["claim_boundary"].startswith("stub demo")
        assert status["live_host_mutations"] == 0


def test_demo_stack_bring_up_rejects_a_non_loopback_host(tmp_path: Path) -> None:
    with pytest.raises(DemoServerWiringError, match="non_loopback"):
        bring_up_demo_product_servers(
            state_root=tmp_path / "host-guard",
            product_host="0.0.0.0",
            **build_stub_demo_stack_inputs(),
        )


def test_demo_stack_reuses_the_repository_qualification_fixture() -> None:
    """The demo stack is explicit that its qualification is synthetic."""

    inputs = build_stub_demo_stack_inputs()
    qualification = inputs["qualification_source"].current()
    assert qualification is not None
    assert qualification.route_ready is True
    # The fixture renders the *hypothetical* physical shape from synthetic,
    # test-only inputs and test-only signature verifiers; the demo therefore
    # publishes no ready route projection in its observatory bundle.
    assert qualification.evidence_class == "physical_qualification"
    bundle = servers_module.demo_observatory_bundle()
    assert bundle["snapshot"]["qualification"] is None
    assert bundle["provisioning"]["route_ready"] is False
    coordinator_status = inputs["swarm_coordinator"].status()
    assert coordinator_status["protocol"] == "mycelium.product_ui.swarm.v1"
    assert coordinator_status["native_nodes"] == []
    assert coordinator_status["browser_workers"] == []
    assert isinstance(inputs["request_bearer_token"], str)
    assert inputs["request_bearer_token"]


def test_demo_stack_close_stops_both_listeners(tmp_path: Path) -> None:
    servers = bring_up_demo_product_servers(
        state_root=tmp_path / "closing",
        **build_stub_demo_stack_inputs(),
    )
    try:
        _wait_for_ports(servers)
        shim_port = servers.openai_shim_port
        product_port = servers.product_port
    finally:
        servers.close()
    with pytest.raises(OSError):
        _request(shim_port, "GET", "/v1/models", timeout=1.0)
    with pytest.raises(OSError):
        _request(product_port, "GET", "/api/v1/bootstrap", timeout=1.0)
