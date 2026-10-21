# SPDX-License-Identifier: AGPL-3.0-only
"""RED-first: the target's claim path must be woken, not polled on an interval.

Measured defect this reproduces (DIAGNOSIS-p81r1-cleanup-unproven.md, head 7d0ffb6,
run a13-qwen-20260920T105701Z, request a0281568): the LAPTOP peer's control pump
claimed ``infer_cancel_wait`` at **3 957 ms of that command's 4 427 ms life** and then
needed **3 862 ms** to return it -- i.e. it was claimed with **470 ms** of window left
and could never complete, so the mailbox recorded ``reconciliation_required`` and the
route withheld the terminal as ``request_terminal_blocked_cleanup_unproven``. Six
``snapshot`` commands expired UNCLAIMED outright (one with a 187 ms window) while the
OWNER node, on the same head, claimed the same command family at 64-220 ms.

The inversion is between the *delivery path* and the window it must fit inside:
``claim latency + execution latency > window``.  PLAN.md D9 fixes exactly that side:

    "If the measured p99 exceeds the window, fix the delivery path (e.g. long-poll
     wake-up instead of interval polling), not the 2 s budget."

So the claimed behaviour under test is: **an enqueue must WAKEN the target's claim
path; the claim latency must not be a function of any poll interval.**  A command whose
window is shorter than (delivery interval + execution) must still be claimed and
complete once the wake-up exists.

Nothing here changes a timeout, deadline, cleanup window, skew or staleness constant:
the only numbers the tests drive are the *test's own* owner-side window and a
test-configuration of an existing, validated constructor parameter
(``long_poll_step``, whose production default is asserted unchanged below).

**What the wake-up assertions are measured against, and why.**  The guarantee above is
a statement about *delivery*: an enqueue must waken the parked poll, so a park is ended
by the enqueue rather than by the poll interval.  The quantity that states it is
therefore measured **from the delivery signal** -- the moment ``enqueue`` called
``ManagedControlMailbox._signal_delivery``, after its row was committed
(``_delivery_wake_evidence``) -- and not from before the owner's own enqueue.  Those
are different quantities by a wide margin under load:

* the *owner-side enqueue* (Ed25519 signing, the mailbox lock, ``BEGIN IMMEDIATE``, the
  ``synchronous=FULL`` commit, and the loopback-TLS request/response)
  is **not** part of the delivery path, and it is what this test's number used to be
  dominated by.  Measured here on one host at step 0.02, one sample: an enqueue that
  cost 0.075 s delivered in 0.041 s -- i.e. 65 % of the number belonged to the owner's
  own bookkeeping, and the L2's ``0.147 s`` splits as ``enqueue_cost = 0.145 s`` plus
  ``delivery_cost = 0.042 s`` (packet ``m0-delivery-wakeup-metric``);
* at ``step = 0.02`` the poll interval is **shorter than the wake-up it is supposed to
  lose to**, so no wall-clock bound can tell the two apart there at all: with the
  wake-up removed outright, the interval-gated path costs 0.018-0.032 s at that step,
  comfortably *inside* the old bound.  A bound that a flatly interval-gated
  implementation passes is not measuring the guarantee, whatever it does under load.

So the assertions below are the two statements that *do* separate the paths, and they
are load-independent because they are not latency comparisons:

1. the enqueue **signalled** the parked polls (no signal at all is the interval-gated
   implementation, at every step);
2. the park never **started** an interval sleep after the command was durable -- the
   generation has already moved by then, so such a wait returns at once, and a wait that
   actually times out is the interval, not the wake-up (this one holds at ``step = 0.02``
   too, where the latency comparison cannot see the difference);

and the wall-clock bound it keeps is unchanged (``WAKE_BOUND_SECONDS``) and still the
pinned statement -- claim latency must not be a function of the poll interval -- with
its anchor moved onto the delivery path, so its number is the wake-up's own cost.

**The control loop's own wait, measured the same way.**
``test_native_completion_wakes_the_control_loop_before_its_interval_expires`` states the
same guarantee on the *pump's* wait (``TargetControlPump.wake`` / ``wait_for_wake``), and
it used to measure its latency from **before the native command it stubs**: ``release.set()``
unblocks the stub, the real ``send`` then runs over the sidecar, and only when that future
completes does its done-callback signal ``wake``.  Measured on one host at load1 8.6-13.1,
ten samples: the stubbed command's own execution was 0.028-0.047 s of a 0.028-0.047 s total
(99.0-99.3 %), and the signal-to-release cost the assertion is actually about was
0.00010-0.00024 s (<= 0.5 %) in the same samples.  A second ten-sample batch on the same
host at load1 5.6-6.2 measured the same shape at 0.016-0.020 s of 0.016-0.021 s with the
signal-to-release cost at 0.00005-0.00007 s: the quantity the old anchor measured tracks
host load (16 ms -> 47 ms between the two batches), the one it must measure does not.
The consumed failure (0.1046 s against
``< 0.1 s``) is that same pre-signal quantity at L2 self-load.  So it is re-anchored onto
the pump's own delivery signal, exactly as above, and the facts that separate a wake-up from
interval polling are asserted directly instead: the completion **signalled**; the signal
**moved** the state the parked wait measures (``_delivery_wakeup``); and the parked wait was
**ended** by that signal -- ``threading.Event.wait`` returns True only when the flag was set,
so a wait that returns False paid its full timeout, which is the interval and not the
wake-up.  The bound itself is unchanged (``WAKE_BOUND_SECONDS``).

Offline and fleet-free: a loopback-TLS mailbox, a real native worker and the real
``ManagedTargetRuntime`` control loop on one machine.  No Mac, no relay, no fleet.
"""
from __future__ import annotations
import inspect
import threading
import time

