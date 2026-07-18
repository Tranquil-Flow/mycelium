"""After-every-action production/reference comparison and stable counterexamples."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import json
from typing import Sequence

from .model import AdapterAction, IrohAdapterModel
from .production import ProductionAdapterDriver, ProductionSnapshot, project_model
from .trace import minimize_trace, trace_to_json


@dataclass(frozen=True)
class FieldDifference:
    field: str
    expected: object
    observed: object


@dataclass(frozen=True)
class TraceDifference:
    action_index: int
    action: AdapterAction
    differences: tuple[FieldDifference, ...]
    expected: ProductionSnapshot
    observed: ProductionSnapshot

    @property
    def signature(self) -> tuple[str, ...]:
        return tuple(item.field for item in self.differences)


def _differences(
    expected: ProductionSnapshot,
    observed: ProductionSnapshot,
) -> tuple[FieldDifference, ...]:
    return tuple(
        FieldDifference(field.name, getattr(expected, field.name), getattr(observed, field.name))
        for field in fields(ProductionSnapshot)
        if getattr(expected, field.name) != getattr(observed, field.name)
    )


def check_trace(
    trace: Sequence[AdapterAction],
    *,
    queue_capacity: int = 2,
    initial_generation: int = 7,
) -> TraceDifference | None:
    """Run one trace against fresh fixtures and stop at its first discrepancy."""

    machine = IrohAdapterModel(
        queue_capacity=queue_capacity,
        initial_generation=initial_generation,
    )
    state = machine.initial_state()
    driver = ProductionAdapterDriver(
        queue_capacity=queue_capacity,
        initial_generation=initial_generation,
    )
    try:
        initial_expected = project_model(state)
        initial_observed = driver.snapshot()
        initial_difference = _differences(initial_expected, initial_observed)
        if initial_difference:
            return TraceDifference(
                action_index=-1,
                action=AdapterAction("initial_state"),
                differences=initial_difference,
                expected=initial_expected,
                observed=initial_observed,
            )
        for index, action in enumerate(trace):
            state = machine.apply(state, action).state
            driver.apply(action)
            expected = project_model(state)
            observed = driver.snapshot()
            difference = _differences(expected, observed)
            if difference:
                return TraceDifference(index, action, difference, expected, observed)
        return None
    finally:
        driver.close()


def minimize_discrepancy(
    trace: Sequence[AdapterAction],
    difference: TraceDifference,
    *,
    queue_capacity: int = 2,
    initial_generation: int = 7,
) -> tuple[AdapterAction, ...]:
    """Deletion-minimize while preserving the original differing fields."""

    target = difference.differences

    def reproduces(candidate: tuple[AdapterAction, ...]) -> bool:
        candidate_difference = check_trace(
            candidate,
            queue_capacity=queue_capacity,
            initial_generation=initial_generation,
        )
        return (
            candidate_difference is not None
            and candidate_difference.differences == target
        )

    return minimize_trace(tuple(trace), reproduces)


def counterexample_json(
    trace: Sequence[AdapterAction],
    difference: TraceDifference,
) -> str:
    """Serialize a discrepancy with stable ordering and no runtime identifiers."""

    payload = {
        "action": json.loads(trace_to_json((difference.action,)))[0],
        "action_index": difference.action_index,
        "differences": [asdict(item) for item in difference.differences],
        "trace": json.loads(trace_to_json(trace)),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


__all__ = [
    "FieldDifference",
    "TraceDifference",
    "check_trace",
    "counterexample_json",
    "minimize_discrepancy",
]
