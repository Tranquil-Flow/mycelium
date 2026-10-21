# SPDX-License-Identifier: AGPL-3.0-only
"""Class invariant: the reviewed trace-identity allow-list must track the canonical producer.

``tests/seed_coordinator/test_local_two_node.py::_assert_request_route_evidence`` bounds every
remote trace entry to 512 bytes (``_TRACE_ENTRY_BYTES``) and asserts each entry's identity key set
is a subset of ``PERMITTED_REMOTE_TRACE_IDENTITY_KEYS`` - a hand-reviewed allow-list of everything
that may legitimately appear in a delivery trace.  RCA ``wp1-baseline`` section 6 closes the class
question with an invariant that derives the permitted key universe *from the producer itself*, so
the next deliberate producer change fails here (naming the offending key) instead of drifting
silently until an unrelated end-to-end suite goes red.

Single-identity behaviour is already covered by the root-level tests and is deliberately NOT
duplicated here:

* ``test_router_iroh_transport.py::test_hop_trace_binds_request_path_placement_and_delivery_receipt``
* ``test_router_iroh_transport.py::test_hop_trace_does_not_leak_overlong_attribution_or_displace_receipt``

This module adds the class invariant only: containment of the producer's emitted key universe in the
reviewed allow-list (both directions), retention of the attribution fields added by ``4ee5fb6``,
drop-not-truncate on overlong attribution without displacing the delivery receipt, and the per-entry
bound that the two-node fixture hard-codes as the literal ``512``.
"""

from __future__ import annotations

import inspect
import json

from mycelium_router.contracts import (
    ExecutionGraph,
    HopHeader,
    LayerRange,
    Placement,
    ProgressivePrefillMessage,
    RequestContext,
    Stage,
    StageCost,
)
from mycelium_router.transports.iroh import (
    _TRACE_ENTRY_BYTES,
    _TRACE_ID_BYTES,
    _bounded_trace_identity,
)
from tests.seed_coordinator.test_local_two_node import (
    PERMITTED_REMOTE_TRACE_IDENTITY_KEYS,
)

# 4ee5fb6 "fix(a5): retain bounded placement and path attribution in delivery traces"
PLACEMENT_ATTRIBUTION_FIELDS = (
    "destination_placement_id",
    "path_id",
    "source_placement_id",
)
PATH_ATTEMPT_FIELD = "path_attempt"
ATTRIBUTION_FIELDS = (*PLACEMENT_ATTRIBUTION_FIELDS, PATH_ATTEMPT_FIELD)

# Private hop metadata that must never reach a trace identity, even fully populated.
PRIVATE_HOP_FIELDS = ("hop_index", "topology_version", "idempotency_key")

# The one entry the two-node fixture observers actually exercise (the class that first
# tripped the allow-list when 4ee5fb6 landed).
TWO_NODE_TRACE_PREFIX = "ProgressivePrefillMessage->peer:remote:"

# Reviewed tolerance for the allow-list drift reported by `wp1-fix`: a request-context field
# that `_bounded_trace_identity` does not emit for any input class (asserted structurally
# below).  It is left in place because this work packet is scoped to *adding* the four fields
# `4ee5fb6` introduced, and narrowing the set is a separate reviewable change.
PERMITTED_BUT_UNREACHABLE_KEYS = frozenset({"generation_config_digest"})


def _hop_header(
    *,
    request_id: str = "request-invariant",
    path_id: str = "path-invariant",
) -> HopHeader:
    """A ``HopHeader`` with every optional attribution field populated to a short value."""
    return HopHeader(
        request_id=request_id,
        path_id=path_id,
        path_attempt=2,
        phase="DECODE",
        token_index=3,
        hop_index=1,
        source_placement_id="placement-source",
        destination_placement_id="placement-destination",
        topology_version=1,
        idempotency_key="idempotency-key-not-for-traces",
    )


