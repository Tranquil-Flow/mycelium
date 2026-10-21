# SPDX-License-Identifier: AGPL-3.0-only
"""The soak observer's evidence discipline (packet ``wp2-8`` task 1/2).

The red artifact ``runs/wp2-5/soak-flake-observation/`` fired
``nonterminal_row_not_retired_within_bound`` for ``request-iroh-e2e-001`` with
``last_sequence=-1`` and ``note='absent_from_session_table'``, inside the bound, once in three
runs and never again.  The design review (``rca/wp2-harness-assertion-design-review.md``,
instance 3) leaves one question open: is that a product lifecycle defect (a durable row
retired without a terminal) or a harness mis-observation (the observer declared an absence
from a table that may never have held the row)?

These tests pin what the observer may and may not declare, and they fail while the observer
can still look too late:

* ``test_the_observer_refuses_to_retire_a_row_the_table_never_held`` is the RED case - the
  observer must not mint ``absent_from_session_table`` from a table that never held the row,
  which is the only local mechanism that reproduces the artifact's ``note`` + ``last_sequence
  =-1`` signature (see ``runs/wp2-8/flake-rate/observer_probe.txt``);
* ``test_the_observer_retires_a_row_the_table_did_hold`` is the discriminating control: the
  same declaration must still fire, and still fire the invariant, when the table really did
  hold the row.  A guard that silences the invariant entirely would fail here.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from mycelium_testbed import soak


def _bounds() -> soak.SoakBounds:
    return soak.soak_bounds()


def _session() -> soak.SoakSession:
    return soak.SoakSession(seed=1, bounds=_bounds())


def _episode(session: soak.SoakSession, tmp_path, table: dict[str, Any]) -> soak.MailboxEpisode:
    config = soak.SoakConfig(
        seeds=(1,),
        events=0,
        run_id="observer-evidence",
        out_root=tmp_path,
        native_binary=None,
    )
    episode = soak.MailboxEpisode(
        config=config, session=session, run_dir=tmp_path / "mailbox"
    )
    episode.service = SimpleNamespace(_sessions=table)
    return episode


def _admit_mailbox_row(session: soak.SoakSession, request_id: str) -> None:
    session.ledger.admit_request(
        request_id,
        bound_name="cancel_and_cleanup_budget_seconds",
        bound_seconds=_bounds().request_terminal_cancelled_seconds,
        scope="mailbox",
        bound_origin="last_progress",
    )


def _live_row_session(*, latest_sequence: int) -> SimpleNamespace:
    """The shape the observer reads out of a real gateway session."""

    return SimpleNamespace(
        condition=threading.Condition(),
        latest_sequence=latest_sequence,
        terminal_event=None,
        worker_done=False,
        active_subscription=True,
    )


def _codes(session: soak.SoakSession) -> set[str]:
    return {violation.code for violation in session.ledger.check_invariants(_bounds())}


def test_the_observer_refuses_to_retire_a_row_the_table_never_held(tmp_path) -> None:
    """RED: a table that never held the row is not evidence that the product retired it.

    This is the local reproduction of the artifact's signature: without this guard the
    observer declares ``absent_from_session_table`` with ``last_sequence=-1`` for a row it
    never sampled present, and the invariant fires inside the bound.
    """

    session = _session()
    request_id = "request-soak-0001-001"
    _admit_mailbox_row(session, request_id)
    episode = _episode(session, tmp_path, table={})
    episode.request_id = request_id

    episode._observe()

    row = session.ledger.rows[request_id]
    assert row.retired_at is None, row.retirement_note
    assert row.retirement_note == ""
    assert soak.INVARIANT_NONTERMINAL_ROW_NOT_RETIRED_WITHIN_BOUND not in _codes(session)
    # The refusal must be recorded, not silent: a gap is an observation, never a green.
    assert any(
        gap.startswith(soak.RETIREMENT_WITHOUT_MEMBERSHIP_EVIDENCE)
        for gap in session.ledger.observation_gaps
    ), session.ledger.observation_gaps


def test_the_observer_retires_a_row_the_table_did_hold(tmp_path) -> None:
    """Discriminating control: a real absence still fires the invariant."""

    session = _session()
    request_id = "request-soak-0002-001"
    _admit_mailbox_row(session, request_id)
    episode = _episode(session, tmp_path, table={request_id: _live_row_session(latest_sequence=7)})
    episode.request_id = request_id

    episode._observe()  # the table holds the row: sampled, nothing declared

    row = session.ledger.rows[request_id]
    assert row.retired_at is None
    assert row.present_samples == 1
    assert row.last_sequence == 7

    episode.service = SimpleNamespace(_sessions={})  # the row leaves the table
    episode._observe()

    assert row.retired_at is not None
    assert row.retirement_note == "absent_from_session_table"
    assert soak.INVARIANT_NONTERMINAL_ROW_NOT_RETIRED_WITHIN_BOUND in _codes(session)


def test_the_retirement_detail_names_the_sequences_and_the_membership_evidence(tmp_path) -> None:
    """A judgement must carry the evidence it was made on (the packet's guard)."""

    session = _session()
    request_id = "request-soak-0003-001"
    _admit_mailbox_row(session, request_id)
    episode = _episode(session, tmp_path, table={request_id: _live_row_session(latest_sequence=4)})
    episode.request_id = request_id
    episode._observe()
    episode.service = SimpleNamespace(_sessions={})
    episode._observe()

    violations = [
        violation
        for violation in session.ledger.check_invariants(_bounds())
        if violation.code == soak.INVARIANT_NONTERMINAL_ROW_NOT_RETIRED_WITHIN_BOUND
    ]
    assert violations, session.ledger.rows[request_id]
    detail = violations[0].detail
    assert "note='absent_from_session_table'" in detail
    assert "sequence_source=session_table" in detail
    assert "observed_sequences=[4]" in detail
    assert "present_samples=1" in detail


def test_the_canonical_request_row_is_never_judged_against_the_mailbox_table(tmp_path) -> None:
    """The red artifact's shape: a canonical-request row is not the mailbox's to retire.

    On the committed code this passes (the observer already filters by row scope); it is
    pinned here because a pre-guard revision of exactly this observer is the only reading
    that produces the artifact's ``subject=request-iroh-e2e-001`` +
    ``note='absent_from_session_table'`` pair - and because a future refactor must not
    re-open it.
    """

    session = _session()
    request_id = "request-iroh-e2e-001"
    session.ledger.admit_request(
        request_id,
        bound_name="node_command_timeout_seconds",
        bound_seconds=_bounds().request_terminal_driver_seconds,
        scope="real_request",
        bound_origin="registration",
    )
    episode = _episode(session, tmp_path, table={})
    episode.request_id = "request-soak-0001-001"

    episode._observe()

    row = session.ledger.rows[request_id]
    assert row.retired_at is None
    assert row.retirement_note == ""
    assert row.last_sequence == -1  # the constructor default: no session sequence exists
    assert soak.INVARIANT_NONTERMINAL_ROW_NOT_RETIRED_WITHIN_BOUND not in _codes(session)


def test_the_observer_records_the_table_membership_it_sampled(tmp_path) -> None:
    """Presence is counted per sample, so a declaration can be audited after the fact."""

    session = _session()
    request_id = "request-soak-0004-001"
    _admit_mailbox_row(session, request_id)
    episode = _episode(session, tmp_path, table={request_id: _live_row_session(latest_sequence=2)})
    episode.request_id = request_id

    episode._observe()
    episode._observe()

    row = session.ledger.rows[request_id]
    assert row.membership_samples == 2
    assert row.present_samples == 2
    assert row.first_present_at is not None and row.last_present_at is not None


def test_the_real_request_episode_records_the_trace_sequences_it_saw(tmp_path) -> None:
    """A row whose lifecycle is a trace records trace sequences, with their source named."""

    session = _session()
    request_id = "request-iroh-e2e-001"
    session.ledger.admit_request(
        request_id,
        bound_name="node_command_timeout_seconds",
        bound_seconds=_bounds().request_terminal_driver_seconds,
        scope="real_request",
        bound_origin="registration",
    )
    events = [
        SimpleNamespace(
            kind="enqueue", sequence=3, status="ok", host_id="node-a", request_id=request_id
        ),
        SimpleNamespace(
            kind="terminal", sequence=9, status="failed", host_id="node-a", request_id=request_id
        ),
    ]

    soak._record_request_lifecycle(session, request_id, events)

    row = session.ledger.rows[request_id]
    assert row.sequence_source == "trace"
    assert row.observed_sequences == [3, 9]
    assert row.last_sequence == 9
    evidence = session.ledger.lifecycle_evidence(request_id)
    assert [entry["kind"] for entry in evidence["events"]] == ["enqueue", "terminal"]
    assert evidence["sequence_source"] == "trace"
    assert evidence["terminal_seen"] is True


def test_a_row_that_reached_its_terminal_is_never_a_nonterminal_retirement(tmp_path) -> None:
    """The invariant's own definition, exercised through the observer."""

    session = _session()
    request_id = "request-soak-0005-001"
    _admit_mailbox_row(session, request_id)
    session.ledger.observe_row_terminal(request_id, kind="completed", code=None)
    episode = _episode(session, tmp_path, table={request_id: _live_row_session(latest_sequence=1)})
    episode.request_id = request_id
    episode._observe()
    episode.service = SimpleNamespace(_sessions={})
    episode._observe()

    assert soak.INVARIANT_NONTERMINAL_ROW_NOT_RETIRED_WITHIN_BOUND not in _codes(session)


@pytest.mark.parametrize("terminal_mode", ["missing", "failed", "closed"])
def test_finish_observes_the_full_row_relative_stall_budget(
    tmp_path, monkeypatch, terminal_mode
) -> None:
    clock = {"now": 1_000.0}
    monkeypatch.setattr(soak.time, "monotonic", lambda: clock["now"])
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(soak.time, "sleep", sleep)
    session = _session()
    session.ledger.started_at = clock["now"]
    request_id = "request-finish-stall"
    _admit_mailbox_row(session, request_id)
    live_row = _live_row_session(latest_sequence=0)
    episode = _episode(session, tmp_path, table={request_id: live_row})
    episode.request_id = request_id
    bounds = _bounds()
    polls = []
    max_polls = int(
        (bounds.control_loop_stall_seconds + bounds.request_terminal_cancelled_seconds)
        / bounds.poll_seconds
    ) + 3

    def next_event(*, timeout):
        polls.append(clock["now"])
        assert len(polls) <= max_polls, "finish busy-spins on a closed subscription"
        if terminal_mode == "closed":
            # A dropped terminal leaves the subscription at EOF: no blocking poll.
            return None
        clock["now"] += timeout
        if terminal_mode == "failed" and session.ledger.elapsed() >= (
            bounds.control_loop_stall_seconds + bounds.request_terminal_cancelled_seconds / 2
        ):
            live_row.latest_sequence = 1
            live_row.terminal_event = SimpleNamespace(
                request_id=request_id, sequence=1, kind="failed", code="backend_failed",
                terminal=True,
            )
            return live_row.terminal_event
        raise TimeoutError

    episode.subscription = SimpleNamespace(next_event=next_event, ack=lambda sequence: None)
    episode._observe()
    episode.finish()

    row = session.ledger.rows[request_id]
    assert row.stalled_at is not None
    pending = session.ledger.elapsed() - row.stalled_at
    if terminal_mode == "failed":
        assert row.terminal_kind == "failed"
        assert pending < bounds.request_terminal_cancelled_seconds
        assert soak.INVARIANT_REQUEST_WITHOUT_TERMINAL_FRAME_WITHIN_BUDGET not in _codes(session)
    else:
        assert pending >= bounds.request_terminal_cancelled_seconds
        assert soak.INVARIANT_REQUEST_WITHOUT_TERMINAL_FRAME_WITHIN_BUDGET in _codes(session)
    if terminal_mode == "closed":
        assert sleeps
        assert all(seconds == bounds.poll_seconds for seconds in sleeps)
    assert len(polls) <= max_polls


def test_the_observer_refusal_is_a_named_constant() -> None:
    """The class guard has a stable name so a report can name it."""

    assert soak.RETIREMENT_WITHOUT_MEMBERSHIP_EVIDENCE == "retirement_without_membership_evidence"
