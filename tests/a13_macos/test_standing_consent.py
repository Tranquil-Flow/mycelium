# SPDX-License-Identifier: AGPL-3.0-only
"""Standing consent: one explicit grant at join replaces per-run clicks.

Deterministic: the per-run preview/accept functions are injected fakes with
the same call shape as ``work_acceptance.preview_work``/``accept_work``.
"""
from __future__ import annotations

import os
import stat

import pytest

from mycelium_a13_macos.standing_consent import (
    AUTO_STEP_PROTOCOL,
    DISCLOSURE_DIGEST,
    auto_poll_step,
    check_work_preview,
    DISCLOSURE_TEXT,
    StandingAcceptor,
    StandingConsentError,
    bind_standing_consent,
    grant_standing_consent,
    load_standing_consent,
    standing_admission,
    standing_limits,
    withdraw_standing_consent,
)
from mycelium_a13_macos.worker import WorkerLimits

NOW = 2_000_000_000.0
POLICY = {"peer_visibility": True, "power": "battery_allowed", "storage_bytes": 4_000_000_000,
          "network": "unmetered_only", "background": False, "diagnostics": False,
          "thermal": "stop_on_serious"}
CAPS = {"storage_bytes": 3_000_000_000, "max_memory_bytes": 8 * 1024**3,
        "max_wall_seconds": 1800, "max_commands": 5000}
GRANT = {"acknowledged": True, "disclosure_digest": DISCLOSURE_DIGEST, "caps": CAPS}
BINDING = {"swarm_id": "swarm-1", "node_id": "remote-mac-a", "seed_key_digest": "sha256:" + "a" * 64}
OFFER = {"offer_id": "offer-1", "archive_digest": "sha256:" + "b" * 64, "archive_bytes": 1024,
         "run_id": "run-1", "deployment_id": "dep-1", "target_node_id": "remote-mac-a",
         "expires_at": NOW + 300}


def test_disclosure_states_what_the_device_does_its_caps_and_revocation():
    text = DISCLOSURE_TEXT.lower()
    for phrase in ("automatically", "assigned", "model", "disk", "memory", "thermal",
                   "capability", "link", "withdraw", "any time", "not confidential"):
        assert phrase in text
    assert DISCLOSURE_DIGEST.startswith("sha256:")


def test_grant_requires_explicit_current_disclosure_and_bounded_caps(tmp_path):
    for body in ({**GRANT, "acknowledged": False},
                 {**GRANT, "disclosure_digest": "sha256:" + "0" * 64},
                 {k: v for k, v in GRANT.items() if k != "caps"}):
        with pytest.raises(StandingConsentError, match="standing_consent_required"):
            grant_standing_consent(tmp_path, body, policy=POLICY, now=NOW)
    for caps in ({**CAPS, "storage_bytes": POLICY["storage_bytes"] + 1},
                 {**CAPS, "max_memory_bytes": 1},
                 {**CAPS, "max_wall_seconds": 7200},
                 {**CAPS, "max_commands": 0},
                 {**CAPS, "extra": 1}):
        with pytest.raises(StandingConsentError, match="standing_consent_caps_invalid"):
            grant_standing_consent(tmp_path, {**GRANT, "caps": caps}, policy=POLICY, now=NOW)
    assert load_standing_consent(tmp_path) is None
    grant = grant_standing_consent(tmp_path, GRANT, policy=POLICY, now=NOW)
    assert grant["state"] == "granted_unbound" and grant["caps"] == CAPS
    path = tmp_path / "standing-consent.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert load_standing_consent(tmp_path) == grant
    # A retry with the same caps is idempotent; changed caps need a withdrawal.
    assert grant_standing_consent(tmp_path, GRANT, policy=POLICY, now=NOW + 9) == grant
    with pytest.raises(StandingConsentError, match="standing_consent_changed"):
        grant_standing_consent(tmp_path, {**GRANT, "caps": {**CAPS, "max_commands": 10}},
                               policy=POLICY, now=NOW)


