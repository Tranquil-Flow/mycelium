# SPDX-License-Identifier: AGPL-3.0-only
"""Demo bring-up that actually launches both product servers.

Two complete servers existed in the tree but were never launched by the demo:
the OpenAI/OpenRouter/Anthropic/Ollama HTTP shim (``mycelium_openai_shim``) and
the read-only Observatory ASGI application
(``mycelium_gateway.asgi.ObservatoryASGIApplication``).  This module wires both
into the demo product stack:

* the Observatory application is mounted into the ``observatory_app`` slot of
  :func:`mycelium_demo.product_stack.build_loopback_product_stack` -- the
  parameter the demo always passed as a stand-in and never populated with the
  real gateway application;
* the shim is co-launched in-process as a second loopback listener, so the
  product stack and the OpenAI-compatible surface come up together.

Claim boundary
--------------
The default bring-up is the *stub demo*: the repository's own synthetic
qualification fixture (``tests/qualification/conftest.py`` through the real
qualifier) plus a simulated route.  It proves that both servers are reachable
end-to-end from the demo stack, and nothing else.  It is not physical evidence
and never claims a qualified physical route; ``status()['claim_boundary']`` says
so in one line.

Credential handling
-------------------
The product gateway bearer credential is read from the environment
(``MYCELIUM_DEMO_REQUEST_BEARER_TOKEN``) or generated in-process; it is never an
argument and never printed.
"""
from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from mycelium_openai_shim.translator import default_detokenizer, default_tokenizer

from .gateway_control_runtime import LoopbackASGIServer, load_stub_qualification, stub_token_plan
from .product_stack import build_loopback_product_stack

