# SPDX-License-Identifier: AGPL-3.0-only
"""Derived sharing state: ``paused`` -> ``rejoining`` (connecting route) -> ``sharing``.

Computed in one place from facts the app already has (the A16 controller's mode/stop fence and the route readiness
of the owner run); it adds no wire field and no closed reason.  ``sharing`` is claimed only while the route is
really ready.  ``rejoining`` exists only after this Mac paused (the controller's ``rejoin_pending`` latch) and the
fence is released but the route is not ready; an ordinary warm-up or a failed run is never called rejoining.
"""
from __future__ import annotations

from typing import Any

LINES = {
    "paused": "Paused",
    "rejoining": "Rejoining - connecting route",
    "sharing": "Sharing",
    "idle": "Not sharing yet",
}
_PAUSED_MODES = frozenset({"drain", "withdraw"})


def _controller(app: Any) -> Any | None:
    runtime = getattr(app, "_adaptive_runtime", None)
    controller = getattr(runtime, "controller", None)
    return controller if controller is not None and getattr(controller, "enabled", False) else None


def route_ready(app: Any) -> bool | None:
    """Whether a serving route exists now; ``None`` when the run state is a failure (not a rejoin)."""
    run = getattr(app, "owner_run", None)
    if run is not None:
        try:
            snapshot = getattr(run, 'status_snapshot', None)
            status = snapshot() if snapshot is not None else run.status()
        except Exception:  # noqa: BLE001
            return False
        if not isinstance(status, dict):
            return False
        if status.get("state") == "failed":
            return None
        return status.get("state") == "serving" and status.get("route_ready", True) is True
    from . import adaptive_drain
    return adaptive_drain.target_runtime(app) is not None


def sharing_state(app: Any, *, observe_only: bool = False) -> dict[str, Any]:
    controller = _controller(app)
    ready = route_ready(app)
    paused = False
    if controller is not None:
        status = controller.status()
        paused = status["mode"] in _PAUSED_MODES or bool(status["stopped"])
    if paused:
        state = "paused"
    elif ready:
        if controller is not None and not observe_only:
            controller.clear_rejoin()
        state = "sharing"
    elif ready is False and controller is not None and getattr(controller, "rejoin_pending", False):
        state = "rejoining"
    else:
        state = "idle"
    return {"state": state, "line": LINES[state]}