def test_admission_is_bound_to_the_membership_and_within_caps(tmp_path):
    grant = grant_standing_consent(tmp_path, GRANT, policy=POLICY, now=NOW)
    assert standing_admission(grant, binding=BINDING, offer=OFFER, resource_violation=None) == \
        "standing_consent_unbound"
    grant = bind_standing_consent(tmp_path, BINDING, now=NOW + 1)
    assert grant["state"] == "granted" and grant["binding"] == BINDING
    assert standing_admission(grant, binding=BINDING, offer=OFFER, resource_violation=None) is None
    other_owner = {**BINDING, "seed_key_digest": "sha256:" + "c" * 64}
    assert standing_admission(grant, binding=other_owner, offer=OFFER,
                              resource_violation=None) == "standing_consent_owner_mismatch"
    assert standing_admission(grant, binding=None, offer=OFFER,
                              resource_violation=None) == "membership_required"
    assert standing_admission(grant, binding=BINDING,
        offer={**OFFER, "target_node_id": "remote-mac-z"},
        resource_violation=None) == "standing_offer_not_for_this_member"
    assert standing_admission(grant, binding=BINDING,
        offer={**OFFER, "archive_bytes": CAPS["storage_bytes"] + 1},
        resource_violation=None) == "standing_offer_exceeds_caps"
    assert standing_admission(grant, binding=BINDING, offer=OFFER,
        resource_violation="resource_thermal_policy_violation") == \
        "resource_thermal_policy_violation"
    with pytest.raises(StandingConsentError, match="standing_consent_binding_changed"):
        bind_standing_consent(tmp_path, other_owner, now=NOW)
    assert standing_limits(grant) == WorkerLimits(max_rss_bytes=CAPS["max_memory_bytes"],
        max_disk_bytes=CAPS["storage_bytes"], max_wall_seconds=1800, max_commands=5000)
    withdrawn = withdraw_standing_consent(tmp_path, now=NOW + 2)
    assert withdrawn["state"] == "withdrawn" and withdrawn["withdrawn_at"] == NOW + 2
    assert load_standing_consent(tmp_path)["state"] == "withdrawn"
    assert standing_admission(withdrawn, binding=BINDING, offer=OFFER,
                              resource_violation=None) == "standing_consent_withdrawn"
    with pytest.raises(StandingConsentError, match="standing_consent_withdrawn"):
        bind_standing_consent(tmp_path, BINDING, now=NOW)


class Actor:
    def __init__(self, offer):
        self.offer = offer

    def pending_stage(self):
        return self.offer


def _acceptor(tmp_path, events):
    def preview(actor, offer, *, stage_acknowledged, limits, resource_policy, authorize_stage,
                _test_observer=None):
        events.append(("preview", offer["offer_id"], stage_acknowledged, limits))
        return {**PREVIEW, "offer_id": offer["offer_id"]}

    def accept(actor, preview_value, *, work_acknowledged, limits, resource_policy,
               authorize_stage, _test_observer=None):
        events.append(("accept", preview_value["offer_id"], work_acknowledged, limits))
        return {"state": "started"}

    def report(purpose, offer):
        events.append(("report", purpose, offer["run_id"]))
    return StandingAcceptor(tmp_path, preview=preview, accept=accept, report=report,
                            clock=lambda: NOW)


def test_acceptor_runs_each_offer_once_automatically_within_caps(tmp_path):
    grant_standing_consent(tmp_path, GRANT, policy=POLICY, now=NOW)
    bind_standing_consent(tmp_path, BINDING, now=NOW)
    events = []
    acceptor = _acceptor(tmp_path, events)
    actor = Actor(dict(OFFER))
    kwargs = dict(policy=POLICY, binding=BINDING, resource_check=lambda: None)
    outcome = acceptor.tick(actor, **kwargs)
    assert outcome == {"offer_id": "offer-1", "state": "accepted", "reason": None}
    limits = standing_limits(load_standing_consent(tmp_path))
    assert events == [("report", "pre_run", "run-1"), ("preview", "offer-1", True, limits),
                      ("accept", "offer-1", True, limits)]
    assert acceptor.tick(actor, **kwargs) is None  # same offer is never re-accepted
    assert len(events) == 3
    assert acceptor.status()["last"] == outcome
    actor.offer = None
    assert acceptor.tick(actor, **kwargs) is None