import pytest

from test_control_pump import _world as _pump_world
from test_managed_control import _enqueue, _world, DEPLOYMENT_ID, RUN_ID
from test_managed_runtime import runtime
from test_control_session import owner, session
from test_worker import worker
from mycelium_a13_macos.concurrent_pump import ConcurrentTargetControlPump
from mycelium_a13_macos.managed_control import (
    ManagedControlMailbox,
    _LONG_POLL_HOLD_SECONDS,
    _LONG_POLL_MAX_HOLD_SECONDS,
    _LONG_POLL_STEP_SECONDS,
    _empty_poll_delivery,
)
from mycelium_a13_macos.managed_runtime import ManagedTargetRuntime

#: The window THIS test's owner waits for one command, in this offline reproduction.
#: Not a production constant: it stands in for the measured ~4.0-4.5 s cleanup window
#: and keeps the test bounded, exactly as p81r1's route waiter did.
#:
#: It is a real-clock budget for the *whole* owner->target->worker->owner round trip
#: (~0.08 s idle), so what matters is its headroom over that trip, not its size: at 0.7 s
#: the headroom was 8.7x, which the L2 rehearsal's full-suite load consumed once
#: (``command_deadline_exceeded``).  4.0 s is ~50x.  It must stay *shorter than the
#: interval it is tested against* (``E2E_POLL_STEP_SECONDS`` / ``E2E_POLL_HOLD_SECONDS``,
#: asserted below), because that inversion is the whole point: an interval-gated claim
#: lands at about one step (~5 s) and cannot fit in this window at any load.
COMMAND_WINDOW_SECONDS = 4.0

#: Poll step and park hold of the end-to-end test's mailbox: the largest values the
#: validated constructor accepts (``_LONG_POLL_MAX_HOLD_SECONDS``).  The window above
#: scales with them instead of the interval shrinking toward the window.  Both bound the
#: no-wake-up claim latency: the park ends at the hold and the pump re-polls ~50 ms later,
#: so the hold must be as long as the step or an interval-gated path would still land
#: inside the window.
E2E_POLL_STEP_SECONDS = 5.0
E2E_POLL_HOLD_SECONDS = 5.0

#: Test configuration of ``ManagedControlMailbox(long_poll_step=...)``, an existing
#: validated parameter (``0 < step <= _LONG_POLL_MAX_HOLD_SECONDS``).  The defect class
#: is "claim latency is a function of the poll interval"; it is only *observable* if
#: some interval exists, so the test drives it to a value at which the inversion is
#: measurable in both directions.  The production default is asserted unchanged, and
#: the same test is run at that default too, where the same inversion exists in
#: miniature (up to one interval of dead time per park).
OBSERVABLE_POLL_STEP_SECONDS = 1.0

#: The longest the wake-up itself may cost: measured from the delivery signal (the
#: enqueue's committed, deliverable moment -- `_delivery_wake_evidence`) to the parked
#: poll returning the command.  Generous next to the measured local claim of 64-220 ms
#: and far below one interval of either step under test.  Unchanged bound; the anchor
#: moved from before the owner-side enqueue (which is not the delivery path and which
#: dominated this number under load) onto the delivery path -- see the module docstring.
WAKE_BOUND_SECONDS = 0.1


