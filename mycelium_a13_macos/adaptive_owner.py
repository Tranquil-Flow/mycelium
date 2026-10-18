# SPDX-License-Identifier: AGPL-3.0-or-later
"""A16 owner surface: opt-in, Game mode, Stop/Resume, and a privacy-reduced status projection.

Reached only through the A13 local-launcher action boundary (session, origin and
``X-Mycelium-Action`` already enforced by the HTTP layer).  Every body is an exact key set; an
unknown key (for example an application name) is refused, and nothing here ever reads, stores
or returns application identity.
"""
from __future__ import annotations

import copy
import time
from typing import Any

from . import adaptive_host as ah

ADAPTIVE_ROUTES = frozenset({"/api/adaptive/policy", "/api/adaptive/stop", "/api/adaptive/resume"})


def _error(code: str):
    from .app import DemoError
    return DemoError(code)


def _runtime(app: Any) -> ah.AdaptiveHostRuntime:
    ah.adaptive_resource_check(app)  # creates the shared runtime; never samples while off
    runtime = getattr(app, "_adaptive_runtime", None)
    if runtime is None:
        raise _error("adaptive_unavailable")
    return runtime


def adaptive_status(app: Any) -> dict[str, Any]:
    """Coarse owner-visible state; allow-listed fields only."""
    try:
        policy = ah.read_adaptive_policy(app.root)
    except ah.AdaptiveHostError as error:
        return {"enabled": False, "state": "error", "policy_error": error.code,
                "new_admissions_allowed": False}
    if not policy["enabled"]:
        return {"enabled": False, "game_mode": False, "stopped": False, "state": "off",
                "reserve_bytes": policy["reserve_bytes"]}
    runtime = _runtime(app)
    runtime.check()  # applies a fresh policy and samples if a sample is due
    controller = runtime.controller
    status = controller.status()
    publication = controller.publication()
    now = getattr(app, "_adaptive_clock", time.monotonic)()
    return {
        "enabled": True, "game_mode": policy["game_mode"], "stopped": status["stopped"],
        "state": "active" if status["mode"] == "normal" else status["mode"],
        "mode": status["mode"], "reason": status["reason"], "generation": status["generation"],
        "capacity_scale": publication["capacity_scale"],
        "new_admissions_allowed": publication["new_admissions_allowed"],
        "expires_in_s": round(max(0.0, publication["expires_at"] - now), 1),
        "withdraw_overdue": status["withdraw_overdue"], "reserve_bytes": policy["reserve_bytes"],
    }


def adaptive_publication(app: Any) -> dict[str, Any] | None:
    """The allow-listed A1 projection (``ah.PUBLICATION_KEYS``), or ``None`` while off."""
    try:
        policy = ah.read_adaptive_policy(app.root)
    except ah.AdaptiveHostError:
        return None
    if not policy["enabled"]:
        return None
    return _runtime(app).controller.publication()


def adaptive_action(app: Any, path: str, body: Any) -> dict[str, Any]:
    if path == "/api/adaptive/policy":
        keys = {"enabled", "game_mode", "acknowledged"}
        if not isinstance(body, dict) or set(body) != keys or any(type(body[k]) is not bool for k in keys):
            raise _error("adaptive_request_invalid")
        if body["enabled"] and not body["acknowledged"]:
            raise _error("adaptive_acknowledgement_required")
        try:
            policy = copy.deepcopy(ah.read_adaptive_policy(app.root))
            policy["enabled"] = body["enabled"]
            policy["game_mode"] = body["enabled"] and body["game_mode"]
            ah.write_adaptive_policy(app.root, policy, owner_authorized=True)
        except ah.AdaptiveHostError as error:
            raise _error(error.code) from None
        _runtime(app).check()
        return adaptive_status(app)
    if body != {}:
        raise _error("invalid_request")
    try:
        enabled = ah.read_adaptive_policy(app.root)["enabled"]
    except ah.AdaptiveHostError as error:
        raise _error(error.code) from None
    if not enabled:
        raise _error("adaptive_not_enabled")
    runtime = _runtime(app)
    runtime.check()
    if path == "/api/adaptive/stop":
        runtime.controller.stop_serving()
    else:
        runtime.controller.resume_serving()
    return adaptive_status(app)
