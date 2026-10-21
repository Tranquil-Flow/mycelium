# SPDX-License-Identifier: AGPL-3.0-only
"""The soak's replay claim, split the way the design review requires (packet ``wp2-8`` task 3).

``rca/wp2-harness-assertion-design-review.md`` decision **2'** (revised 2026-09-15 after the
wp2-8 measurement contradicted decision 2): **only structural properties gate** - any
invariant firing in any run, and terminal coverage.  Neither replay identity gates, because
a fixed ``--events`` budget covers a different number of request lifecycles from run to run:
measured on 20 canonical runs, product-digest identity held 5/20 while event-stream identity
held 29/30, so gating either would refuse the pilot ~15 times in 20 for a non-defect reason.
Both are *recorded* as observations with their seed and exact replay command.

The tests here pin that split in both directions, with ``run_seed`` replaced by a crafted
result so they are deterministic and need no native sidecar:

* a product-digest mismatch **alone** does not gate
  (``test_a_product_digest_mismatch_alone_does_not_gate``);
* an event-stream difference alone exits zero and is recorded as an observation
  (``test_an_event_stream_difference_alone_exits_zero``);
* an invariant violation still exits non-zero
  (``test_an_invariant_violation_still_exits_non_zero``) - the structural gate survives.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mycelium_testbed import soak


def _result(
    *,
    seed: int,
    label: str,
    run_digest: str,
    product_digest: str,
    violations: tuple[soak.Violation, ...] = (),
    product_material: dict[str, Any] | None = None,
) -> soak.SeedResult:
    return soak.SeedResult(
        seed=seed,
        label=label,
        schedule_digest="sha256:schedule",
        run_digest=run_digest,
        product_digest=product_digest,
        violations=violations,
        application_counts={},
        execution_counts={},
        terminal_classes={},
        events=200,
        trace_dirs=(),
        replayed=label in {"replay", "divergence"},
        wall_seconds=0.01,
        product_material=product_material or {"seed": seed, "execution_counts": {}},
    )


def _violation(code: str = soak.INVARIANT_NONTERMINAL_ROW_NOT_RETIRED_WITHIN_BOUND) -> soak.Violation:
    return soak.Violation(
        code=code,
        subject="request-soak-0001-001",
        detail="detail",
        bound_name="cancel_and_cleanup_budget_seconds",
        bound_value=2.0,
    )


def _install_sweep(
    monkeypatch: pytest.MonkeyPatch,
    *,
    first_run_digest: str,
    replay_run_digest: str,
    first_product_digest: str,
    replay_product_digest: str,
    first_violations: tuple[soak.Violation, ...] = (),
    replay_violations: tuple[soak.Violation, ...] = (),
    divergence_violations: tuple[soak.Violation, ...] = (),
) -> list[dict[str, Any]]:
    """Replace ``run_seed`` with a crafted sweep; return the calls it received."""

    calls: list[dict[str, Any]] = []

    def fake_run_seed(
        config: soak.SoakConfig,
        seed: int,
        *,
        label: str | None = None,
        replayed: bool = False,
    ) -> soak.SeedResult:
        label = label or f"seed-{seed:04d}"
        calls.append({"seed": seed, "label": label, "replayed": replayed})
        if label == "replay":
            return _result(
                seed=seed,
                label=label,
                run_digest=replay_run_digest,
                product_digest=replay_product_digest,
                violations=replay_violations,
                product_material={"seed": seed, "execution_counts": {"a": 1}},
            )
        if label == "divergence":
            return _result(
                seed=seed,
                label=label,
                run_digest="sha256:divergence",
                product_digest="sha256:divergence",
                violations=divergence_violations,
                product_material={"seed": seed, "execution_counts": {"divergence": 1}},
            )
        return _result(
            seed=seed,
            label=label,
            run_digest=first_run_digest,
            product_digest=first_product_digest,
            violations=first_violations,
            product_material={"seed": seed, "execution_counts": {"a": 2}},
        )

    monkeypatch.setattr(soak, "run_seed", fake_run_seed)
    return calls


def _run_cli(tmp_path: Path) -> tuple[int, str, dict[str, Any]]:
    exit_code = soak.main(
        [
            "--seeds",
            "1",
            "--events",
            "4",
            "--run-id",
            "gate",
            "--out",
            str(tmp_path),
            "--native-binary",
            str(tmp_path / "unused"),
            "--json",
        ]
    )
    report = (tmp_path / "gate" / "report.txt").read_text(encoding="utf-8")
    sweep_document = json.loads((tmp_path / "gate" / "sweep.json").read_text(encoding="utf-8"))
    return exit_code, report, sweep_document


def test_the_split_is_declared_in_the_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gated property is named, and both replay identities are observations.

    Design-review decision 2' (2026-09-15): only structural properties gate.  Identity of
    either digest drifts with how much work fitted a fixed event budget - measured on 20
    canonical runs, product-digest identity held 5/20 and event-stream identity 29/30 - so
    gating one would refuse the pilot ~15 times in 20 for a non-defect reason.
    """

    _install_sweep(
        monkeypatch,
        first_run_digest="sha256:run-a",
        replay_run_digest="sha256:run-b",
        first_product_digest="sha256:product",
        replay_product_digest="sha256:product",
    )

    exit_code, report, document = _run_cli(tmp_path)
    replay = document["replay_check"]

    assert replay["gated_property"] == "invariants_and_terminal_coverage"
    assert replay["product_digest_identical"] is True
    assert not any(
        "digest" in failure for failure in replay["gate_failures"]
    ), replay["gate_failures"]
    assert replay["event_stream_identity"]["gated"] is False
    assert replay["event_stream_identity"]["identical"] is False
    assert replay["event_stream_identity"]["seed"] == 1
    assert "mycelium_testbed.soak" in replay["event_stream_identity"]["replay_command"]
    assert "--seed 1" in replay["event_stream_identity"]["replay_command"]
    # The whole-run identity stays readable under its old name for existing readers.
    assert replay["identical"] is False
    assert exit_code == 0, report
    assert "event_stream" in report