class _HoldClock:
    """The mailbox's own clock for the park's hold, advanced only by the test.

    ``ManagedControlMailbox(monotonic=...)`` is the clock ``poll`` measures its
    ``long_poll_hold`` against.  The test parks a poll and enqueues "from inside the
    hold"; on the wall clock that is a race against the owner-side enqueue (signing, a
    ``synchronous=FULL`` commit and a loopback-TLS round trip), which a loaded host can
    stretch past the whole 1.5 s hold, so the hold would end the park empty before the
    command exists.  On this clock the hold cannot end until ``expire`` -- which the test
    calls only on teardown, so a wake-up that never comes ends the poll rather than
    leaving its thread parked.  The hold constant itself is untouched (asserted above)
    and ``long_poll_step`` still bounds each interval wait on the real clock.
    """

    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        return self._now

    def expire(self) -> None:
        self._now = float("inf")


def _production_poll_defaults_are_untouched() -> None:
    """The fix is the delivery path.  These are the shipped numbers, unchanged."""

    assert _LONG_POLL_HOLD_SECONDS == 1.5
    assert _LONG_POLL_STEP_SECONDS == 0.02


_E2E_MAILBOX_OPTIONS = {
    "long_poll_step": E2E_POLL_STEP_SECONDS,
    "long_poll_hold": E2E_POLL_HOLD_SECONDS,
}


def _the_window_is_shorter_than_the_interval_it_is_tested_against() -> None:
    """The inversion under test: the window cannot fit a poll interval or a park."""

    assert COMMAND_WINDOW_SECONDS < E2E_POLL_STEP_SECONDS
    assert COMMAND_WINDOW_SECONDS < E2E_POLL_HOLD_SECONDS
    assert E2E_POLL_STEP_SECONDS <= _LONG_POLL_MAX_HOLD_SECONDS
    assert E2E_POLL_HOLD_SECONDS <= _LONG_POLL_MAX_HOLD_SECONDS


def _the_window_command_was_woken_not_polled(record: dict) -> None:
    """Load-independent evidence that the window command was delivered by the wake-up.

    The latency of a woken command is host speed; whether an *interval* ended its park is
    not.  The enqueue signalled in a way the parked poll can observe, and no interval wait
    started after that signal slept itself out.
    """

    signals = record["signals"]
    assert signals, "no delivery signal: the command could only be claimed by an interval attempt"
    assert record["generation_bumps"] >= 1
    slept_out = [w for w in record["waits"] if not w[0] and w[1] >= signals[0]]
    assert not slept_out, (
        f"{len(slept_out)} poll-interval wait(s) started after the command was durable "
        "and timed out: the claim path waited out its interval instead of the wake-up"
    )


def _delivery_wake_evidence(monkeypatch) -> dict:
    """Observe the mailbox's own delivery wake-up, without changing it.

    ``ManagedControlMailbox`` is an in-process object in this reproduction, so the test
    can see the two facts the guarantee is stated in terms of:

    * ``signals`` -- when ``enqueue`` called ``_signal_delivery``.  It is called after
      the row is committed and is the moment the command became deliverable.  Everything
      the owner spends before that point (Ed25519, the mailbox lock, ``BEGIN IMMEDIATE``,
      ``synchronous=FULL``, the loopback-TLS round trip) is owner-side bookkeeping, not
      the delivery path, and measuring a *delivery* bound from before it is what made
      this test's number a function of host load instead of a function of the wake-up.
      ``generation_bumps`` counts the signals the parked poll's own protocol could
      actually observe (the generation ``_wait_for_delivery`` measures its wait against):
      a call that leaves the waiter's state untouched cannot waken it, so "was it called"
      is the weaker half of the wiring assertion.
    * ``waits`` -- every ``_wait_for_delivery`` inside the park, as
      ``(released_by_signal, started_at, ended_at)``.  The invariant that separates a
      wake-up from interval polling is that the poll never *starts* an interval sleep
      once the command is durable: the generation has already moved, so such a wait
      returns immediately instead of timing out a whole ``long_poll_step`` later.
    """

    real_signal = ManagedControlMailbox._signal_delivery
    real_wait = ManagedControlMailbox._wait_for_delivery
    record: dict = {
        "signals": [], "generation_bumps": 0, "waits": [],
        # Set when the poll has entered its first interval wait, i.e. is provably parked.
        "parked": threading.Event(),
    }

    def signal(self):
        before = self._delivery_generation_now()
        real_signal(self)
        record["signals"].append(time.monotonic())
        if self._delivery_generation_now() > before:
            record["generation_bumps"] += 1

    def wait(self, timeout, *, observed=None):
        started = time.monotonic()
        record["parked"].set()
        released = real_wait(self, timeout, observed=observed)
        record["waits"].append((bool(released), started, time.monotonic()))
        return released

    monkeypatch.setattr(ManagedControlMailbox, "_signal_delivery", signal)
    monkeypatch.setattr(ManagedControlMailbox, "_wait_for_delivery", wait)
    return record


