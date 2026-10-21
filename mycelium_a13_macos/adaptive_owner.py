# SPDX-License-Identifier: AGPL-3.0-only
"""A16 owner surface: Adaptive sharing, Pause sharing, Stop/Resume, and a privacy-reduced status projection.

User-visible names (R24/R26): the manual switch is *Pause sharing* and the automatic controller is
*Adaptive sharing*.  The wire keeps ``game_mode`` / reason ``owner_game_mode`` (``pause_sharing`` is
an additive alias); ``{adaptive_sharing, pause_sharing, acknowledged}`` is the independent form.

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

# Closed, allow-listed reason wording for the one-line status (never app identity).
REASON_TEXT = {"owner_game_mode": "you paused sharing", "owner_stop": "you pressed Stop",
               "host_memory_pressure": "memory is low", "host_cpu_saturation": "the CPU is busy"}


def _error(code: str):
    from .app import DemoError
    return DemoError(code)


def _runtime(app: Any) -> ah.AdaptiveHostRuntime:
    ah.adaptive_resource_check(app)  # creates the shared runtime; never samples while off
    runtime = getattr(app, "_adaptive_runtime", None)
    if runtime is None:
        raise _error("adaptive_unavailable")
    return runtime


def status_line(*, automatic: bool, mode: str, reason: str) -> str:
    """'Adaptive sharing: reduced - <reason>' / 'paused - <reason>' (<= 120 chars, allow-listed text)."""
    why = REASON_TEXT.get(reason, "")
    if not automatic:
        line = "Adaptive sharing: off" + (f" - {why}" if mode in ("drain", "withdraw") and why else "")
    elif mode == "reduced":
        line = "Adaptive sharing: reduced" + (f" - {why}" if why else "")
    elif mode in ("drain", "withdraw"):
        line = "Adaptive sharing: paused" + (f" - {why}" if why else "")
    else:
        line = "Adaptive sharing: on"
    return line[:120]


def adaptive_status(app: Any) -> dict[str, Any]:
    """Coarse owner-visible state; allow-listed fields only."""
    try:
        policy = ah.read_adaptive_policy(app.root)
    except ah.AdaptiveHostError as error:
        return {"enabled": False, "state": "error", "policy_error": error.code,
                "new_admissions_allowed": False}
    if not policy["enabled"]:
        return {"enabled": False, "game_mode": False, "pause_sharing": False, "adaptive_sharing": False,
                "stopped": False, "state": "off", "line": "Adaptive sharing: off",
                "reserve_bytes": policy["reserve_bytes"]}
    automatic = ah.policy_automatic(policy)
    runtime = _runtime(app)
    runtime.check()  # applies a fresh policy and samples if a sample is due
    controller = runtime.controller
    status = controller.status()
    publication = controller.publication()
    now = getattr(app, "_adaptive_clock", time.monotonic)()
    return {
        "enabled": True, "game_mode": policy["game_mode"], "pause_sharing": policy["game_mode"],
        "adaptive_sharing": automatic, "stopped": status["stopped"],
        "state": ("active" if automatic else "manual") if status["mode"] == "normal" else status["mode"],
        "line": status_line(automatic=automatic, mode=status["mode"], reason=status["reason"]),
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
        # Exactly one of three key sets; the first two are the legacy wire (``pause_sharing`` is an
        # alias of ``game_mode``), the third switches the automatic controller and the manual pause
        # independently.
        shapes = ({"enabled", "game_mode", "acknowledged"}, {"enabled", "pause_sharing", "acknowledged"},
                  {"adaptive_sharing", "pause_sharing", "acknowledged"})
        if (not isinstance(body, dict) or set(body) not in shapes
                or any(type(value) is not bool for value in body.values())):
            raise _error("adaptive_request_invalid")
        pause = body["pause_sharing"] if "pause_sharing" in body else body["game_mode"]
        independent = "adaptive_sharing" in body
        try:
            policy = copy.deepcopy(ah.read_adaptive_policy(app.root))
            if independent:
                automatic = body["adaptive_sharing"]
                if automatic and not body["acknowledged"] and not (
                        policy["enabled"] and ah.policy_automatic(policy)):
                    raise _error("adaptive_acknowledgement_required")
                policy["enabled"], policy["automatic"], policy["game_mode"] = automatic or pause, automatic, pause
            else:  # legacy: enabled = automatic controller on; Pause only rides along with it
                if body["enabled"] and not body["acknowledged"]:
                    raise _error("adaptive_acknowledgement_required")
                policy["enabled"], policy["automatic"] = body["enabled"], body["enabled"]
                policy["game_mode"] = body["enabled"] and pause
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


def apply_first_consent_choice(app: Any, choice: bool | None) -> None:
    """Adaptive sharing at first consent (R26): ticked by default in the A13 consent form.

    ``choice`` is the optional top-level ``adaptive_sharing`` bool of the consent call (``None`` =
    the caller did not offer it, e.g. the A16 harness: nothing is written).  The tick is the
    acknowledgement.  An existing policy file (an earlier or explicit choice) is never changed, and
    nothing is sampled here.
    """
    if choice is None:
        return
    import os
    if os.path.lexists(os.path.join(app.root, ah.POLICY_FILENAME)):
        return
    try:
        ah.write_adaptive_policy(
            app.root, {**ah.DEFAULT_POLICY, "enabled": choice, "automatic": choice}, owner_authorized=True)
    except ah.AdaptiveHostError:
        pass  # a policy problem must never fail enrolment; status shows it
