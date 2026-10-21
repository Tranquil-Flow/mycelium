# SPDX-License-Identifier: AGPL-3.0-only
"""RED-first reachability + behaviour tests for the A7 deterministic runtime half.

Written *before* the runtime half of `mycelium_a7_kv_successor.py` existed. At the
base commit `0fe6037` every seam in the implementation contract section 4 raises
`A7ScaffoldBoundary`, so both the reachability guard and every behaviour test
below fail -- that is the RED state this module is the receipt for. Against the
implemented in-process runtime cycle they pass.

What this module is for:

* it is the *reachability guard*: it fails when a runtime seam is not importable
  or not callable from the module's public API, and when any seam still refuses
  with `A7ScaffoldBoundary` instead of doing deterministic in-process work or
  failing closed with a contract `ValueError`;
* it pins that the runtime half stays honest: every runtime and gate record the
  scaffold can produce declares `physical_evidence=false`,
  `qualification_claim=false` and `promotion_authorized=false`, and the closed
  record shapes cannot express a physical or qualified claim at all;
* it proves nothing about KV transport, durable fencing, product integration,
  browser behaviour, or any physical gate. See the implementation contract
  (`docs/superpowers/specs/2026-09-20-mycelium-a7-successor-recovery-implementation-contract.md`)
  section 5 for what passing these tests does not prove.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import mycelium_a7_kv_successor as a7


# The closed set of seams the scaffold used to refuse, copied from the
# implementation contract section 4. These are exactly the seams the runtime
# half now implements in-process.
RUNTIME_SEAMS = frozenset(
    {
        "validate_kv_standby_intent",
        "validate_kv_successor_runtime",
        "validate_kv_successor_qualification",
        "validate_kv_successor_recovery_gate",
        "seal_checkpoint_payload",
        "open_checkpoint_payload",
        "transfer_shadow_checkpoint",
        "persist_fence_generation",
        "perform_fenced_cutover",
        "run_successor_recovery",
    }
)

ZERO_DIGEST = "sha256:" + "0" * 64


def _digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _stage(name: str) -> str:
    return _digest(f"stage:{name}")


def _incarnation(name: str) -> str:
    return _digest(f"incarnation:{name}")


def _hostile_seam_calls() -> dict[str, object]:
    """One minimal call per seam: every one must fail closed, never scaffold-refuse."""

    return {
        "validate_kv_standby_intent": lambda: a7.validate_kv_standby_intent({}),
        "validate_kv_successor_runtime": lambda: a7.validate_kv_successor_runtime({}),
        "validate_kv_successor_qualification": lambda: (
            a7.validate_kv_successor_qualification({})
        ),
        "validate_kv_successor_recovery_gate": lambda: (
            a7.validate_kv_successor_recovery_gate({})
        ),
        "seal_checkpoint_payload": lambda: a7.seal_checkpoint_payload(
            checkpoint={}, kv_payload=b"private"
        ),
        "open_checkpoint_payload": lambda: a7.open_checkpoint_payload(
            reference="ref:0"
        ),
        "transfer_shadow_checkpoint": lambda: a7.transfer_shadow_checkpoint(
            checkpoint={}, standby_placement_id=ZERO_DIGEST
        ),
        "persist_fence_generation": lambda: a7.persist_fence_generation(
            fence_state={}
        ),
        "perform_fenced_cutover": lambda: a7.perform_fenced_cutover(
            fence_state={}, checkpoint={}
        ),
        "run_successor_recovery": lambda: a7.run_successor_recovery(
            request_id=ZERO_DIGEST
        ),
    }


# ---------------------------------------------------------------------------
# Fixtures for a well-formed in-process cycle
# ---------------------------------------------------------------------------


def _checkpoint(**overrides: object) -> dict:
    document: dict[str, object] = {
        "protocol": a7.CHECKPOINT_PROTOCOL,
        "checkpoint_id": _digest("checkpoint-id:0"),
        "request_id": _digest("request"),
        "session_id": _digest("session"),
        "request_attempt": 1,
        "source_placement_id": _digest("placement:source"),
        "source_membership_incarnation": _incarnation("source"),
        "layer_range": [0, 8],
        "checkpoint_sequence": 0,
        "predecessor_digest": None,
        "committed_token_count": 64,
        "committed_prefix_digest": _digest("prefix:64"),
        "kv_position": 64,
        "byte_size": 32,
        "encrypted_payload_reference": "ref:00000000000000000000000000000000",
        "expires_at_unix_ms": 1_800_000_000_000,
        "source_authority_generation": 3,
    }
    document.update(overrides)
    document["evidence_digest"] = a7.binding_digest(document)
    return document


def _identity(**overrides: object) -> dict:
    values: dict[str, object] = {
        field: _digest(f"identity:{field}")
        for fields in a7.COMPATIBILITY_IDENTITY_GROUPS.values()
        for field in fields
    }
    values.update(overrides)
    return values


def _intent(**overrides: object) -> dict:
    document: dict[str, object] = {
        "protocol": a7.STANDBY_INTENT_PROTOCOL,
        "intent_id": _digest("intent"),
        "request_id": _digest("request"),
        "standby_placement_id": _digest("placement:standby"),
        "standby_membership_incarnation": _incarnation("standby"),
        "layer_range": [0, 8],
        "covered_failure_scope": "placement",
        "required_identity_fields": sorted(
            a7.COMPATIBILITY_IDENTITY_FIELDS[:3]
        ),
        "max_context_tokens": 4096,
        "max_checkpoint_bytes": 1_048_576,
        "max_retained_checkpoints": 8,
        "hysteresis_generations": 3,
        "hysteresis_seconds": 10,
        "reservation_generation": 2,
        "route_ready": False,
    }
    document.update(overrides)
    document["evidence_digest"] = a7.binding_digest(document)
    return document


def _qualification(**overrides: object) -> dict:
    document: dict[str, object] = {
        "protocol": a7.SUCCESSOR_QUALIFICATION_PROTOCOL,
        "qualification_id": _digest("qualification"),
        "request_id": _digest("request"),
        "standby_placement_id": _digest("placement:standby"),
        "successor_membership_incarnation": _incarnation("standby"),
        "compatibility": a7.compare_compatibility(_identity(), _identity()),
        "replaced_stage_ids": [_stage("stage-a")],
        "qualified_stage_ids": [_stage("stage-a")],
        "path_generation": 1,
        "resource_reservation_generation": 2,
        "parity_challenged": True,
        "parity_passed": True,
        "fence_generation": 1,
        "fallback_available": True,
        "physical_challenge_executed": False,
        "qualification_scope": "deterministic_in_process",
        "issuer": "qualifier",
        "issued_at_unix_ms": 1_000,
        "expires_at_unix_ms": 2_000_000,
    }
    document.update(overrides)
    document["evidence_digest"] = a7.binding_digest(document)
    return document


def _cycle(**overrides: object) -> a7.SuccessorRecoveryCycle:
    arguments: dict[str, object] = {
        "request_id": _digest("request"),
        "session_id": _digest("session"),
        "request_attempt": 1,
        "replaced_stage_ids": [_stage("stage-a")],
        "current_incarnations": {_stage("stage-a"): _incarnation("stage-a")},
        "source_placement_id": _digest("placement:source"),
        "standby_placement_id": _digest("placement:standby"),
        "source_identity": _identity(),
        "successor_identity": _identity(),
        "layer_range": [0, 8],
        "gateway_committed_token_count": 64,
        "gateway_committed_prefix_digest": _digest("prefix:64"),
        "standby_intent": _intent(),
        "qualification": _qualification(),
        "now_unix_ms": 1_500,
        "reservation_generation": 2,
    }
    arguments.update(overrides)
    return a7.SuccessorRecoveryCycle(**arguments)


def _shadowed_cycle(**overrides: object) -> a7.SuccessorRecoveryCycle:
    """A cycle with one sealed+acknowledged checkpoint: watermark == 64."""

    cycle = _cycle(**overrides)
    cycle.accept_checkpoint(
        checkpoint=_checkpoint(), kv_payload=b"k" * 32, stage_id=_stage("stage-a")
    )
    return cycle


# ---------------------------------------------------------------------------
# Reachability guard (the RED -> GREEN receipt)
# ---------------------------------------------------------------------------


def test_every_runtime_seam_is_reachable_from_the_public_api() -> None:
    for name in sorted(RUNTIME_SEAMS):
        assert hasattr(a7, name), f"{name} is not importable from the public API"
        assert callable(getattr(a7, name)), f"{name} is not callable"

    assert set(a7.IMPLEMENTED_RUNTIME_SEAMS) == set(RUNTIME_SEAMS)
    assert a7.SCAFFOLD_BOUNDARIES == {}, (
        "no runtime seam may still be declared a scaffold boundary"
    )


def test_no_runtime_seam_refuses_with_a_scaffold_boundary() -> None:
    calls = _hostile_seam_calls()
    assert set(calls) == set(RUNTIME_SEAMS)

    for name, call in calls.items():
        try:
            value = call()
        except a7.A7ScaffoldBoundary as exc:  # pragma: no cover - the RED path
            pytest.fail(f"{name} still refuses: {exc}")
        except ValueError:
            continue  # fail-closed contract rejection: the intended behaviour
        pytest.fail(f"{name} returned {value!r} for a hostile input")


def test_the_runtime_half_can_run_without_any_scaffold_boundary_class() -> None:
    # The exception class stays exported (a future seam may need it), but the
    # runtime half never raises it: it is a plain NotImplementedError subclass.
    assert issubclass(a7.A7ScaffoldBoundary, NotImplementedError)
    assert not issubclass(a7.A7ScaffoldBoundary, ValueError)


# ---------------------------------------------------------------------------
# Registers, payloads, transport
# ---------------------------------------------------------------------------


def test_standby_intent_is_closed_and_never_route_ready() -> None:
    document = _intent()
    assert a7.validate_kv_standby_intent(document) == document

    extra = _intent()
    extra["note"] = "unexpected"
    with pytest.raises(ValueError):
        a7.validate_kv_standby_intent(extra)

    with pytest.raises(ValueError):
        a7.validate_kv_standby_intent(_intent(route_ready=True))
    with pytest.raises(ValueError):
        a7.validate_kv_standby_intent(_intent(covered_failure_scope="cluster"))
    with pytest.raises(ValueError):
        a7.validate_kv_standby_intent(
            _intent(required_identity_fields=["not_an_identity_field"])
        )
    with pytest.raises(ValueError):
        a7.validate_kv_standby_intent(_intent(max_checkpoint_bytes=0))


def test_sealing_is_byte_exact_bounded_and_privacy_reduced() -> None:
    store = a7.InProcessPayloadStore()
    checkpoint = _checkpoint()

    envelope = a7.seal_checkpoint_payload(
        checkpoint=checkpoint, kv_payload=b"k" * 32, store=store
    )
    assert envelope == {
        "protocol": a7.PAYLOAD_ENVELOPE_PROTOCOL,
        "encrypted_payload_reference": checkpoint["encrypted_payload_reference"],
        "payload_digest": "sha256:" + hashlib.sha256(b"k" * 32).hexdigest(),
        "byte_size": 32,
        "confidentiality": "not_provided_in_process_scaffold",
        "store_scope": "process_local_in_memory",
        "checkpoint_sequence": 0,
        "committed_token_count": 64,
        "payload": None,
    }
    assert b"k" * 32 not in repr(envelope).encode("utf-8")

    opened = a7.open_checkpoint_payload(
        reference=checkpoint["encrypted_payload_reference"], store=store
    )
    assert opened["payload_digest"] == envelope["payload_digest"]
    assert opened["payload"] is None

    assert (
        a7.open_checkpoint_payload(
            reference=checkpoint["encrypted_payload_reference"],
            store=store,
            include_bytes=True,
        )["payload"]
        == b"k" * 32
    )

    with pytest.raises(ValueError):
        a7.seal_checkpoint_payload(
            checkpoint=checkpoint, kv_payload=b"k" * 31, store=store
        )
    with pytest.raises(a7.A7ContractViolation):
        a7.open_checkpoint_payload(reference="ref:unregistered", store=store)
    with pytest.raises(a7.A7ContractViolation):
        a7.open_checkpoint_payload(reference="ref:unregistered")


def test_transfer_produces_the_acknowledgement_the_watermark_ledger_consumes() -> None:
    cycle = _cycle()
    channel = cycle.channel

    envelope = cycle.seal(checkpoint=_checkpoint(), kv_payload=b"k" * 32)
    ack = a7.transfer_shadow_checkpoint(
        checkpoint=_checkpoint(),
        standby_placement_id=_digest("placement:standby"),
        channel=channel,
        stage_id=_stage("stage-a"),
        payload_digest=envelope["payload_digest"],
    )

    assert a7.validate_private_kv_checkpoint_ack(ack) == ack
    assert ack["checkpoint_digest"] == _checkpoint()["evidence_digest"]
    assert ack["stored_checkpoint_digest_verified"] is True
    assert ack["acknowledgement_authority"] == "standby_runtime"
    assert cycle.ledger.record(ack) == "advanced"
    assert cycle.ledger.route_watermark["committed_token_count"] == 64

    # A replayed transfer of the same reference with a different payload digest
    # is a conflict, not new shadow data.
    with pytest.raises(a7.A7ContractViolation):
        a7.transfer_shadow_checkpoint(
            checkpoint=_checkpoint(),
            standby_placement_id=_digest("placement:standby"),
            channel=channel,
            stage_id=_stage("stage-a"),
            payload_digest=_digest("other-payload"),
        )

    oversized = _checkpoint(
        encrypted_payload_reference="ref:" + "1" * 32, byte_size=2_000_000
    )
    with pytest.raises(a7.A7ContractViolation):
        a7.transfer_shadow_checkpoint(
            checkpoint=oversized,
            standby_placement_id=_digest("placement:standby"),
            channel=channel,
            stage_id=_stage("stage-a"),
            payload_digest=_digest("over-bound"),
        )

    with pytest.raises(a7.A7ContractViolation):
        a7.transfer_shadow_checkpoint(
            checkpoint=_checkpoint(),
            standby_placement_id=_digest("placement:standby"),
            channel=channel,
            stage_id=_stage("stage-unknown"),
            payload_digest=_digest("other-payload"),
        )


def test_fence_persistence_is_monotonic_and_declares_itself_not_durable() -> None:
    store = a7.FenceGenerationStore()
    state = a7.FenceLedger(
        request_attempt=1, path_generation=1, fencing_generation=1
    ).state

    receipt = a7.persist_fence_generation(fence_state=state, store=store)
    assert receipt["protocol"] == a7.FENCE_PERSISTENCE_PROTOCOL
    assert receipt["durability"] == "not_durable_in_process_scaffold"
    assert receipt["store_scope"] == "process_local_in_memory"
    assert receipt["fencing_generation"] == 1
    assert receipt["sequence"] == 1
    assert store.latest(request_attempt=1)["fencing_generation"] == 1

    with pytest.raises(a7.A7ContractViolation):
        a7.persist_fence_generation(fence_state=state, store=store)

    advanced = a7.FenceLedger(
        request_attempt=2, path_generation=2, fencing_generation=2
    ).state
    assert a7.persist_fence_generation(fence_state=advanced, store=store)["sequence"] == 2

    with pytest.raises(a7.A7ContractViolation):
        a7.persist_fence_generation(fence_state={}, store=store)


# ---------------------------------------------------------------------------
# Cutover
# ---------------------------------------------------------------------------


def test_fenced_cutover_requires_an_explicit_request_and_a_well_formed_fence() -> None:
    with pytest.raises(a7.A7ContractViolation):
        a7.perform_fenced_cutover(fence_state={}, checkpoint={})
    with pytest.raises(a7.A7ContractViolation):
        a7.perform_fenced_cutover(
            fence_state=a7.FenceLedger(
                request_attempt=1, path_generation=1, fencing_generation=1
            ).state,
            checkpoint=_checkpoint(),
        )


def test_fenced_cutover_continues_without_replay_when_every_gate_passes() -> None:
    cycle = _shadowed_cycle()
    cyc_fence = cycle.fence
    for participant in a7.FENCE_INSTALLERS:
        cyc_fence.install(participant)

    result = a7.perform_fenced_cutover(
        fence_state=cyc_fence.state,
        checkpoint=_checkpoint(),
        cutover_request=cycle.build_cutover_request(),
    )

    assert result["protocol"] == a7.CUTOVER_RESULT_PROTOCOL
    assert result["outcome"] == "fenced_kv_successor"
    assert result["reason"] == "eligible"
    assert result["committed_token_count"] == 64
    assert result["next_position"] == 64
    assert result["recovery_prefill_performed"] is False
    assert result["prompt_or_prefix_transferred"] is False
    assert result["execution_mode"] == "in_process_deterministic"
    assert result["physical_evidence"] is False
    assert result["a6_handoff"]["handoff"] == "not_requested"


def test_fenced_cutover_hands_off_to_a6_and_never_relabels_replay() -> None:
    cycle = _shadowed_cycle(gateway_committed_token_count=80)
    for participant in a7.FENCE_INSTALLERS:
        cycle.fence.install(participant)

    result = a7.perform_fenced_cutover(
        fence_state=cycle.fence.state,
        checkpoint=_checkpoint(),
        cutover_request=cycle.build_cutover_request(),
    )

    assert result["outcome"] == "full_context_replay"
    assert result["reason"] == "watermark_behind_gateway"
    assert result["outcome"] != "fenced_kv_successor"
    handoff = result["a6_handoff"]
    assert handoff["protocol"] == a7.A6_HANDOFF_PROTOCOL
    assert handoff["a6_reachable"] is True
    assert handoff["a6_runtime_admitted"] is True
    assert handoff["handoff"] == "full_context_replay_requested"
    assert handoff["a6_qualification_claim"] is False
    assert handoff["a6_promotion_authorized"] is False
    assert handoff["physical_evidence"] is False
    # the A7 scaffold requests the A6 interface; it never claims A6 executed it
    assert "executed" not in handoff["handoff"]


def test_fenced_cutover_aborts_explicitly_when_a6_is_unavailable() -> None:
    cycle = _shadowed_cycle(gateway_committed_token_count=80, a6_available=False)
    for participant in a7.FENCE_INSTALLERS:
        cycle.fence.install(participant)

    result = a7.perform_fenced_cutover(
        fence_state=cycle.fence.state,
        checkpoint=_checkpoint(),
        cutover_request=cycle.build_cutover_request(),
    )

    assert result["outcome"] == "aborted"
    assert result["reason"] == "watermark_behind_gateway"
    assert result["a6_handoff"]["handoff"] == "not_requested"
    assert result["a6_handoff"]["a6_reachable"] is True


# ---------------------------------------------------------------------------
# Orchestration, projection and the honest boundary
# ---------------------------------------------------------------------------


def test_run_successor_recovery_completes_the_cycle_in_process() -> None:
    cycle = _shadowed_cycle()

    runtime = a7.run_successor_recovery(request_id=_digest("request"), cycle=cycle)

    assert a7.validate_kv_successor_runtime(runtime) == runtime
    assert runtime["recovery_mode"] == "fenced_kv_successor"
    assert runtime["terminal_outcome"] == "fenced_kv_successor"
    assert runtime["standby_state"] == "released"
    assert runtime["source_state"] == "released"
    assert runtime["fence_installed"] is True
    assert runtime["cutover_granted"] is True
    assert runtime["cleanup_complete"] is True
    assert runtime["acknowledged_watermark"] == 64
    assert runtime["acknowledged_lag"] == 0
    assert runtime["checkpoint_count"] == 1
    assert runtime["aggregate_kv_bytes"] == 32

    assert runtime["execution_mode"] == "in_process_deterministic"
    assert runtime["physical_evidence"] is False
    assert runtime["qualification_claim"] is False
    assert runtime["promotion_authorized"] is False


def test_run_successor_recovery_fails_closed_without_a_cycle() -> None:
    with pytest.raises(a7.A7ContractViolation):
        a7.run_successor_recovery(request_id=_digest("request"))
    with pytest.raises(a7.A7ContractViolation):
        a7.run_successor_recovery(request_id=_digest("request"), cycle=object())
    with pytest.raises(a7.A7ContractViolation):
        a7.run_successor_recovery(
            request_id=_digest("other-request"), cycle=_shadowed_cycle()
        )


def test_the_runtime_projection_cannot_express_a_physical_claim() -> None:
    cycle = _shadowed_cycle()
    runtime = a7.run_successor_recovery(request_id=_digest("request"), cycle=cycle)

    for change in (
        {"physical_evidence": True},
        {"qualification_claim": True},
        {"promotion_authorized": True},
        {"execution_mode": "physical"},
        {"source_state": "qualified_somehow"},
        {"acknowledged_lag": -1},
    ):
        tampered = {**runtime, **change}
        tampered["evidence_digest"] = a7.binding_digest(tampered)
        with pytest.raises(ValueError):
            a7.validate_kv_successor_runtime(tampered)

    extra = {**runtime, "note": "unexpected"}
    with pytest.raises(ValueError):
        a7.validate_kv_successor_runtime(extra)


def test_the_recovery_gate_record_cannot_claim_an_earned_physical_gate() -> None:
    cycle = _shadowed_cycle()
    a7.run_successor_recovery(request_id=_digest("request"), cycle=cycle)
    gate = cycle.recovery_gate()

    assert a7.validate_kv_successor_recovery_gate(gate) == gate
    assert gate["gate_state"] == "design_only"
    assert gate["gate_evidence_kind"] == "deterministic_in_process_test_only_seam"
    assert gate["physical_evidence"] is False
    assert gate["browser_scenarios_executed"] is False
    assert gate["performance_measured"] is False
    assert gate["promotion_authorized"] is False
    assert gate["counter_report"]["standby_kv_live"] == 0

    for change in (
        {"gate_state": "passed"},
        {"physical_evidence": True},
        {"browser_scenarios_executed": True},
        {"performance_measured": True},
        {"promotion_authorized": True},
        {"counter_report": {**gate["counter_report"], "standby_kv_live": 1}},
    ):
        tampered = {**gate, **change}
        tampered["evidence_digest"] = a7.binding_digest(tampered)
        with pytest.raises(ValueError):
            a7.validate_kv_successor_recovery_gate(tampered)


def test_the_qualification_record_cannot_claim_a_physical_challenge() -> None:
    document = _qualification()
    assert a7.validate_kv_successor_qualification(document) == document
    assert a7.qualification_is_current(document, now_unix_ms=1_500) is True
    assert a7.qualification_is_current(document, now_unix_ms=2_000_001) is False

    for change in (
        {"physical_challenge_executed": True},
        {"qualification_scope": "physical"},
        {"issuer": "standby_runtime"},
        {"qualified_stage_ids": [_stage("stage-b")]},
        {"parity_passed": False},
    ):
        tampered = {**document, **change}
        tampered["evidence_digest"] = a7.binding_digest(tampered)
        with pytest.raises(ValueError):
            a7.validate_kv_successor_qualification(tampered)

    incompatible = _qualification(
        compatibility=a7.compare_compatibility(
            _identity(), _identity(cache_implementation="mlx")
        )
    )
    assert a7.validate_kv_successor_qualification(incompatible) == incompatible
    assert a7.qualification_is_current(incompatible, now_unix_ms=1_500) is False


def test_a_withheld_cutover_is_reported_as_replay_or_abort_never_as_continuity() -> None:
    for a6_available, expected in ((True, "full_context_replay"), (False, "aborted")):
        cycle = _shadowed_cycle(
            gateway_committed_token_count=80, a6_available=a6_available
        )
        runtime = a7.run_successor_recovery(request_id=_digest("request"), cycle=cycle)

        assert runtime["recovery_mode"] == expected
        assert runtime["recovery_mode"] != "fenced_kv_successor"
        assert runtime["cutover_granted"] is False
        assert runtime["fallback_reason"] == "watermark_behind_gateway"
        assert runtime["physical_evidence"] is False
        assert runtime["standby_state"] == "rejected"


def test_the_in_process_cycle_is_deterministic_and_releases_every_payload() -> None:
    request_id = _digest("request")

    first = a7.run_successor_recovery(request_id=request_id, cycle=_shadowed_cycle())
    second = a7.run_successor_recovery(request_id=request_id, cycle=_shadowed_cycle())

    assert first == second
    assert first["runtime_id"] == second["runtime_id"]
    assert first["evidence_digest"] == second["evidence_digest"]

    cycle = _shadowed_cycle()
    a7.run_successor_recovery(request_id=request_id, cycle=cycle)
    assert cycle.recovery_gate() == cycle.recovery_gate()
    assert cycle.payloads.retained_payloads == 0
    assert cycle.payloads.retained_bytes == 0
    assert cycle.payloads.references == ()


def test_the_a6_seam_is_reached_lazily_and_never_at_import_time() -> None:
    source = Path(a7.__file__).read_text("utf-8")
    before_the_handoff = source.split("def _a6_replay_handoff", 1)[0]

    # neither the deterministic core nor the runtime half imports A6 eagerly
    assert "import mycelium_a6_recovery" not in source
    assert "importlib.import_module(A6_SEAM_MODULE)" not in before_the_handoff
    # the handoff reaches the real seam through the guarded lazy import
    assert "importlib.import_module(A6_SEAM_MODULE)" in source
    assert a7.A6_SEAM_MODULE == "mycelium_a6_recovery"