def _assert_the_enqueue_woke_the_park(
    record: dict, box: dict, *, step_seconds: float, owner_enqueue_cost: float
) -> None:
    """The stated guarantee, on quantities the owner-side enqueue cannot dominate.

    Each of these is false for an interval-gated delivery path -- at *both* steps, and
    independently of how loaded the host is:

    * an enqueue that makes a command deliverable **signals** the parked polls -- and the
      signal is one the parked poll's own wait protocol can *observe*, not merely a call.
      A poll loop that only re-attempts on an interval never signals at all, and one that
      signals without moving the state the waiter measures against cannot waken it either;
    * the park never starts an interval sleep after that signal.  The generation is
      already past the observed one, so a wait entered at/after the signal returns at
      once; one that times out means the interval, not the wake-up, ended the park.
    * the claim latency, measured from the signal, is under ``WAKE_BOUND_SECONDS``.

    ``owner_enqueue_cost`` is reported for diagnosis only: it is the owner-side cost the
    bound deliberately excludes, so a future failure here names how much of the old
    number was never the delivery path.
    """

    signals = record["signals"]
    assert signals, (
        "no delivery signal: the enqueue did not waken the parked poll, so the command "
        "could only be picked up by a later poll-interval attempt"
    )
    assert record["generation_bumps"] >= 1, (
        "the enqueue signalled, but no signal moved the generation the parked poll "
        "measures its wait against: a signal the waiter cannot observe cannot waken it, "
        "so the park could only end on its poll interval"
    )
    woke_at = signals[-1]

    slept_out = [w for w in record["waits"] if not w[0] and w[1] >= woke_at]
    assert not slept_out, (
        f"{len(slept_out)} poll-interval wait(s) started after the enqueue had already "
        f"committed at poll step {step_seconds}s: the parked poll waited for its poll "
        "interval instead of the wake-up"
    )

    claim_latency = box["returned_at"] - woke_at
    assert claim_latency < WAKE_BOUND_SECONDS, (
        f"claim latency {claim_latency:.3f}s measured from the delivery signal at poll "
        f"step {step_seconds}s (owner-side enqueue cost {owner_enqueue_cost:.3f}s, "
        "excluded: it is Ed25519 + BEGIN IMMEDIATE + synchronous=FULL + loopback TLS, "
        "not the delivery path): the delivery path is still interval-gated"
    )


class _DeliveryWakeSpy(threading.Event):
    """The pump's own delivery flag, observed without changing its semantics.

    ``set``/``wait`` record and then delegate to ``threading.Event``, so the pump's
    behaviour is bit-for-bit unchanged.  This is the lowest level at which the guarantee
    can be stated honestly: ``TargetControlPump.wake`` is ``self._delivery_wakeup.set()``
    and the control loop's wait is ``self._delivery_wakeup.wait(interval)`` inside
    ``wait_for_wake``, so this flag *is* the delivery signal -- and a wait that never
    touches it (an interval poll over any other state) is exactly the implementation the
    assertion has to exclude.

    * ``signals`` -- when the flag was set, i.e. when ``wake`` ran.
      ``ConcurrentTargetControlPump._dispatch_ready`` calls it from the native dispatch
      future's done-callback, the moment the native command finished and the ack that
      frees this peer's next delivery slot became sendable.  Everything the test spends
      before that point -- unblocking the stubbed ``send``, the real native command
      itself, the executor thread's own scheduling -- is not the wake-up, and measuring a
      *wake-up* bound from before it is what made this test's number a function of host
      load.  The stamp is taken at or before the real ``set``, so a latency measured from
      it is never smaller than the true signal-to-release cost.
    * ``moved`` -- the sets that took the flag from clear to set.  A set on a flag that
      was already set (a signal nobody consumed) cannot have released a parked wait.
    * ``waits`` -- every wait on the flag, as ``(woken, started_at, ended_at)``.
      ``threading.Event.wait`` returns True only when the flag was set, so a wait that
      returned True was *ended by the signal*, and one that returned False paid its whole
      timeout -- which is the interval, not the wake-up.
    """

    def __init__(self) -> None:
        super().__init__()
        self.signals: list[float] = []
        self.moved = 0
        self.waits: list[tuple[bool, float, float]] = []

    def set(self) -> None:
        at = time.monotonic()
        moved_before = self.is_set()
        super().set()
        self.signals.append(at)
        if self.is_set() and not moved_before:
            self.moved += 1

    def wait(self, timeout=None):
        started = time.monotonic()
        woken = super().wait(timeout)
        self.waits.append((bool(woken), started, time.monotonic()))
        return woken