def test_a_product_digest_mismatch_alone_does_not_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Decision 2': a product-digest mismatch is recorded, not gated.

    Superseded rule, kept as a comment because it is the mistake this pins against: the
    original decision 2 gated this property, which made the soak refuse to exit zero in
    ~15 of 20 canonical runs with no invariant firing.
    """

    _install_sweep(
        monkeypatch,
        first_run_digest="sha256:run",
        replay_run_digest="sha256:run",
        first_product_digest="sha256:product-a",
        replay_product_digest="sha256:product-b",
    )

    exit_code, report, document = _run_cli(tmp_path)

    assert document["replay_check"]["product_digest_identical"] is False
    assert document["replay_check"]["gated_property"] == "invariants_and_terminal_coverage"
    # The mismatch is diagnosed field by field and recorded...
    assert document["replay_check"]["product_digest_difference"]
    # ...and it does not, by itself, refuse the run.
    assert not document["replay_check"]["gate_failures"], document["replay_check"]["gate_failures"]
    assert exit_code == 0, report
    assert "observed_product_digest_identical=False" in report


def test_an_event_stream_difference_alone_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_sweep(
        monkeypatch,
        first_run_digest="sha256:run-a",
        replay_run_digest="sha256:run-b",
        first_product_digest="sha256:product",
        replay_product_digest="sha256:product",
    )

    exit_code, report, document = _run_cli(tmp_path)

    assert exit_code == 0, report
    assert document["replay_check"]["event_stream_identity"]["identical"] is False
    assert "observed_event_stream_identical=False" in report
    # Recorded as an observation, and explicitly not a gate failure.
    assert not any("event_stream" in failure for failure in document["replay_check"]["gate_failures"])


def test_a_matching_replay_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_sweep(
        monkeypatch,
        first_run_digest="sha256:run",
        replay_run_digest="sha256:run",
        first_product_digest="sha256:product",
        replay_product_digest="sha256:product",
    )

    exit_code, report, _ = _run_cli(tmp_path)

    assert exit_code == 0, report
    assert "gate=PASS" in report


def test_a_product_digest_mismatch_is_diagnosed_field_by_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_sweep(
        monkeypatch,
        first_run_digest="sha256:run",
        replay_run_digest="sha256:run",
        first_product_digest="sha256:product-a",
        replay_product_digest="sha256:product-b",
    )

    _, _, document = _run_cli(tmp_path)

    difference = document["replay_check"]["product_digest_difference"]
    assert set(difference) == {"execution_counts"}
    assert difference["execution_counts"]["first"] == {"a": 2}
    assert difference["execution_counts"]["replay"] == {"a": 1}


def test_an_invariant_violation_still_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gated digest split must not displace the invariant gate."""

    _install_sweep(
        monkeypatch,
        first_run_digest="sha256:run",
        replay_run_digest="sha256:run",
        first_product_digest="sha256:product",
        replay_product_digest="sha256:product",
        first_violations=(_violation(),),
    )

    exit_code, report, document = _run_cli(tmp_path)

    assert exit_code != 0
    assert soak.INVARIANT_NONTERMINAL_ROW_NOT_RETIRED_WITHIN_BOUND in report
    assert any(
        failure.startswith("invariant_violated:")
        for failure in document["replay_check"]["gate_failures"]
    )


