# SPDX-License-Identifier: AGPL-3.0-only
"""Reachability proof for the two product servers from the *live* composition.

Apex composition point: :func:`mycelium_live.supervisor.build_live_stack`
(and :func:`mycelium_live.supervisor.build_registry_stack`), which is what
``python -m mycelium_demo serve --mode live`` composes.  Before this wiring the
live stack mounted an ad-hoc ``LiveObservatoryApplication`` stand-in in the
``observatory_app`` slot and launched no OpenAI-compatible surface at all.

What "reachable from the live path" means here:

* ``mycelium_gateway`` -- the live composition builds the real gateway
  (``mycelium_gateway.live_projection`` -> ``mycelium_gateway.asgi``) and mounts
  its ASGI application in the product stack's ``observatory_app`` slot, fed by
  the live route qualification through the durable publisher.  If the module is
  not importable, :func:`build_live_stack` fails loudly (no silent stand-in).
* ``mycelium_openai_shim`` -- :func:`mycelium_live.supervisor.start_live_openai_shim`
  is what the live server bring-up (``run_physical_server``) calls; it launches a
  real HTTP listener driving ``route.infer``.  If the module is not importable,
  live bring-up fails loudly instead of serving without the OpenAI surface.

Scope: loopback only, repository-synthetic qualification fixtures and simulated
routes.  No model, device, physical route, or browser is involved here and none
is claimed.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import http.client
import importlib
import json
from pathlib import Path
import sys
import time
from typing import Any, Iterator

import pytest

import mycelium_live.supervisor as supervisor_module
from mycelium_live.route import FakeLiveRoute

REPO_ROOT = Path(__file__).resolve().parents[2]
OBSERVATORY_PROTOCOL = "mycelium.observatory_stream.v1"
OBSERVATORY_SNAPSHOT_PATH = "/v1/observatory/snapshot"

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


def _asgi_get(
    app: Any,
    path: str,
    *,
    client: tuple[str, int] = ("127.0.0.1", 0),
    headers: tuple[tuple[bytes, bytes], ...] = (),
) -> tuple[int, bytes]:
    """Call one composed ASGI application exactly as the product gateway does."""

    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(
        app(
            {
                "type": "http",
                "method": "GET",
                "path": path,
                "client": client,
                "headers": list(headers),
            },
            receive,
            send,
        )
    )
    status = next(
        message["status"]
        for message in sent
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return int(status), body


@contextmanager
def _live_stack(
    tmp_path: Path, *, qualified_route: Any, deployment_dir: Path, route: Any = None
) -> Iterator[Any]:
    qualification, graph = qualified_route
    owned_route = FakeLiveRoute(scripted_tokens=(4599, 3329)) if route is None else route
    owned_route.open()
    stack = supervisor_module.build_live_stack(
        route=owned_route,
        deployment_dir=deployment_dir,
        execution_graph=graph,
        bearer_token="test-token",
        product_state_root=tmp_path / "product-state",
    )
    try:
        yield stack, qualification, owned_route
    finally:
        close = getattr(owned_route, "close", None)
        if callable(close):
            close()


def _live_observatory_gateway_class() -> type:
    module = importlib.import_module("mycelium_gateway.asgi")
    return getattr(module, "ObservatoryASGIApplication")


def test_live_stack_mounts_the_real_observatory_gateway(
    tmp_path: Path, qualified_route: Any, deployment_dir: Path
) -> None:
    """The live composition mounts the real gateway *and* feeds it the live route."""

    with _live_stack(
        tmp_path, qualified_route=qualified_route, deployment_dir=deployment_dir
    ) as (stack, qualification, _route):
        mounted = stack.app._observatory_app
        assert type(mounted) is _live_observatory_gateway_class()
        assert stack.observatory.app is mounted
        assert isinstance(stack.observatory.publisher.publish, object)

        # Before the qualification is published the gateway already serves the
        # seeded bundle, and that bundle claims nothing: no projection, no
        # ready route.  A stand-in would have invented both.
        status, body = _asgi_get(mounted, OBSERVATORY_SNAPSHOT_PATH)
        assert status == 200
        seeded = json.loads(body)
        assert seeded["bundle"]["snapshot"]["qualification"] is None
        assert seeded["bundle"]["provisioning"]["route_ready"] is False

        stack.health.publish(qualification)
        status, body = _asgi_get(mounted, OBSERVATORY_SNAPSHOT_PATH)
        assert status == 200
        envelope = json.loads(body)
        assert envelope["protocol"] == OBSERVATORY_PROTOCOL
        assert set(envelope["bundle"]) == {"snapshot", "incidents", "provisioning"}
        # The mounted gateway is fed by the *live* qualification, not a stub:
        # a simulated route would publish no ready projection.
        assert envelope["bundle"]["provisioning"]["route_ready"] is True
        assert envelope["bundle"]["snapshot"]["qualification"] is not None
        generation = int(envelope["generation"])
        assert generation >= 1

        # A read with an unchanged projection republishes nothing: the durable
        # generation advances only on a real change, never once per read.
        status, body = _asgi_get(mounted, OBSERVATORY_SNAPSHOT_PATH)
        assert status == 200
        assert int(json.loads(body)["generation"]) == generation


def test_live_gateway_generation_never_restarts_below_the_browser_mark(
    tmp_path: Path, qualified_route: Any, deployment_dir: Path
) -> None:
    """A supervisor restart never replays a generation below the accepted one."""

    state_root = tmp_path / "restart-state"
    qualification, graph = qualified_route
    route = FakeLiveRoute(scripted_tokens=(4599,))
    route.open()
    first_stack = supervisor_module.build_live_stack(
        route=route,
        deployment_dir=deployment_dir,
        execution_graph=graph,
        bearer_token="test-token",
        product_state_root=state_root,
    )
    first_stack.health.publish(qualification)
    status, body = _asgi_get(first_stack.app._observatory_app, OBSERVATORY_SNAPSHOT_PATH)
    assert status == 200
    accepted = int(json.loads(body)["generation"])
    assert accepted >= int(time.time() * 1_000) - 60_000

    restarted_stack = supervisor_module.build_live_stack(
        route=route,
        deployment_dir=deployment_dir,
        execution_graph=graph,
        bearer_token="test-token",
        product_state_root=state_root,
    )
    restarted_stack.health.publish(qualification)
    status, body = _asgi_get(
        restarted_stack.app._observatory_app, OBSERVATORY_SNAPSHOT_PATH
    )
    assert status == 200
    assert int(json.loads(body)["generation"]) > accepted
    route.close()


def test_live_gateway_denies_readers_outside_the_product_gateway_scope(
    tmp_path: Path, qualified_route: Any, deployment_dir: Path
) -> None:
    """The real gateway's deny-by-default policy is what the live stack serves."""

    with _live_stack(
        tmp_path, qualified_route=qualified_route, deployment_dir=deployment_dir
    ) as (stack, _qualification, _route):
        mounted = stack.app._observatory_app
        status, _body = _asgi_get(
            mounted, OBSERVATORY_SNAPSHOT_PATH, client=("203.0.113.7", 5151)
        )
        assert status == 403
        status, _body = _asgi_get(
            mounted,
            OBSERVATORY_SNAPSHOT_PATH,
            headers=((b"x-mycelium-proxy-capability", b"0" * 64),),
        )
        assert status == 403
        status, _body = _asgi_get(
            mounted,
            OBSERVATORY_SNAPSHOT_PATH,
            headers=((b"authorization", b"Bearer forged"),),
        )
        assert status == 403