def _assert_the_completion_woke_the_control_loop(
    wake: _DeliveryWakeSpy, *, native_send_cost: float
) -> None:
    """The stated guarantee, on quantities the native command's own cost cannot dominate.

    Each of these is false for an interval-gated control-loop wait, at any host load:

    * the parked loop **waited on the delivery signal at all** -- a loop that only
      re-attempts on an interval never touches the flag;
    * the native command finishing **signalled** it (the flag was set by ``wake``, which
      the pump runs from the completion callback) and the set **moved** it -- a signal
      that does not move the state the wait measures cannot waken it;
    * the signal *ended* the parked wait: no wait that started after the signal paid its
      own timeout, and a wait returned True after it (``Event.wait`` returns True only
      when the flag was set) -- that is the difference between a wake-up and the interval;
    * the wake-up latency, measured from the signal, is under ``WAKE_BOUND_SECONDS``.

    ``native_send_cost`` is reported for diagnosis only: it is the cost the bound
    deliberately excludes (unblocking the stub plus the real native command), so a future
    failure here names how much of the old number was never the wake-up.
    """

    assert wake.waits, (
        "the parked control loop never waited on the pump's delivery signal "
        "(_delivery_wakeup): its wait was not the signal under test, so the guarantee "
        "was not exercised"
    )
    assert wake.signals, (
        "no delivery wake-up: the native command finishing did not set the signal the "
        "control loop waits on (_delivery_wakeup), so the loop could only leave its park "
        "on its own interval"
    )
    assert wake.moved >= 1, (
        "the pump set the delivery signal, but no set moved it from clear to set: a "
        "signal that was already set cannot have released a parked wait"
    )
    woke_at = wake.signals[0]

    timed_out = [w for w in wake.waits if not w[0] and w[1] >= woke_at]
    assert not timed_out, (
        f"{len(timed_out)} control-loop wait(s) started after the native command had "
        "already finished and paid their full timeout: the loop waited out its interval "
        "instead of the wake-up"
    )
    released = [w for w in wake.waits if w[0] and w[2] >= woke_at]
    assert released, (
        "the delivery signal was set, but no parked wait returned True afterwards: "
        "nothing was actually released by the wake-up"
    )

    wake_latency = released[-1][2] - woke_at
    assert wake_latency < WAKE_BOUND_SECONDS, (
        f"wake-up latency {wake_latency:.6f}s measured from the pump's own delivery signal "
        f"(native command's own cost {native_send_cost:.3f}s, excluded: it is unblocking "
        "the stub plus the real native send, not the wake-up): the control loop's wait was "
        "not released by the signal"
    )


def _poll_in_background(world, *, sequence: int, request_id: str, step_seconds: float):
    """Issue one idle *concurrent* poll (the parkable shape) and return its delivery.

    A concurrent poll that finds nothing to deliver and reports no outstanding claim is
    held open by the mailbox (``ManagedControlMailbox.poll``) -- that is the long poll
    this test needs to be parked when the command is enqueued.  ``box['returned_at']`` is
    stamped by the polling thread itself, as the poll returns, so the measured latency
    never includes this thread's own scheduling behind a ``join()``.
    """

    mailbox = world["mailbox"]
    assert 0.0 < step_seconds <= 5.0
    mailbox.long_poll_step = float(step_seconds)
    box: dict = {}
    started = threading.Event()

    def run():
        started.set()
        box["delivery"] = world["target_client"].poll(
            target=world["member"],
            run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,
            sequence=sequence,
            nonce=f"target-nonce-{sequence}",
            request_id=request_id,
            active_claims={},
        )
        box["returned_at"] = time.monotonic()

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(1.0)
    return thread, box