def test_a_violation_that_only_the_replay_run_saw_is_not_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A run of the sweep that fires an invariant is a finding wherever it appeared."""

    _install_sweep(
        monkeypatch,
        first_run_digest="sha256:run",
        replay_run_digest="sha256:run",
        first_product_digest="sha256:product",
        replay_product_digest="sha256:product",
        replay_violations=(_violation(),),
    )

    exit_code, report, document = _run_cli(tmp_path)

    assert exit_code != 0
    assert soak.INVARIANT_NONTERMINAL_ROW_NOT_RETIRED_WITHIN_BOUND in report
    assert document["green"] is False
    assert document["replay_check"]["replay_violation_codes"] == [
        soak.INVARIANT_NONTERMINAL_ROW_NOT_RETIRED_WITHIN_BOUND
    ]


def test_the_divergence_check_still_needs_a_different_seed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A different seed must not replay the first seed's digest (PLAN 5.1)."""

    calls = _install_sweep(
        monkeypatch,
        first_run_digest="sha256:run",
        replay_run_digest="sha256:run",
        first_product_digest="sha256:product",
        replay_product_digest="sha256:product",
    )

    _run_cli(tmp_path)

    assert [call["label"] for call in calls] == ["seed-0001", "replay", "divergence"]
    assert {call["seed"] for call in calls} == {1, 1, 1_000_004}


def test_gate_failures_are_empty_for_a_green_matching_sweep(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_sweep(
        monkeypatch,
        first_run_digest="sha256:run",
        replay_run_digest="sha256:run",
        first_product_digest="sha256:product",
        replay_product_digest="sha256:product",
    )

    _, _, document = _run_cli(tmp_path)

    assert document["replay_check"]["gate_failures"] == []
    assert document["green"] is True


def test_the_event_stream_identity_is_recorded_per_seed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The observation carries the exact replay command a reader needs to reproduce it."""

    _install_sweep(
        monkeypatch,
        first_run_digest="sha256:run-a",
        replay_run_digest="sha256:run-b",
        first_product_digest="sha256:product",
        replay_product_digest="sha256:product",
    )

    _, _, document = _run_cli(tmp_path)

    observation = document["replay_check"]["event_stream_identity"]
    assert observation["first_digest"] == "sha256:run-a"
    assert observation["replay_digest"] == "sha256:run-b"
    assert "note" in observation
