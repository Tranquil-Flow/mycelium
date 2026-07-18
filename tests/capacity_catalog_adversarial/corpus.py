"""Deterministic seeded operation corpus for the catalog reference model."""
from __future__ import annotations

import json
import math
import random
from collections.abc import Iterable

from tests.capacity_catalog_adversarial.model import AdversarialValue, Operation


TRACE_SEEDS = (
    0,
    1,
    2,
    7,
    11,
    19,
    23,
    29,
    31,
    37,
    42,
    101,
    31337,
    0x5EED,
    0xA57A,
    0xC0FFEE,
)
MIN_TRACE_LENGTH = 20
MAX_TRACE_LENGTH = 20
_PROFILE_IDS = (
    "base",
    "revision-1",
    "revision-2",
    "same-source-claims",
    "slot-model",
    "slot-quantization",
    "slot-backend",
    "slot-runtime",
    "slot-hardware",
    "slot-power",
    "slot-context",
    "slot-kv",
)
_SLOT_IDS = (
    "base",
    "slot-model",
    "slot-quantization",
    "slot-backend",
    "slot-runtime",
    "slot-hardware",
    "slot-power",
    "slot-context",
    "slot-kv",
)


def _prefix(index: int) -> tuple[Operation, ...]:
    variants = (
        (
            Operation.insert("base", now=0.0, ttl=3.0),
            Operation.insert("base", now=1.0, ttl=10.0),
            Operation.resolve("base", now=2.0),
            Operation.insert(
                "revision-1",
                now=3.0,
                ttl=3.0,
                allow_replacement=True,
                expected_current_profile_id="base",
            ),
            Operation.resolve("base", now=3.0, lookup_profile_id="base"),
            Operation.resolve("base", now=6.0),
            Operation.insert(
                "revision-2",
                now=6.0,
                ttl=4.0,
                allow_replacement=True,
                expected_current_profile_id="revision-1",
            ),
            Operation.resolve("base", now=6.0),
        ),
        (
            Operation.insert("base", now=0.0, ttl=1.0),
            Operation.insert("slot-model", now=1.0, ttl=1.0),
            Operation.insert("slot-quantization", now=2.0, ttl=1.0),
            Operation.insert("slot-backend", now=3.0, ttl=1.0),
            Operation.insert("slot-runtime", now=4.0, ttl=1.0),
            Operation.insert("base", now=5.0, ttl=10.0),
            Operation.insert("slot-hardware", now=6.0, ttl=1.0),
            Operation.resolve("slot-runtime", now=6.0),
        ),
        (
            Operation.insert("base", now=0.0, ttl=10.0),
            Operation.insert(
                "revision-1",
                now=1.0,
                ttl=10.0,
                expected_current_profile_id="base",
            ),
            Operation.insert(
                "revision-1",
                now=2.0,
                ttl=10.0,
                allow_replacement=True,
                expected_current_profile_id="slot-model",
            ),
            Operation.insert(
                "same-source-claims",
                now=3.0,
                ttl=10.0,
                allow_replacement=True,
                expected_current_profile_id="base",
            ),
            Operation.insert(
                "revision-1",
                now=4.0,
                ttl=10.0,
                allow_replacement=True,
                expected_current_profile_id="base",
            ),
            Operation.resolve("base", now=4.0),
            Operation.resolve("base", now=4.0, lookup_profile_id="base"),
            Operation.resolve("slot-model", now=4.0),
        ),
        (
            Operation.insert("base", now=2.0, ttl=8.0),
            Operation.resolve("base", now=AdversarialValue.TRUE),
            Operation.resolve("base", now=AdversarialValue.NAN),
            Operation.insert("base", now=3.0, ttl=AdversarialValue.TRUE),
            Operation.resolve("base", now=2.5),
            Operation.resolve("base", now=AdversarialValue.NEGATIVE_INFINITY),
            Operation.resolve("base", now=AdversarialValue.POSITIVE_INFINITY),
            Operation.resolve("base", now=AdversarialValue.OVERSIZED_INTEGER),
        ),
    )
    return variants[index % len(variants)]


