# SPDX-License-Identifier: AGPL-3.0-only
"""R24(3): derived sharing_state Paused -> Rejoining (connecting route) -> Sharing; 'Sharing' only once the route is ready."""
from __future__ import annotations

from types import SimpleNamespace

from mycelium_a13_macos import adaptive_host as ah
from mycelium_a13_macos import swarm_live

POLICY_PAUSED = {**ah.DEFAULT_POLICY, "enabled": True, "automatic": False, "game_mode": True}
POLICY_RELEASED = {**ah.DEFAULT_POLICY, "enabled": True, "automatic": False, "game_mode": False}


class Run:
    def __init__(self, state="serving", ready=True):
        self.state, self.ready = state, ready

    def status(self):
        return {"state": self.state, "step": None, "registry_origin": "http://127.0.0.1:5", "run_id": "r1",
                "participants": [], "route_ready": self.ready and self.state == "serving"}


def make(policy=None, run=None):
    clock = SimpleNamespace(now=1000.0)
    controller = ah.AdaptiveHostController(ah.DEFAULT_POLICY, clock=lambda: clock.now)
    runtime = SimpleNamespace(controller=controller)
    app = SimpleNamespace(owner_run=run or Run(), _adaptive_runtime=runtime, installation_role="owner")
    if policy:
        controller.apply_policy(policy)
    return app, controller, clock


def state(app):
    from mycelium_a13_macos.sharing_state import sharing_state
    return sharing_state(app)["state"]


def test_paused_while_drain_or_withdraw():
    app, controller, _ = make(POLICY_PAUSED)
    assert controller.mode == "drain" and state(app) == "paused"
    controller._enter("withdraw", "owner_game_mode")
    assert state(app) == "paused"


def test_paused_by_manual_stop_with_adaptive_sharing_off():
    app, controller, _ = make(POLICY_RELEASED, Run("running"))
    controller.stop_serving()
    assert state(app) == "paused"


def test_rejoining_after_release_before_route_ready():
    app, controller, _ = make(POLICY_PAUSED, Run("running"))
    assert state(app) == "paused"
    controller.apply_policy(POLICY_RELEASED)
    controller._enter("normal", "none")  # fence released (re-entry done)
    assert state(app) == "rejoining"
    app.owner_run = Run("serving", ready=False)
    assert state(app) == "rejoining"


def test_sharing_only_when_serving_and_route_ready():
    app, controller, _ = make(POLICY_PAUSED, Run("running"))
    controller.apply_policy(POLICY_RELEASED)
    controller._enter("normal", "none")
    app.owner_run = Run("serving", ready=True)
    assert state(app) == "sharing"
    app.owner_run = Run("running")  # the latch was cleared: an ordinary warm-up is not a rejoin
    assert state(app) == "idle"


def test_failed_run_after_pause_is_not_reported_as_rejoining():
    app, controller, _ = make(POLICY_PAUSED, Run("failed"))
    controller.apply_policy(POLICY_RELEASED)
    controller._enter("normal", "none")
    assert state(app) == "idle"


def test_no_adaptive_runtime_is_idle_or_sharing():
    app = SimpleNamespace(owner_run=Run(), installation_role="owner")
    assert state(app) == "sharing"
    app = SimpleNamespace(owner_run=Run("idle"), installation_role="owner")
    assert state(app) == "idle"


def test_status_reports_sharing_state_and_line(tmp_path):
    from mycelium_a13_macos.app import DemoApp
    app = DemoApp(tmp_path / "app", check_platform=False).start()
    try:
        doc = app.status()
        assert doc["sharing"]["state"] in {"idle", "paused", "rejoining", "sharing"}
        assert "connecting route" in __import__("mycelium_a13_macos.sharing_state", fromlist=["x"]).LINES["rejoining"]
    finally:
        app.close()


def test_hosted_page_never_shows_sharing_while_rejoining():
    run = Run("serving", ready=True)
    rejoin = {"state": "rejoining", "line": "Rejoining - connecting route"}
    doc = swarm_live.project(owner_run=run.status(), adaptive=None, recovery=None, observatory=None, busy=False,
                             sharing=rejoin)
    assert doc["route"]["state"] == "rejoining" and doc["route"]["route_ready"] is False
    assert "serving" not in str(doc["route"]["state"])
    ok = swarm_live.project(owner_run=run.status(), adaptive=None, recovery=None, observatory=None, busy=False,
                            sharing={"state": "sharing", "line": "Sharing"})
    assert ok["route"]["state"] == "serving"
    old = swarm_live.project(owner_run=run.status(), adaptive=None, recovery=None, observatory=None, busy=False)
    assert old["route"]["state"] == "serving"  # existing callers unchanged


def test_hosted_host_section_projects_pause_and_adaptive_flags_as_bools_only():
    doc = swarm_live.project(owner_run=None, adaptive={"enabled": True, "state": "manual", "mode": "drain",
                                                       "reason": "owner_game_mode", "capacity_scale": "zero",
                                                       "pause_sharing": True, "adaptive_sharing": False,
                                                       "line": "Adaptive sharing: off - you paused sharing"},
                             recovery=None, observatory=None, busy=False)
    host = doc["host"]
    assert host["pause_sharing"] is True and host["adaptive_sharing"] is False and "line" not in host