@pytest.mark.parametrize(
    "step_seconds", [OBSERVABLE_POLL_STEP_SECONDS, _LONG_POLL_STEP_SECONDS]
)
def test_enqueue_wakes_a_parked_poll_instead_of_waiting_for_the_poll_interval(
    tmp_path, step_seconds, monkeypatch
) -> None:
    """Claim latency must be a wake-up, not a fraction of the poll interval.

    RED at ``step=1.0``: the parked poll returns the command only at its next interval
    tick, ~0.8 s after the enqueue (p81r1's shape: a 4 427 ms life claimed at 3 957 ms).
    GREEN: the parked poll returns the command as soon as the enqueue lands -- which is
    the enqueue's *signal*, so the quantity asserted here is the delivery path and not
    the owner-side bookkeeping in front of it (see ``_assert_the_enqueue_woke_the_park``
    and the module docstring).
    """

    _production_poll_defaults_are_untouched()
    hold_clock = _HoldClock()
    with _world(
        tmp_path,
        mailbox_options={"long_poll_step": step_seconds, "monotonic": hold_clock},
    ) as world:
        record = _delivery_wake_evidence(monkeypatch)
        thread, box = _poll_in_background(
            world, sequence=1, request_id="parked-poll-1", step_seconds=step_seconds
        )
        try:
            # Enqueue from *inside* the hold, not before it: only once the poll has
            # provably entered its first interval wait, and the hold (measured on
            # ``hold_clock``) cannot have ended by the time the enqueue lands.
            assert record["parked"].wait(60), "the poll never parked"
            enqueued_at = time.monotonic()
            _enqueue(world, "woken-1")
            owner_enqueue_cost = time.monotonic() - enqueued_at
            thread.join(5)
            assert not thread.is_alive()
        finally:
            hold_clock.expire()  # never leave a parked poll behind a failed assertion
            thread.join(5)

        assert box["delivery"]["delivery_kind"] == "command", box
        assert box["delivery"]["command_id"] == "woken-1"
        _assert_the_enqueue_woke_the_park(
            record, box, step_seconds=step_seconds, owner_enqueue_cost=owner_enqueue_cost
        )


def test_an_enqueue_that_lands_during_an_attempt_is_not_a_lost_wakeup(
    tmp_path, monkeypatch
) -> None:
    """A signal that lands *inside* a claim attempt must not cost a poll interval.

    The wait between attempts has to be measured against the delivery generation read
    **before** the attempt.  An enqueue that has already committed while the attempt was
    in flight has made a new command deliverable, so the next attempt must start at once;
    if the generation is read *after* the attempt instead, that enqueue is
    indistinguishable from "nothing has happened yet" and the poll pays a whole
    ``long_poll_step`` of dead time for a command that is already waiting.

    RED before the fix at ``step=1.0`` -- measured 1 in 3 quiet runs of the probe this
    test is derived from (`_wait_for_delivery` returning ``timeout`` immediately after an
    enqueue that committed during the previous attempt): the poll waits out its interval.
    GREEN: the poll returns the command as soon as the enqueue lands, exactly as it does
    when the signal is observed while the poll is already parked.  Nothing here widens a
    hold, a step, a deadline, a skew or a staleness constant; ``long_poll_step`` stays the
    ceiling on the same wait and is only the fallback when no signal was missed.
    """

    _production_poll_defaults_are_untouched()
    step_seconds = OBSERVABLE_POLL_STEP_SECONDS
    with _world(tmp_path, mailbox_options={"long_poll_step": step_seconds}) as world:
        record = _delivery_wake_evidence(monkeypatch)
        real_once = ManagedControlMailbox._poll_once
        signal_landed = threading.Event()
        owner_enqueue_cost: list[float] = []

        def once(self, envelope):
            response = real_once(self, envelope)
            if not signal_landed.is_set() and _empty_poll_delivery(response):
                # The attempt has returned, so the mailbox lock is free and the owner's
                # own enqueue can land inside the window under test -- deterministically,
                # instead of racing a scheduling window.  Its signal is stamped by
                # `_delivery_wake_evidence`, which is the anchor the latency is measured
                # from: the enqueue's own cost is not the delivery path.
                began = time.monotonic()
                _enqueue(world, "woken-during-attempt")
                owner_enqueue_cost.append(time.monotonic() - began)
                signal_landed.set()
            return response

        monkeypatch.setattr(ManagedControlMailbox, "_poll_once", once)
        thread, box = _poll_in_background(
            world,
            sequence=1,
            request_id="parked-poll-during-attempt",
            step_seconds=step_seconds,
        )
        thread.join(5)
        assert not thread.is_alive()
        assert signal_landed.is_set(), "the test never landed its enqueue"

        assert box["delivery"]["delivery_kind"] == "command", box
        assert box["delivery"]["command_id"] == "woken-during-attempt"
        # The wait that follows the attempt must not sleep out its interval: the enqueue
        # already committed, so its signal is what has to end the park.
        _assert_the_enqueue_woke_the_park(
            record, box, step_seconds=step_seconds, owner_enqueue_cost=owner_enqueue_cost[0]
        )