def test_acceptor_refuses_without_side_effects_when_withdrawn_or_out_of_caps(tmp_path):
    events = []
    acceptor = _acceptor(tmp_path, events)
    actor = Actor(dict(OFFER))
    kwargs = dict(policy=POLICY, binding=BINDING, resource_check=lambda: None)
    assert acceptor.tick(actor, **kwargs) is None  # no grant: manual path only
    grant_standing_consent(tmp_path, GRANT, policy=POLICY, now=NOW)
    bind_standing_consent(tmp_path, BINDING, now=NOW)
    big = Actor({**OFFER, "offer_id": "offer-big", "archive_bytes": CAPS["storage_bytes"] + 1})
    assert acceptor.tick(big, **kwargs) == {"offer_id": "offer-big", "state": "refused",
                                            "reason": "standing_offer_exceeds_caps"}
    hot = dict(kwargs, resource_check=lambda: "resource_thermal_policy_violation")
    assert acceptor.tick(actor, **hot)["reason"] == "resource_thermal_policy_violation"
    # A transient resource refusal is retried on a later tick.
    withdraw_standing_consent(tmp_path, now=NOW)
    assert acceptor.tick(actor, **kwargs)["reason"] == "standing_consent_withdrawn"
    assert events == []


PREVIEW = {"protocol": "mycelium.a13.work_preview.v1", "offer_id": "offer-1",
           "archive_digest": OFFER["archive_digest"], "assignment_digest": "sha256:" + "1" * 64,
           "manifest_digest": "sha256:" + "2" * 64, "graph_digest": "sha256:" + "3" * 64,
           "node_id": "remote-mac-a", "run_id": "run-1", "deployment_id": "dep-1",
           "model_id": "Qwen/Qwen3-0.6B", "resolved_commit": "c" * 40,
           "limits": {"max_rss_bytes": CAPS["max_memory_bytes"], "max_disk_bytes": CAPS["storage_bytes"],
                      "max_wall_seconds": 1800, "max_commands": 5000},
           "resource_policy": POLICY, "route_ready": False, "consent_digest": "sha256:" + "4" * 64}


def test_work_preview_check_allows_only_previews_within_the_consented_caps(tmp_path):
    grant = grant_standing_consent(tmp_path, GRANT, policy=POLICY, now=NOW)
    check = lambda preview, **kw: check_work_preview(
        load_standing_consent(tmp_path), preview, binding=kw.get("binding", BINDING),
        policy=kw.get("policy", POLICY), resource_violation=kw.get("violation"))
    assert check(PREVIEW) == {"allowed": False, "reason": "standing_consent_unbound"}
    bind_standing_consent(tmp_path, BINDING, now=NOW)
    assert check(PREVIEW) == {"allowed": True, "reason": None}
    for field, value, reason in (
            ("max_rss_bytes", CAPS["max_memory_bytes"] + 1, "standing_preview_exceeds_caps"),
            ("max_disk_bytes", CAPS["storage_bytes"] + 1, "standing_preview_exceeds_caps"),
            ("max_wall_seconds", 1801, "standing_preview_exceeds_caps"),
            ("max_commands", 5001, "standing_preview_exceeds_caps")):
        assert check({**PREVIEW, "limits": {**PREVIEW["limits"], field: value}}) == \
            {"allowed": False, "reason": reason}
    assert check({**PREVIEW, "node_id": "remote-mac-z"})["reason"] == "standing_offer_not_for_this_member"
    assert check({**PREVIEW, "route_ready": True})["reason"] == "standing_preview_invalid"
    assert check({k: v for k, v in PREVIEW.items() if k != "limits"})["reason"] == "standing_preview_invalid"
    assert check({**PREVIEW, "resource_policy": {**POLICY, "power": "ac_only"}})["reason"] == \
        "standing_preview_policy_changed"
    assert check(PREVIEW, binding={**BINDING, "swarm_id": "other"})["reason"] == \
        "standing_consent_owner_mismatch"
    assert check(PREVIEW, violation="resource_power_policy_violation")["reason"] == \
        "resource_power_policy_violation"
    withdraw_standing_consent(tmp_path, now=NOW)
    assert check(PREVIEW)["reason"] == "standing_consent_withdrawn"
    assert check_work_preview(None, PREVIEW, binding=BINDING, policy=POLICY)["reason"] == \
        "standing_consent_missing"
    del grant


