from __future__ import annotations

from dataclasses import replace

from mycelium_live.command_controller import (
    CheckpointAction,
    CleanupResult,
    CleanupStatus,
    CommandController,
    CommandEnvelope,
    CommandIdentity,
    CommandKind,
    TerminalResult,
    TerminalStatus,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64


def _envelope(
    *,
    request_id: str = "request-a",
    attempt: int = 1,
    operation_id: str = "operation-a",
    deadline_ms: int = 10_000,
    cancellation_generation: int = 0,
) -> CommandEnvelope:
    return CommandEnvelope(
        identity=CommandIdentity(
            deployment_id="deployment-a",
            deployment_epoch=1,
            qualification_digest=DIGEST_B,
            request_id=request_id,
            request_attempt=attempt,
            path_id="path-a",
            path_attempt=0,
            path_digest=DIGEST_A,
            topology_generation=1,
            command_id=operation_id,
            publisher_generation=1,
            absolute_deadline_ms=deadline_ms,
            cancellation_generation=cancellation_generation,
        ),
        stage_id="stage-a",
        placement_id="placement-a",
        assignment_id="assignment-a",
        kind=CommandKind.DECODE,
        issued_at_ms=1_000,
        idempotency_digest=DIGEST_C,
        cleanup_owner_id="placement-owner-a",
        maximum_request_bytes=4_096,
        maximum_response_bytes=4_096,
    )


def _terminal(
    identity: CommandIdentity,
    *,
    status: TerminalStatus,
    observed_at_ms: int,
    digest: str = DIGEST_D,
) -> TerminalResult:
    return TerminalResult(
        identity=identity,
        status=status,
        observed_at_ms=observed_at_ms,
        result_digest=digest,
    )


def _cleanup(*, digest: str = DIGEST_B) -> CleanupResult:
    return CleanupResult(
        status=CleanupStatus.COMPLETED,
        released_resource_count=2,
        result_digest=digest,
    )


def test_registration_is_bounded_and_exact_duplicates_are_idempotent() -> None:
    controller = CommandController(maximum_requests=1, maximum_commands_per_request=1)
    envelope = _envelope()

    first = controller.register(envelope)
    duplicate = controller.register(envelope)
    conflict = controller.register(replace(envelope, idempotency_digest=DIGEST_D))
    command_limit = controller.register(_envelope(operation_id="operation-b"))
    request_limit = controller.register(_envelope(request_id="request-b"))

    assert first.accepted is True
    assert duplicate.accepted is True and duplicate.duplicate is True
    assert conflict.accepted is False and conflict.reason == "conflicting_duplicate"
    assert command_limit.accepted is False and command_limit.reason == "command_limit"
    assert request_limit.accepted is False and request_limit.reason == "request_limit"


def test_publisher_generation_advance_is_exact_cas_and_fences_old_identity() -> None:
    controller = CommandController()
    original = _envelope()
    controller.register(original)

    advanced = controller.advance_publisher_generation(
        original.identity,
        expected_generation=1,
        new_generation=2,
    )
    assert advanced.accepted is True
    assert advanced.snapshot is not None
    assert advanced.snapshot.identity.publisher_generation == 2

    stale = controller.checkpoint(original.identity, observed_at_ms=2_000)
    assert stale.accepted is False
    assert stale.reason == "stale_generation"
    conflicting = controller.advance_publisher_generation(
        advanced.snapshot.identity,
        expected_generation=1,
        new_generation=2,
    )
    assert conflicting.accepted is False
    assert conflicting.reason == "publisher_generation_cas_mismatch"


def test_cancellation_advances_once_and_fences_the_old_generation() -> None:
    controller = CommandController()
    original = _envelope()
    controller.register(original)

    cancelled = controller.cancel(
        original.identity,
        new_cancellation_generation=1,
        observed_at_ms=2_000,
        idempotency_digest=DIGEST_D,
    )
    duplicate = controller.cancel(
        original.identity,
        new_cancellation_generation=1,
        observed_at_ms=2_000,
        idempotency_digest=DIGEST_D,
    )

    assert cancelled.accepted is True
    assert cancelled.snapshot is not None
    current = cancelled.snapshot.identity
    assert current.cancellation_generation == 1
    assert cancelled.snapshot.cleanup_deadline_ms == 4_000
    assert duplicate.accepted is True and duplicate.duplicate is True

    stale = controller.terminal_compare_and_swap(
        _terminal(
            original.identity,
            status=TerminalStatus.COMPLETED,
            observed_at_ms=2_100,
        ),
        expected_terminal_revision=0,
    )
    assert stale.accepted is False
    assert stale.reason == "stale_generation"
    assert stale.snapshot is not None and stale.snapshot.terminal is None

    checkpoint = controller.checkpoint(current, observed_at_ms=2_100)
    assert checkpoint.action is CheckpointAction.CANCEL
    assert checkpoint.maximum_next_step_ms == 0


def test_terminal_compare_and_swap_accepts_one_canonical_result() -> None:
    controller = CommandController()
    envelope = _envelope()
    controller.register(envelope)
    result = _terminal(
        envelope.identity,
        status=TerminalStatus.COMPLETED,
        observed_at_ms=2_000,
    )

    first = controller.terminal_compare_and_swap(
        result,
        expected_terminal_revision=0,
    )
    duplicate = controller.terminal_compare_and_swap(
        result,
        expected_terminal_revision=0,
    )
    conflict = controller.terminal_compare_and_swap(
        replace(result, result_digest=DIGEST_C),
        expected_terminal_revision=0,
    )

    assert first.accepted is True
    assert first.snapshot is not None and first.snapshot.terminal_revision == 1
    assert duplicate.accepted is True and duplicate.duplicate is True
    assert conflict.accepted is False and conflict.reason == "already_terminal"
    assert conflict.snapshot is not None and conflict.snapshot.terminal == result


def test_deadline_is_controller_owned_and_stops_future_work_units() -> None:
    controller = CommandController(cooperative_step_ms=100)
    envelope = _envelope(deadline_ms=1_250)
    controller.register(envelope)

    before = controller.checkpoint(envelope.identity, observed_at_ms=1_200)
    at_deadline = controller.checkpoint(envelope.identity, observed_at_ms=1_250)

    assert before.action is CheckpointAction.CONTINUE
    assert before.maximum_next_step_ms == 50
    assert at_deadline.action is CheckpointAction.TERMINAL
    assert at_deadline.snapshot is not None
    assert at_deadline.snapshot.terminal is not None
    assert at_deadline.snapshot.terminal.status is TerminalStatus.DEADLINE_EXCEEDED
    assert at_deadline.snapshot.cleanup_deadline_ms == 3_250


def test_interruption_and_cleanup_share_one_total_two_second_bound() -> None:
    controller = CommandController(interruption_and_cleanup_ms=2_000)
    envelope = _envelope(deadline_ms=20_000)
    controller.register(envelope)
    cancelled = controller.cancel(
        envelope.identity,
        new_cancellation_generation=1,
        observed_at_ms=5_000,
        idempotency_digest=DIGEST_D,
    )
    assert cancelled.snapshot is not None
    current = cancelled.snapshot.identity
    cleanup = controller.record_cleanup(
        current,
        owner_id="placement-owner-a",
        result=_cleanup(),
        observed_at_ms=6_500,
    )
    assert cleanup.accepted is True
    assert cleanup.snapshot is not None
    assert cleanup.snapshot.cleanup_deadline_ms == 7_000
    assert cleanup.snapshot.cleanup_within_interruption_budget is True
    terminal = controller.terminal_compare_and_swap(
        _terminal(
            current,
            status=TerminalStatus.CANCELLED,
            observed_at_ms=6_600,
        ),
        expected_terminal_revision=0,
    )
    assert terminal.accepted is True

    late_controller = CommandController()
    late_controller.register(envelope)
    late_cancel = late_controller.cancel(
        envelope.identity,
        new_cancellation_generation=1,
        observed_at_ms=5_000,
        idempotency_digest=DIGEST_D,
    )
    assert late_cancel.snapshot is not None
    late_identity = late_cancel.snapshot.identity
    late = late_controller.record_cleanup(
        late_identity,
        owner_id="placement-owner-a",
        result=_cleanup(),
        observed_at_ms=7_001,
    )
    assert late.accepted is True
    assert late.snapshot is not None
    assert late.snapshot.cleanup_within_interruption_budget is False
    late_terminal = late_controller.terminal_compare_and_swap(
        _terminal(
            late_identity,
            status=TerminalStatus.CANCELLED,
            observed_at_ms=7_001,
        ),
        expected_terminal_revision=0,
    )
    assert late_terminal.accepted is False
    assert late_terminal.reason == "already_terminal"
    assert late_terminal.snapshot is not None
    assert late_terminal.snapshot.terminal is not None
    assert (
        late_terminal.snapshot.terminal.status
        is TerminalStatus.DEADLINE_EXCEEDED
    )


def test_controller_consumes_owner_issued_deadline_without_restarting_budget() -> None:
    controller = CommandController(interruption_and_cleanup_ms=2_000)
    envelope = _envelope(deadline_ms=20_000)
    controller.register(envelope)

    cancelled = controller.cancel(
        envelope.identity,
        new_cancellation_generation=1,
        observed_at_ms=5_125,
        idempotency_digest=DIGEST_D,
        cleanup_deadline_ms=7_000,
    )

    assert cancelled.accepted is True
    assert cancelled.snapshot is not None
    assert cancelled.snapshot.cleanup_deadline_ms == 7_000


def test_cleanup_is_owner_scoped_generation_fenced_and_idempotent() -> None:
    controller = CommandController()
    envelope = _envelope()
    controller.register(envelope)
    controller.terminal_compare_and_swap(
        _terminal(
            envelope.identity,
            status=TerminalStatus.COMPLETED,
            observed_at_ms=2_000,
        ),
        expected_terminal_revision=0,
    )

    wrong_owner = controller.record_cleanup(
        envelope.identity,
        owner_id="placement-owner-b",
        result=_cleanup(),
        observed_at_ms=2_100,
    )
    accepted = controller.record_cleanup(
        envelope.identity,
        owner_id="placement-owner-a",
        result=_cleanup(),
        observed_at_ms=2_100,
    )
    duplicate = controller.record_cleanup(
        envelope.identity,
        owner_id="placement-owner-a",
        result=_cleanup(),
        observed_at_ms=2_200,
    )
    conflict = controller.record_cleanup(
        envelope.identity,
        owner_id="placement-owner-a",
        result=_cleanup(digest=DIGEST_C),
        observed_at_ms=2_200,
    )

    assert wrong_owner.accepted is False
    assert wrong_owner.reason == "cleanup_owner_mismatch"
    assert accepted.accepted is True
    assert duplicate.accepted is True and duplicate.duplicate is True
    assert conflict.accepted is False and conflict.reason == "cleanup_conflict"


def test_late_result_cannot_mutate_new_request_generation() -> None:
    controller = CommandController()
    first = _envelope(attempt=1)
    controller.register(first)
    controller.terminal_compare_and_swap(
        _terminal(
            first.identity,
            status=TerminalStatus.COMPLETED,
            observed_at_ms=2_000,
        ),
        expected_terminal_revision=0,
    )
    controller.record_cleanup(
        first.identity,
        owner_id="placement-owner-a",
        result=_cleanup(),
        observed_at_ms=2_100,
    )

    second = _envelope(attempt=2)
    registered = controller.register(second)
    late = controller.terminal_compare_and_swap(
        _terminal(
            first.identity,
            status=TerminalStatus.PEER_UNAVAILABLE,
            observed_at_ms=2_200,
        ),
        expected_terminal_revision=0,
    )

    assert registered.accepted is True
    assert late.accepted is False and late.reason == "stale_attempt"
    current = controller.snapshot("request-a", request_attempt=2)
    assert len(current) == 1
    assert current[0].terminal is None
    assert current[0].terminal_revision == 0


def test_request_metadata_retires_only_after_terminal_cleanup() -> None:
    controller = CommandController()
    envelope = _envelope()
    controller.register(envelope)
    assert controller.retire("request-a", expected_attempt=1).reason == "request_live"

    controller.terminal_compare_and_swap(
        _terminal(
            envelope.identity,
            status=TerminalStatus.COMPLETED,
            observed_at_ms=2_000,
        ),
        expected_terminal_revision=0,
    )
    assert controller.retire("request-a", expected_attempt=1).reason == "request_live"
    controller.record_cleanup(
        envelope.identity,
        owner_id="placement-owner-a",
        result=_cleanup(),
        observed_at_ms=2_100,
    )

    retired = controller.retire("request-a", expected_attempt=1)
    assert retired.accepted is True
    assert controller.snapshot("request-a", request_attempt=1) == ()