def _maximum_finite_time(trace: Iterable[Operation]) -> float:
    values = (
        float(operation.now)
        for operation in trace
        if isinstance(operation.now, (int, float))
        and not isinstance(operation.now, bool)
        and math.isfinite(float(operation.now))
    )
    return max(values, default=0.0)


def _tail(seed: int, prefix: tuple[Operation, ...]) -> tuple[Operation, ...]:
    rng = random.Random(seed)
    clock = _maximum_finite_time(prefix)
    operations: list[Operation] = []
    invalid_times = (
        AdversarialValue.TRUE,
        AdversarialValue.NAN,
        AdversarialValue.POSITIVE_INFINITY,
        AdversarialValue.NEGATIVE_INFINITY,
        AdversarialValue.OVERSIZED_INTEGER,
    )
    invalid_ttls = (
        AdversarialValue.TRUE,
        AdversarialValue.NAN,
        AdversarialValue.POSITIVE_INFINITY,
    )
    while len(prefix) + len(operations) < MAX_TRACE_LENGTH:
        choice = rng.randrange(8)
        if choice in {0, 1, 2, 3, 4, 5}:
            clock += rng.choice((0.0, 0.5, 1.0))
        if choice == 0:
            operations.append(Operation.resolve(rng.choice(_SLOT_IDS), now=clock))
        elif choice == 1:
            operations.append(
                Operation.resolve(
                    rng.choice(_SLOT_IDS),
                    now=clock,
                    lookup_profile_id=rng.choice(_PROFILE_IDS),
                )
            )
        elif choice == 2:
            operations.append(
                Operation.insert(
                    rng.choice(_PROFILE_IDS),
                    now=clock,
                    ttl=rng.choice((1.0, 2.0, 10.0)),
                )
            )
        elif choice == 3:
            operations.append(
                Operation.insert(
                    rng.choice(_PROFILE_IDS),
                    now=clock,
                    ttl=rng.choice((1.0, 2.0, 10.0)),
                    allow_replacement=True,
                    expected_current_profile_id=rng.choice(_PROFILE_IDS),
                )
            )
        elif choice == 4:
            operations.append(
                Operation.insert(
                    rng.choice(_PROFILE_IDS),
                    now=clock,
                    ttl=rng.choice(invalid_ttls),
                )
            )
        elif choice == 5:
            operations.append(
                Operation.insert(
                    "same-source-claims",
                    now=clock,
                    ttl=1.0,
                    allow_replacement=True,
                    expected_current_profile_id="base",
                )
            )
        elif choice == 6:
            operations.append(
                Operation.resolve(
                    rng.choice(_SLOT_IDS),
                    now=max(0.0, clock - 0.25),
                )
            )
        else:
            operations.append(
                Operation.resolve(
                    rng.choice(_SLOT_IDS),
                    now=rng.choice(invalid_times),
                )
            )
    return tuple(operations)


def generate_trace_corpus() -> tuple[tuple[Operation, ...], ...]:
    traces = []
    for index, seed in enumerate(TRACE_SEEDS):
        prefix = _prefix(index)
        traces.append(prefix + _tail(seed, prefix))
    return tuple(traces)


def _json_value(value: object) -> object:
    if isinstance(value, AdversarialValue):
        return {"adversarial": value.value}
    if isinstance(value, float):
        if math.isnan(value):
            return {"float": "nan"}
        if value == math.inf:
            return {"float": "positive-infinity"}
        if value == -math.inf:
            return {"float": "negative-infinity"}
    return value


def _operation_document(operation: Operation) -> dict[str, object]:
    document: dict[str, object] = {
        "kind": operation.kind,
        "profile_id": operation.profile_id,
        "now": _json_value(operation.now),
    }
    if operation.ttl is not None:
        document["ttl"] = _json_value(operation.ttl)
    if operation.allow_replacement is not False:
        document["allow_replacement"] = operation.allow_replacement
    if operation.expected_current_profile_id is not None:
        document["expected_current_profile_id"] = operation.expected_current_profile_id
    if operation.lookup_profile_id is not None:
        document["lookup_profile_id"] = operation.lookup_profile_id
    return document


def trace_to_json(trace: Iterable[Operation]) -> str:
    return json.dumps(
        [_operation_document(operation) for operation in trace],
        sort_keys=True,
        separators=(",", ":"),
    )
