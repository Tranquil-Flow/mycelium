# SPDX-License-Identifier: AGPL-3.0-only
"""RED/GREEN for the two harness defects the live two-Mac 7B A10 probe exposed.

1. ``ProductClient.run`` hashed the raw SSE ``data:`` line.  A real gateway token
   event carries the per-request ``request_id`` (and ``sequence``), so the same
   tokens from two requests could never hash equal: every candidate-vs-serial
   output-parity comparison reported a mismatch.
2. ``ProductClient.queue_overflow`` submitted exactly ``max_pending_requests``
   (64, the request gateway's default admission bound) requests.  A burst that
   only *reaches* the bound is never refused, so ``overflowed`` stayed 0 and the
   negative read ``violated`` by construction.
"""
from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

ensure_worktree_imports()

import hashlib
import inspect
import json

from mycelium_request_gateway.service import RequestGatewayService
from scripts import run_a10_physical_gate as gate
from scripts import run_a5_benchmark_gate as a5

SHAPE = gate.RequestShape(3, "prompt_short", "output_short", "interactive", 0.0)


class _Response:
   def __init__(self, lines):
      self.lines = lines

   def __enter__(self):
      return iter(self.lines)

   def __exit__(self, *_args):
      pass


class _Session:
   def __init__(self, request_id, tokens):
      self.request_id = request_id
      self.tokens = tokens

   def fork(self):
      return self

   def submit(self, **_kwargs):
      return {"request_id": self.request_id, "event_path": f"/api/v1/inference/{self.request_id}/events"}

   def request(self, *_args):
      lines = []
      for sequence, (index, text, token_id) in enumerate(self.tokens, start=2):
         event = {"protocol": "mycelium.request_gateway.v2", "request_id": self.request_id,
                  "sequence": sequence, "type": "token", "publisher_generation": 1,
                  "token_index": index, "text": text, "token_id": token_id}
         lines += [b"event: token\n", b"data: " + json.dumps(event, sort_keys=True, separators=(",", ":")).encode() + b"\n"]
      return _Response(lines + [b"event: completed\n"])


def _run(monkeypatch, request_id, tokens):
   session = _Session(request_id, tokens)
   monkeypatch.setattr(a5, "ProductSession", lambda _url: session)
   return gate.ProductClient("http://invalid.example").run(SHAPE)


TOKENS = [(0, "Hello", 9707), (1, " world", 1879)]


def test_same_tokens_from_distinct_requests_hash_equal(monkeypatch):
   first = _run(monkeypatch, "11111111-1111-4111-8111-111111111111", TOKENS)
   second = _run(monkeypatch, "22222222-2222-4222-8222-222222222222", TOKENS)
   assert first.outcome == second.outcome == "completed"
   assert first.output_digest == second.output_digest


def test_different_tokens_still_hash_different(monkeypatch):
   first = _run(monkeypatch, "11111111-1111-4111-8111-111111111111", TOKENS)
   other = _run(monkeypatch, "11111111-1111-4111-8111-111111111111", [(0, "Hello", 9707), (1, " there", 1052)])
   assert first.output_digest != other.output_digest


def test_burst_exceeds_the_gateway_admission_bound(monkeypatch):
   bound = inspect.signature(RequestGatewayService.__init__).parameters["max_pending_requests"].default
   assert gate.GATEWAY_MAX_PENDING_REQUESTS == bound
   submitted = []

   def run(self, shape):
      submitted.append(shape.index)
      outcome = "overflowed" if len(submitted) > bound else "completed"
      return gate.RequestResult(shape, outcome, 0.0, None, 0.0, 0, None)

   monkeypatch.setattr(a5, "ProductSession", lambda _url: object())
   monkeypatch.setattr(gate.ProductClient, "run", run)
   monkeypatch.setattr(gate.ProductClient, "_zero_residue", lambda self, timeout=120.0: 0)
   product = gate.ProductClient("http://invalid.example")
   result = product.queue_overflow(SHAPE)
   assert len(submitted) > bound
   assert len(set(submitted)) == len(submitted)
   assert result == {"outcome": "failed_closed_within_scope", "residue": 0}
