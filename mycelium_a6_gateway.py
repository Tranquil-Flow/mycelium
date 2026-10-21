# SPDX-License-Identifier: AGPL-3.0-only
"""A6 gateway-side replay checkpoint freeze seam.

Bridges the request gateway's committed delivery state — the exact canonical
encoded prompt token IDs, the exact committed generated-token IDs, the delivery
ledger cursor, and the publisher generation — into the A6 owner-private
replay checkpoint.  The gateway is the sole authority for the canonical
prompt and the committed prefix (spec section 4); this module freezes that
authority without changing any existing gateway behaviour.

Token text never enters the checkpoint.  Integer token IDs do: digests alone
can verify a prefix but cannot reconstruct it, while A6 requires exact replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from mycelium_a6_recovery import (
    CHECKPOINT_PROTOCOL,
    ReplayCheckpoint,
    _digest,
    _prefix_digest,
)

GENERATION_SETTINGS_FIELDS: frozenset[str] = frozenset(
    {"protocol", "max_new_tokens", "workload_profile_id", "qos_class"}
)


def canonical_generation_settings(value: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical generation settings digest input (exact closed shape)."""
    if not isinstance(value, Mapping) or set(value) != GENERATION_SETTINGS_FIELDS:
        raise ValueError("generation_settings_shape_invalid")
    result: dict[str, Any] = {}
    for key in ("protocol", "workload_profile_id", "qos_class"):
        if value[key] is not None and not isinstance(value[key], str):
            raise ValueError("generation_settings_shape_invalid")
        result[key] = value[key]
    if (
        type(value["max_new_tokens"]) is not int
        or value["max_new_tokens"] < 1
    ):
        raise ValueError("generation_settings_shape_invalid")
    result["max_new_tokens"] = value["max_new_tokens"]
    return result


def _token_ids(
    value: Sequence[int],
    *,
    field: str,
    allow_empty: bool,
) -> tuple[int, ...]:
    if (
        not isinstance(value, tuple)
        or (not allow_empty and not value)
        or not all(type(token_id) is int and token_id >= 0 for token_id in value)
    ):
        raise ValueError(f"{field}_invalid")
    return value


