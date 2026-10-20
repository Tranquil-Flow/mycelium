# SPDX-License-Identifier: AGPL-3.0-only
"""Per-request tables in the request gateway stay bounded for the life of the process.

Every table keyed by a request id used to grow with the request count when the request never reached its
cleanup (cancel for a request that never ran, an ack that never comes, an HTTP client that never forgets a
token).  Entries are evicted oldest-first at a hard cap and, where a lifetime is known, by age.
"""
from __future__ import annotations

import pytest

from mycelium_request_gateway.backend import RouterSessionBackend
from mycelium_request_gateway.bounded import BoundedIdMap, BoundedIdSet
from mycelium_request_gateway.client import HTTPGatewayClient


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_map_evicts_the_oldest_entry_at_the_cap():
    table = BoundedIdMap(3)
    for index in range(5):
        table[f"r{index}"] = index
    assert len(table) == 3
    assert table.get("r0") is None and table.get("r1") is None
    assert [table.get(f"r{i}") for i in (2, 3, 4)] == [2, 3, 4]


def test_map_rewrite_keeps_the_entry_and_refreshes_its_age_order():
    table = BoundedIdMap(2)
    table["a"] = 1
    table["b"] = 2
    table["a"] = 3  # a is now the newest
    table["c"] = 4
    assert "a" in table and "c" in table and "b" not in table
    assert table.pop("a") == 3 and table.pop("a", None) is None


def test_map_expires_entries_by_age_with_an_injected_clock():
    clock = Clock()
    table = BoundedIdMap(10, ttl_seconds=60, clock=clock)
    table["old"] = 1
    clock.now = 59
    table["new"] = 2
    assert table.get("old") == 1
    clock.now = 61
    assert table.get("old") is None and "old" not in table
    assert table.get("new") == 2 and len(table) == 1


def test_set_is_bounded_and_ages_out():
    clock = Clock()
    ids = BoundedIdSet(4, ttl_seconds=10, clock=clock)
    for index in range(10):
        ids.add(f"r{index}")
    assert len(ids) == 4 and "r9" in ids and "r5" not in ids
    ids.discard("r9")
    ids.discard("missing")
    assert "r9" not in ids
    clock.now = 11
    assert "r8" not in ids and len(ids) == 0


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, None, "3"])
def test_cap_must_be_a_positive_integer(bad):
    with pytest.raises(ValueError, match="invalid_max_entries"):
        BoundedIdMap(bad)
    with pytest.raises(ValueError, match="invalid_max_entries"):
        BoundedIdSet(bad)


class _Router:
    def cancel(self, request_id):
        return True


class _Codec:
    pass


def _backend(cap):
    return RouterSessionBackend(router=_Router(), codec=_Codec(), clock=lambda: 0.0, max_tracked_requests=cap)


def test_cancels_for_requests_that_never_run_do_not_accumulate_in_the_backend():
    backend = _backend(64)
    for index in range(2_000):
        backend.cancel_with_deadline(f"never-ran-{index}", deadline_monotonic_s=float(index))
    assert len(backend._cancellation_deadlines) <= 64
    assert len(backend._pending_cancelled) <= 64
    assert backend._cancellation_deadlines.get("never-ran-1999") == 1999.0  # the newest survive
    assert "never-ran-1999" in backend._pending_cancelled


def test_publisher_generations_for_requests_that_never_finish_are_bounded():
    backend = _backend(64)
    for index in range(2_000):
        assert backend.update_publisher_generation(f"r{index}", expected_generation=1, new_generation=2) in {True, False}
    assert len(backend._publisher_generations) <= 64


def test_awaiting_ack_set_is_bounded():
    backend = _backend(32)
    for index in range(500):
        backend._awaiting_cancel_ack.add(f"r{index}")
    assert len(backend._awaiting_cancel_ack) <= 32


def test_backend_cap_is_validated():
    with pytest.raises(ValueError, match="invalid_max_entries"):
        _backend(0)


def test_http_client_forgets_old_owner_tokens():
    client = HTTPGatewayClient(base_url="http://127.0.0.1:9", bearer_token="x" * 16, max_tracked_requests=16)
    for index in range(400):
        request_id = f"request-{index:04d}"
        client._json_request = lambda *a, _r=request_id, **k: {"request_id": _r, "session_token": "tok-" + _r}
        client.submit(object.__new__(type("S", (), {"to_dict": lambda self: {}})))
    assert len(client._session_tokens) <= 16
    assert client._session_tokens.get("request-0399") == "tok-request-0399"
    assert client._session_tokens.get("request-0000") is None