def test_acceptor_refuses_a_preview_outside_caps_before_accepting(tmp_path):
    grant_standing_consent(tmp_path, GRANT, policy=POLICY, now=NOW)
    bind_standing_consent(tmp_path, BINDING, now=NOW)
    events = []

    def preview(actor, offer, **kwargs):
        events.append("preview")
        return {**PREVIEW, "limits": {**PREVIEW["limits"], "max_rss_bytes": CAPS["max_memory_bytes"] * 2}}

    def accept(*args, **kwargs):
        events.append("accept")
    acceptor = StandingAcceptor(tmp_path, preview=preview, accept=accept, clock=lambda: NOW)
    outcome = acceptor.tick(Actor(dict(OFFER)), policy=POLICY, binding=BINDING, resource_check=lambda: None)
    assert outcome == {"offer_id": "offer-1", "state": "refused", "reason": "standing_preview_exceeds_caps"}
    assert events == ["preview"]


class App:
    """Duck-typed stand-in for DemoApp's auto-step surface (no sockets)."""

    def __init__(self, root, *, role="target", running=True, membership="current"):
        self.root = root
        self.installation_role = role
        self.running = running
        self.membership = membership
        self.calls = []
        self.auto_steps = []

    def auto_step_context(self):
        return {"binding": BINDING if self.membership == "current" else None, "policy": POLICY}

    def auto_report(self, purpose):
        self.calls.append(("report", purpose))
        return {"purpose": purpose, "state": "delivered"}

    def auto_accept(self):
        self.calls.append(("accept",))
        return {"offer_id": "offer-1", "state": "accepted", "reason": None}


def test_auto_poll_step_is_gated_by_role_membership_and_standing_consent(tmp_path):
    seen = []
    app = App(tmp_path)
    app.auto_steps.append(lambda app_, context: seen.append(context["binding"]) or {"step": "descriptor"})
    assert auto_poll_step(app, now=NOW) == {"protocol": AUTO_STEP_PROTOCOL, "state": "no_standing_consent",
                                            "report": None, "offer": None, "steps": []}
    grant_standing_consent(tmp_path, GRANT, policy=POLICY, now=NOW)
    bind_standing_consent(tmp_path, BINDING, now=NOW)
    assert auto_poll_step(App(tmp_path, role="owner"), now=NOW)["state"] == "not_target"
    assert auto_poll_step(App(tmp_path, running=False), now=NOW)["state"] == "stopped"
    assert auto_poll_step(App(tmp_path, membership="expired"), now=NOW)["state"] == "not_member"
    result = auto_poll_step(app, now=NOW, report_due=True)
    assert result == {"protocol": AUTO_STEP_PROTOCOL, "state": "ran",
                      "report": {"purpose": "periodic", "state": "delivered"},
                      "offer": {"offer_id": "offer-1", "state": "accepted", "reason": None},
                      "steps": [{"step": "descriptor"}]}
    assert app.calls == [("report", "periodic"), ("accept",)] and seen == [BINDING]
    app.calls.clear()
    assert auto_poll_step(app, now=NOW)["report"] is None and app.calls == [("accept",)]
    # A failing registered step is contained and reported with a closed code.
    app.auto_steps.append(lambda *_: (_ for _ in ()).throw(ValueError("boom")))
    assert auto_poll_step(app, now=NOW)["steps"][-1] == {"error": "auto_step_failed"}
    withdraw_standing_consent(tmp_path, now=NOW)
    assert auto_poll_step(app, now=NOW)["state"] == "standing_consent_withdrawn"
