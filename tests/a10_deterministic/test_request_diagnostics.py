# SPDX-License-Identifier: AGPL-3.0-only
"""Offline, opt-in observations of the A10 product client's request boundary."""
from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

ensure_worktree_imports()

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from scripts import run_a10_physical_gate as gate
from scripts import run_a5_benchmark_gate as a5

CANARY = "secret-prompt-token-cookie-https://private.example/path"
SHAPE = gate.RequestShape(7, "prompt_short", "output_short", "interactive", 0.0)


class FakeResponse:
   def __init__(self, lines):
      self.lines = lines

   def __enter__(self):
      return iter(self.lines)

   def __exit__(self, *_args):
      pass


class FakeSession:
   def __init__(self, *, submit_error=None, lines=(), stream_error=None, accepted=None):
      self.submit_error = submit_error
      self.lines = lines
      self.stream_error = stream_error
      self.accepted = accepted if accepted is not None else {
         "request_id": "request-7", "event_path": "/api/v1/inference/request-7/events"
      }
      self.calls = []

   def fork(self):
      self.calls.append("fork")
      return self

   def submit(self, **kwargs):
      self.calls.append("submit")
      assert kwargs["maximum_new_tokens"] == 8
      if self.submit_error:
         raise self.submit_error
      return self.accepted

   def request(self, method, path):
      self.calls.append("request")
      assert (method, path) == ("GET", self.accepted["event_path"])
      if self.stream_error:
         raise self.stream_error
      return FakeResponse(self.lines)


def client(monkeypatch, session, **kwargs):
   # Intercept the actual ProductSession constructor before ProductClient is made.
   # A fallback transport call fails closed, so RED cannot touch a live origin.
   monkeypatch.setattr(a5, "ProductSession", lambda _url: session)
   monkeypatch.setattr(a5.urllib.request, "urlopen", lambda *_a, **_kw: pytest.fail("live transport"))
   monkeypatch.setattr(a5.urllib.request, "build_opener", lambda *_a, **_kw: pytest.fail("live transport"))
   product = gate.ProductClient("http://invalid.example", **kwargs)
   assert product._session is session
   return product


def row(product):
   (record,) = product.request_diagnostics()
   assert record.shape_index == SHAPE.index
   return record


def test_transport_interception_and_default_outcome(monkeypatch):
   session = FakeSession(lines=[b"event: token\n", b"data: abc\n", b"event: completed\n"])
   product = client(monkeypatch, session)
   result = product.run(SHAPE)
   assert session.calls == ["fork", "submit", "request"]
   assert result.outcome == "completed" and result.tokens == 1
   assert result.output_digest == "sha256:" + hashlib.sha256(b"abc").hexdigest()
   assert product.request_diagnostics() == ()


@pytest.mark.parametrize("error,outcome,status", [
   (a5.GateError("http_post_429"), "overflowed", 429),
   (a5.GateError("http_post_503"), "overflowed", 503),
   (a5.GateError("http_post_unavailable"), "failed", None),
   (RuntimeError(CANARY), "failed", None),
])
def test_submit_refusal(monkeypatch, error, outcome, status):
   product = client(monkeypatch, FakeSession(submit_error=error), diagnostic_limit=8)
   result = product.run(SHAPE)
   observed = row(product)
   assert result.outcome == outcome and result.tokens == 0
   assert observed.stage == "submit_error" and observed.outcome == outcome
   assert observed.status == status and observed.request_ref is None
   assert observed.token_events == 0


