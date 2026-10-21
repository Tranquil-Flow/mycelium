# SPDX-License-Identifier: AGPL-3.0-only
"""A worker guard trip must record WHICH guard fired.

Round 64r1, run `a13-qwen-20260916T195658Z`. The local member's guard fired one second
before the run-level `configure` became unknowable, and it wrote
`native-exit.private.json` as

    {"exception_types":[],"frames":[],"node_id":"remote-mac-6df0e88d...",
     "returncode":null,"run_id":"a13-qwen-20260916T195658Z"}

`_watch` calls `self._check()` and the exit test inside one `try`, and catches a
`WorkerError` from EITHER, then records here. `_check()` raises `WorkerError` for
`worker_membership_not_current` (:589), a resource-policy violation (:598),
`worker_wall_limit_exceeded` (:604), the storage/memory limits (:613-:670) -- and
`self._session` is never cleared (only :551 and :711), so `returncode: null` means
`_check()` raised and NO process exit occurred.

Dropping the guard's own code made the stop unfalsifiable: a membership trip, a policy
trip and a real exit were indistinguishable, and the artifact's name actively misled by
calling all three a "native exit". These tests pin the code into the record.
"""

import json
import types

import pytest

from test_worker import worker

from mycelium_a13_macos.worker import WorkerError


def _tripped(tmp_path, monkeypatch, code):
    """Run one guard iteration that trips on `code`, and return its record."""

    capacity = tmp_path / 'capacity'
    capacity.mkdir()
    capacity.chmod(0o700)
    w = worker(tmp_path, capacity_root=capacity)
    # No real process: the guard is what is under test, not the native session.
    w._session = types.SimpleNamespace(returncode=None, stderr=b'')

    def trip(*_args, **_kwargs):
        raise WorkerError(code)

    monkeypatch.setattr(w, '_check', trip)
    w._watch()
    return w, json.loads((w.capacity_root / 'native-exit.private.json').read_text())


def test_a_guard_trip_records_the_error_code_that_fired(tmp_path, monkeypatch):
    """RED before the fix: the record omitted the only field that names the cause."""

    w, record = _tripped(tmp_path, monkeypatch, 'worker_membership_not_current')
    try:
        assert record['error_code'] == 'worker_membership_not_current'
        # And it must stay honest about what did NOT happen.
        assert record['returncode'] is None
        assert record['protocol'] == 'mycelium.a13.native_exit_diagnostic.v1'
        assert record['run_id'] == 'run-test'
        assert record['node_id'] == 'mac-test'
    finally:
        w.close()


def test_a_policy_trip_is_distinguishable_from_a_membership_trip(tmp_path, monkeypatch):
    """Two different tripping guards must not produce the same record."""

    first_root = tmp_path / 'first'
    first_root.mkdir()
    _w1, membership = _tripped(first_root, monkeypatch, 'worker_membership_not_current')

    second_root = tmp_path / 'second'
    second_root.mkdir()
    _w2, policy = _tripped(second_root, monkeypatch, 'worker_disk_limit_exceeded')

    assert membership['error_code'] != policy['error_code']
    assert policy['error_code'] == 'worker_disk_limit_exceeded'


def test_a_limit_trip_records_the_observed_value_and_the_bound(tmp_path, monkeypatch):
    """66r1 tripped the memory guard and left NO numbers anywhere.

    Run `a13-qwen-20260916T215111Z`: the record named `worker_memory_limit_exceeded`
    (that much the code fix bought us) but not how far over the bound the group RSS
    actually was -- and the gap size is exactly what decides whether the resource
    policy is too small or the accounting is wrong. The guard measures the process
    GROUP while the proposal was derived from the heaviest worker stage peak alone,
    so the overshoot is the whole question. Pin that it is recorded.
    """

    capacity = tmp_path / 'capacity'
    capacity.mkdir()
    capacity.chmod(0o700)
    w = worker(tmp_path, capacity_root=capacity)
    w._session = types.SimpleNamespace(returncode=None, stderr=b'')

    def trip(*_args, **_kwargs):
        error = WorkerError('worker_memory_limit_exceeded')
        error.limit_detail = {
            'observed_group_rss_bytes': 6_000_000_000,
            'max_rss_bytes': 5_752_698_265,
            'overshoot_bytes': 247_301_735,
        }
        raise error

    monkeypatch.setattr(w, '_check', trip)
    try:
        w._watch()
        record = json.loads((w.capacity_root / 'native-exit.private.json').read_text())
        assert record['error_code'] == 'worker_memory_limit_exceeded'
        detail = record['error_detail']
        assert detail['observed_group_rss_bytes'] == 6_000_000_000
        assert detail['max_rss_bytes'] == 5_752_698_265
        assert detail['overshoot_bytes'] > 0
    finally:
        w.close()


def test_a_trip_without_detail_records_none(tmp_path, monkeypatch):
    """A guard that has no numbers to give must not invent any."""
    w, record = _tripped(tmp_path, monkeypatch, 'worker_membership_not_current')
    try:
        assert record['error_detail'] is None
    finally:
        w.close()
