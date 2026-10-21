# SPDX-License-Identifier: AGPL-3.0-only
"""Closed A6 full-context replay recovery contracts, checkpoint, and runtime.

A6 is an *authority consumer*, never an authority issuer.  This module owns:

* the five closed capability-named contracts (spec section 7),
* the owner-private replay checkpoint and its privacy-reduced projection (section 4),
* the Planner full-context recovery intent (section 5 hysteresis / circuit-break),
* the qualifier compatibility result (section 3),
* the Router/gateway recovery runtime state machine (sections 5 and 6).

Recovery code never mints request attempts, canonical path identity, command
IDs, cancellation generations, publisher generations, terminal revisions, or
cleanup receipts.  It requests a transition from an A4 owner, receives an
owner-issued grant, and quotes that grant back verbatim.  Anything absent,
stale, forged, or self-minted fails closed with a bounded reason and no
observable effect.

The module is deterministic and self-contained: it defines the interface an
A4 owner must satisfy (see :class:`OwnerAuthority`) and exercises every
transition against that interface.  Integration with the live gateway,
Router, command controller, and liveness authority is a separate
product-integration step; this module is the replay slice those owners feed.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

# --------------------------------------------------------------------------- #
# Protocol identifiers (spec section 7)
# --------------------------------------------------------------------------- #

CHECKPOINT_PROTOCOL = "mycelium.private_request_replay_checkpoint.v1"
INTENT_PROTOCOL = "mycelium.full_context_recovery_intent.v1"
RUNTIME_PROTOCOL = "mycelium.full_context_recovery_runtime.v1"
QUALIFICATION_PROTOCOL = "mycelium.full_context_recovery_qualification.v1"
GATE_PROTOCOL = "mycelium.full_context_recovery_gate.v1"

# --------------------------------------------------------------------------- #
# Recovery state machine (spec section 5)
# --------------------------------------------------------------------------- #

RECOVERY_CHAIN: tuple[str, ...] = (
    "active",
    "failure_observed",
    "checkpoint_frozen",
    "successor_pending",
    "recovery_prefill",
    "cutover_pending",
    "recovered_decode",
    "cleanup",
    "terminal",
)

ABORTABLE_STATES: frozenset[str] = frozenset(
    {
        "failure_observed",
        "checkpoint_frozen",
        "successor_pending",
        "recovery_prefill",
        "cutover_pending",
        "recovered_decode",
    }
)

ABORT_CHAIN: tuple[str, ...] = ("cleanup", "aborted")

RECOVERY_STATES: frozenset[str] = frozenset(RECOVERY_CHAIN) | {"aborted"}

# Bounded replay policy (spec section 5, frozen before physical measurement).
MAX_RECOVERY_ATTEMPTS = 2
SUCCESSOR_BREAKER_SECONDS = 30
SUCCESSOR_BREAKER_WINDOW_SECONDS = 60
SUCCESSOR_BREAKER_FAILURES = 3
CANDIDATE_HYSTERESIS_GENERATIONS = 3
CANDIDATE_HYSTERESIS_SECONDS = 10

# Reasons a cancelled request must never be relabelled as (spec section 5).
RECOVERY_FAILURE_STATUSES: frozenset[str] = frozenset(
    {"recovery_failed", "successor_failed", "deadline_exceeded", "aborted"}
)

# A4-owned deployment-fatal allowlist.  Recovery cannot widen an unknown
# exception into deployment-fatal state (spec section 6).
FATAL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "immutable_authority_contradiction",
        "reservation_ledger_corruption",
        "deployment_authority_conflict",
        "all_qualified_tracks_lost",
    }
)

SCOPED_LIVENESS_SUBJECTS: frozenset[str] = frozenset(
    {"request", "edge", "placement"}
)

# Private runtime content that must never enter a public projection (spec 4/7).
_PRIVATE_FIELDS: frozenset[str] = frozenset(
    {
        "prompt", "prompt_text", "prompt_bytes", "encoded_prompt", "prefix",
        "prefix_text", "prefix_tokens", "token", "tokens", "token_ids",
        "token_text", "output", "output_text", "response", "response_text",
        "logits", "tensor", "tensors", "kv", "kv_bytes", "activations",
        "credential", "secret", "private_key", "endpoint",
    }
)

# --------------------------------------------------------------------------- #
# Shared validation helpers
# --------------------------------------------------------------------------- #


def _digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _text(value: object, name: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded non-empty string")
    return value


def _sha(value: object, name: str) -> str:
    value = _text(value, name, maximum=71)
    if len(value) != 71 or not value.startswith("sha256:") or any(
        char not in "0123456789abcdef" for char in value[7:]
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 reference")
    return value


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _require_text(value: object, name: str, *, maximum: int = 256) -> None:
    _text(value, name, maximum=maximum)


def _require_sha(value: object, name: str) -> None:
    _sha(value, name)


def _require_integer(value: object, name: str, *, minimum: int = 0) -> None:
    _integer(value, name, minimum=minimum)


def _reject_private(value: object, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in _PRIVATE_FIELDS:
                raise ValueError(f"private content at {path}.{key}")
            _reject_private(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_private(child, f"{path}[{index}]")


def _json_copy(value: object) -> Any:
    _reject_private(value)
    try:
        return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("projection must be finite JSON") from exc


def is_valid_recovery_state(value: object) -> bool:
    return isinstance(value, str) and value in RECOVERY_STATES


# --------------------------------------------------------------------------- #
# Deployment binding (identity shared across intent / qualification / runtime)
# --------------------------------------------------------------------------- #

_BINDING_FIELDS: frozenset[str] = frozenset(
    {"deployment_id", "deployment_epoch", "topology_generation", "model_id",
     "model_revision", "representation_digest", "graph_digest"}
)
_BINDING_TEXT_FIELDS: frozenset[str] = frozenset(
    {"deployment_id", "model_id", "model_revision"}
)
_BINDING_DIGEST_FIELDS: frozenset[str] = frozenset(
    {"representation_digest", "graph_digest"}
)
_BINDING_INTEGER_FIELDS: frozenset[str] = frozenset(
    {"deployment_epoch", "topology_generation"}
)


def normalize_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _BINDING_FIELDS:
        raise ValueError("binding_shape_invalid")
    result: dict[str, Any] = {}
    for key in _BINDING_TEXT_FIELDS:
        result[key] = _text(value[key], key, maximum=128)
    for key in _BINDING_DIGEST_FIELDS:
        result[key] = _sha(value[key], key)
    for key in _BINDING_INTEGER_FIELDS:
        result[key] = _integer(value[key], key, minimum=1)
    return result


# --------------------------------------------------------------------------- #
# Compatibility binding (spec section 3)
# --------------------------------------------------------------------------- #

_COMPATIBILITY_FIELDS: frozenset[str] = frozenset(
    {
        "model_id",
        "model_revision",
        "representation_digest",
        "manifest_digest",
        "tokenizer_digest",
        "vocabulary_digest",
        "special_token_policy_digest",
        "architecture_digest",
        "context_policy_digest",
        "output_semantics_digest",
        "generation_settings_digest",
        "qualification_parity_policy_digest",
        "workload_envelope_digest",
    }
)
_COMPATIBILITY_TEXT_FIELDS: frozenset[str] = frozenset({"model_id", "model_revision"})


def normalize_compatibility(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _COMPATIBILITY_FIELDS:
        raise ValueError("compatibility_binding_shape_invalid")
    result: dict[str, Any] = {}
    for key in _COMPATIBILITY_TEXT_FIELDS:
        result[key] = _text(value[key], key, maximum=128)
    for key in _COMPATIBILITY_FIELDS - _COMPATIBILITY_TEXT_FIELDS:
        result[key] = _sha(value[key], key)
    return result


def compatibility_matches(
    source: Mapping[str, Any], successor: Mapping[str, Any]
) -> bool:
    """Exact equality of the frozen compatibility fields, not a guess."""
    return normalize_compatibility(source) == normalize_compatibility(successor)


# --------------------------------------------------------------------------- #
# Canonical identity (spec section 3, redesign packet section 3)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CanonicalIdentity:
    """The complete identity every recovery mutation and receipt must bind."""

    deployment_id: str
    deployment_epoch: int
    qualification_digest: str
    request_id: str
    request_attempt: int
    path_id: str
    path_digest: str
    path_attempt: int
    topology_generation: int
    command_id: str
    cancellation_generation: int
    publisher_generation: int
    recovery_transition_id: str
    recovery_revision: int

    def __post_init__(self) -> None:
        for name in (
            "deployment_id", "request_id", "path_id", "command_id",
            "recovery_transition_id",
        ):
            _require_text(getattr(self, name), name)
        _require_sha(self.qualification_digest, "qualification_digest")
        _require_sha(self.path_digest, "path_digest")
        for name, minimum in (
            ("deployment_epoch", 1),
            ("request_attempt", 1),
            ("path_attempt", 0),
            ("topology_generation", 1),
            ("cancellation_generation", 0),
            ("publisher_generation", 1),
            ("recovery_revision", 0),
        ):
            _require_integer(getattr(self, name), name, minimum=minimum)

    def to_dict(self) -> dict[str, Any]:
        return {
            "deployment_id": self.deployment_id,
            "deployment_epoch": self.deployment_epoch,
            "qualification_digest": self.qualification_digest,
            "request_id": self.request_id,
            "request_attempt": self.request_attempt,
            "path_id": self.path_id,
            "path_digest": self.path_digest,
            "path_attempt": self.path_attempt,
            "topology_generation": self.topology_generation,
            "command_id": self.command_id,
            "cancellation_generation": self.cancellation_generation,
            "publisher_generation": self.publisher_generation,
            "recovery_transition_id": self.recovery_transition_id,
            "recovery_revision": self.recovery_revision,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CanonicalIdentity:
        expected = {
            "deployment_id", "deployment_epoch", "qualification_digest",
            "request_id", "request_attempt", "path_id", "path_digest",
            "path_attempt", "topology_generation", "command_id",
            "cancellation_generation", "publisher_generation",
            "recovery_transition_id", "recovery_revision",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("canonical_identity_shape_invalid")
        return cls(**{key: value[key] for key in expected})

    def replace(self, **overrides: Any) -> CanonicalIdentity:
        return replace(self, **overrides)


# --------------------------------------------------------------------------- #
# Owner-issued grants (authority consumption)
# --------------------------------------------------------------------------- #


class Issuer(str, Enum):
    REQUEST_GATEWAY = "request_gateway"
    ROUTER = "router"
    M16_LEDGER = "m16_admission_ledger"
    COMMAND_CONTROLLER = "a4_command_controller"
    LIVENESS = "a4_liveness_authority"


class GrantKind(str, Enum):
    FENCE = "fence"
    CUTOVER = "cutover"
    CLEANUP = "cleanup"
    TERMINAL = "terminal"
    PUBLISHER_RESET = "publisher_reset"


@dataclass(frozen=True, slots=True)
class OwnerGrant:
    """An owner decision recovery may quote back; recovery never builds one."""

    issuer: Issuer
    kind: GrantKind
    identity: CanonicalIdentity
    expected_revision: int
    a4_atomic_commit: str | None
    token: str = "self_minted"


@dataclass(frozen=True, slots=True)
class Decision:
    """A bounded fail-closed outcome with its observable effect set."""

    accepted: bool
    reason: str
    effects: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return self.accepted


# --------------------------------------------------------------------------- #
# 1. Private request replay checkpoint (spec section 4)
# --------------------------------------------------------------------------- #

_CHECKPOINT_PUBLIC_FIELDS: frozenset[str] = frozenset(
    {
        "protocol", "store_identity", "request_id", "session_id",
        "source_attempt", "path_id", "path_digest", "path_attempt",
        "checkpoint_digest", "prompt_digest", "generation_settings_digest",
        "committed_token_count", "committed_prefix_digest", "delivery_cursor",
        "watermark", "publisher_generation", "deployment_id", "deployment_epoch", "model_id",
        "topology_generation", "source_qualification_digest",
        "representation_digest", "tokenizer_digest", "recovery_attempt",
        "terminal_authority", "created_at_unix_ms", "expiry_unix_ms",
        "reconciliation_state",
    }
)


def _prefix_digest(tokens: Sequence[int]) -> str:
    return _digest(list(tokens))


@dataclass(slots=True)
class ReplayCheckpoint:
    """Owner-private durable checkpoint.

    ``encoded_prompt`` and ``committed_prefix_tokens`` are exact private token
    ID sequences
    stored only in the owner-private store.  The public projection exposes
    digests and counts, never the raw bytes, and the checkpoint digest binds
    the full private document so tampering with the prompt or prefix is
    detected on reload.
    """

    protocol: str
    store_identity: str
    request_id: str
    session_id: str
    source_attempt: int
    path_id: str
    path_digest: str
    path_attempt: int
    prompt_digest: str
    generation_settings_digest: str
    committed_token_count: int
    committed_prefix_digest: str | None
    delivery_cursor: int
    watermark: int
    publisher_generation: int
    deployment_id: str
    deployment_epoch: int
    topology_generation: int
    source_qualification_digest: str
    model_id: str
    representation_digest: str
    tokenizer_digest: str
    recovery_attempt: int
    terminal_authority: str | None
    created_at_unix_ms: int
    expiry_unix_ms: int
    reconciliation_state: str
    encoded_prompt: tuple[int, ...] = ()
    committed_prefix_tokens: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        _text(self.protocol, "protocol", maximum=96)
        if self.protocol != CHECKPOINT_PROTOCOL:
            raise ValueError("checkpoint_protocol_mismatch")
        _text(self.store_identity, "store_identity", maximum=128)
        _text(self.request_id, "request_id", maximum=128)
        _text(self.session_id, "session_id", maximum=128)
        _text(self.path_id, "path_id", maximum=128)
        _text(self.deployment_id, "deployment_id", maximum=128)
        _text(self.model_id, "model_id", maximum=128)
        _sha(self.path_digest, "path_digest")
        _sha(self.prompt_digest, "prompt_digest")
        _sha(self.generation_settings_digest, "generation_settings_digest")
        _sha(self.representation_digest, "representation_digest")
        _sha(self.tokenizer_digest, "tokenizer_digest")
        _sha(self.source_qualification_digest, "source_qualification_digest")
        for name, minimum in (
            ("source_attempt", 1), ("path_attempt", 0), ("recovery_attempt", 0),
            ("deployment_epoch", 1),
            ("topology_generation", 1),
        ):
            _require_integer(getattr(self, name), name, minimum=minimum)
        for name in ("committed_token_count", "watermark"):
            _require_integer(getattr(self, name), name, minimum=0)
        # -1 means "nothing committed yet" (gateway acknowledged_through).
        _require_integer(self.delivery_cursor, "delivery_cursor", minimum=-1)
        _require_integer(self.publisher_generation, "publisher_generation", minimum=0)
        _require_integer(self.created_at_unix_ms, "created_at_unix_ms", minimum=1)
        _require_integer(self.expiry_unix_ms, "expiry_unix_ms", minimum=1)
        if self.expiry_unix_ms <= self.created_at_unix_ms:
            raise ValueError("checkpoint_already_expired")
        _text(self.reconciliation_state, "reconciliation_state", maximum=32)
        if self.reconciliation_state not in {"active", "frozen", "reconciled"}:
            raise ValueError("checkpoint_reconciliation_state_invalid")
        if self.terminal_authority is not None:
            _text(self.terminal_authority, "terminal_authority", maximum=64)
        self._verify_bindings()

    def _verify_bindings(self) -> None:
        if (
            not isinstance(self.encoded_prompt, tuple)
            or not self.encoded_prompt
            or not all(type(token_id) is int and token_id >= 0 for token_id in self.encoded_prompt)
        ):
            raise ValueError("checkpoint_encoded_prompt_invalid")
        if not isinstance(self.committed_prefix_tokens, tuple) or not all(
            type(token_id) is int and token_id >= 0
            for token_id in self.committed_prefix_tokens
        ):
            raise ValueError("checkpoint_prefix_tokens_invalid")
        # The prompt digest must bind the exact encoded prompt.
        if self.prompt_digest != _digest(self.encoded_prompt):
            raise ValueError("checkpoint_prompt_digest_mismatch")
        # The prefix digest must bind the exact committed prefix.
        if self.committed_prefix_digest is None:
            if self.committed_prefix_tokens or self.committed_token_count:
                raise ValueError("checkpoint_prefix_digest_missing")
        else:
            if self.committed_prefix_digest != _prefix_digest(self.committed_prefix_tokens):
                raise ValueError("checkpoint_prefix_digest_mismatch")
            if len(self.committed_prefix_tokens) != self.committed_token_count:
                raise ValueError("checkpoint_prefix_count_mismatch")
        if self.watermark != self.committed_token_count:
            raise ValueError("checkpoint_watermark_mismatch")

    def _canonical_document(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "store_identity": self.store_identity,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "source_attempt": self.source_attempt,
            "path_id": self.path_id,
            "path_digest": self.path_digest,
            "path_attempt": self.path_attempt,
            "prompt_digest": self.prompt_digest,
            "generation_settings_digest": self.generation_settings_digest,
            "committed_token_count": self.committed_token_count,
            "committed_prefix_digest": self.committed_prefix_digest,
            "delivery_cursor": self.delivery_cursor,
            "watermark": self.watermark,
            "publisher_generation": self.publisher_generation,
            "deployment_id": self.deployment_id,
            "deployment_epoch": self.deployment_epoch,
            "topology_generation": self.topology_generation,
            "source_qualification_digest": self.source_qualification_digest,
            "model_id": self.model_id,
            "representation_digest": self.representation_digest,
            "tokenizer_digest": self.tokenizer_digest,
            "recovery_attempt": self.recovery_attempt,
            "terminal_authority": self.terminal_authority,
            "created_at_unix_ms": self.created_at_unix_ms,
            "expiry_unix_ms": self.expiry_unix_ms,
            "reconciliation_state": self.reconciliation_state,
            "encoded_prompt": self.encoded_prompt,
            "committed_prefix_tokens": list(self.committed_prefix_tokens),
        }

    def checkpoint_digest(self) -> str:
        return _digest(self._canonical_document())

    def to_dict(self) -> dict[str, Any]:
        document = self._canonical_document()
        document.pop("encoded_prompt")
        document.pop("committed_prefix_tokens")
        document["checkpoint_digest"] = self.checkpoint_digest()
        return document

    def public_projection(self) -> dict[str, Any]:
        return validate_checkpoint_projection(self.to_dict())


def validate_checkpoint_projection(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a privacy-reduced checkpoint projection (no private material).

    The checkpoint digest is validated for form only: it binds the private
    prompt/prefix material that a projection intentionally omits, so it cannot
    be recomputed here.  The owner-private store re-checks the full binding.
    """
    try:
        _reject_private(document)
        if not isinstance(document, Mapping) or set(document) != _CHECKPOINT_PUBLIC_FIELDS:
            raise ValueError
        result = _json_copy(document)
        if result["protocol"] != CHECKPOINT_PROTOCOL:
            raise ValueError
        _text(result["store_identity"], "store_identity", maximum=128)
        _text(result["request_id"], "request_id", maximum=128)
        _text(result["session_id"], "session_id", maximum=128)
        _text(result["path_id"], "path_id", maximum=128)
        _text(result["deployment_id"], "deployment_id", maximum=128)
        _text(result["model_id"], "model_id", maximum=128)
        for key in ("path_digest", "prompt_digest", "generation_settings_digest",
                    "representation_digest", "tokenizer_digest", "checkpoint_digest",
                    "source_qualification_digest"):
            _sha(result[key], key)
        if result["committed_prefix_digest"] is not None:
            _sha(result["committed_prefix_digest"], "committed_prefix_digest")
        for key, minimum in (("source_attempt", 1), ("path_attempt", 0),
                             ("recovery_attempt", 0), ("deployment_epoch", 1),
                             ("topology_generation", 1),
                             ("committed_token_count", 0), ("watermark", 0),
                             ("publisher_generation", 0)):
            _integer(result[key], key, minimum=minimum)
        _integer(result["delivery_cursor"], "delivery_cursor", minimum=-1)
        _integer(result["created_at_unix_ms"], "created_at_unix_ms", minimum=1)
        _integer(result["expiry_unix_ms"], "expiry_unix_ms", minimum=1)
        if result["expiry_unix_ms"] <= result["created_at_unix_ms"]:
            raise ValueError
        if result["reconciliation_state"] not in {"active", "frozen", "reconciled"}:
            raise ValueError
        if result["terminal_authority"] is not None:
            _text(result["terminal_authority"], "terminal_authority", maximum=64)
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("checkpoint_projection_invalid") from exc