def test_live_stack_fails_closed_when_the_observatory_gateway_is_unreachable(
    tmp_path: Path,
    qualified_route: Any,
    deployment_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED: an unimportable gateway stops the live composition instead of degrading."""

    monkeypatch.delitem(sys.modules, "mycelium_gateway.live_projection", raising=False)
    monkeypatch.setitem(sys.modules, "mycelium_gateway.asgi", None)
    qualification, graph = qualified_route
    route = FakeLiveRoute(scripted_tokens=(4599,))
    route.open()
    with pytest.raises(ImportError, match="mycelium_gateway"):
        supervisor_module.build_live_stack(
            route=route,
            deployment_dir=deployment_dir,
            execution_graph=graph,
            bearer_token="test-token",
            product_state_root=tmp_path / "unreachable-gateway",
        )
    route.close()


def test_live_stack_fails_closed_when_the_live_gateway_wiring_is_unreachable(
    tmp_path: Path,
    qualified_route: Any,
    deployment_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED: the live wiring module is itself part of the reachable surface."""

    monkeypatch.setitem(sys.modules, "mycelium_gateway.live_projection", None)
    qualification, graph = qualified_route
    route = FakeLiveRoute(scripted_tokens=(4599,))
    route.open()
    with pytest.raises(ImportError, match="mycelium_gateway"):
        supervisor_module.build_live_stack(
            route=route,
            deployment_dir=deployment_dir,
            execution_graph=graph,
            bearer_token="test-token",
            product_state_root=tmp_path / "unreachable-wiring",
        )
    route.close()


def test_live_openai_shim_serves_the_live_route_inference_surface() -> None:
    """The shim the live server launches answers on loopback and drives route.infer."""

    route = FakeLiveRoute(scripted_tokens=(4599, 3329, 2506))
    route.open()
    shim = supervisor_module.start_live_openai_shim(route=route)
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                status, _headers, body = _request(shim.port, "GET", "/v1/models")
            except OSError:
                time.sleep(0.05)
                continue
            break
        else:  # pragma: no cover - the listener is created synchronously
            raise AssertionError("live shim listener never became reachable")
        assert status == 200
        assert [entry["id"] for entry in json.loads(body)["data"]] == [shim.model_id]

        before = route.counters().frames_sent
        payload = json.dumps(
            {
                "model": shim.model_id,
                "messages": [{"role": "user", "content": "alpha beta"}],
            }
        ).encode("utf-8")
        status, _headers, body = _request(
            shim.port,
            "POST",
            "/v1/chat/completions",
            headers={"content-type": "application/json"},
            body=payload,
        )
        assert status == 200
        document = json.loads(body)
        assert document["object"] == "chat.completion"
        assert document["choices"][0]["message"]["role"] == "assistant"
        assert route.counters().frames_sent > before
        assert shim.status()["model_id"] == shim.model_id
        assert shim.base_url == f"http://127.0.0.1:{shim.port}"
    finally:
        shim.close()
        route.close()
    with pytest.raises(OSError):
        _request(shim.port, "GET", "/v1/models", timeout=1.0)


def test_live_openai_shim_fails_closed_when_the_shim_module_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED: an unimportable shim stops live bring-up instead of degrading."""

    monkeypatch.setitem(sys.modules, "mycelium_openai_shim.server", None)
    route = FakeLiveRoute(scripted_tokens=(4599,))
    route.open()
    with pytest.raises(ImportError, match="mycelium_openai_shim"):
        supervisor_module.start_live_openai_shim(route=route)
    route.close()


def test_live_openai_shim_refuses_a_non_loopback_bind() -> None:
    with pytest.raises(ValueError, match="loopback"):
        supervisor_module.start_live_openai_shim(
            route=FakeLiveRoute(scripted_tokens=(4599,)), host="0.0.0.0"
        )