def test_a_command_whose_window_is_shorter_than_the_delivery_interval_still_completes(
    tmp_path, monkeypatch
) -> None:
    """The p81r1 end-to-end shape, fleet-free.

    The real ``ManagedTargetRuntime`` control loop polls the real mailbox; the mailbox
    parks that poll.  The owner then sends one command whose window is shorter than the
    delivery interval plus execution -- so on the interval-gated path it can never
    complete (expired unclaimed, or claimed with no window left).  With the wake-up the
    enqueue releases the parked poll and the command completes inside its window.
    """

    _production_poll_defaults_are_untouched()
    _the_window_is_shorter_than_the_interval_it_is_tested_against()
    with _world(tmp_path / "swarm", mailbox_options=_E2E_MAILBOX_OPTIONS) as world:
        record = _delivery_wake_evidence(monkeypatch)
        target = runtime(tmp_path / "target", world)
        sender = owner(tmp_path / "sender", world)
        control = session(sender, world)
        try:
            assert target.start()["state"] == "running"
            # Let the control loop reach its first idle poll: provably parked (it has
            # entered its first interval wait), not "0.2 s later", which a starved
            # control-loop thread need not have reached.
            assert record["parked"].wait(5.0), "the control loop never reached its first park"
            started_at = time.monotonic()
            reply = control.send_before(
                command_id="windowed-hello",
                command="hello",
                payload={},
                deadline_monotonic_s=started_at + COMMAND_WINDOW_SECONDS,
            )
            elapsed = time.monotonic() - started_at

            assert reply["ok"] is True, reply
            assert elapsed < COMMAND_WINDOW_SECONDS, (
                f"completed after {elapsed:.3f}s, outside its "
                f"{COMMAND_WINDOW_SECONDS}s window"
            )
            assert world["mailbox"].command_receipt("windowed-hello")["state"] == "completed"
            _the_window_command_was_woken_not_polled(record)
        finally:
            control.close()
            target.close()
            sender.close()


#: How long the stalled native execution below holds a woken command.  It models a host
#: that is `SLOW_HOST_STALL_SECONDS / 0.08` times slower than an idle one (the whole
#: owner->target->worker->owner round trip of this test costs ~0.08 s idle), which is
#: what the L2 rehearsal's full-suite load did to the old 0.7 s window.
SLOW_HOST_STALL_SECONDS = 1.2


