# SPDX-License-Identifier: AGPL-3.0-only
"""A11 product runner against the gateway's 64-admission, 3600 s browser limits."""
from __future__ import annotations

from argparse import Namespace
from contextlib import contextmanager
from types import SimpleNamespace
import threading

import pytest

from scripts import run_a5_benchmark_gate as a5
from scripts import run_a11_route_benchmark as a11


class CappedGateway:
    def __init__(self):
        self.now = 100.0
        self.sessions = []
        self.lock = threading.Lock()
        self.stream_owners = []
        self.advance_after = None
        self.stream_ending = b"event: completed\n"
        self.cancelled = []

    def session(self, base_url):
        gateway = self

        class Session:
            def __init__(self, base_url):
                self.base_url = base_url
                self.owner = self
                self._qualification = {"binding": {"route": "fixed"}}
                self.born = gateway.now
                self.attempts = 0
                with gateway.lock:
                    self.number = len(gateway.sessions)
                    gateway.sessions.append(self)

            def fork(self):
                child = object.__new__(type(self))
                child.owner = self.owner
                child._qualification = self._qualification
                return child

            def json(self, method, path, **kwargs):
                if method == "GET":
                    assert path == "/api/v1/bootstrap"
                    return {"features": {"speculation_preference": True}}
                assert method == "POST" and path == "/api/v1/inference" and kwargs["mutate"] is True
                assert kwargs["body"]["qualification"] == {"route": "fixed"}
                assert kwargs["body"]["speculation_preference"] in {"target_only", "speculative_requested"}
                owner = self.owner
                with gateway.lock:
                    if gateway.now - owner.born >= 3600 or owner.attempts >= 64:
                        raise a5.GateError("http_post_429")
                    owner.attempts += 1
                    serial = sum(session.attempts for session in gateway.sessions)
                    if serial == gateway.advance_after:
                        gateway.now += 1800
                    path = f"{owner.number}:{owner.attempts}"
                    return {"event_path": path, "cancel_path": path + "/cancel"}

            def cancel(self, accepted):
                gateway.cancelled.append(accepted["cancel_path"])

            @contextmanager
            def request(self, method, path):
                assert method == "GET" and path.startswith(f"{self.owner.number}:")
                gateway.stream_owners.append((path, self.owner.number))
                yield iter((b"event: token\n", b"data: 1\n", gateway.stream_ending))

        return Session(base_url)


@pytest.fixture
def gateway(monkeypatch):
    gateway = CappedGateway()
    monkeypatch.setattr(a5, "ProductSession", gateway.session)
    monkeypatch.setattr(a5, "time", SimpleNamespace(monotonic=lambda: gateway.now))
    monkeypatch.setattr(a11, "time", SimpleNamespace(monotonic=lambda: gateway.now))
    # This is a transport test; don't run bootstrap statistics or write evidence.
    monkeypatch.setattr(a11, "decide", lambda *_args, **_kwargs: {"decision": {"state": "transport_fixture"}})
    monkeypatch.setattr(a11, "write_private_json", lambda *_args: None)
    return gateway


def test_product_rotates_during_frozen_96_submission_protocol(gateway, tmp_path):
    args = Namespace(base_url="http://fixture.invalid", out=tmp_path, quick=False,
                           plan_digest="sha256:" + "a" * 64)
    document = a11.run_product(args)
    assert len(document["windows"]) == len(a11.WINDOW_ORDER)
    assert len(document["requests"]) == len(a11.WINDOW_ORDER) * len(a11.window_workload())
    assert [session.attempts for session in gateway.sessions] == [32, 32, 32]
    assert len(gateway.stream_owners) == 96
    assert all(path.startswith(f"{owner}:") for path, owner in gateway.stream_owners)
    assert gateway.cancelled == []


def test_product_rotates_at_half_ttl_before_expiry(gateway, tmp_path):
    gateway.advance_after = 2
    args = Namespace(base_url="http://fixture.invalid", out=tmp_path, quick=True,
                           plan_digest="sha256:" + "a" * 64)
    a11.run_product(args)
    assert [session.attempts for session in gateway.sessions] == [2, 30]
    assert all(path.startswith(f"{owner}:") for path, owner in gateway.stream_owners)


def test_product_cancels_each_premature_stream_eof(gateway, tmp_path):
    gateway.stream_ending = b"event: token\n"
    args = Namespace(base_url="http://fixture.invalid", out=tmp_path, quick=True,
                     plan_digest="sha256:" + "a" * 64)
    a11.run_product(args)
    assert gateway.cancelled == [path + "/cancel" for path, _owner in gateway.stream_owners]
