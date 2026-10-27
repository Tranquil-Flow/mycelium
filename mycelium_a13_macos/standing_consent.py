# SPDX-License-Identifier: AGPL-3.0-only
"""Standing consent: one explicit grant at join replaces per-run consent clicks.

Owner decision 2026-10-13.  The target shows ``DISCLOSURE_TEXT`` once when it
joins; the person grants it with explicit resource caps.  The grant is stored
privately, bound to the accepted membership (swarm, node, owner seed key), and
lets ``StandingAcceptor`` accept each owner work offer automatically when the
offer and the live resource observation are within those caps.  Withdrawing it
(at any time, from the target app) stops automatic acceptance immediately; the
manual per-run routes remain available and unchanged.

Interface for other lanes: ``StandingAcceptor.tick`` calls exactly the same
``work_acceptance.preview_work``/``accept_work`` functions as the manual
``/api/work/preview|accept`` routes; it never edits staging, worker or runtime
code.  ``report(purpose, offer)`` is called before every accept so the owner
receives a fresh capability + link report before each run.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable, Mapping

from .control_failures import TRANSIENT_CONTROL_CODES, error_http
from .managed_control import ManagedControlError

STANDING_CONSENT_PROTOCOL = "mycelium.a13_standing_consent.v1"
DISCLOSURE_TEXT = (
    "While this Mac is a member of the owner's swarm and this app is open, it will "
    "automatically: accept work the owner assigns to it; download and store only the "
    "model slice assigned to it; load that slice with MLX and compute it for requests "
    "routed through the swarm; and send the owner a capability report (chip, memory, "
    "free disk, MLX availability, power and thermal state) and a link measurement over "
    "the relay on join and before each run. Limits: at most the disk, memory, run time "
    "and command budget you choose below, only on the power and network policy you chose, "
    "and it stops on serious thermal pressure. Cooperative workers observe the "
    "activations they compute, so this is not confidential inference. You can withdraw "
    "this consent at any time from this app; automatic acceptance stops immediately."
)
DISCLOSURE_DIGEST = "sha256:" + hashlib.sha256(DISCLOSURE_TEXT.encode("utf-8")).hexdigest()
_FILE = "standing-consent.json"
_CAP_FIELDS = frozenset({"storage_bytes", "max_memory_bytes", "max_wall_seconds", "max_commands"})
_BINDING_FIELDS = frozenset({"swarm_id", "node_id", "seed_key_digest"})
_FIELDS = frozenset({"protocol", "disclosure_digest", "caps", "granted_at", "binding",
                     "bound_at", "withdrawn_at", "state"})
_MIN_MEMORY = 256 * 1024**2
_MAX_MEMORY = 512 * 1024**3
#: The member-report code contract (``run_exchange._CODE``).
_PUBLIC_CODE = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
#: Reasons that can change on a later tick (the offer is retried, not dropped).
_TRANSIENT = frozenset({"membership_required", "standing_consent_withdrawn",
                        "standing_consent_unbound", "standing_consent_missing"})

# These calls in preview_work/accept_work all precede contribution enablement
# and runtime construction. A later tick revalidates the offer and staged bytes;
# prepare returns the same digest-bound ack, and the mailbox accepts only an
# equal prior ack. This is NOT HTTP-envelope replay permission for ack routes.
_RETRYABLE_STAGING_PATHS = frozenset({
    '/managed-control/staging/read', '/managed-control/staging/ack',
})


def _finite_expiry(offer: Mapping[str, Any]) -> float | None:
    expires = offer.get('expires_at')
    if type(expires) in (int, float) and math.isfinite(expires):
        return float(expires)
    return None


def _retryable_staging_error(error: Exception, actor: Any, offer: Mapping[str, Any]) -> bool:
    # Retry is bounded by the offer's own expiry: without one, keep the
    # one-failure-and-handled behaviour.
    return (isinstance(error, ManagedControlError)
            and error.code in TRANSIENT_CONTROL_CODES
            and isinstance(error.request_path, str)
            and error.request_path in _RETRYABLE_STAGING_PATHS
            and getattr(actor, '_runtime', None) is None
            and _finite_expiry(offer) is not None)


class StandingConsentError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _int_in(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _caps(value: Any, policy: Mapping[str, Any], *, device_memory_bytes: int | None = None) -> dict[str, int]:
    if (not isinstance(value, Mapping) or set(value) != _CAP_FIELDS
            or not isinstance(policy, Mapping) or type(policy.get("storage_bytes")) is not int
            or not _int_in(value["storage_bytes"], 100_000_000, policy["storage_bytes"])
            or not _int_in(value["max_memory_bytes"], _MIN_MEMORY, _MAX_MEMORY)
            or not _int_in(value["max_wall_seconds"], 60, 3600)
            or not _int_in(value["max_commands"], 1, 10000)):
        raise StandingConsentError("standing_consent_caps_invalid")
    if type(device_memory_bytes) is int and device_memory_bytes > 0:
        # A per-run memory limit this Mac cannot honour is not a real consent:
        # at most 3/4 of its RAM (model_resources.MEMBER_PEAK_MEMORY_FRACTION).
        from .model_resources import member_memory_ceiling
        if value["max_memory_bytes"] > member_memory_ceiling(device_memory_bytes):
            raise StandingConsentError("standing_consent_memory_exceeds_device")
    return {key: int(value[key]) for key in sorted(_CAP_FIELDS)}


def _path(root: Path | str) -> Path:
    return Path(root) / _FILE


def _write(root: Path | str, document: Mapping[str, Any]) -> None:
    from .authority import _atomic_private_json

    _atomic_private_json(_path(root), dict(document))


def load_standing_consent(root: Path | str) -> dict[str, Any] | None:
    from .authority import _load_private_json

    value = _load_private_json(_path(root))
    if value is None:
        return None
    if (set(value) != _FIELDS or value.get("protocol") != STANDING_CONSENT_PROTOCOL
            or value.get("state") not in {"granted_unbound", "granted", "withdrawn"}
            or not isinstance(value.get("caps"), dict) or set(value["caps"]) != _CAP_FIELDS
            or not (value.get("binding") is None
                    or (isinstance(value["binding"], dict) and set(value["binding"]) == _BINDING_FIELDS))):
        raise StandingConsentError("standing_consent_state_invalid")
    return value


def grant_standing_consent(root: Path | str, body: Any, *, policy: Mapping[str, Any],
                           now: float, device_memory_bytes: int | None = None) -> dict[str, Any]:
    """Record the one explicit grant shown at join (unbound until membership)."""

    if (not isinstance(body, Mapping) or set(body) != {"acknowledged", "disclosure_digest", "caps"}
            or body["acknowledged"] is not True or body["disclosure_digest"] != DISCLOSURE_DIGEST):
        raise StandingConsentError("standing_consent_required")
    caps = _caps(body["caps"], policy, device_memory_bytes=device_memory_bytes)
    existing = load_standing_consent(root)
    if existing is not None and existing["state"] != "withdrawn":
        if existing["caps"] != caps:
            raise StandingConsentError("standing_consent_changed")
        return existing
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise StandingConsentError("standing_consent_time_invalid")
    document = {"protocol": STANDING_CONSENT_PROTOCOL, "disclosure_digest": DISCLOSURE_DIGEST,
                "caps": caps, "granted_at": float(now), "binding": None, "bound_at": None,
                "withdrawn_at": None, "state": "granted_unbound"}
    _write(root, document)
    return document


def bind_standing_consent(root: Path | str, binding: Mapping[str, Any], *, now: float) -> dict[str, Any]:
    """Bind the grant to the accepted membership; a different owner never inherits it."""

    if not isinstance(binding, Mapping) or set(binding) != _BINDING_FIELDS or not all(
            isinstance(binding[k], str) and binding[k] for k in _BINDING_FIELDS):
        raise StandingConsentError("standing_consent_binding_invalid")
    existing = load_standing_consent(root)
    if existing is None:
        raise StandingConsentError("standing_consent_missing")
    if existing["state"] == "withdrawn":
        raise StandingConsentError("standing_consent_withdrawn")
    wanted = {k: binding[k] for k in sorted(_BINDING_FIELDS)}
    if existing["binding"] is not None:
        if existing["binding"] != wanted:
            raise StandingConsentError("standing_consent_binding_changed")
        return existing
    document = {**existing, "binding": wanted, "bound_at": float(now), "state": "granted"}
    _write(root, document)
    return document


def withdraw_standing_consent(root: Path | str, *, now: float) -> dict[str, Any] | None:
    existing = load_standing_consent(root)
    if existing is None or existing["state"] == "withdrawn":
        return existing
    document = {**existing, "withdrawn_at": float(now), "state": "withdrawn"}
    _write(root, document)
    return document


def standing_admission(grant: Mapping[str, Any] | None, *, binding: Mapping[str, Any] | None,
                       offer: Mapping[str, Any], resource_violation: str | None) -> str | None:
    """``None`` when the offer may be accepted automatically, else one reason code."""

    if grant is None:
        return "standing_consent_missing"
    if grant["state"] == "withdrawn":
        return "standing_consent_withdrawn"
    if grant["state"] != "granted" or grant["binding"] is None:
        return "standing_consent_unbound"
    if binding is None:
        return "membership_required"
    if dict(binding) != grant["binding"]:
        return "standing_consent_owner_mismatch"
    if offer.get("target_node_id") != binding["node_id"]:
        return "standing_offer_not_for_this_member"
    if type(offer.get("archive_bytes")) is not int or offer["archive_bytes"] > grant["caps"]["storage_bytes"]:
        return "standing_offer_exceeds_caps"
    if resource_violation:
        return str(resource_violation)
    return None


def planning_caps(root) -> dict[str, int]:
    """Known local limits for placement, never an execution authorization.

    The owner can also have a standing acceptor: retain its memory cap and
    intersect storage with the app policy, rather than treating it as uncapped.
    """
    from .app import read_resource_policy
    grant = load_standing_consent(root)
    caps = {} if grant is None else {k: grant['caps'][k] for k in ('storage_bytes', 'max_memory_bytes')}
    policy = read_resource_policy(root) or {}
    if type(policy.get('storage_bytes')) is int:
        caps['storage_bytes'] = min(caps.get('storage_bytes', policy['storage_bytes']), policy['storage_bytes'])
    return caps


def standing_limits(grant: Mapping[str, Any], *, required: Mapping[str, Any] | None = None):
    """Worker limits under the standing caps.

    With ``required`` (this member's share-derived limits from the owner's
    per-split resource proposal) the worker runs under exactly its share's
    guard and disk limit, which must fit within the caps; otherwise the share
    is refused by name before anything is staged.
    """
    from .worker import WorkerLimits

    caps = grant["caps"]
    rss, disk = caps["max_memory_bytes"], caps["storage_bytes"]
    if required is not None:
        need_rss, need_disk = required.get("max_rss_bytes"), required.get("max_disk_bytes")
        if type(need_rss) is not int or type(need_disk) is not int or need_rss <= 0 or need_disk <= 0:
            raise StandingConsentError("member_resources_invalid")
        if need_rss > rss:
            raise StandingConsentError("member_memory_cap_below_share")
        if need_disk > disk:
            raise StandingConsentError("member_storage_cap_below_share")
        rss, disk = need_rss, need_disk
    return WorkerLimits(max_rss_bytes=rss, max_disk_bytes=disk,
                        max_wall_seconds=caps["max_wall_seconds"], max_commands=caps["max_commands"])


def _default_preview(*args: Any, **kwargs: Any) -> Any:
    from .work_acceptance import preview_work

    return preview_work(*args, **kwargs)


def _default_accept(*args: Any, **kwargs: Any) -> Any:
    from .work_acceptance import accept_work

    return accept_work(*args, **kwargs)


AUTO_STEP_PROTOCOL = "mycelium.a13_target_auto_step.v1"


_PREVIEW_LIMIT_CAPS = {"max_rss_bytes": "max_memory_bytes", "max_disk_bytes": "storage_bytes",
                       "max_wall_seconds": "max_wall_seconds", "max_commands": "max_commands"}
_AUTO_STEP_ERROR = "auto_step_failed"


def check_work_preview(grant: Mapping[str, Any] | None, preview: Any, *,
                       binding: Mapping[str, Any] | None, policy: Mapping[str, Any],
                       resource_violation: str | None = None) -> dict[str, Any]:
    """STABLE INTERFACE (a) -- may this exact work preview be accepted without a click?

    ``preview`` is the dict returned by ``work_acceptance.preview_work`` (the
    same document the manual ``/api/work/preview`` route returns).  Returns
    ``{"allowed": bool, "reason": str | None}``; ``reason`` is a closed code.
    Allowed only when the standing grant is granted, bound to THIS membership
    (swarm, node, owner seed key), the preview is for this node, its worker
    limits are within the consented caps, its resource policy is the one the
    person consented to, and the live resource observation permits work.
    """

    def deny(reason: str) -> dict[str, Any]:
        return {"allowed": False, "reason": reason}

    if grant is None:
        return deny("standing_consent_missing")
    if grant["state"] == "withdrawn":
        return deny("standing_consent_withdrawn")
    if grant["state"] != "granted" or grant["binding"] is None:
        return deny("standing_consent_unbound")
    if binding is None:
        return deny("membership_required")
    if dict(binding) != grant["binding"]:
        return deny("standing_consent_owner_mismatch")
    limits = preview.get("limits") if isinstance(preview, Mapping) else None
    if (not isinstance(preview, Mapping) or preview.get("protocol") != "mycelium.a13.work_preview.v1"
            or preview.get("route_ready") is not False or not isinstance(limits, Mapping)
            or set(limits) != set(_PREVIEW_LIMIT_CAPS)
            or any(type(limits[k]) is not int or limits[k] <= 0 for k in _PREVIEW_LIMIT_CAPS)
            or not isinstance(preview.get("consent_digest"), str)):
        return deny("standing_preview_invalid")
    if preview.get("node_id") != binding["node_id"]:
        return deny("standing_offer_not_for_this_member")
    if any(limits[k] > grant["caps"][cap] for k, cap in _PREVIEW_LIMIT_CAPS.items()):
        return deny("standing_preview_exceeds_caps")
    if dict(preview.get("resource_policy") or {}) != dict(policy):
        return deny("standing_preview_policy_changed")
    if resource_violation:
        return deny(str(resource_violation))
    return {"allowed": True, "reason": None}


def auto_poll_step(app: Any, *, now: float | None = None, report_due: bool = False) -> dict[str, Any]:
    """STABLE INTERFACE (b) -- one target auto-poll step under standing consent.

    Call it periodically (the app calls it every 2 s while holding its guard).
    ``app`` must provide: ``root``, ``installation_role``, ``running``,
    ``auto_step_context() -> {"binding": dict | None, "policy": dict}``,
    ``auto_report(purpose) -> dict``, ``auto_accept() -> dict | None`` and
    ``auto_steps`` (a list).  Other lanes add their own click-free work (for
    example applying the installation descriptor, or reconnecting) by
    appending ``fn(app, context) -> dict | None`` to ``app.auto_steps``; each
    runs only when this gate passes, and a failure is contained as
    ``{"error": "auto_step_failed"}``.  Returns a closed outcome document.
    """

    del now  # reserved for deterministic callers; gating uses persisted state
    outcome: dict[str, Any] = {"protocol": AUTO_STEP_PROTOCOL, "state": "ran", "report": None,
                               "offer": None, "steps": []}
    grant = load_standing_consent(app.root)
    if app.installation_role != "target":
        return {**outcome, "state": "not_target"}
    if grant is None:
        return {**outcome, "state": "no_standing_consent"}
    if grant["state"] == "withdrawn":
        return {**outcome, "state": "standing_consent_withdrawn"}
    if grant["state"] != "granted":
        return {**outcome, "state": "standing_consent_unbound"}
    if not app.running:
        return {**outcome, "state": "stopped"}
    context = app.auto_step_context()
    if context.get("binding") is None:
        return {**outcome, "state": "not_member"}
    if context["binding"] != grant["binding"]:
        return {**outcome, "state": "standing_consent_owner_mismatch"}
    if report_due:
        outcome["report"] = app.auto_report("periodic")
    outcome["offer"] = app.auto_accept()
    for step in list(getattr(app, "auto_steps", [])):
        try:
            value = step(app, dict(context))
        except Exception:  # noqa: BLE001 - another lane's step; closed code only
            value = {"error": _AUTO_STEP_ERROR}
        if value is not None:
            outcome["steps"].append(value)
    return outcome


class StandingAcceptor:
    """Accept each pending owner offer once, automatically, within standing caps."""

    def __init__(self, root: Path | str, *, preview: Callable[..., Any] = _default_preview,
                 accept: Callable[..., Any] = _default_accept,
                 report: Callable[[str, Mapping[str, Any]], Any] | None = None,
                 clock: Callable[[], float] = time.time,
                 limits_for: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None = None) -> None:
        self.root = Path(root)
        self._limits_for = limits_for
        self._preview = preview
        self._accept = accept
        self._report = report
        self._clock = clock
        self._lock = threading.Lock()
        self._handled: set[str] = set()
        self._last: dict[str, Any] | None = None

    def status(self) -> dict[str, Any]:
        grant = load_standing_consent(self.root)
        return {"state": "absent" if grant is None else grant["state"],
                "caps": None if grant is None else dict(grant["caps"]),
                "disclosure_digest": DISCLOSURE_DIGEST, "last": copy.deepcopy(self._last)}

    def _expired(self, offer: Mapping[str, Any]) -> bool:
        # Only a known, passed expiry expires an offer here; the offer client
        # already refuses real offers without a valid future expiry.
        expires = _finite_expiry(offer)
        return expires is not None and self._clock() >= expires

    def _expire(self, offer_id: str) -> dict[str, Any]:
        self._handled.add(offer_id)
        self._last = {'offer_id': offer_id, 'state': 'refused', 'reason': 'offer_expired'}
        return dict(self._last)

    def tick(self, actor: Any, *, policy: Mapping[str, Any], binding: Mapping[str, Any] | None,
             resource_check: Callable[[], str | None],
             authorize_stage: Callable[[Mapping[str, Any]], bool] | None = None,
             _test_observer: Any = None) -> dict[str, Any] | None:
        with self._lock:
            grant = load_standing_consent(self.root)
            if grant is None:
                return None
            offer = actor.pending_stage()
            if offer is None or not isinstance(offer, Mapping):
                return None
            offer_id = offer.get("offer_id")
            if not isinstance(offer_id, str) or offer_id in self._handled:
                return None
            if self._expired(offer):
                return self._expire(offer_id)
            reason = standing_admission(grant, binding=binding, offer=offer,
                                        resource_violation=resource_check())
            if reason is None:
                required = self._limits_for(offer) if self._limits_for is not None else None
                try:
                    limits = standing_limits(grant, required=required)
                except StandingConsentError as error:
                    self._handled.add(offer_id)
                    self._last = {"offer_id": offer_id, "state": "refused", "reason": error.code}
                    return dict(self._last)
                authorize = authorize_stage or (lambda _intent: resource_check() is None)
                options = {"limits": limits, "resource_policy": dict(policy),
                           "authorize_stage": authorize, "_test_observer": _test_observer}
                try:
                    if self._report is not None:
                        self._report("pre_run", offer)
                    if self._expired(offer):
                        return self._expire(offer_id)
                    preview = self._preview(actor, offer, stage_acknowledged=True, **options)
                    decision = check_work_preview(load_standing_consent(self.root), preview,
                        binding=binding, policy=policy, resource_violation=resource_check())
                    if not decision["allowed"]:
                        reason = decision["reason"]
                        if reason not in _TRANSIENT and not reason.startswith("resource_"):
                            self._handled.add(offer_id)
                        self._last = {"offer_id": offer_id, "state": "refused", "reason": reason}
                        return dict(self._last)
                    if self._expired(offer):
                        return self._expire(offer_id)
                    self._accept(actor, preview, work_acknowledged=True, **options)
                except Exception as exc:  # noqa: BLE001 - recorded as a closed code
                    # A failed native launch names its closed launch step
                    # (``node_process_start_failed_launcher_ready_timeout``);
                    # every other error keeps its bare code.
                    public = getattr(exc, "public_code", None)
                    code = (public if isinstance(public, str) and _PUBLIC_CODE.fullmatch(public)
                            else getattr(exc, "code", None)
                            or (str(exc) if str(exc).isidentifier() else None))
                    reason = code if isinstance(code, str) and _PUBLIC_CODE.fullmatch(code) else "standing_accept_failed"
                    if not _retryable_staging_error(exc, actor, offer):
                        self._handled.add(offer_id)
                    self._last = {"offer_id": offer_id, "state": "failed", "reason": reason}
                    http = error_http(exc, reason)
                    if http is not None:
                        self._last['error_http'] = http
                    return copy.deepcopy(self._last)
                self._handled.add(offer_id)
                self._last = {"offer_id": offer_id, "state": "accepted", "reason": None}
                return dict(self._last)
            if reason not in _TRANSIENT and not reason.startswith("resource_"):
                self._handled.add(offer_id)
            self._last = {"offer_id": offer_id, "state": "refused", "reason": reason}
            return dict(self._last)


__all__ = [
    "AUTO_STEP_PROTOCOL",
    "DISCLOSURE_DIGEST",
    "auto_poll_step",
    "check_work_preview",
    "DISCLOSURE_TEXT",
    "STANDING_CONSENT_PROTOCOL",
    "StandingAcceptor",
    "StandingConsentError",
    "bind_standing_consent",
    "grant_standing_consent",
    "load_standing_consent",
    "standing_admission",
    "standing_limits",
    "withdraw_standing_consent",
]
