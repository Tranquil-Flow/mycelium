# SPDX-License-Identifier: AGPL-3.0-only
"""A7 durable fence store on the win32 backend (FakeWin32Api): Windows NumPy successors.

The same CAS, single-publisher, quarantine, reconciliation and last-cutover
contracts as tests/a7_acceptance, with owner-only private directories and the
LockFileEx lock from mycelium_host_platform instead of POSIX modes and flock.
"""

from __future__ import annotations

import threading

import pytest

from mycelium_a7_fence_store import DurableFenceStore, FenceStoreError, request_key

from tests.host_platform.fake_win32 import OTHER_USER_SID, owner_only, simulate_win32

REQ = "request:a7-fence-win"


def _code(call, *args, **kwargs):
    with pytest.raises(FenceStoreError) as refused:
        call(*args, **kwargs)
    return refused.value.code


@pytest.fixture
def fake(monkeypatch):
    return simulate_win32(monkeypatch)


def test_cas_publication_terminal_and_restart(fake, tmp_path):
    store = DurableFenceStore(tmp_path / "fence")
    first = store.advance(REQ, expected_fencing_generation=0)
    assert first["fencing_generation"] == 1
    assert _code(store.advance, REQ, expected_fencing_generation=0) == "fence_generation_conflict"
    assert store.grant_publication(REQ, generation=1)["published_generation"] == 1
    assert _code(store.grant_publication, REQ, generation=1) == "duplicate_publisher"
    restarted = DurableFenceStore(tmp_path / "fence")
    assert restarted.current(REQ)["fencing_generation"] == 1
    assert any(call[0] == "lock_file" for call in fake.calls)
    store.mark_terminal(REQ, generation=1, outcome="full_context_replay")
    assert store.release(REQ) is True
    assert store.current(REQ) is None


def test_concurrent_grants_in_one_process_admit_exactly_one(fake, tmp_path):
    store = DurableFenceStore(tmp_path / "fence")
    store.advance(REQ, expected_fencing_generation=0)
    results = []

    def grant():
        try:
            results.append(store.grant_publication(REQ, generation=1)["published_generation"])
        except FenceStoreError as error:
            results.append(error.code)

    threads = [threading.Thread(target=grant) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(1) == 1 and results.count("duplicate_publisher") == 7


def test_tampered_or_foreign_owned_record_is_quarantined(fake, tmp_path):
    root = tmp_path / "fence"
    store = DurableFenceStore(root)
    store.advance(REQ, expected_fencing_generation=0)
    record = root / f"{request_key(REQ)}.json"
    record.write_bytes(record.read_bytes().replace(b'"fencing_generation":1', b'"fencing_generation":9'))
    assert _code(store.current, REQ) == "fence_record_digest_mismatch"
    assert not record.exists() and list((root / "quarantine").iterdir())
    reconciled = store.reconcile(REQ, shadow_checkpoint_valid=True, a6_available=True)
    assert (reconciled["state"], reconciled["reason"]) == ("fallback_required", "fence_record_quarantined")
    other = tmp_path / "fence2"
    second = DurableFenceStore(other)
    second.advance(REQ, expected_fencing_generation=0)
    fake.set_security(other / f"{request_key(REQ)}.json", owner_only(OTHER_USER_SID))
    assert _code(second.current, REQ) == "fence_record_permissions_invalid"


def test_last_cutover_persists_for_the_same_request_and_node_only(fake, tmp_path):
    store = DurableFenceStore(tmp_path / "fence")
    store.persist_last_cutover(REQ, node_id="node-win", snapshot={"state": "cut_over"})
    assert DurableFenceStore(tmp_path / "fence").last_cutover(REQ, node_id="node-win") == {"state": "cut_over"}
    assert store.last_cutover(REQ, node_id="node-other") is None
    assert store.last_cutover("request:other", node_id="node-win") is None
