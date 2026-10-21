# SPDX-License-Identifier: AGPL-3.0-only
"""The soak's bounds come from ``mycelium_deadlines`` - and nowhere else.

Packet wp2-3 requirement 2: "the bounds come from ``mycelium_deadlines.py`` - do
not re-declare any number in the soak".  A comment cannot prove that, so these
tests move the registry and assert the soak follows: a value the soak had
re-declared in its own source would not move.

The determinism half is the replay guarantee of packet item 1: a schedule is a
pure function of its seed, and the run digest is stable for one seed and
different for another.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import pytest

import mycelium_deadlines as deadlines
from mycelium_testbed import soak


def _bound_names() -> dict[str, str]:
    """``SoakBounds`` field -> the registry entry it must be read from."""

    return {
        "nonterminal_row_retirement_seconds": "cancel_and_cleanup_budget_seconds",
        "request_terminal_cancelled_seconds": "cancel_and_cleanup_budget_seconds",
        "request_terminal_driver_seconds": "node_command_timeout_seconds",
        "control_loop_stall_seconds": "liveness_active_failure_detection_ms",
        "control_loop_idle_seconds": "liveness_idle_keepalive_ms",
        "row_eviction_grace_seconds": "gateway_session_eviction_grace_seconds",
        "silent_subscriber_stall_seconds": "gateway_session_silent_subscriber_stall_seconds",
        "poll_seconds": "gateway_sse_disconnect_poll_seconds",
        "probe_seconds": "idle_keepalive_probe_deadline_seconds",
        "launch_handshake_seconds": "node_launch_handshake_seconds",
    }


def test_every_soak_bound_equals_its_registry_entry() -> None:
    bounds = soak.soak_bounds()

    for field, name in _bound_names().items():
        assert getattr(bounds, field) == soak.registry_seconds(name), (
            field,
            name,
        )


def test_delay_magnitudes_are_registry_cadences() -> None:
    bounds = soak.soak_bounds()
    names = {name for name, _ in soak.DELAY_SOURCES}

    assert {name for name, _ in bounds.delay_choices} == names
    for name, value in bounds.delay_choices:
        assert value == soak.registry_seconds(name)
        assert value > 0


def test_the_soak_follows_the_registry_when_it_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-declared value would not move; this one does."""

    original = deadlines.DEADLINES_BY_NAME
    record = original["liveness_active_failure_detection_ms"]
    moved = record.__class__(
        name=record.name,
        value=record.value * 3,
        unit=record.unit,
        owner=record.owner,
        derivation=record.derivation,
        source=record.source,
    )
    patched = dict(original)
    patched[record.name] = moved
    monkeypatch.setattr(
        deadlines, "DEADLINES_BY_NAME", MappingProxyType(patched), raising=True
    )

    assert soak.registry_seconds(record.name) == pytest.approx(record.value * 3 / 1_000)
    assert soak.soak_bounds().control_loop_stall_seconds == pytest.approx(
        record.value * 3 / 1_000
    )


def test_a_non_duration_registry_entry_is_not_silently_accepted() -> None:
    with pytest.raises(soak.SoakError):
        soak.registry_seconds("cleanup_reconciliation_workers")
    with pytest.raises(soak.SoakError):
        soak.registry_count("cancel_and_cleanup_budget_seconds")


def test_an_unknown_registry_name_fails_closed() -> None:
    with pytest.raises(soak.SoakError):
        soak.registry_seconds("no_such_deadline_in_the_registry")


def test_the_bounds_digest_changes_when_a_bound_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = soak.soak_bounds().digest()
    original = deadlines.DEADLINES_BY_NAME
    record = original["node_command_timeout_seconds"]
    patched = dict(original)
    patched[record.name] = record.__class__(
        name=record.name,
        value=record.value + 1,
        unit=record.unit,
        owner=record.owner,
        derivation=record.derivation,
        source=record.source,
    )
    monkeypatch.setattr(deadlines, "DEADLINES_BY_NAME", MappingProxyType(patched))
    assert soak.soak_bounds().digest() != before