DEMO_SERVERS_STATUS_PROTOCOL = "mycelium.demo_product_servers.status.v1"
DEMO_SERVERS_READY_PROTOCOL = "mycelium.demo_product_servers.ready.v1"
OPENAI_SHIM_DEFAULT_MODEL_ID = "mycelium-demo-stub"
OBSERVATORY_STATE_FILENAME = "coherent-snapshot.json"
OBSERVATORY_SNAPSHOT_PATH = "/v1/observatory/snapshot"
OBSERVATORY_EVENTS_PATH = "/v1/observatory/events"
BROWSER_BOOTSTRAP_PATH = "/api/v1/bootstrap"
BROWSER_OBSERVATORY_SNAPSHOT_PATH = "/api/v1/observatory/snapshot"
BROWSER_OBSERVATORY_EVENTS_PATH = "/api/v1/observatory/events"
OBSERVATORY_PROJECTION_PROTOCOL = "mycelium.observatory.request_projection.v1"
OBSERVATORY_STATUS_PROTOCOL = "mycelium.observatory.event_adapter_status.v1"
SWARM_PROTOCOL = "mycelium.product_ui.swarm.v1"
ENV_REQUEST_BEARER_TOKEN = "MYCELIUM_DEMO_REQUEST_BEARER_TOKEN"
CLAIM_BOUNDARY = (
    "stub demo: synthetic test-only assertion inputs and a simulated route; "
    "reachability and composition only, never physical evidence"
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_AUTHORIZATION_HEADER = b"authorization"
_PROXY_IDENTITY_HEADERS = (
    b"x-mycelium-authenticated-user",
    b"x-mycelium-proxy-capability",
)


class DemoServerWiringError(RuntimeError):
    """Fail-closed demo wiring error with a bounded machine-readable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise DemoServerWiringError(code)


def _require_loopback_host(host: object, code: str) -> str:
    _require(isinstance(host, str) and host != "", code)
    assert isinstance(host, str)
    text = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    _require(text in _LOOPBACK_HOSTS, code)
    return host


# --------------------------------------------------------------------------- #
# Import seams: the demo fails *loudly* when either server is unreachable
# --------------------------------------------------------------------------- #
def _load_observatory_gateway(
    *,
    state_path: Path,
    read_policy: Any,
) -> Any:
    """Build the real Observatory gateway, or fail with the import error.

    The patchable seam is deliberate: ``tests/demo/test_product_servers_reachability.py``
    replaces it to prove the bring-up is RED (fails closed) when the gateway
    module cannot be imported from the demo stack.
    """

    from mycelium_gateway.asgi import ObservatoryASGIApplication
    from mycelium_gateway.init import build_observatory_gateway

    runtime = build_observatory_gateway(state_path=state_path, read_policy=read_policy)
    if not isinstance(runtime.app, ObservatoryASGIApplication):
        raise DemoServerWiringError("observatory_gateway_missing_asgi_application")
    return runtime


def _load_openai_shim_serve_in_thread() -> Any:
    """Return ``mycelium_openai_shim.server.serve_in_thread`` or raise."""

    from mycelium_openai_shim.server import serve_in_thread

    return serve_in_thread


# --------------------------------------------------------------------------- #
# Observatory read policy and the initial published bundle
# --------------------------------------------------------------------------- #
def demo_observatory_read_policy(expected_token: bytes | None = None) -> Any:
    """Admit exactly the product gateway's synthesized loopback upstream scope.

    ``mycelium_ui_gateway.upstream.upstream_scope`` builds the upstream scope with
    ``client == ("127.0.0.1", 0)`` and forwards no browser metadata, so the policy
    keys on that loopback peer and refuses any scope that carries a
    browser-forgeable proxy identity header.  When ``expected_token`` is given the
    single ``Authorization: Bearer <token>`` value must match it.
    """

    if expected_token is not None and not isinstance(expected_token, bytes):
        raise DemoServerWiringError("observatory_expected_token_must_be_bytes")

    def authorize(scope: Mapping[str, Any]) -> bool:
        client = scope.get("client")
        if (
            not isinstance(client, (list, tuple))
            or not client
            or str(client[0]) not in _LOOPBACK_HOSTS
        ):
            return False
        headers = scope.get("headers", ())
        if not isinstance(headers, (list, tuple)):
            return False
        values: list[bytes] = []
        for item in headers:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                return False
            name, value = item
            if not isinstance(name, bytes) or not isinstance(value, bytes):
                return False
            lowered = name.lower()
            if lowered in _PROXY_IDENTITY_HEADERS:
                return False
            if lowered == _AUTHORIZATION_HEADER:
                values.append(value)
        if expected_token is None:
            return not values
        return (
            len(values) == 1
            and hmac.compare_digest(values[0], b"Bearer " + expected_token)
        )

    return authorize


def demo_observatory_bundle(
    *,
    source_cursor: int = 0,
    observed_at_unix_ms: int | None = None,
    qualification: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One complete Observatory bundle the product gateway will validate.

    ``qualification`` stays ``None`` for the stub demo: the synthetic fixture's
    ``evidence_class`` is not ``physical_qualification``, so a ready route
    projection would be a claim the demo must not publish.
    """

    observed = (
        int(time.time() * 1_000)
        if observed_at_unix_ms is None
        else int(observed_at_unix_ms)
    )
    route_ready = bool(qualification) and qualification.get("route_ready") is True
    return {
        "snapshot": {
            "protocol": OBSERVATORY_PROJECTION_PROTOCOL,
            "source_cursor": int(source_cursor),
            "observed_at_unix_ms": observed,
            "qualification": None if qualification is None else dict(qualification),
            "sessions": [],
        },
        "incidents": [],
        "provisioning": {
            "protocol": OBSERVATORY_STATUS_PROTOCOL,
            "route_ready": route_ready,
            "source_cursor": int(source_cursor),
            "buffered_sessions": 0,
            "quarantine_capacity": 256,
            "dropped_quarantine_count": 0,
        },
    }


def publish_demo_observatory_bundle(publisher: Any, bundle: Mapping[str, Any]) -> Any:
    """Publish one bundle through the durable publisher; returns the publication."""

    publication = publisher.publish(dict(bundle))
    if publisher.snapshot_json() is None:
        raise DemoServerWiringError("observatory_publication_not_readable")
    return publication


# --------------------------------------------------------------------------- #
# Stub demo pieces (simulated; never a model, a device or a physical route)
# --------------------------------------------------------------------------- #
class StubDemoLiveRoute:
    """Simulated ``LiveRoute`` driving the shim's ``infer`` surface."""

    is_simulated = True
    acts_as_qualification_source = False

    def __init__(self, *, name: str = "stub-demo-route") -> None:
        self.name = name
        self.inference_calls: list[dict[str, Any]] = []
        self._detokenize = default_detokenizer()

    def infer(
        self,
        *,
        token_ids: Sequence[int],
        max_new_tokens: int,
        request_id: str,
        sink: Any,
    ) -> str:
        prompt = self._detokenize(tuple(int(item) for item in token_ids)).strip()
        plan = stub_token_plan(prompt or "stub", int(max_new_tokens))
        for index, (token_id, _text) in enumerate(plan):
            sink.emit(index, token_id)
        self.inference_calls.append(
            {
                "request_id": request_id,
                "prompt_tokens": len(token_ids),
                "emitted_tokens": len(plan),
            }
        )
        return "completed"


class StubDemoQualificationSource:
    """Current-record source over the repository's synthetic fixture qualification."""

    def __init__(self, record: Any) -> None:
        self._record = record

    @property
    def record(self) -> Any:
        return self._record

    def current(self) -> Any:
        return self._record


class StubDemoPromptCodec:
    """Deterministic prompt codec: one token id per character."""

    def encode(self, prompt: str) -> tuple[int, ...]:
        _require(isinstance(prompt, str), "invalid_prompt")
        encoded = tuple(ord(character) for character in prompt)
        return encoded or (ord(" "),)

    def decode_token(self, token_id: int) -> str:
        return chr(token_id) if 0 <= token_id < 0x110000 else "\ufffd"

    stop_token_ids: frozenset[int] = frozenset()


class StubDemoRouter:
    """``RouterPort`` shape for the stub demo stack: bounded, deterministic, no model."""

    supports_publisher_generation = False
    requires_qualification_binding = False
    is_simulated = True

    def __init__(self) -> None:
        self._graph = object()
        self._requests: dict[str, dict[str, Any]] = {}

    def current_deployment(self) -> Any:
        return self._graph

    def admit(
        self,
        request: Any,
        client_sink: Any,
        *,
        pinned_deployment: Any = None,
        **_options: Any,
    ) -> str:
        prompt = "".join(
            chr(int(token_id))
            for token_id in getattr(request, "prompt_token_ids", ())
            if 0 <= int(token_id) < 0x110000
        ).strip()
        self._requests[request.request_id] = {
            "sink": client_sink,
            "next_token": 0,
            "status": "DECODING",
            "plan": stub_token_plan(prompt or "alpha beta gamma", request.max_new_tokens),
        }
        return request.request_id

    def request_status(self, request_id: str) -> str:
        state = self._requests.get(request_id)
        return "FAILED" if state is None else str(state["status"])

    def decode_one(self, request_id: str) -> bool:
        state = self._requests.get(request_id)
        if state is None:
            return False
        plan: tuple[tuple[int, str], ...] = state["plan"]
        index = int(state["next_token"])
        if index < len(plan):
            state["sink"].emit(index, plan[index][0])
            state["next_token"] = index + 1
        if int(state["next_token"]) >= len(plan):
            state["status"] = "COMPLETED"
        return True

    def cancel(self, request_id: str) -> bool:
        state = self._requests.get(request_id)
        if state is None:
            return False
        state["status"] = "CANCELLED"
        return True


class StubDemoSwarmCoordinator:
    """Empty, valid swarm projection; mutations are refused in the stub demo."""

    def status(self) -> Mapping[str, Any]:
        return {"protocol": SWARM_PROTOCOL, "native_nodes": [], "browser_workers": []}

    def create_invite(self, _document: Mapping[str, Any]) -> Mapping[str, Any]:
        raise DemoServerWiringError("stub_demo_has_no_swarm_invites")

    def join(self, _document: Mapping[str, Any]) -> Mapping[str, Any]:
        raise DemoServerWiringError("stub_demo_has_no_swarm_joins")

    def leave(self, _document: Mapping[str, Any]) -> Mapping[str, Any]:
        raise DemoServerWiringError("stub_demo_has_no_swarm_leaves")


def build_stub_demo_stack_inputs() -> dict[str, Any]:
    """The demo stack's injected pieces: synthetic qualification + simulated route.

    The bearer credential comes from ``MYCELIUM_DEMO_REQUEST_BEARER_TOKEN`` when
    set, otherwise it is generated in-process.  It is never returned in a status
    document and never printed.
    """

    bearer_token = os.environ.get(ENV_REQUEST_BEARER_TOKEN) or secrets.token_hex(32)
    return {
        "qualification_source": StubDemoQualificationSource(load_stub_qualification()),
        "router": StubDemoRouter(),
        "codec": StubDemoPromptCodec(),
        "swarm_coordinator": StubDemoSwarmCoordinator(),
        "request_bearer_token": bearer_token,
        "live_route": StubDemoLiveRoute(),
    }


# --------------------------------------------------------------------------- #
# Bring-up
# --------------------------------------------------------------------------- #
@dataclass
class _StartedListener:
    server: Any
    thread: threading.Thread


class DemoProductServers:
    """One loopback process hosting the product stack *and* the shim co-listener."""

    def __init__(
        self,
        *,
        product_app: Any,
        product_listener: _StartedListener,
        observatory_runtime: Any,
        observatory_app: Any,
        observatory_publication: Any,
        openai_shim_listener: _StartedListener,
        live_route: Any,
        model_id: str,
        qualification_source: Any,
    ) -> None:
        self.product_app = product_app
        self.observatory_runtime = observatory_runtime
        self.observatory_app = observatory_app
        self.observatory_publication = observatory_publication
        self.openai_shim_server = openai_shim_listener.server
        self.live_route = live_route
        self.model_id = model_id
        self.qualification_source = qualification_source
        self._product_listener = product_listener
        self._openai_shim_listener = openai_shim_listener
        self._closed = False

    # -- observations ------------------------------------------------------- #
    @property
    def product_port(self) -> int:
        return int(self._product_listener.server.port)

    @property
    def openai_shim_port(self) -> int:
        return int(self.openai_shim_server.server_address[1])

    @property
    def product_base_url(self) -> str:
        return f"http://127.0.0.1:{self.product_port}"

    @property
    def openai_shim_base_url(self) -> str:
        return f"http://127.0.0.1:{self.openai_shim_port}"

    def product_observatory_slot(self) -> Any:
        """The observatory application the product gateway actually mounted."""

        return getattr(self.product_app, "_observatory_app", None)

    def endpoints(self) -> tuple[str, ...]:
        product = self.product_base_url
        shim = self.openai_shim_base_url
        return (
            f"GET {shim}/v1/models",
            f"POST {shim}/v1/chat/completions",
            f"GET {shim}/api/tags",
            f"POST {shim}/api/chat",
            f"POST {shim}/v1/messages",
            f"GET {shim}/healthz",
            f"GET {product}{BROWSER_BOOTSTRAP_PATH}",
            f"GET {product}{BROWSER_OBSERVATORY_SNAPSHOT_PATH}",
            f"GET {product}{BROWSER_OBSERVATORY_EVENTS_PATH}",
        )

    def status(self) -> Mapping[str, Any]:
        application = type(self.observatory_app)
        return {
            "protocol": DEMO_SERVERS_STATUS_PROTOCOL,
            "ready_protocol": DEMO_SERVERS_READY_PROTOCOL,
            "ports": {
                "product_stack": self.product_port,
                "openai_shim": self.openai_shim_port,
            },
            "endpoints": list(self.endpoints()),
            "openai_shim": {
                "base_url": self.openai_shim_base_url,
                "model_id": self.model_id,
                "route": type(self.live_route).__name__,
                "simulated": bool(getattr(self.live_route, "is_simulated", False)),
            },
            "observatory": {
                "base_path": BROWSER_OBSERVATORY_SNAPSHOT_PATH,
                "events_path": BROWSER_OBSERVATORY_EVENTS_PATH,
                "upstream_snapshot_path": OBSERVATORY_SNAPSHOT_PATH,
                "upstream_events_path": OBSERVATORY_EVENTS_PATH,
                "gateway_application": f"{application.__module__}.{application.__qualname__}",
                "generation": int(self.observatory_publication.generation),
                "publisher": type(self.observatory_runtime.publisher).__name__,
            },
            "claim_boundary": CLAIM_BOUNDARY,
            "live_host_mutations": 0,
        }

    # -- lifecycle ---------------------------------------------------------- #
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_listener(self._openai_shim_listener)
        self._stop_listener(self._product_listener)
        close_runtime = getattr(self.observatory_runtime, "close", None)
        if callable(close_runtime):
            close_runtime()

    @staticmethod
    def _stop_listener(listener: _StartedListener) -> None:
        try:
            listener.server.shutdown()
        except BaseException:  # pragma: no cover - shutdown is best effort
            pass
        close = getattr(listener.server, "stop", None)
        if callable(close):
            close(listener.thread)
            return
        listener.server.server_close()
        if listener.thread.is_alive():
            listener.thread.join(timeout=5.0)

    def __enter__(self) -> "DemoProductServers":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()


def bring_up_demo_product_servers(
    *,
    qualification_source: Any,
    router: Any,
    codec: Any,
    swarm_coordinator: Any,
    request_bearer_token: str,
    live_route: Any,
    state_root: str | os.PathLike[str],
    product_host: str = "127.0.0.1",
    product_port: int = 0,
    openai_shim_host: str = "127.0.0.1",
    openai_shim_port: int = 0,
    observatory_bearer_token: bytes | None = None,
    model_id: str = OPENAI_SHIM_DEFAULT_MODEL_ID,
    product_app: Any | None = None,
    product_app_factory: Callable[[Callable[[], Any]], Any] | None = None,
    publish_initial_snapshot: bool = True,
) -> DemoProductServers:
    """Launch the demo product stack with both servers actually serving.

    The Observatory ASGI application is mounted in the product stack's
    ``observatory_app`` slot; the OpenAI-compatible shim is co-launched on its own
    loopback listener in the same process.  Any failure closes everything already
    started and re-raises.
    """

    product_host = _require_loopback_host(product_host, "non_loopback_product_host")
    openai_shim_host = _require_loopback_host(
        openai_shim_host, "non_loopback_openai_shim_host"
    )
    _require(
        isinstance(product_port, int)
        and not isinstance(product_port, bool)
        and 0 <= product_port <= 65_535,
        "invalid_product_port",
    )
    _require(
        isinstance(openai_shim_port, int)
        and not isinstance(openai_shim_port, bool)
        and 0 <= openai_shim_port <= 65_535,
        "invalid_openai_shim_port",
    )
    _require(
        isinstance(request_bearer_token, str) and bool(request_bearer_token),
        "invalid_request_bearer_token",
    )

    root = Path(state_root).expanduser()
    observatory_state_path = root / OBSERVATORY_STATE_FILENAME

    observatory_runtime = _load_observatory_gateway(
        state_path=observatory_state_path,
        read_policy=demo_observatory_read_policy(observatory_bearer_token),
    )
    publication = None
    if publish_initial_snapshot:
        publication = publish_demo_observatory_bundle(
            observatory_runtime.publisher, demo_observatory_bundle()
        )
    if publication is None:
        publication = observatory_runtime.publisher.current_publication()
    _require(publication is not None, "observatory_publication_missing")

    serve_in_thread = _load_openai_shim_serve_in_thread()

    product_listener: _StartedListener | None = None
    shim_listener: _StartedListener | None = None
    try:
        composed = build_loopback_product_stack(
            qualification_source=qualification_source,
            router=router,
            codec=codec,
            observatory_app=observatory_runtime.app,
            product_app=product_app,
            product_app_factory=product_app_factory,
            swarm_coordinator=swarm_coordinator,
            request_bearer_token=request_bearer_token,
        )
        gateway = LoopbackASGIServer((product_host, product_port), composed)
        gateway.start()
        product_listener = _StartedListener(
            server=gateway, thread=gateway.serve_forever_thread()
        )
        shim_server, shim_thread = serve_in_thread(
            live_route=live_route,
            host=openai_shim_host,
            port=openai_shim_port,
            tokenizer=default_tokenizer(),
            detokenizer=default_detokenizer(),
            model_id=model_id,
        )
        shim_listener = _StartedListener(server=shim_server, thread=shim_thread)
    except BaseException:
        if shim_listener is not None:
            DemoProductServers._stop_listener(shim_listener)
        if product_listener is not None:
            DemoProductServers._stop_listener(product_listener)
        close_runtime = getattr(observatory_runtime, "close", None)
        if callable(close_runtime):
            close_runtime()
        raise

    return DemoProductServers(
        product_app=composed,
        product_listener=product_listener,
        observatory_runtime=observatory_runtime,
        observatory_app=observatory_runtime.app,
        observatory_publication=publication,
        openai_shim_listener=shim_listener,
        live_route=live_route,
        model_id=model_id,
        qualification_source=qualification_source,
    )


__all__ = [
    "CLAIM_BOUNDARY",
    "BROWSER_OBSERVATORY_SNAPSHOT_PATH",
    "DEMO_SERVERS_READY_PROTOCOL",
    "DEMO_SERVERS_STATUS_PROTOCOL",
    "DemoProductServers",
    "DemoServerWiringError",
    "ENV_REQUEST_BEARER_TOKEN",
    "OBSERVATORY_EVENTS_PATH",
    "OBSERVATORY_SNAPSHOT_PATH",
    "OPENAI_SHIM_DEFAULT_MODEL_ID",
    "StubDemoLiveRoute",
    "StubDemoPromptCodec",
    "StubDemoQualificationSource",
    "StubDemoRouter",
    "StubDemoSwarmCoordinator",
    "bring_up_demo_product_servers",
    "build_stub_demo_stack_inputs",
    "demo_observatory_bundle",
    "demo_observatory_read_policy",
    "publish_demo_observatory_bundle",
]