# --------------------------------------------------------------------------- #
# Durable checkpoint store (spec section 4: fsynced, atomically replaced)
# --------------------------------------------------------------------------- #


class CheckpointStore:
    """Owner-private bounded store.  Canonical, digest-bound, atomically
    replaced, fsynced, and tied to the durable store identity."""

    def __init__(self, root: str | os.PathLike[str], *, store_identity: str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._root.is_symlink() or not self._root.is_dir():
            raise ValueError("checkpoint_store_root_invalid")
        os.chmod(self._root, 0o700)
        self._identity = _text(store_identity, "store_identity", maximum=128)
        self._path = self._root / f"{_safe_identity(store_identity)}.checkpoint.json"

    @property
    def store_identity(self) -> str:
        return self._identity

    def write(self, checkpoint: ReplayCheckpoint) -> None:
        if checkpoint.store_identity != self._identity:
            raise ValueError("checkpoint_store_identity_mismatch")
        current = self.load()
        if current is not None and (
            checkpoint.watermark < current.watermark
            or checkpoint.delivery_cursor < current.delivery_cursor
            or checkpoint.committed_token_count < current.committed_token_count
            or checkpoint.publisher_generation < current.publisher_generation
        ):
            raise ValueError("checkpoint_watermark_rollback")
        payload = checkpoint._canonical_document()
        payload["checkpoint_digest"] = checkpoint.checkpoint_digest()
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        fd, tmp = tempfile.mkstemp(dir=str(self._root), prefix=".checkpoint-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        dir_fd = os.open(str(self._root), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def load(self) -> ReplayCheckpoint | None:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(self._path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError("checkpoint_store_corrupt") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16_777_216:
                raise ValueError("checkpoint_store_corrupt")
            raw = bytearray()
            while len(raw) <= 16_777_216:
                chunk = os.read(fd, min(65_536, 16_777_217 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > 16_777_216:
                raise ValueError("checkpoint_store_corrupt")
        except OSError as exc:
            raise ValueError("checkpoint_store_corrupt") from exc
        finally:
            os.close(fd)

        def closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("checkpoint_store_corrupt")
                result[key] = value
            return result

        try:
            document = json.loads(
                bytes(raw).decode("utf-8"),
                object_pairs_hook=closed_object,
            )
            expected = _CHECKPOINT_PUBLIC_FIELDS | {
                "encoded_prompt",
                "committed_prefix_tokens",
            }
            if not isinstance(document, dict) or set(document) != expected:
                raise ValueError("checkpoint_store_corrupt")
            encoded_prompt = document.pop("encoded_prompt")
            prefix_tokens = document.pop("committed_prefix_tokens")
            checkpoint_digest = document.pop("checkpoint_digest")
            if (
                not isinstance(encoded_prompt, list)
                or not encoded_prompt
                or len(encoded_prompt) > 1_048_576
                or not all(type(token_id) is int and token_id >= 0 for token_id in encoded_prompt)
                or not isinstance(prefix_tokens, list)
                or len(prefix_tokens) > 1_048_576
                or not all(type(token_id) is int and token_id >= 0 for token_id in prefix_tokens)
            ):
                raise ValueError("checkpoint_store_corrupt")
            checkpoint = ReplayCheckpoint(
                encoded_prompt=tuple(encoded_prompt),
                committed_prefix_tokens=tuple(prefix_tokens),
                **document,
            )
            if checkpoint_digest != checkpoint.checkpoint_digest():
                raise ValueError("checkpoint_store_digest_mismatch")
            return checkpoint
        except ValueError as exc:
            if str(exc) == "checkpoint_store_digest_mismatch":
                raise
            raise ValueError("checkpoint_store_corrupt") from exc
        except (KeyError, TypeError, UnicodeDecodeError) as exc:
            raise ValueError("checkpoint_store_corrupt") from exc

    @staticmethod
    def sweep_temporaries(root: str | os.PathLike[str]) -> int:
        """Remove ``.checkpoint-*.tmp`` files a crash left between ``mkstemp`` and ``os.replace``.

        They hold the serialized checkpoint, encoded prompt token ids included, and no reconciliation ever sees
        them.  Only names this store creates are touched, links are unlinked and never followed.  Call it before
        any writer exists (gateway start-up); returns how many were removed.
        """
        directory = Path(root)
        removed = 0
        try:
            candidates = sorted(directory.glob(".checkpoint-*.tmp"))
        except OSError:
            return 0
        for candidate in candidates:
            try:
                candidate.unlink()
                removed += 1
            except FileNotFoundError:
                continue
            except OSError:
                continue  # unremovable: start-up still proceeds; the reconciliation below fails closed on real records
        return removed

    @classmethod
    def reconcile_orphans(
        cls,
        root: str | os.PathLike[str],
        *,
        maximum_records: int = 1_024,
        maximum_record_bytes: int = 16_777_216,
    ) -> tuple[dict[str, str], ...]:
        """Fail closed over checkpoints whose runtime owner did not restart."""

        directory = Path(root)
        journal = directory / ".reconciliations.json"
        recorded: list[dict[str, str]] = []
        if journal.exists():
            try:
                recorded = json.loads(journal.read_text(encoding="utf-8"))
                if not isinstance(recorded, list) or any(
                    not isinstance(item, dict)
                    or set(item) != {"checkpoint_digest", "outcome", "reason_code"}
                    or not all(isinstance(value, str) for value in item.values())
                    for item in recorded
                ):
                    raise ValueError
                if len(recorded) > maximum_records:
                    raise ValueError("checkpoint_reconciliation_capacity_exceeded")
            except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
                raise ValueError("checkpoint_store_corrupt") from exc
        candidates = sorted(directory.glob("*.checkpoint.json"))
        if len(candidates) > maximum_records:
            raise ValueError("checkpoint_reconciliation_capacity_exceeded")
        validated: list[tuple[CheckpointStore, ReplayCheckpoint]] = []
        for candidate in candidates:
            if candidate.is_symlink() or candidate.stat().st_size > maximum_record_bytes:
                raise ValueError("checkpoint_store_corrupt")
            try:
                document = json.loads(candidate.read_text(encoding="utf-8"))
                identity = document["store_identity"]
            except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
                raise ValueError("checkpoint_store_corrupt") from exc
            store = cls(directory, store_identity=identity)
            if store._path != candidate:
                raise ValueError("checkpoint_store_identity_mismatch")
            checkpoint = store.load()
            if checkpoint is None:
                raise ValueError("checkpoint_store_corrupt")
            validated.append((store, checkpoint))

        reconciliations = [dict(item) for item in recorded]
        recorded_digests = {item["checkpoint_digest"] for item in reconciliations}
        for _, checkpoint in validated:
            digest = checkpoint.checkpoint_digest()
            if digest in recorded_digests:
                continue
            reconciliations.append(
                {
                    "checkpoint_digest": digest,
                    "outcome": "aborted",
                    "reason_code": "runtime_owner_not_restored",
                }
            )
            recorded_digests.add(digest)
        if len(reconciliations) > maximum_records:
            raise ValueError("checkpoint_reconciliation_capacity_exceeded")
        if reconciliations != recorded:
            serialized = json.dumps(
                reconciliations,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            fd, temporary = tempfile.mkstemp(
                dir=str(directory), prefix=".reconciliations-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(serialized)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, journal)
                os.chmod(journal, 0o600)
                dir_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        for store, _ in validated:
            store.discard()
        return tuple(reconciliations)

    def discard(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            return
        dir_fd = os.open(str(self._root), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _safe_identity(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# 2. Full-context recovery intent (Planner-owned, spec section 5)
# --------------------------------------------------------------------------- #

_INTENT_FIELDS: frozenset[str] = frozenset(
    {
        "protocol", "generated_at_unix_ms", "binding", "retained_path_ids",
        "candidate_path_ids", "hysteresis", "rejection_reasons", "breaker",
        "route_ready", "intent_digest",
    }
)
_INTENT_HYSTERESIS_FIELDS: frozenset[str] = frozenset(
    {"equivalent_candidate_generations", "candidate_first_seen_unix_ms", "state"}
)
_INTENT_BREAKER_FIELDS: frozenset[str] = frozenset({"state"})


def build_recovery_intent(
    binding: Mapping[str, Any],
    *,
    retained_path_ids: Sequence[str],
    candidate_path_ids: Sequence[str],
    equivalent_candidate_generations: int,
    candidate_first_seen_unix_ms: int,
    generated_at_unix_ms: int,
    rejection_reasons: Sequence[str] = (),
    breaker_open: bool = False,
) -> dict[str, Any]:
    bound = normalize_binding(binding)
    now = _integer(generated_at_unix_ms, "generated_at_unix_ms", minimum=1)
    first_seen = _integer(candidate_first_seen_unix_ms, "candidate_first_seen_unix_ms", minimum=1)
    generations = _integer(equivalent_candidate_generations, "equivalent_candidate_generations", minimum=1)
    retained = sorted({_text(item, "retained_path_id", maximum=128) for item in retained_path_ids})
    candidates = sorted({_text(item, "candidate_path_id", maximum=128) for item in candidate_path_ids})
    reasons = sorted({_text(item, "rejection_reason", maximum=128) for item in rejection_reasons})
    stable = (
        generations >= CANDIDATE_HYSTERESIS_GENERATIONS
        and now - first_seen >= CANDIDATE_HYSTERESIS_SECONDS * 1000
    )
    emergency = not retained
    body: dict[str, Any] = {
        "protocol": INTENT_PROTOCOL,
        "generated_at_unix_ms": now,
        "binding": bound,
        "retained_path_ids": retained,
        "candidate_path_ids": candidates,
        "hysteresis": {
            "equivalent_candidate_generations": generations,
            "candidate_first_seen_unix_ms": first_seen,
            "state": "emergency_candidate" if emergency else ("stable_candidate" if stable else "hysteresis_pending"),
        },
        "rejection_reasons": reasons,
        "breaker": {"state": "open" if breaker_open else "closed"},
        "route_ready": False,
    }
    body["intent_digest"] = _digest(body)
    return validate_recovery_intent(body)


def validate_recovery_intent(document: Mapping[str, Any]) -> dict[str, Any]:
    try:
        if not isinstance(document, Mapping) or set(document) != _INTENT_FIELDS:
            raise ValueError
        result = _json_copy(document)
        if result["protocol"] != INTENT_PROTOCOL:
            raise ValueError
        if result["route_ready"] is not False:
            raise ValueError  # the Planner never marks a successor ready
        _integer(result["generated_at_unix_ms"], "generated_at_unix_ms", minimum=1)
        normalize_binding(result["binding"])
        for key in ("retained_path_ids", "candidate_path_ids", "rejection_reasons"):
            values = result[key]
            if (
                not isinstance(values, list)
                or len(values) > 64
                or len(set(values)) != len(values)
            ):
                raise ValueError
            for item in values:
                _text(item, key, maximum=128)
        hysteresis = result["hysteresis"]
        if not isinstance(hysteresis, Mapping) or set(hysteresis) != _INTENT_HYSTERESIS_FIELDS:
            raise ValueError
        if hysteresis["state"] not in {"hysteresis_pending", "stable_candidate", "emergency_candidate"}:
            raise ValueError
        breaker = result["breaker"]
        if not isinstance(breaker, Mapping) or set(breaker) != _INTENT_BREAKER_FIELDS:
            raise ValueError
        if breaker["state"] not in {"closed", "open"}:
            raise ValueError
        body = dict(result)
        actual = body.pop("intent_digest")
        if _sha(actual, "intent_digest") != actual or _digest(body) != actual:
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("recovery_intent_invalid") from exc


# --------------------------------------------------------------------------- #
# 3. Full-context recovery qualification (qualifier-owned, spec section 3)
# --------------------------------------------------------------------------- #

_QUALIFICATION_FIELDS: frozenset[str] = frozenset(
    {
        "protocol", "qualification_id", "binding", "request_binding",
        "successor_binding", "compatibility",
        "compatible", "freshness", "parity", "resource", "route",
        "fallback", "physical_challenge", "issued_at_unix_ms",
        "expiry_unix_ms", "reason_codes", "qualification_digest",
    }
)

_QUALIFICATION_REQUEST_BINDING_FIELDS: frozenset[str] = frozenset(
    {
        "request_id",
        "source_attempt",
        "committed_watermark",
        "recovery_attempt",
        "owner_qualification_digest",
        "checkpoint_digest",
    }
)

_QUALIFICATION_SUCCESSOR_BINDING_FIELDS: frozenset[str] = frozenset(
    {
        "request_id",
        "request_attempt",
        "path_id",
        "path_attempt",
        "path_manifest_digest",
        "placement_ids",
    }
)


def normalize_recovery_request_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _QUALIFICATION_REQUEST_BINDING_FIELDS
    ):
        raise ValueError("recovery_request_binding_shape_invalid")
    return {
        "request_id": _text(value["request_id"], "request_id", maximum=128),
        "source_attempt": _integer(value["source_attempt"], "source_attempt", minimum=1),
        "committed_watermark": _integer(
            value["committed_watermark"],
            "committed_watermark",
        ),
        "recovery_attempt": _integer(
            value["recovery_attempt"],
            "recovery_attempt",
        ),
        "owner_qualification_digest": _sha(
            value["owner_qualification_digest"],
            "owner_qualification_digest",
        ),
        "checkpoint_digest": _sha(value["checkpoint_digest"], "checkpoint_digest"),
    }


def recovery_request_binding(checkpoint: ReplayCheckpoint) -> dict[str, Any]:
    if not isinstance(checkpoint, ReplayCheckpoint):
        raise TypeError("recovery_checkpoint_invalid")
    checkpoint._verify_bindings()
    return {
        "request_id": checkpoint.request_id,
        "source_attempt": checkpoint.source_attempt,
        "committed_watermark": checkpoint.watermark,
        "recovery_attempt": checkpoint.recovery_attempt,
        "owner_qualification_digest": checkpoint.source_qualification_digest,
        "checkpoint_digest": checkpoint.checkpoint_digest(),
    }


def normalize_recovery_successor_binding(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _QUALIFICATION_SUCCESSOR_BINDING_FIELDS
    ):
        raise ValueError("recovery_successor_binding_shape_invalid")
    placements = value["placement_ids"]
    if (
        not isinstance(placements, (list, tuple))
        or not 1 <= len(placements) <= 64
    ):
        raise ValueError("recovery_successor_placements_invalid")
    placement_ids = [
        _text(item, "placement_id", maximum=128) for item in placements
    ]
    if len(set(placement_ids)) != len(placement_ids):
        raise ValueError("recovery_successor_placements_invalid")
    return {
        "request_id": _text(value["request_id"], "request_id", maximum=128),
        "request_attempt": _integer(
            value["request_attempt"], "request_attempt", minimum=1
        ),
        "path_id": _text(value["path_id"], "path_id", maximum=128),
        "path_attempt": _integer(value["path_attempt"], "path_attempt"),
        "path_manifest_digest": _sha(
            value["path_manifest_digest"], "path_manifest_digest"
        ),
        "placement_ids": placement_ids,
    }


def build_recovery_qualification(
    binding: Mapping[str, Any],
    *,
    qualification_id: str,
    request_binding: Mapping[str, Any],
    successor_binding: Mapping[str, Any],
    compatibility: Mapping[str, Any],
    compatible: bool,
    issued_at_unix_ms: int,
    expiry_unix_ms: int,
    reason_codes: Sequence[str] = (),
    physical_challenge: str = "unexecuted",
) -> dict[str, Any]:
    now = _integer(issued_at_unix_ms, "issued_at_unix_ms", minimum=1)
    expiry = _integer(expiry_unix_ms, "expiry_unix_ms", minimum=1)
    if expiry <= now:
        raise ValueError("qualification_already_expired")
    challenge = _text(
        physical_challenge,
        "physical_challenge",
        maximum=32,
    )
    compatible_value = bool(compatible)
    qualified = compatible_value and challenge == "passed"
    body: dict[str, Any] = {
        "protocol": QUALIFICATION_PROTOCOL,
        "qualification_id": _text(qualification_id, "qualification_id", maximum=128),
        "binding": normalize_binding(binding),
        "request_binding": normalize_recovery_request_binding(request_binding),
        "successor_binding": normalize_recovery_successor_binding(successor_binding),
        "compatibility": normalize_compatibility(compatibility),
        "compatible": compatible_value,
        "freshness": "current" if qualified else "unverified",
        "parity": "exact" if qualified else "unverified",
        "resource": "reserved" if qualified else "unverified",
        "route": "current" if qualified else "unverified",
        "fallback": "none" if qualified else "unverified",
        "physical_challenge": challenge,
        "issued_at_unix_ms": now,
        "expiry_unix_ms": expiry,
        "reason_codes": sorted({_text(item, "reason_code", maximum=64) for item in reason_codes}),
    }
    body["qualification_digest"] = _digest(body)
    return validate_recovery_qualification(body)


def validate_recovery_qualification(document: Mapping[str, Any]) -> dict[str, Any]:
    try:
        if not isinstance(document, Mapping) or set(document) != _QUALIFICATION_FIELDS:
            raise ValueError
        result = _json_copy(document)
        if result["protocol"] != QUALIFICATION_PROTOCOL:
            raise ValueError
        _text(result["qualification_id"], "qualification_id", maximum=128)
        normalize_binding(result["binding"])
        normalize_recovery_request_binding(result["request_binding"])
        normalize_recovery_successor_binding(result["successor_binding"])
        normalize_compatibility(result["compatibility"])
        if not isinstance(result["compatible"], bool):
            raise ValueError
        for key in ("freshness", "parity", "resource", "route", "fallback", "physical_challenge"):
            _text(result[key], key, maximum=32)
        if result["physical_challenge"] not in {"unexecuted", "passed", "failed"}:
            raise ValueError
        claim_fields = {
            "freshness": {"current", "unverified"},
            "parity": {"exact", "unverified"},
            "resource": {"reserved", "unverified"},
            "route": {"current", "unverified"},
            "fallback": {"none", "unverified"},
        }
        if any(result[key] not in allowed for key, allowed in claim_fields.items()):
            raise ValueError
        positive = {
            "freshness": "current",
            "parity": "exact",
            "resource": "reserved",
            "route": "current",
            "fallback": "none",
        }
        if result["physical_challenge"] != "passed":
            if any(result[key] != "unverified" for key in positive):
                raise ValueError
        elif result["compatible"] and any(
            result[key] != value for key, value in positive.items()
        ):
            raise ValueError
        _integer(result["issued_at_unix_ms"], "issued_at_unix_ms", minimum=1)
        _integer(result["expiry_unix_ms"], "expiry_unix_ms", minimum=1)
        if result["expiry_unix_ms"] <= result["issued_at_unix_ms"]:
            raise ValueError
        if not isinstance(result["reason_codes"], list) or any(
            not isinstance(item, str) for item in result["reason_codes"]
        ):
            raise ValueError
        body = dict(result)
        actual = body.pop("qualification_digest")
        if _sha(actual, "qualification_digest") != actual or _digest(body) != actual:
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("recovery_qualification_invalid") from exc


# --------------------------------------------------------------------------- #
# 4. Recovery runtime (Router/gateway-owned, spec sections 5 and 6)
# --------------------------------------------------------------------------- #


class RecoveryPhase(str, Enum):
    ACTIVE = "active"
    FAILURE_OBSERVED = "failure_observed"
    CHECKPOINT_FROZEN = "checkpoint_frozen"
    SUCCESSOR_PENDING = "successor_pending"
    RECOVERY_PREFILL = "recovery_prefill"
    CUTOVER_PENDING = "cutover_pending"
    RECOVERED_DECODE = "recovered_decode"
    CLEANUP = "cleanup"
    TERMINAL = "terminal"
    ABORTED = "aborted"


_RESUMABLE_PHASES: frozenset[RecoveryPhase] = frozenset(
    {
        RecoveryPhase.ACTIVE,
        RecoveryPhase.CHECKPOINT_FROZEN,
        RecoveryPhase.RECOVERY_PREFILL,
        RecoveryPhase.RECOVERED_DECODE,
    }
)


class OwnerAuthority:
    """The single source of every authority A6 consumes.  In the deterministic
    slice this is a faithful in-memory owner; the live gateway, Router, M16
    ledger, command controller, and liveness authority implement the same
    surface in the product-integration step."""

    def __init__(
        self,
        *,
        a4_atomic_commit: str | None = None,
        deployment_id: str = "deployment-a4",
        deployment_epoch: int = 1,
        qualification_digest: str = "sha256:" + "0" * 64,
    ) -> None:
        self._a4_atomic_commit: str | None = None
        if a4_atomic_commit is not None:
            self.resolve_a4_atomic_commit(a4_atomic_commit)
        self.deployment_id = _text(deployment_id, "deployment_id", maximum=128)
        self.deployment_epoch = _integer(deployment_epoch, "deployment_epoch", minimum=1)
        self.qualification_digest = _sha(qualification_digest, "qualification_digest")
        self.topology_generation = 1
        self.selected_deployment_changed = False
        self._paths: dict[str, tuple[str, int]] = {}
        self._tokens: dict[str, OwnerGrant] = {}
        self._current: dict[tuple[str, GrantKind], str] = {}
        self._issued_attempts: dict[str, set[int]] = {}
        self._revisions: dict[str, int] = {}
        self._sequence = 0
        self._model: RecoveryRuntime | None = None

    @property
    def a4_atomic_commit(self) -> str | None:
        return self._a4_atomic_commit

    def resolve_a4_atomic_commit(self, digest: str) -> str:
        if not isinstance(digest, str) or len(digest) != 40 or any(
            char not in "0123456789abcdef" for char in digest
        ):
            raise ValueError("invalid_a4_atomic_commit")
        self._a4_atomic_commit = digest
        return digest

    def attach(self, model: RecoveryRuntime) -> None:
        self._model = model

    def register_canonical_path(self, path_id: str, *, digest: str, attempt: int) -> None:
        self._paths[_text(path_id, "path_id", maximum=128)] = (
            _sha(digest, "path_digest"), _integer(attempt, "path_attempt")
        )

    def canonical_path(self, path_id: str) -> tuple[str, int] | None:
        return self._paths.get(path_id)

    def issued_attempts(self, request_id: str) -> frozenset[int]:
        return frozenset(self._issued_attempts.get(request_id, set()))

    def advance_topology_generation(self) -> int:
        self.topology_generation += 1
        return self.topology_generation

    def bump_revision(self, request_id: str) -> int:
        self._revisions[request_id] = self._revisions.get(request_id, 0) + 1
        return self._revisions[request_id]

    def admission_identity(self, request_id: str, *, path_id: str) -> CanonicalIdentity:
        digest, attempt = self._require_path(path_id)
        self._issued_attempts.setdefault(request_id, set()).add(1)
        return CanonicalIdentity(
            deployment_id=self.deployment_id,
            deployment_epoch=self.deployment_epoch,
            qualification_digest=self.qualification_digest,
            request_id=request_id,
            request_attempt=1,
            path_id=path_id,
            path_digest=digest,
            path_attempt=attempt,
            topology_generation=self.topology_generation,
            command_id=f"cmd-{request_id}-1",
            cancellation_generation=0,
            publisher_generation=1,
            recovery_transition_id=f"rt-{request_id}-0",
            recovery_revision=0,
        )

    def issue_fence(self, request_id: str, *, expected_revision: int) -> OwnerGrant:
        return self._issue(Issuer.ROUTER, GrantKind.FENCE, self._current_identity(request_id), expected_revision)

    def issue_cutover(self, request_id: str, *, successor_path_id: str, expected_revision: int) -> OwnerGrant:
        current = self._current_identity(request_id)
        digest, attempt = self._require_path(successor_path_id)
        successor_attempt = current.request_attempt + 1
        self._issued_attempts.setdefault(request_id, set()).add(successor_attempt)
        identity = current.replace(
            request_attempt=successor_attempt,
            path_id=successor_path_id,
            path_digest=digest,
            path_attempt=attempt,
            topology_generation=self.topology_generation,
            qualification_digest=self.qualification_digest,
            command_id=f"cmd-{request_id}-{successor_attempt}",
            recovery_transition_id=f"rt-{request_id}-{successor_attempt}",
            recovery_revision=current.recovery_revision + 1,
        )
        return self._issue(Issuer.ROUTER, GrantKind.CUTOVER, identity, expected_revision)

    def issue_cleanup(self, request_id: str, *, identity: CanonicalIdentity, expected_revision: int) -> OwnerGrant:
        return self._issue(Issuer.M16_LEDGER, GrantKind.CLEANUP, identity, expected_revision)

    def issue_terminal(self, request_id: str, *, identity: CanonicalIdentity, expected_revision: int) -> OwnerGrant:
        return self._issue(Issuer.COMMAND_CONTROLLER, GrantKind.TERMINAL, identity, expected_revision)

    def issue_publisher_reset(self, request_id: str, *, identity: CanonicalIdentity, expected_revision: int) -> OwnerGrant:
        return self._issue(Issuer.REQUEST_GATEWAY, GrantKind.PUBLISHER_RESET, identity, expected_revision)

    def is_issued(self, grant: OwnerGrant) -> bool:
        recorded = self._tokens.get(grant.token)
        return recorded is not None and recorded == grant

    def is_current(self, grant: OwnerGrant) -> bool:
        key = (grant.identity.request_id, grant.kind)
        return self._current.get(key) == grant.token

    def _issue(self, issuer: Issuer, kind: GrantKind, identity: CanonicalIdentity, expected_revision: int) -> OwnerGrant:
        self._sequence += 1
        token = f"owner-grant-{self._sequence}"
        grant = OwnerGrant(
            issuer=issuer, kind=kind, identity=identity,
            expected_revision=expected_revision, a4_atomic_commit=self._a4_atomic_commit,
            token=token,
        )
        self._tokens[token] = grant
        self._current[(identity.request_id, kind)] = token
        return grant

    def _require_path(self, path_id: str) -> tuple[str, int]:
        path = self._paths.get(path_id)
        if path is None:
            raise ValueError("unknown_canonical_path")
        return path

    def _current_identity(self, request_id: str) -> CanonicalIdentity:
        if self._model is None:
            raise ValueError("owner_not_attached")
        return self._model.current_identity(request_id)


@dataclass(slots=True)
class _RequestState:
    identity: CanonicalIdentity
    absolute_deadline_ms: int
    phase: RecoveryPhase = RecoveryPhase.ACTIVE
    revision: int = 0
    cleanup_revision: int = 0
    watermark: int = 0
    ledger: list[int] = field(default_factory=list)
    fenced: set[int] = field(default_factory=set)
    cleanup_subjects: set[str] = field(default_factory=set)
    cutover_grant: OwnerGrant | None = None
    terminal: str | None = None
    cancel_observed_at_ms: int | None = None
    recovery_policy_deadline_ms: int | None = None
    successor_model_work: int = 0
    replayed: tuple[int, ...] = ()
    observed_failures: list[str] = field(default_factory=list)
    reconciled: int = 0
    reconciliation_outcome: str | None = None
    orphan_checkpoint: bool = False
    browser_cursor: int | None = None
    recovery_attempt: int = 0


class RecoveryRuntime:
    """Fail-closed recovery state machine consuming owner-issued authority.

    ``active -> failure_observed -> checkpoint_frozen -> successor_pending ->
    recovery_prefill -> cutover_pending -> recovered_decode -> cleanup ->
    terminal``, or any recovery state into ``cleanup -> aborted``.
    """

    qualification_claim = False
    promotion_authorized = False

    def __init__(self, owner: OwnerAuthority) -> None:
        self._owner = owner
        owner.attach(self)
        self._requests: dict[str, _RequestState] = {}
        self._live_resources: dict[str, int] = {}
        self.shared_node_running = True
        self.response_reader_running = True
        self.deployment_fatal = False
        self._failed_waiters: list[str] = []
        self._untrusted_consulted: list[str] = []
        self._successor_failures: list[int] = []
        self._breaker_open_until_unix_ms = 0

    # -- construction and inspection ---------------------------------------

    @property
    def owner(self) -> OwnerAuthority:
        return self._owner

    def admit(self, request_id: str, *, identity: CanonicalIdentity, absolute_deadline_ms: int) -> None:
        request_id = _text(request_id, "request_id", maximum=128)
        if request_id in self._requests:
            raise ValueError("recovery_duplicate_request")
        self._requests[request_id] = _RequestState(
            identity=identity, absolute_deadline_ms=_integer(absolute_deadline_ms, "absolute_deadline_ms", minimum=1)
        )
        self._live_resources[self.subject(identity)] = 1

    def subject(self, identity: CanonicalIdentity) -> str:
        return f"{identity.request_id}:{identity.request_attempt}:{identity.path_id}"

    def current_identity(self, request_id: str) -> CanonicalIdentity:
        return self._state(request_id).identity

    def phase(self, request_id: str) -> RecoveryPhase:
        return self._state(request_id).phase

    def revision(self, request_id: str) -> int:
        return self._state(request_id).revision

    def cleanup_revision(self, request_id: str) -> int:
        return self._state(request_id).cleanup_revision

    def committed_watermark(self, request_id: str) -> int:
        return self._state(request_id).watermark

    def delivery_ledger(self, request_id: str) -> tuple[int, ...]:
        return tuple(self._state(request_id).ledger)

    def replayed_positions(self, request_id: str) -> tuple[int, ...]:
        return self._state(request_id).replayed

    def terminal(self, request_id: str) -> str | None:
        return self._state(request_id).terminal

    def successor_model_work(self, request_id: str) -> int:
        return self._state(request_id).successor_model_work

    def observed_failures(self, request_id: str) -> tuple[str, ...]:
        return tuple(self._state(request_id).observed_failures)

    def live_resources(self, subject: str) -> int:
        return self._live_resources.get(subject, 0)

    def node_global_live_resources(self) -> int:
        return sum(self._live_resources.values())

    def failed_waiters(self) -> tuple[str, ...]:
        return tuple(self._failed_waiters)

    def untrusted_inputs_consulted(self) -> tuple[str, ...]:
        return tuple(self._untrusted_consulted)

    # -- deterministic test scaffolding ------------------------------------

    def enter_phase(self, request_id: str, phase: RecoveryPhase) -> None:
        self._state(request_id).phase = phase

    def force_revision(self, request_id: str, revision: int) -> None:
        self._state(request_id).revision = revision

    def write_orphan_checkpoint(self, request_id: str) -> None:
        self._state(request_id).orphan_checkpoint = True

    def set_browser_authored_cursor(self, request_id: str, *, position: int) -> None:
        self._state(request_id).browser_cursor = position

    # -- gateway-owned delivery ledger (spec section 4) ---------------------

    def commit_position(self, request_id: str, *, position: int) -> Decision:
        state = self._state(request_id)
        rejection = self._position_rejection(state, position)
        if rejection is not None:
            return rejection
        state.ledger.append(position)
        state.watermark = position
        return Decision(True, "committed", frozenset({"ledger_append_and_watermark"}))

    def publish_position(self, request_id: str, *, position: int, identity: CanonicalIdentity | None = None) -> Decision:
        state = self._state(request_id)
        identity = state.identity if identity is None else identity
        if identity.request_attempt in state.fenced:
            return Decision(False, "fenced_attempt")
        if identity.publisher_generation != state.identity.publisher_generation:
            return Decision(False, "stale_publisher_generation")
        if identity.request_attempt != state.identity.request_attempt:
            return Decision(False, "stale_attempt")
        if state.terminal is not None:
            return Decision(False, "late_position")
        rejection = self._position_rejection(state, position)
        if rejection is not None:
            return rejection
        state.ledger.append(position)
        state.watermark = position
        return Decision(True, "published", frozenset({"ledger_append_and_watermark"}))

    def reconnect_replay(self, request_id: str, *, from_position: int) -> Decision:
        state = self._state(request_id)
        state.replayed = tuple(position for position in state.ledger if position > from_position)
        return Decision(True, "replayed", frozenset({"no_watermark_advance"}))

    def advance_publisher_generation(self, request_id: str) -> Decision:
        state = self._state(request_id)
        grant = self._owner.issue_publisher_reset(request_id, identity=state.identity, expected_revision=state.revision)
        state.identity = grant.identity.replace(publisher_generation=grant.identity.publisher_generation + 1)
        return Decision(True, "publisher_generation_advanced")

    # -- recovery progress (failure / checkpoint / successor / prefill) -----

    def observe_failure(self, request_id: str, *, identity: CanonicalIdentity, scope: str, authored_by: str, fatal_reason: str | None = None) -> Decision:
        state = self._state(request_id)
        if authored_by != Issuer.LIVENESS.value:
            return Decision(False, "a6_cannot_author_failure")
        if identity != state.identity:
            return Decision(False, "stale_failure_identity")
        if identity.request_attempt in state.fenced:
            return Decision(False, "fenced_attempt")
        if scope == "deployment":
            if fatal_reason not in FATAL_ALLOWLIST:
                return Decision(False, "deployment_fatal_requires_a4_allowlist")
            self.deployment_fatal = True
            state.observed_failures.append(scope)
            return Decision(True, "deployment_fatal_allowlisted")
        if scope not in SCOPED_LIVENESS_SUBJECTS:
            return Decision(False, "unknown_liveness_scope")
        state.observed_failures.append(scope)
        if state.phase == RecoveryPhase.ACTIVE:
            state.phase = RecoveryPhase.FAILURE_OBSERVED
        return Decision(True, "failure_observed", frozenset({"scoped"}))

    def freeze_checkpoint(self, request_id: str, *, committed_token_count: int, committed_prefix_digest: str) -> Decision:
        state = self._state(request_id)
        if state.phase not in {RecoveryPhase.FAILURE_OBSERVED, RecoveryPhase.CHECKPOINT_FROZEN}:
            return Decision(False, "checkpoint_not_after_failure")
        count = _integer(committed_token_count, "committed_token_count")
        _sha(committed_prefix_digest, "committed_prefix_digest")
        if count < state.watermark:
            return Decision(False, "watermark_rollback")
        if count > state.watermark:
            return Decision(False, "checkpoint_beyond_watermark")
        state.phase = RecoveryPhase.CHECKPOINT_FROZEN
        return Decision(True, "checkpoint_frozen", frozenset({"checkpoint_frozen"}))

    def select_successor(self, request_id: str, *, qualification: Mapping[str, Any]) -> Decision:
        state = self._state(request_id)
        if state.phase != RecoveryPhase.CHECKPOINT_FROZEN:
            return Decision(False, "successor_not_after_checkpoint")
        try:
            validated = validate_recovery_qualification(qualification)
        except ValueError:
            return Decision(False, "qualification_invalid")
        if validated["physical_challenge"] != "passed":
            return Decision(False, "recovery_physical_qualification_required")
        request_binding = validated["request_binding"]
        if (
            request_binding["request_id"] != request_id
            or request_binding["source_attempt"] != state.identity.request_attempt
            or request_binding["committed_watermark"] != state.watermark
            or request_binding["recovery_attempt"] != state.recovery_attempt
            or request_binding["owner_qualification_digest"]
            != state.identity.qualification_digest
        ):
            return Decision(False, "recovery_request_binding_mismatch")
        successor_binding = validated["successor_binding"]
        if (
            successor_binding["request_id"] != request_id
            or successor_binding["request_attempt"]
            != state.identity.request_attempt + 1
        ):
            return Decision(False, "recovery_successor_binding_mismatch")
        if not validated["compatible"]:
            self._untrusted_consulted.append("incompatible_successor")
            return Decision(False, "successor_incompatible")
        if self.breaker_open(observed_at_unix_ms=validated["issued_at_unix_ms"]):
            return Decision(False, "recovery_circuit_breaker_open")
        state.phase = RecoveryPhase.SUCCESSOR_PENDING
        return Decision(True, "successor_pending", frozenset({"successor_pending"}))

    def prefill(self, request_id: str, *, reconstructed_prefix_count: int) -> Decision:
        state = self._state(request_id)
        if state.phase != RecoveryPhase.SUCCESSOR_PENDING:
            return Decision(False, "prefill_not_after_successor")
        if _integer(reconstructed_prefix_count, "reconstructed_prefix_count") != state.watermark:
            return Decision(False, "reconstructed_prefix_mismatch")
        state.phase = RecoveryPhase.RECOVERY_PREFILL
        state.successor_model_work += 1
        return Decision(True, "prefilled", frozenset({"prefix_suppressed"}))

    # -- fencing and cutover ------------------------------------------------

    def fence_source_attempt(self, request_id: str, *, grant: OwnerGrant) -> Decision:
        state = self._state(request_id)
        rejection = self._grant_rejection(grant, GrantKind.FENCE)
        if rejection is not None:
            return rejection
        if grant.expected_revision != state.revision:
            return Decision(False, "expected_revision_mismatch")
        state.fenced.add(state.identity.request_attempt)
        state.revision += 1
        state.phase = RecoveryPhase.CUTOVER_PENDING
        return Decision(True, "source_attempt_fenced", frozenset({"fenced"}))

    def cutover(self, request_id: str, *, grant: OwnerGrant) -> Decision:
        state = self._state(request_id)
        if not self._owner.is_issued(grant):
            return Decision(False, "self_minted_authority")
        if state.cutover_grant == grant:
            return Decision(True, "duplicate_cutover")
        rejection = self._grant_rejection(grant, GrantKind.CUTOVER)
        if rejection is not None:
            return rejection
        if state.cancel_observed_at_ms is not None:
            return Decision(False, "cancellation_precedes_recovery")
        if grant.expected_revision != state.revision:
            if state.cutover_grant is not None:
                return Decision(False, "conflicting_cutover")
            return Decision(False, "expected_revision_mismatch")
        identity = grant.identity
        if self._owner.canonical_path(identity.path_id) != (identity.path_digest, identity.path_attempt):
            return Decision(False, "canonical_path_mismatch")
        if identity.request_attempt not in self._owner.issued_attempts(request_id):
            return Decision(False, "self_minted_authority")
        if identity.topology_generation != self._owner.topology_generation or identity.qualification_digest != self._owner.qualification_digest:
            return Decision(False, "stale_a4_generation")
        if identity.publisher_generation != state.identity.publisher_generation:
            return Decision(False, "stale_publisher_generation")
        if state.identity.request_attempt not in state.fenced:
            return Decision(False, "fence_required_before_cutover")
        if state.recovery_attempt >= MAX_RECOVERY_ATTEMPTS:
            return Decision(False, "recovery_circuit_breaker_open")
        state.identity = identity
        state.revision += 1
        state.cutover_grant = grant
        state.recovery_attempt += 1
        state.phase = RecoveryPhase.RECOVERED_DECODE
        state.successor_model_work += 1
        self._live_resources[self.subject(identity)] = 1
        return Decision(True, "cutover", frozenset({"authority_advanced"}))

    # -- cancellation and deadlines ----------------------------------------

    def observe_cancellation(self, request_id: str, *, at_ms: int) -> Decision:
        state = self._state(request_id)
        state.cancel_observed_at_ms = _integer(at_ms, "at_ms", minimum=1)
        return Decision(True, "cancellation_observed")

    def effective_cancellation_deadline_ms(self, request_id: str) -> int:
        return self._state(request_id).absolute_deadline_ms

    def set_recovery_policy_deadline(self, request_id: str, deadline_ms: int) -> Decision:
        state = self._state(request_id)
        if deadline_ms > state.absolute_deadline_ms:
            if state.cancel_observed_at_ms is not None:
                return Decision(False, "cancellation_budget_restart")
            return Decision(False, "recovery_policy_exceeds_absolute_deadline")
        state.recovery_policy_deadline_ms = _integer(deadline_ms, "deadline_ms", minimum=1)
        return Decision(True, "recovery_policy_set")

    # -- breaker ------------------------------------------------------------

    def record_successor_failure(self, *, observed_at_unix_ms: int) -> None:
        now = _integer(observed_at_unix_ms, "observed_at_unix_ms", minimum=1)
        self._successor_failures = [item for item in self._successor_failures if now - item <= SUCCESSOR_BREAKER_WINDOW_SECONDS * 1000]
        self._successor_failures.append(now)
        self._successor_failures = self._successor_failures[-SUCCESSOR_BREAKER_FAILURES:]
        if len(self._successor_failures) >= SUCCESSOR_BREAKER_FAILURES:
            self._breaker_open_until_unix_ms = max(self._breaker_open_until_unix_ms, now + SUCCESSOR_BREAKER_SECONDS * 1000)

    def breaker_open(self, *, observed_at_unix_ms: int) -> bool:
        return _integer(observed_at_unix_ms, "observed_at_unix_ms", minimum=1) < self._breaker_open_until_unix_ms

    def fail_successor(self, request_id: str, *, reason: str) -> Decision:
        state = self._state(request_id)
        state.phase = RecoveryPhase.CLEANUP
        state.observed_failures.append(_text(reason, "reason", maximum=64))
        return Decision(True, "successor_failed", frozenset({"request_scoped"}))

    # -- cleanup and terminal ordering --------------------------------------

    def record_cleanup(self, request_id: str, *, grant: OwnerGrant, subject: str, stale_source: bool = False) -> Decision:
        state = self._state(request_id)
        rejection = self._grant_rejection(grant, GrantKind.CLEANUP)
        if rejection is not None:
            return rejection
        if stale_source or grant.identity.request_attempt in state.fenced:
            return Decision(False, "fenced_attempt")
        if grant.expected_revision != state.cleanup_revision:
            return Decision(False, "cleanup_cas_mismatch")
        if subject != self.subject(grant.identity):
            return Decision(False, "cleanup_subject_mismatch")
        self._live_resources[subject] = 0
        state.cleanup_subjects.add(subject)
        state.cleanup_revision += 1
        return Decision(True, "cleanup_recorded", frozenset({"exact_subject_released"}))

    def publish_terminal(self, request_id: str, *, grant: OwnerGrant, status: str) -> Decision:
        state = self._state(request_id)
        rejection = self._grant_rejection(grant, GrantKind.TERMINAL)
        if rejection is not None:
            return rejection
        if grant.identity.request_attempt in state.fenced:
            return Decision(False, "fenced_attempt")
        if state.terminal is not None:
            if state.terminal == status:
                return Decision(True, "duplicate_terminal")
            return Decision(False, "already_terminal")
        if state.cancel_observed_at_ms is not None:
            if status in RECOVERY_FAILURE_STATUSES:
                return Decision(False, "cancellation_mislabelled_as_recovery_failure")
            if status not in {"cancelled", "cleanup_failed"}:
                return Decision(False, "cancelled_request_cannot_complete")
        if status != "cleanup_failed" and self.subject(grant.identity) not in state.cleanup_subjects:
            return Decision(False, "cleanup_required_before_terminal")
        state.terminal = status
        state.phase = RecoveryPhase.TERMINAL if status != "cleanup_failed" else RecoveryPhase.ABORTED
        return Decision(True, "terminal_published", frozenset({"one_terminal_result"}))

    def terminate_shared_node(self, request_id: str) -> Decision:
        del request_id
        return Decision(False, "shared_node_termination_is_not_cleanup")

    def late_result(self, request_id: str, *, identity: CanonicalIdentity) -> Decision:
        del request_id, identity
        return Decision(False, "late_result_discarded")

    # -- restart reconciliation --------------------------------------------

    def restart(self) -> dict[str, str]:
        outcomes: dict[str, str] = {}
        for request_id, state in self._requests.items():
            if state.reconciliation_outcome is not None:
                outcomes[request_id] = state.reconciliation_outcome
                continue
            if state.terminal is not None:
                outcome = "already_terminal"
            elif state.phase in _RESUMABLE_PHASES:
                outcome = "resumed"
            else:
                outcome = "aborted"
            state.reconciliation_outcome = outcome
            state.reconciled += 1
            state.identity = state.identity.replace(publisher_generation=state.identity.publisher_generation + 1)
            outcomes[request_id] = outcome
        return outcomes

    # -- claim boundary -----------------------------------------------------

    def authorize_integration(self) -> Decision:
        if self._owner.a4_atomic_commit is None:
            return Decision(False, "unresolved_a4_atomic_commit_binding")
        return Decision(True, "integration_authorization_available")

    def simulate_physical_gate(self, scenario_id: str) -> Decision:
        del scenario_id
        return Decision(False, "physical_gate_requires_executed_product_path")

    # -- runtime projection -------------------------------------------------

    def status(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "protocol": RUNTIME_PROTOCOL,
            "binding": {
                "deployment_id": self._owner.deployment_id,
                "deployment_epoch": self._owner.deployment_epoch,
                "qualification_digest": self._owner.qualification_digest,
                "a4_atomic_commit": self._owner.a4_atomic_commit,
            },
            "requests": [
                {
                    "request_id": request_id,
                    "attempt": state.identity.request_attempt,
                    "path_id": state.identity.path_id,
                    "path_attempt": state.identity.path_attempt,
                    "phase": state.phase.value,
                    "committed_token_count": state.watermark,
                    "recovery_attempt": state.recovery_attempt,
                    "terminal": state.terminal,
                    "cleanup_complete": bool(state.cleanup_subjects),
                    "reconciled": state.reconciliation_outcome,
                }
                for request_id, state in sorted(self._requests.items())
            ],
            "breaker": {
                "state": "open" if self._breaker_open_until_unix_ms else "closed",
                "failure_observations_unix_ms": list(self._successor_failures),
                "open_until_unix_ms": self._breaker_open_until_unix_ms,
            },
        }
        body["runtime_digest"] = _digest(body)
        return validate_recovery_runtime(body)

    # -- internals ----------------------------------------------------------

    def _state(self, request_id: str) -> _RequestState:
        state = self._requests.get(request_id)
        if state is None:
            raise ValueError("unknown_request")
        return state

    def _grant_rejection(self, grant: OwnerGrant, kind: GrantKind) -> Decision | None:
        if not self._owner.is_issued(grant):
            return Decision(False, "self_minted_authority")
        if not self._owner.is_current(grant):
            return Decision(False, "stale_owner_issued_transition")
        if grant.kind is not kind:
            return Decision(False, "invalid_grant_kind")
        return None

    @staticmethod
    def _position_rejection(state: _RequestState, position: int) -> Decision | None:
        if position in state.ledger:
            return Decision(False, "duplicate_position")
        if position <= state.watermark:
            return Decision(False, "regressed_position")
        if position > state.watermark + 1:
            return Decision(False, "skipped_position")
        return None


_RUNTIME_FIELDS: frozenset[str] = frozenset(
    {"protocol", "binding", "requests", "breaker", "runtime_digest"}
)
_RUNTIME_REQUEST_FIELDS: frozenset[str] = frozenset(
    {"request_id", "attempt", "path_id", "path_attempt", "phase",
     "committed_token_count", "recovery_attempt", "terminal",
     "cleanup_complete", "reconciled"}
)
_RUNTIME_BREAKER_FIELDS: frozenset[str] = frozenset(
    {"state", "failure_observations_unix_ms", "open_until_unix_ms"}
)


def validate_recovery_runtime(document: Mapping[str, Any]) -> dict[str, Any]:
    try:
        if not isinstance(document, Mapping) or set(document) != _RUNTIME_FIELDS:
            raise ValueError
        result = _json_copy(document)
        if result["protocol"] != RUNTIME_PROTOCOL:
            raise ValueError
        binding = result["binding"]
        if not isinstance(binding, Mapping) or set(binding) != {"deployment_id", "deployment_epoch", "qualification_digest", "a4_atomic_commit"}:
            raise ValueError
        _text(binding["deployment_id"], "deployment_id", maximum=128)
        _integer(binding["deployment_epoch"], "deployment_epoch", minimum=1)
        _sha(binding["qualification_digest"], "qualification_digest")
        if binding["a4_atomic_commit"] is not None:
            if not isinstance(binding["a4_atomic_commit"], str) or len(binding["a4_atomic_commit"]) != 40:
                raise ValueError
        requests = result["requests"]
        if not isinstance(requests, list) or len(requests) > 4096:
            raise ValueError
        seen: set[str] = set()
        for record in requests:
            if not isinstance(record, Mapping) or set(record) != _RUNTIME_REQUEST_FIELDS:
                raise ValueError
            if record["request_id"] in seen:
                raise ValueError
            seen.add(record["request_id"])
            _text(record["request_id"], "request_id", maximum=128)
            _text(record["path_id"], "path_id", maximum=128)
            _integer(record["attempt"], "attempt", minimum=1)
            _integer(record["path_attempt"], "path_attempt")
            if not is_valid_recovery_state(record["phase"]):
                raise ValueError
            _integer(record["committed_token_count"], "committed_token_count")
            _integer(record["recovery_attempt"], "recovery_attempt")
            if record["terminal"] is not None:
                _text(record["terminal"], "terminal", maximum=64)
            if not isinstance(record["cleanup_complete"], bool):
                raise ValueError
            if record["reconciled"] is not None and record["reconciled"] not in {"resumed", "aborted", "already_terminal"}:
                raise ValueError
        breaker = result["breaker"]
        if not isinstance(breaker, Mapping) or set(breaker) != _RUNTIME_BREAKER_FIELDS:
            raise ValueError
        if breaker["state"] not in {"closed", "open"}:
            raise ValueError
        body = dict(result)
        actual = body.pop("runtime_digest")
        if _sha(actual, "runtime_digest") != actual or _digest(body) != actual:
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("recovery_runtime_invalid") from exc


# --------------------------------------------------------------------------- #
# 5. Full-context recovery gate (spec section 7, owner-private executed evidence)
# --------------------------------------------------------------------------- #

_GATE_FIELDS: frozenset[str] = frozenset(
    {
        "protocol", "gate_id", "scenario_id", "gate_kind", "executed",
        "passed", "checks", "privacy_reduced_projection", "gate_digest",
    }
)


def build_recovery_gate(
    *,
    gate_id: str,
    scenario_id: str,
    gate_kind: str,
    executed: bool,
    passed: bool,
    checks: Mapping[str, Any],
) -> dict[str, Any]:
    if gate_kind not in {"deterministic_positive", "deterministic_negative", "physical_browser_positive", "physical_browser_negative"}:
        raise ValueError("gate_kind_invalid")
    if passed and not executed:
        raise ValueError("unexecuted_gate_cannot_pass")
    body: dict[str, Any] = {
        "protocol": GATE_PROTOCOL,
        "gate_id": _text(gate_id, "gate_id", maximum=128),
        "scenario_id": _text(scenario_id, "scenario_id", maximum=128),
        "gate_kind": gate_kind,
        "executed": bool(executed),
        "passed": bool(passed),
        "checks": _json_copy(checks),
        "privacy_reduced_projection": bool(passed) and not _contains_private(checks),
    }
    body["gate_digest"] = _digest(body)
    return validate_recovery_gate(body)


def _contains_private(value: object) -> bool:
    try:
        _reject_private(value)
        return False
    except ValueError:
        return True


def validate_recovery_gate(document: Mapping[str, Any]) -> dict[str, Any]:
    try:
        if not isinstance(document, Mapping) or set(document) != _GATE_FIELDS:
            raise ValueError
        result = _json_copy(document)
        if result["protocol"] != GATE_PROTOCOL:
            raise ValueError
        _text(result["gate_id"], "gate_id", maximum=128)
        _text(result["scenario_id"], "scenario_id", maximum=128)
        if result["gate_kind"] not in {"deterministic_positive", "deterministic_negative", "physical_browser_positive", "physical_browser_negative"}:
            raise ValueError
        if not isinstance(result["executed"], bool) or not isinstance(result["passed"], bool):
            raise ValueError
        if not isinstance(result["privacy_reduced_projection"], bool):
            raise ValueError
        if result["passed"] and not result["executed"]:
            raise ValueError  # an unexecuted gate can never pass
        body = dict(result)
        actual = body.pop("gate_digest")
        if _sha(actual, "gate_digest") != actual or _digest(body) != actual:
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("recovery_gate_invalid") from exc
