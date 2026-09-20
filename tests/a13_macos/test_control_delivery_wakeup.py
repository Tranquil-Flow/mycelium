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
from mycelium_a13_macos.managed_control import _LONG_POLL_HOLD_SECONDS, _LONG_POLL_STEP_SECONDS
from mycelium_a13_macos.managed_runtime import ManagedTargetRuntime

#: The window THIS test's owner waits for one command, in this offline reproduction.
#: Not a production constant: it stands in for the measured ~4.0-4.5 s cleanup window
#: and keeps the test bounded, exactly as p81r1's route waiter did.
COMMAND_WINDOW_SECONDS = 0.7

#: Test configuration of ``ManagedControlMailbox(long_poll_step=...)``, an existing
#: validated parameter (``0 < step <= _LONG_POLL_MAX_HOLD_SECONDS``).  The defect class
#: is "claim latency is a function of the poll interval"; it is only *observable* if
#: some interval exists, so the test drives it to a value at which the inversion is
#: measurable in both directions.  The production default is asserted unchanged, and
#: the same test is run at that default too, where the same inversion exists in
#: miniature (up to one interval of dead time per park).
OBSERVABLE_POLL_STEP_SECONDS = 1.0

#: The longest claim latency the wake-up path may cost.  Generous next to the measured
#: local claim of 64-220 ms, and far below one interval of either step under test.
WAKE_BOUND_SECONDS = 0.1


def _production_poll_defaults_are_untouched() -> None:
    """The fix is the delivery path.  These are the shipped numbers, unchanged."""

    assert _LONG_POLL_HOLD_SECONDS == 1.5
    assert _LONG_POLL_STEP_SECONDS == 0.02


def _poll_in_background(world, *, sequence: int, request_id: str, step_seconds: float):
    """Issue one idle *concurrent* poll (the parkable shape) and return its delivery.

    A concurrent poll that finds nothing to deliver and reports no outstanding claim is
    held open by the mailbox (``ManagedControlMailbox.poll``) -- that is the long poll
    this test needs to be parked when the command is enqueued.
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

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(1.0)
    return thread, box


@pytest.mark.parametrize(
    "step_seconds", [OBSERVABLE_POLL_STEP_SECONDS, _LONG_POLL_STEP_SECONDS]
)
def test_enqueue_wakes_a_parked_poll_instead_of_waiting_for_the_poll_interval(
    tmp_path, step_seconds
) -> None:
    """Claim latency must be a wake-up, not a fraction of the poll interval.

    RED at ``step=1.0``: the parked poll returns the command only at its next interval
    tick, ~0.8 s after the enqueue (p81r1's shape: a 4 427 ms life claimed at 3 957 ms).
    GREEN: the parked poll returns the command as soon as the enqueue lands.
    """

    _production_poll_defaults_are_untouched()
    with _world(tmp_path, mailbox_options={"long_poll_step": step_seconds}) as world:
        thread, box = _poll_in_background(
            world, sequence=1, request_id="parked-poll-1", step_seconds=step_seconds
        )
        # Enqueue from *inside* the hold, not before it: the poll is parked for
        # _LONG_POLL_HOLD_SECONDS, and 0.2 s of that is spent first.
        time.sleep(0.2)
        enqueued_at = time.monotonic()
        _enqueue(world, "woken-1")
        thread.join(5)
        assert not thread.is_alive()
        claim_latency = time.monotonic() - enqueued_at

        assert box["delivery"]["delivery_kind"] == "command", box
        assert box["delivery"]["command_id"] == "woken-1"
        assert claim_latency < WAKE_BOUND_SECONDS, (
            f"claim latency {claim_latency:.3f}s at poll step {step_seconds}s: the "
            "delivery path is still interval-gated"
        )


def test_a_command_whose_window_is_shorter_than_the_delivery_interval_still_completes(
    tmp_path,
) -> None:
    """The p81r1 end-to-end shape, fleet-free.

    The real ``ManagedTargetRuntime`` control loop polls the real mailbox; the mailbox
    parks that poll.  The owner then sends one command whose window is shorter than the
    delivery interval plus execution -- so on the interval-gated path it can never
    complete (expired unclaimed, or claimed with no window left).  With the wake-up the
    enqueue releases the parked poll and the command completes inside its window.
    """

    _production_poll_defaults_are_untouched()
    with _world(
        tmp_path / "swarm", mailbox_options={"long_poll_step": OBSERVABLE_POLL_STEP_SECONDS}
    ) as world:
        target = runtime(tmp_path / "target", world)
        sender = owner(tmp_path / "sender", world)
        control = session(sender, world)
        try:
            assert target.start()["state"] == "running"
            # Let the control loop reach its first idle poll (parked, no claims yet).
            time.sleep(0.2)
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
        release = threading.Event()
        entered = threading.Event()
        original = native.send

        def blocked(**kwargs):
            if kwargs["command_id"] == "slow-hello":
                entered.set()
                release.wait(5)
            return original(**kwargs)

        monkeypatch.setattr(native, "send", blocked)
        try:
            native.start()
            _enqueue(world, "slow-hello")
            assert pump.step()["state"] == "dispatched"
            assert entered.wait(2.0)
            # Nothing has completed: the wake-up must not have fired yet.
            assert pump.wait_for_wake(0.3) is False
            woken_at = time.monotonic()
            release.set()
            assert pump.wait_for_wake(2.0) is True
            assert time.monotonic() - woken_at < WAKE_BOUND_SECONDS
        finally:
            release.set()
            pump.close()
            native.close()
        # The wake-up is wired into the real control loop, not left as an unused hook.
        source = inspect.getsource(ManagedTargetRuntime._serve)
        assert "wait_for_wake" in source