@dataclass(frozen=True, slots=True)
class CommittedDeliverySnapshot:
    """The exact committed delivery state one replay checkpoint binds.

    Delivery-state fields are owned and read from the live gateway session;
    recovery-context fields (session/path/deployment/model authority) are
    exact inputs supplied by the recovery coordinator and the captured
    qualification — the gateway never invents them.
    """

    request_id: str
    encoded_prompt_token_ids: tuple[int, ...]  # private; never projected
    generation_settings: Mapping[str, Any]
    committed_token_ids: tuple[int, ...]  # private exact committed prefix
    delivery_cursor: int  # acknowledged_through event sequence
    publisher_generation: int
    session_id: str
    source_attempt: int
    path_id: str
    path_digest: str  # canonical path manifest digest
    path_attempt: int
    deployment_id: str
    deployment_epoch: int
    topology_generation: int
    source_qualification_digest: str
    model_id: str
    representation_digest: str  # serving representation + manifest digest
    tokenizer_digest: str
    recovery_attempt: int = 0

    def __post_init__(self) -> None:
        _token_ids(
            self.encoded_prompt_token_ids,
            field="snapshot_encoded_prompt_token_ids",
            allow_empty=False,
        )
        canonical_generation_settings(self.generation_settings)
        _token_ids(
            self.committed_token_ids,
            field="snapshot_committed_token_ids",
            allow_empty=True,
        )
        for name, minimum in (
            ("delivery_cursor", -1),  # -1 = nothing committed yet
            ("publisher_generation", 0),
            ("source_attempt", 1),
            ("path_attempt", 0),
            ("deployment_epoch", 1),
            ("topology_generation", 1),
            ("recovery_attempt", 0),
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < minimum:
                raise ValueError(f"snapshot_{name}_invalid")
        if (
            not isinstance(self.source_qualification_digest, str)
            or len(self.source_qualification_digest) != 71
            or not self.source_qualification_digest.startswith("sha256:")
            or any(
                char not in "0123456789abcdef"
                for char in self.source_qualification_digest[7:]
            )
        ):
            raise ValueError("snapshot_source_qualification_digest_invalid")

    def prompt_digest(self) -> str:
        return _digest(self.encoded_prompt_token_ids)

    def generation_settings_digest(self) -> str:
        return _digest(canonical_generation_settings(self.generation_settings))

    def committed_prefix_digest(self) -> str | None:
        if not self.committed_token_ids:
            return None
        return _prefix_digest(self.committed_token_ids)


def freeze_replay_checkpoint(
    snapshot: CommittedDeliverySnapshot,
    *,
    store_identity: str,
    created_at_unix_ms: int,
    expiry_unix_ms: int,
    reconciliation_state: str = "frozen",
) -> ReplayCheckpoint:
    """Freeze the snapshot into a digest-bound owner-private replay checkpoint."""
    return ReplayCheckpoint(
        protocol=CHECKPOINT_PROTOCOL,
        store_identity=store_identity,
        request_id=snapshot.request_id,
        session_id=snapshot.session_id,
        source_attempt=snapshot.source_attempt,
        path_id=snapshot.path_id,
        path_digest=snapshot.path_digest,
        path_attempt=snapshot.path_attempt,
        prompt_digest=snapshot.prompt_digest(),
        generation_settings_digest=snapshot.generation_settings_digest(),
        committed_token_count=len(snapshot.committed_token_ids),
        committed_prefix_digest=snapshot.committed_prefix_digest(),
        delivery_cursor=snapshot.delivery_cursor,
        watermark=len(snapshot.committed_token_ids),
        publisher_generation=snapshot.publisher_generation,
        deployment_id=snapshot.deployment_id,
        deployment_epoch=snapshot.deployment_epoch,
        topology_generation=snapshot.topology_generation,
        source_qualification_digest=snapshot.source_qualification_digest,
        model_id=snapshot.model_id,
        representation_digest=snapshot.representation_digest,
        tokenizer_digest=snapshot.tokenizer_digest,
        recovery_attempt=snapshot.recovery_attempt,
        terminal_authority=None,
        created_at_unix_ms=created_at_unix_ms,
        expiry_unix_ms=expiry_unix_ms,
        reconciliation_state=reconciliation_state,
        encoded_prompt=snapshot.encoded_prompt_token_ids,
        committed_prefix_tokens=snapshot.committed_token_ids,
    )


def checkpoint_binds_snapshot(
    checkpoint: ReplayCheckpoint, snapshot: CommittedDeliverySnapshot
) -> bool:
    """Exact re-derivation of every binding; a mismatch means the checkpoint
    does not freeze this snapshot and recovery must fail closed."""
    return (
        checkpoint.prompt_digest == snapshot.prompt_digest()
        and checkpoint.generation_settings_digest == snapshot.generation_settings_digest()
        and checkpoint.committed_prefix_digest == snapshot.committed_prefix_digest()
        and checkpoint.committed_token_count == len(snapshot.committed_token_ids)
        and checkpoint.watermark == len(snapshot.committed_token_ids)
        and checkpoint.committed_prefix_tokens == snapshot.committed_token_ids
        and checkpoint.delivery_cursor == snapshot.delivery_cursor
        and checkpoint.publisher_generation == snapshot.publisher_generation
        and checkpoint.encoded_prompt == snapshot.encoded_prompt_token_ids
        and checkpoint.request_id == snapshot.request_id
        and checkpoint.session_id == snapshot.session_id
        and checkpoint.source_attempt == snapshot.source_attempt
        and checkpoint.path_id == snapshot.path_id
        and checkpoint.path_digest == snapshot.path_digest
        and checkpoint.path_attempt == snapshot.path_attempt
        and checkpoint.deployment_id == snapshot.deployment_id
        and checkpoint.deployment_epoch == snapshot.deployment_epoch
        and checkpoint.topology_generation == snapshot.topology_generation
        and checkpoint.source_qualification_digest
        == snapshot.source_qualification_digest
        and checkpoint.model_id == snapshot.model_id
        and checkpoint.representation_digest == snapshot.representation_digest
        and checkpoint.tokenizer_digest == snapshot.tokenizer_digest
        and checkpoint.recovery_attempt == snapshot.recovery_attempt
    )