def _overlong_hop_header() -> HopHeader:
    """The same header with every private attribution value far beyond ``_TRACE_ID_BYTES``."""
    return HopHeader(
        request_id="request-short",
        path_id="private-path-" + "x" * 2048,
        path_attempt=2,
        phase="DECODE",
        token_index=3,
        hop_index=1,
        source_placement_id="private-source-" + "x" * 2048,
        destination_placement_id="private-destination-" + "x" * 2048,
        topology_version=1,
        idempotency_key="private-idempotency-key",
    )


def _request_context() -> RequestContext:
    return RequestContext(
        request_id="request-invariant",
        prompt_token_ids=(1, 2, 3),
        max_new_tokens=3,
        expected_new_tokens=3,
        qos_class="interactive",
        admitted_at=0.0,
        target_ttft_ms=1_000.0,
        target_tpot_ms=1_000.0,
        target_tokens_per_second=1.0,
        sampling_seed=37,
        generation_config_digest="sha256:" + "e" * 64,
    )


def _execution_graph() -> ExecutionGraph:
    placement = Placement(
        placement_id="placement-0",
        node_id="node-0",
        replica_group_id="group-0",
        assignment_id="assignment-0",
        stage_signature="sha256:" + "a" * 64,
        load_proof_digest="sha256:" + "b" * 64,
        runtime_backend="mlx",
        runtime_endpoint="endpoint-0",
    )
    stage = Stage(
        stage_id="stage-0",
        layer_range=LayerRange(start_layer=0, end_layer_exclusive=1, layer_count=1),
        component_roles=("decoder",),
        stage_cost=StageCost(
            prefill_work_units_per_prompt_token=1.0,
            decode_work_units_per_token=1.0,
            kv_bytes_per_context_token=8,
        ),
        placements=(placement,),
    )
    return ExecutionGraph(
        deployment_id="deployment-0",
        deployment_epoch=4,
        topology_version=1,
        model_id="model-0",
        resolved_commit="0" * 40,
        manifest_digest="sha256:" + "c" * 64,
        entry_stage_id="stage-0",
        final_stage_id="stage-0",
        hidden_size=8,
        activation_bytes=8,
        token_envelope_bytes=8,
        stages=(stage,),
        edges=(),
        loopback_edges=(),
    )


def _producer_identity(message: object) -> dict[str, object]:
    """Read-only call of the canonical producer; no production code is patched or edited."""
    rendered = _bounded_trace_identity(message, delivery_message_id=b"x" * 16)
    identity = json.loads(rendered)
    assert isinstance(identity, dict)
    assert len(rendered.encode("utf-8")) <= _TRACE_ENTRY_BYTES
    return identity


def _producer_reachable_keys() -> set[str]:
    """Union of keys the canonical producer emits across its in-band message classes.

    The union - not any single call - is the universe, because each optional field is admitted
    only while the rendered identity still fits the per-entry budget.
    """
    reachable = set(_producer_identity(_hop_header()))
    # `request_id_sha256` replaces `request_id` only when the request id is overlong.
    reachable |= set(_producer_identity(_hop_header(request_id="request-" + "r" * 2048)))
    # The message class that actually appears in the two-node trace.
    reachable |= set(
        _producer_identity(
            ProgressivePrefillMessage(
                header=_hop_header(),
                graph=_execution_graph(),
                request=_request_context(),
                ordered_hops=(),
            )
        )
    )
    return reachable


def test_producer_key_universe_is_permitted_by_the_reviewed_allow_list() -> None:
    """Every key the producer can emit is permitted, so a new field cannot drift silently."""
    reachable = _producer_reachable_keys()
    assert reachable <= PERMITTED_REMOTE_TRACE_IDENTITY_KEYS, (
        "the canonical producer emitted trace-identity keys the reviewed seed_coordinator "
        "allow-list does not permit; extend the allow-list with the commit and bound that "
        "introduced them (do not widen the producer to make the fixture pass): "
        f"{sorted(reachable - PERMITTED_REMOTE_TRACE_IDENTITY_KEYS)}"
    )
    assert set(ATTRIBUTION_FIELDS) <= reachable


