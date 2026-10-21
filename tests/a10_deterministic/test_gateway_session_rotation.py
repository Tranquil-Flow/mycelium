# SPDX-License-Identifier: AGPL-3.0-only
"""CPU-only session-cap/TTL regression for the physical benchmark clients."""
from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

ensure_worktree_imports()

from concurrent.futures import ThreadPoolExecutor
import random
import threading
from types import SimpleNamespace

import pytest

from scripts import run_a10_physical_gate as a10
from scripts import run_a5_benchmark_gate as a5


class Gateway:
   """Browser ownership: 64 lifetime admissions, 3600-second TTL."""

   def __init__(self):
      self.now = 100.0
      self.sessions = []
      self.lock = threading.Lock()
      self.hold = False
      self.entered = threading.Event()
      self.release = threading.Event()
      self.stream_owners = []

   def session(self, base_url):
      gateway = self

      class Session:
         def __init__(self, base_url):
            self.base_url = base_url
            self.owner = self
            with gateway.lock:
               self.number = len(gateway.sessions)
               gateway.sessions.append(self)
            self.born = gateway.now
            self.attempts = 0

         def fork(self):
            child = object.__new__(type(self))
            child.owner = self.owner
            return child

         def submit(self, **kwargs):
            del kwargs
            owner = self.owner
            with gateway.lock:
               if gateway.now - owner.born >= 3600 or owner.attempts >= 64:
                  raise a5.GateError("http_post_429")
               owner.attempts += 1
               request_id = f"{owner.number}:{owner.attempts}"
            return {"event_path": request_id, "cancel_path": request_id}

         def request(self, method, path, **kwargs):
            del kwargs
            assert method == "GET"
            assert path.startswith(f"{self.owner.number}:")
            gateway.stream_owners.append((path, self.owner.number))
            if gateway.hold and path == "0:1":
               gateway.entered.set()
               assert gateway.release.wait(5), "held stream not released"

            class Stream:
               def __enter__(self):
                  return iter((b"event: completed\n",))

               def __exit__(self, *_args):
                  return False

            return Stream()

         def stream_summary(self, accepted):
            with self.request("GET", accepted["event_path"]):
               return {"token": 1}, "completed"

         def cancel(self, accepted):
            assert accepted["cancel_path"].startswith(f"{self.owner.number}:")

      return Session(base_url)


@pytest.fixture
def fake_gateway(monkeypatch):
   gateway = Gateway()
   # The benchmark creates a real ProductSession here; replace only transport.
   monkeypatch.setattr(a5, "ProductSession", gateway.session)
   monkeypatch.setattr(a10, "time", SimpleNamespace(monotonic=lambda: gateway.now))
   monkeypatch.setattr(a5, "time", SimpleNamespace(monotonic=lambda: gateway.now))
   return gateway


def shape(index):
   return a10.RequestShape(index, "prompt_short", "output_short", "interactive", 0.0)


def test_a10_rotates_before_gateway_lifetime_429(fake_gateway):
   client = a10.ProductClient("http://fixture.invalid")
   outcomes = [client.run(shape(index)).outcome for index in range(100)]
   assert outcomes == ["completed"] * 100, f"429 after 64: {outcomes.count('overflowed')} overflowed"
   assert [session.attempts for session in fake_gateway.sessions] == [32, 32, 32, 4]


def test_a10_rotates_at_half_ttl_and_retains_in_flight_owner(fake_gateway):
   fake_gateway.hold = True
   client = a10.ProductClient("http://fixture.invalid")
   with ThreadPoolExecutor(max_workers=2) as pool:
      first = pool.submit(client.run, shape(0))
      assert fake_gateway.entered.wait(5)
      fake_gateway.now += 1800
      second = client.run(shape(1))
      fake_gateway.release.set()
      assert first.result(timeout=5).outcome == "completed"
   assert second.outcome == "completed"
   assert [session.attempts for session in fake_gateway.sessions] == [1, 1]
   assert fake_gateway.stream_owners == [("0:1", 0), ("1:1", 1)]


def test_a10_concurrent_rotations_are_bounded(fake_gateway):
   client = a10.ProductClient("http://fixture.invalid")
   with ThreadPoolExecutor(max_workers=48) as pool:
      outcomes = list(pool.map(lambda index: client.run(shape(index)).outcome, range(160)))
   assert outcomes == ["completed"] * 160
   assert [session.attempts for session in fake_gateway.sessions] == [32] * 5


def test_a5_window_rotates_before_gateway_lifetime_429(fake_gateway):
   driver = a5._WindowDriver(fake_gateway.session("http://fixture.invalid"), random.Random(1))
   for index in range(1, 101):
      driver._submit_one(index, a5._RequestShape(
         "short", 16, "test", 8, "interactive", "interactive_chat_v1", "interactive"
      ))
   assert driver._measured_outcomes["rejected_admission"] == 0
   assert [session.attempts for session in fake_gateway.sessions] == [32, 32, 32, 4]


def test_a5_window_rotates_before_half_ttl(fake_gateway):
   driver = a5._WindowDriver(fake_gateway.session("http://fixture.invalid"), random.Random(1))
   request_shape = a5._RequestShape("short", 16, "test", 8, "interactive", "interactive_chat_v1", "interactive")
   driver._submit_one(1, request_shape)
   fake_gateway.now += 1799
   driver._submit_one(2, request_shape)
   assert [session.attempts for session in fake_gateway.sessions] == [2]
   fake_gateway.now += 1
   driver._submit_one(3, request_shape)
   assert [session.attempts for session in fake_gateway.sessions] == [2, 1]


def test_a5_concurrent_window_submissions_obey_session_cap(fake_gateway):
   driver = a5._WindowDriver(fake_gateway.session("http://fixture.invalid"), random.Random(1))
   request_shape = a5._RequestShape("short", 16, "test", 8, "interactive", "interactive_chat_v1", "interactive")
   with ThreadPoolExecutor(max_workers=48) as pool:
      list(pool.map(lambda index: driver._submit_one(index, request_shape), range(1, 161)))
   assert [session.attempts for session in fake_gateway.sessions] == [32] * 5
   assert driver._measured_outcomes["rejected_admission"] == 0