@pytest.mark.parametrize("lines,stream_error,stage,tokens", [
   ([b"event: token\n", b"data: " + CANARY.encode() + b"\n"], RuntimeError(CANARY), "stream_exception", 1),
   ([b"event: token\n", b"data: " + CANARY.encode() + b"\n"], None, "stream_eof", 1),
   ([], None, "stream_eof", 0),
])
def test_stream_loss_distinct_from_terminal(monkeypatch, lines, stream_error, stage, tokens):
   if stream_error:
      class BrokenResponse(FakeResponse):
         def __enter__(self):
            def broken():
               yield from self.lines
               raise stream_error
            return broken()
      session = FakeSession(lines=lines)
      session.request = lambda *_args: BrokenResponse(lines)
   else:
      session = FakeSession(lines=lines)
   product = client(monkeypatch, session, diagnostic_limit=8)
   result = product.run(SHAPE)
   observed = row(product)
   assert result.outcome == "failed" and result.tokens == tokens
   assert result.output_digest is None
   assert observed.stage == stage and observed.outcome == "failed"
   assert observed.token_events == tokens and observed.status is None
   assert observed.request_ref == "sha256:" + hashlib.sha256(b"request-7").hexdigest()
   assert CANARY not in repr(observed) and CANARY not in json.dumps(observed.__dict__)


@pytest.mark.parametrize("terminal,outcome", [
   ("completed", "completed"), ("cancelled", "cancelled"), ("failed", "failed")
])
def test_observed_terminals(monkeypatch, terminal, outcome):
   session = FakeSession(lines=[b"event: token\n", b"data: " + CANARY.encode() + b"\n",
                                f"event: {terminal}\n".encode()])
   product = client(monkeypatch, session, diagnostic_limit=8)
   result = product.run(SHAPE)
   observed = row(product)
   assert result.outcome == outcome
   assert observed.stage == "terminal" and observed.outcome == outcome
   assert observed.token_events == 1 and observed.status is None
   assert observed.request_ref == "sha256:" + hashlib.sha256(b"request-7").hexdigest()
   assert CANARY not in repr(observed)


def test_stream_timeout_cancels_before_return_and_records_outcome(monkeypatch):
   session = FakeSession(stream_error=TimeoutError("read timed out"),
                         accepted={"request_id": "request-7", "event_path": "/api/v1/inference/request-7/events",
                                   "cancel_path": "/api/v1/inference/request-7/cancel"})
   def cancel(accepted):
      session.calls.append(("cancel", accepted["cancel_path"]))
   session.cancel = cancel
   product = client(monkeypatch, session, diagnostic_limit=8)
   assert product.run(SHAPE).outcome == "failed"
   assert session.calls[-1] == ("cancel", "/api/v1/inference/request-7/cancel")
   assert row(product).cancel_outcome == "sent"


def test_stream_open_refusal_retains_only_safe_status(monkeypatch):
   product = client(monkeypatch, FakeSession(stream_error=a5.GateError("http_get_404")), diagnostic_limit=8)
   result = product.run(SHAPE)
   observed = row(product)
   assert result.outcome == "failed" and result.tokens == 0
   assert observed.stage == "stream_exception" and observed.status == 404


def test_malformed_request_id_is_not_hashed_or_retained(monkeypatch):
   session = FakeSession(accepted={"request_id": CANARY, "event_path": "/fake"}, lines=[b"event: completed\n"])
   product = client(monkeypatch, session, diagnostic_limit=8)
   product.run(SHAPE)
   assert row(product).request_ref is None
   assert CANARY not in repr(product.request_diagnostics())


def test_bounded_concurrent_retention_and_immutable_snapshots(monkeypatch):
   session = FakeSession(lines=[b"event: completed\n"])
   product = client(monkeypatch, session, diagnostic_limit=5)
   with ThreadPoolExecutor(max_workers=8) as pool:
      results = list(pool.map(product.run, [gate.RequestShape(i, "prompt_short", "output_short", "interactive", 0) for i in range(80)]))
   assert len(results) == 80 and all(result.outcome == "completed" for result in results)
   records = product.request_diagnostics()
   assert len(records) == 5 and len({r.shape_index for r in records}) == 5
   with pytest.raises((AttributeError, TypeError)):
      records[0].stage = "tampered"
   assert len(product.request_diagnostics()) == 5