def test_permitted_key_set_is_not_silently_broader_than_the_producer() -> None:
    """The allow-list may not grow without a producer that can actually emit the key."""
    stale = PERMITTED_REMOTE_TRACE_IDENTITY_KEYS - _producer_reachable_keys()
    assert stale == PERMITTED_BUT_UNREACHABLE_KEYS, (
        "a permitted trace-identity key is no longer reachable from the canonical producer; "
        f"review it instead of leaving a dead permission: {sorted(stale)}"
    )
    # Structural proof for the tolerated entry: the producer never names the field at all.
    assert "generation_config_digest" not in inspect.getsource(_bounded_trace_identity)


def test_hop_header_attribution_is_retained_and_private_metadata_is_not() -> None:
    """4ee5fb6's four bounded attribution fields are emitted; private hop metadata is not."""
    identity = _producer_identity(_hop_header())
    assert set(ATTRIBUTION_FIELDS) <= set(identity)
    assert identity["path_id"] == "path-invariant"
    assert identity["path_attempt"] == 2
    assert identity["source_placement_id"] == "placement-source"
    assert identity["destination_placement_id"] == "placement-destination"
    assert not set(PRIVATE_HOP_FIELDS) & set(identity)


def test_two_node_trace_entry_stays_within_the_hard_coded_entry_bound() -> None:
    """The fixture's literal 512 mirrors the producer's own per-entry budget."""
    assert _TRACE_ENTRY_BYTES == 512
    assert _TRACE_ID_BYTES == 128
    identity_budget = _TRACE_ENTRY_BYTES - len(TWO_NODE_TRACE_PREFIX.encode("utf-8"))
    entry = TWO_NODE_TRACE_PREFIX + _bounded_trace_identity(
        ProgressivePrefillMessage(
            header=_hop_header(),
            graph=_execution_graph(),
            request=_request_context(),
            ordered_hops=(),
        ),
        max_bytes=identity_budget,
        delivery_message_id=b"x" * 16,
    )
    identity = json.loads(entry.partition(":remote:")[2])
    assert len(entry.encode("utf-8")) <= _TRACE_ENTRY_BYTES
    assert set(identity) <= PERMITTED_REMOTE_TRACE_IDENTITY_KEYS
    # The delivery receipt id is never the field sacrificed to the entry bound.
    assert identity["delivery_message_id"] == (b"x" * 16).hex()
    assert set(identity) & set(ATTRIBUTION_FIELDS)
    # Attribution is dropped only because the bound forbids more, never by policy: re-adding any
    # missing attribution field must overflow the producer's per-entry budget.
    for field in set(ATTRIBUTION_FIELDS) - set(identity):
        oversized = {
            **identity,
            field: getattr(_hop_header(), field),
        }
        assert (
            len(json.dumps(oversized, sort_keys=True, separators=(",", ":")).encode())
            > identity_budget
        )


def test_overlong_attribution_is_dropped_without_displacing_the_receipt() -> None:
    """Overlong attribution is dropped, never truncated, and never displaces the receipt id."""
    header = _overlong_hop_header()
    rendered = _bounded_trace_identity(header, delivery_message_id=b"x" * 16)
    identity = json.loads(rendered)
    assert len(rendered.encode("utf-8")) <= _TRACE_ENTRY_BYTES
    # The three overlong strings are gone; the bounded int/phase/request evidence survives.
    assert set(identity) == {
        "delivery_message_id",
        "path_attempt",
        "phase",
        "request_id",
        "token_index",
    }
    assert identity["delivery_message_id"] == (b"x" * 16).hex()
    assert identity["request_id"] == "request-short"
    assert "private-" not in rendered
    # Dropped, not truncated into a digest substitute of their own.
    assert not any(key.endswith("_sha256") for key in identity)
    assert set(identity) <= PERMITTED_REMOTE_TRACE_IDENTITY_KEYS
    # Provenance of the drop: each rejected value would still overflow the entry bound, so the
    # overlong guard is what removed it - not an incidental budget accident.
    for field in PLACEMENT_ATTRIBUTION_FIELDS:
        oversized = {**identity, field: getattr(header, field)}
        assert (
            len(json.dumps(oversized, sort_keys=True, separators=(",", ":")).encode())
            > _TRACE_ENTRY_BYTES
        )