def test_a_schedule_is_a_pure_function_of_its_seed() -> None:
    first = soak.FaultSchedule(seed=11, events=128)
    second = soak.FaultSchedule(seed=11, events=128)
    other = soak.FaultSchedule(seed=12, events=128)

    assert first.digest() == second.digest()
    assert first.digest() != other.digest()
    assert first.events_document() == second.events_document()


def test_a_schedule_consumes_exactly_one_fault_per_step() -> None:
    schedule = soak.FaultSchedule(seed=3, events=17)
    seen = [schedule.next_fault() for _ in range(17)]

    assert [event.index for event in seen] == list(range(17))
    assert schedule.consumed == 17
    with pytest.raises(soak.SoakError):
        schedule.next_fault()


def test_the_transport_slice_is_a_function_of_the_seed_alone() -> None:
    one = soak.FaultSchedule(seed=5, events=64).events_for("transport")
    two = soak.FaultSchedule(seed=5, events=64).events_for("transport")
    other = soak.FaultSchedule(seed=6, events=64).events_for("transport")

    assert one == two
    assert one != other
    assert {event.target for event in one} == {soak.FaultTarget.TRANSPORT}
    assert {event.kind for event in one} <= set(soak.TRANSPORT_SCHEDULE_FAULTS)


def test_an_empty_schedule_path_is_rejected() -> None:
    with pytest.raises(soak.SoakError):
        soak.FaultSchedule(seed=1, events=4).events_for("nowhere")


def test_run_digest_is_stable_for_one_seed_and_differs_for_another(
    tmp_path: Path,
) -> None:
    """The replay identity, exercised without the native sidecar.

    Both episodes are switched off, so this asserts the digest machinery (config,
    schedule, bounds, verdicts, fault applications) and not the product path; the
    real-path form of the same assertion lives in ``test_soak_real_path.py``.
    """

    config = soak.SoakConfig(
        seeds=(21,),
        events=40,
        run_id="digest-only",
        out_root=tmp_path,
        native_binary=None,
        real_request=False,
        mailbox=False,
    )

    first = soak.run_seed(config, 21, label="a")
    second = soak.run_seed(config, 21, label="b")
    other = soak.run_seed(config, 22, label="c")

    assert first.run_digest == second.run_digest
    assert first.run_digest != other.run_digest
    assert first.schedule_digest == second.schedule_digest
    assert first.schedule_digest != other.schedule_digest
    assert first.green and second.green and other.green


def test_the_seed_record_is_written_and_names_its_replay_command(
    tmp_path: Path,
) -> None:
    config = soak.SoakConfig(
        seeds=(31,),
        events=8,
        run_id="record-only",
        out_root=tmp_path,
        native_binary=None,
        real_request=False,
        mailbox=False,
    )
    result = soak.run_seed(config, 31)

    record_path = tmp_path / "record-only" / "seeds" / "seed-0031" / "seed.json"
    assert record_path.is_file()
    document = json.loads(record_path.read_text(encoding="utf-8"))

    assert document["run_digest"] == result.run_digest
    assert document["schedule"] != []
    assert document["bounds"]["nonterminal_row_retirement_seconds"] == (
        soak.registry_seconds("cancel_and_cleanup_budget_seconds")
    )
    assert f"--seed {result.seed}" in document["replay_command"]
    assert f"--events {result.events}" in document["replay_command"]


def test_the_cli_prints_a_schedule_digest_per_seed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = soak.main(
        [
            "--plan",
            "--seed",
            "3",
            "--events",
            "12",
            "--native-binary",
            str(tmp_path / "not-used-in-plan-mode"),
        ]
    )

    assert exit_code == 0
    printed = capsys.readouterr().out
    assert "seed=3" in printed
    assert "schedule_digest=sha256:" in printed


def test_the_cli_rejects_a_forced_non_falsification_fault() -> None:
    with pytest.raises(soak.SoakError):
        soak.parse_forced(["drop@1"])
    forced = soak.parse_forced(["double_execution@5", "stall_control_loop"])
    assert forced[0].kind is soak.FaultKind.DOUBLE_EXECUTION
    assert forced[0].step == 5
    assert forced[1].step is None
