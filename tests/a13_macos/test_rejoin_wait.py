# SPDX-License-Identifier: AGPL-3.0-only
"""R24(3): a chat request during Rejoining waits (bounded) for the route instead of an immediate refusal.

The refusing line is ``ChatService.send`` (``chat_not_ready`` when no serving route exists); the wait sits there,
not in the gateway and not in ``AdaptiveHostController.admission``."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from mycelium_a13_macos import adaptive_host as ah
from mycelium_a13_macos.chat import REJOIN_WAIT_S, REJOIN_WAITERS_MAX, ChatError, ChatService, handle

PAUSED = {**ah.DEFAULT_POLICY, "enabled": True, "automatic": False, "game_mode": True}
RELEASED = {**ah.DEFAULT_POLICY, "enabled": True, "automatic": False, "game_mode": False}


class Run:
    def __init__(self):
        self.state = "running"
        self.step = "qualify"

    def status(self):
        return {"state": self.state, "step": self.step, "registry_origin": "http://127.0.0.1:55555", "run_id": "r1",
                "participants": [], "route_ready": self.state == "serving"}


class Client:
    def __init__(self, origin):
        pass

    def stream(self, prompt, n, on_token):
        on_token("ok")
        return {"terminal": "completed", "code": None}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(*, rejoining=True, paused=False):
    clock = Clock()
    controller = ah.AdaptiveHostController(ah.DEFAULT_POLICY, clock=clock)
    controller.apply_policy(PAUSED)
    if not paused:
        controller.apply_policy(RELEASED)
        controller._enter("normal", "none")
    app = SimpleNamespace(owner_run=Run(), _adaptive_runtime=SimpleNamespace(controller=controller),
                          installation_role="owner", server=None)
    if not rejoining and not paused:
        controller.rejoin_pending = False
    return app, controller, clock


def service(app, clock, sleep):
    return ChatService(app, client_factory=Client, clock=clock, sleep=sleep)


def test_request_waits_then_places_when_route_becomes_ready():
    app, _, clock = make()
    slept = []

    def sleep(s):
        slept.append(s)
        clock.t += s
        if clock.t >= 1003:
            app.owner_run.state = "serving"

    reply = service(app, clock, sleep).send("c", {"prompt": "hi"})
    assert reply["request_id"] and 0 < sum(slept) <= REJOIN_WAIT_S


def test_request_times_out_with_route_rejoining_not_generic_error():
    app, _, clock = make()

    def sleep(s):
        clock.t += s

    with pytest.raises(ChatError) as info:
        service(app, clock, sleep).send("c", {"prompt": "hi"})
    assert info.value.code == "route_rejoining" and info.value.status == 503
    assert info.value.extra["retry_after_s"] >= 1
    assert REJOIN_WAIT_S - 1 <= clock.t - 1000.0 <= REJOIN_WAIT_S + 1
    assert REJOIN_WAIT_S == 15


def test_paused_refuses_immediately_no_wait():
    app, _, clock = make(paused=True)
    service_ = service(app, clock, lambda s: pytest.fail("must not wait while paused"))
    with pytest.raises(ChatError) as info:
        service_.send("c", {"prompt": "hi"})
    assert info.value.code in {"chat_not_ready", "chat_warming_up"}


def test_no_wait_when_not_rejoining_and_when_sharing():
    app, _, clock = make(rejoining=False)
    app.owner_run.state = "idle"
    with pytest.raises(ChatError) as info:
        service(app, clock, lambda s: pytest.fail("no wait outside rejoining")).send("c", {"prompt": "hi"})
    assert info.value.code == "chat_not_ready"
    app, _, clock = make()
    app.owner_run.state = "serving"
    service(app, clock, lambda s: pytest.fail("no wait while sharing")).send("c", {"prompt": "hi"})


def test_waiter_cap_refuses_extra():
    app, _, clock = make()
    svc = service(app, clock, lambda s: None)
    entered, release, errors = threading.Event(), threading.Event(), []

    def blocking_sleep(s):
        entered.set()
        release.wait(5)
        clock.t += 100

    svc._sleep = blocking_sleep
    threads = [threading.Thread(target=lambda: _try(svc, errors)) for _ in range(REJOIN_WAITERS_MAX)]
    for t in threads:
        t.start()
    entered.wait(5)
    import time
    for _ in range(100):
        if svc._rejoin_waiters == REJOIN_WAITERS_MAX:
            break
        time.sleep(0.01)
    with pytest.raises(ChatError) as info:
        svc.send("c", {"prompt": "hi"})
    assert info.value.code == "route_rejoining_busy"
    release.set()
    for t in threads:
        t.join(5)
    assert svc._rejoin_waiters == 0


def _try(svc, errors):
    try:
        svc.send("c", {"prompt": "hi"})
    except ChatError as error:
        errors.append(error.code)


def test_client_disconnect_releases_waiter():
    app, _, clock = make()

    def sleep(s):
        raise ConnectionResetError("client went away")

    svc = service(app, clock, sleep)
    with pytest.raises(ConnectionResetError):
        svc.send("c", {"prompt": "hi"})
    assert svc._rejoin_waiters == 0


def test_handle_returns_retry_after_in_body():
    app, _, clock = make()
    app.chat = service(app, clock, lambda s: clock.__setattr__("t", clock.t + s))
    import json
    status, _, raw = handle(app, "POST", "/chat/api/send",
                            {"X-Mycelium-Chat": "1", "Content-Type": "application/json"},
                            json.dumps({"prompt": "hi"}).encode(), via_relay=False)
    body = json.loads(raw)
    assert status == 503 and body["error"] == "route_rejoining" and body["retry_after_s"] >= 1