def test_a_woken_command_is_not_failed_by_a_slow_host_inside_its_window(
    tmp_path, monkeypatch
) -> None:
    """The window of the end-to-end shape must not be a race against host speed.

    ``test_a_command_whose_window_is_shorter_than_the_delivery_interval_still_completes``
    failed once in an L2 rehearsal (``TimeoutError: command_deadline_exceeded``, full
    suite under load ~10) although the delivery path behaved: the owner's window there is
    a real-clock budget for the *whole* round trip (Ed25519 signing, a
    ``synchronous=FULL`` commit, loopback TLS, the native worker, the owner's 50 ms result
    polls), which costs ~0.08 s idle against a 0.7 s window -- 8.7x of headroom, which a
    loaded host consumes.  Idle, 0 in 30 runs fail it even under ``yes`` burners, so this
    pins the mechanism instead of the rate: stall only the **native execution** of the
    command (after the wake-up has already delivered it) and the command must still
    complete inside its window.  Nothing in the delivery path is delayed, so the wake-up is
    asserted to have done its job on the stall-independent evidence.
    """

    _production_poll_defaults_are_untouched()
    _the_window_is_shorter_than_the_interval_it_is_tested_against()
    with _world(tmp_path / "swarm", mailbox_options=_E2E_MAILBOX_OPTIONS) as world:
        record = _delivery_wake_evidence(monkeypatch)
        target = runtime(tmp_path / "target", world)
        sender = owner(tmp_path / "sender", world)
        control = session(sender, world)
        try:
            assert target.start()["state"] == "running"
            assert SLOW_HOST_STALL_SECONDS < COMMAND_WINDOW_SECONDS
            real_send = target.worker.send
            stalled = threading.Event()

            def slow_host(**kwargs):
                if kwargs["command_id"] == "windowed-hello":
                    stalled.set()
                    time.sleep(SLOW_HOST_STALL_SECONDS)
                return real_send(**kwargs)

            monkeypatch.setattr(target.worker, "send", slow_host)
            assert record["parked"].wait(5.0), "the control loop never reached its first park"
            started_at = time.monotonic()
            reply = control.send_before(
                command_id="windowed-hello",
                command="hello",
                payload={},
                deadline_monotonic_s=started_at + COMMAND_WINDOW_SECONDS,
            )

            assert stalled.is_set(), "the test never stalled its native execution"
            assert reply["ok"] is True, reply
            assert world["mailbox"].command_receipt("windowed-hello")["state"] == "completed"
            _the_window_command_was_woken_not_polled(record)
        finally:
            control.close()
            target.close()
            sender.close()


def test_native_completion_wakes_the_control_loop_before_its_interval_expires(
    tmp_path, monkeypatch
) -> None:
    """The pump's own claim/ack wait must be released by the delivery event.

    The ack that frees this peer's next delivery slot is sent on the loop's next step,
    so while the loop sits out its interval a command that is already deliverable waits
    behind it.  RED on the unmodified bytes: the pump has no wake-up at all.  GREEN:
    a native command finishing releases the loop's wait immediately, and the runtime's
    control loop waits on that signal instead of on the interval alone.

    The latency is measured from the pump's own delivery signal -- the ``wake`` that runs
    from the completion callback -- and not from before ``release.set()``, because the
    stubbed native command's own execution (tens of milliseconds, and ~all of the consumed
    0.1046 s failure) is not the wake-up: see
    ``_assert_the_completion_woke_the_control_loop`` and the module docstring.
    """

    _production_poll_defaults_are_untouched()
    with _pump_world(tmp_path / "swarm") as world:
        root = tmp_path / "native"
        root.mkdir()
        native = worker(
            root,
            node_id=world["member"]["node_id"],
            run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,
        )
        pump = ConcurrentTargetControlPump(
            tmp_path / "pump",
            client=world["target_client"],
            worker=native,
            current_member=lambda: world["authority"].coordinator.member(
                world["member"]["node_id"]
            ),
        )
        wake = _DeliveryWakeSpy()
        monkeypatch.setattr(pump, "_delivery_wakeup", wake)
        release = threading.Event()
        entered = threading.Event()
        original = native.send
        native_send_cost: list[float] = []

        def blocked(**kwargs):
            if kwargs["command_id"] != "slow-hello":
                return original(**kwargs)
            entered.set()
            release.wait(5)
            unblocked_at = time.monotonic()
            response = original(**kwargs)
            # Reported for diagnosis only: this is the cost the wake-up bound
            # deliberately excludes (see `_assert_the_completion_woke_the_control_loop`).
            native_send_cost.append(time.monotonic() - unblocked_at)
            return response

        monkeypatch.setattr(native, "send", blocked)
        try:
            native.start()
            _enqueue(world, "slow-hello")
            assert pump.step()["state"] == "dispatched"
            assert entered.wait(2.0)
            # Nothing has completed: the wake-up must not have fired yet -- "not merely
            # not observed", which a signal consumed before the park would also satisfy.
            assert pump.wait_for_wake(0.3) is False
            assert not wake.signals, (
                "the pump signalled a delivery wake-up before the native command had "
                "finished: the wake-up is not wired to the completion"
            )
            release.set()
            assert pump.wait_for_wake(2.0) is True
            assert native_send_cost, "the test never ran its native command"
            _assert_the_completion_woke_the_control_loop(
                wake, native_send_cost=native_send_cost[0]
            )
        finally:
            release.set()
            pump.close()
            native.close()
        # The wake-up is wired into the real control loop, not left as an unused hook.
        source = inspect.getsource(ManagedTargetRuntime._serve)
        assert "wait_for_wake" in source
