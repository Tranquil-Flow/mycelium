# SPDX-License-Identifier: AGPL-3.0-only
"""A16 owner surface (case a16_product_status_privacy): opt-in, Game mode, Stop, status panel.

Deterministic (fake clock/sampler) plus one real-Chromium pass over the ordinary A13 page.
No app identity may appear in the status, the actions or the DOM.
"""
from __future__ import annotations

import json
import re

import pytest

from mycelium_a13_macos import adaptive_host as ah
from mycelium_a13_macos.app import DemoApp, DemoError

GIB = 1 << 30
SAFE = {"power": "external", "thermal": "nominal", "network": "unmetered"}
FORBIDDEN = re.compile(r"(?i)(process|bundle|window_title|foreground|app_name|pid\b|keystroke)")
STATUS_KEYS = {"enabled", "game_mode", "stopped", "mode", "reason", "generation", "capacity_scale",
               "new_admissions_allowed", "expires_in_s", "withdraw_overdue", "state", "reserve_bytes",
               "policy_error", "headroom_known", "pause_sharing", "adaptive_sharing", "line"}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


_APPS: list = []


@pytest.fixture(autouse=True)
def _close_apps():
    yield
    while _APPS:
        _APPS.pop().close()


def make_app(tmp_path):
    clock = Clock()
    app = DemoApp(tmp_path / "app", check_platform=False).start()
    _APPS.append(app)
    policy = {"peer_visibility": True, "power": "battery_allowed", "storage_bytes": 10**9,
              "network": "metered_allowed", "background": False, "diagnostics": False,
              "thermal": "stop_on_serious"}
    path = app.root / "resource-policy.json"
    path.write_text(json.dumps(policy))
    path.chmod(0o600)
    app._policy_observer = lambda: dict(SAFE)
    app._adaptive_clock = clock
    app._adaptive_sampler = lambda: {"observed_at": clock.now, "pressure": "normal", "available_bytes": 8 * GIB}
    return app, clock


ON = {"enabled": True, "game_mode": False, "acknowledged": True}


def adaptive(app):
    return app.status()["adaptive_host"]


def test_status_is_off_by_default_and_never_samples(tmp_path):
    app, _ = make_app(tmp_path)
    app._adaptive_sampler = lambda: pytest.fail("sampled while off")
    status = adaptive(app)
    assert status["enabled"] is False and status["state"] == "off"
    assert set(status) <= STATUS_KEYS


def test_enabling_requires_explicit_acknowledgement_and_exact_keys(tmp_path):
    app, _ = make_app(tmp_path)
    for body in ({"enabled": True, "game_mode": False}, {**ON, "acknowledged": False},
                 {**ON, "foreground_app": "x"}, {**ON, "enabled": 1}, {}):
        with pytest.raises(DemoError):
            app.action("/api/adaptive/policy", body)
    assert adaptive(app)["enabled"] is False
    assert not (app.root / ah.POLICY_FILENAME).exists()


def test_opt_in_persists_owner_policy_and_reports_mode(tmp_path):
    app, clock = make_app(tmp_path)
    app.action("/api/adaptive/policy", ON)
    status = adaptive(app)
    assert status["enabled"] is True and status["mode"] == "normal" and status["state"] == "active"
    assert ah.read_adaptive_policy(app.root)["enabled"] is True
    assert (app.root / ah.POLICY_FILENAME).stat().st_mode & 0o777 == 0o600
    app.action("/api/adaptive/policy", {"enabled": False, "game_mode": False, "acknowledged": False})
    assert adaptive(app)["enabled"] is False


def test_policy_update_preserves_owner_timings_and_reserve(tmp_path):
    app, _ = make_app(tmp_path)
    ah.write_adaptive_policy(app.root, {**ah.DEFAULT_POLICY, "reserve_bytes": GIB, "timings": {"recovery_s": 300}},
                             owner_authorized=True)
    app.action("/api/adaptive/policy", ON)
    policy = ah.read_adaptive_policy(app.root)
    assert policy["reserve_bytes"] == GIB and policy["timings"] == {"recovery_s": 300}


def test_game_mode_fences_new_work_immediately_and_is_reversible(tmp_path):
    app, clock = make_app(tmp_path)
    app.action("/api/adaptive/policy", ON)
    app.action("/api/adaptive/policy", {**ON, "game_mode": True})
    status = adaptive(app)
    assert status["mode"] == "drain" and status["reason"] == "owner_game_mode"
    assert status["new_admissions_allowed"] is False
    assert app._resource_check() == "adaptive_host_draining"
    app.action("/api/adaptive/policy", {**ON, "game_mode": False})
    clock.now += 1
    assert app._resource_check() == "adaptive_host_draining"  # hysteresis: no instant return


def test_stop_and_resume_controls(tmp_path):
    app, _ = make_app(tmp_path)
    with pytest.raises(DemoError):
        app.action("/api/adaptive/stop", {})  # not opted in -> nothing to stop
    app.action("/api/adaptive/policy", ON)
    app.action("/api/adaptive/stop", {})
    status = adaptive(app)
    assert status["stopped"] is True and status["reason"] == "owner_stop"
    assert app._resource_check() == "adaptive_host_draining"
    with pytest.raises(DemoError):
        app.action("/api/adaptive/stop", {"x": 1})
    app.action("/api/adaptive/resume", {})
    assert adaptive(app)["stopped"] is False


def test_status_projection_is_allow_listed_and_leaks_no_app_identity(tmp_path):
    app, _ = make_app(tmp_path)
    app.action("/api/adaptive/policy", {**ON, "game_mode": True})
    status = adaptive(app)
    assert set(status) <= STATUS_KEYS
    assert not FORBIDDEN.search(json.dumps(status).replace("withdraw_overdue", ""))
    publication = app.status()["adaptive_host_publication"]
    assert set(publication) == ah.PUBLICATION_KEYS


def test_invalid_policy_file_is_visible_not_silent(tmp_path):
    app, _ = make_app(tmp_path)
    path = app.root / ah.POLICY_FILENAME
    path.write_text("{bad")
    path.chmod(0o600)
    status = adaptive(app)
    assert status["state"] == "error" and status["policy_error"] == "adaptive_policy_invalid"
    assert status["new_admissions_allowed"] is False


def test_assets_have_panel_without_inline_script_or_new_transport():
    from pathlib import Path
    web = Path(ah.__file__).parent / "web"
    html = (web / "index.html").read_text()
    js = (web / "app.js").read_text()
    for ident in ("adaptive-enable", "adaptive-game", "adaptive-stop", "adaptive-status", "adaptive-ack"):
        assert f'id="{ident}"' in html
    assert "<script>" not in html and "onclick=" not in html
    assert js.count("fetch(") == 1  # the single existing api() wrapper; no new transport
    assert "EventSource" not in js and "XMLHttpRequest" not in js
    assert "/api/adaptive/policy" in js and "/api/adaptive/stop" in js


def test_browser_opt_in_pause_sharing_stop_and_status_panel(tmp_path):
    """Real Chromium over the ordinary A13 page: no app identity in DOM; CSP script-src 'self' holds."""
    from playwright.sync_api import expect, sync_playwright

    app, clock = make_app(tmp_path)
    clock.now = __import__("time").monotonic()  # the live sampler/clock pair stays fake but monotonic
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(app.launch_file.as_uri())
        page.get_by_role("button", name="Open private demo", exact=True).click()
        expect(page.locator("#private")).to_be_visible()
        expect(page.locator("#adaptive-status")).to_contain_text("Off.")
        assert page.locator("#adaptive-stop").is_disabled() and page.locator("#adaptive-game").is_enabled()  # Pause works with Adaptive sharing off
        # Without acknowledgement the owner refuses. The UI must then show the real
        # state again at once: a checkbox left ticked would claim "on" while the host
        # is off (and Locator.check() raced that revert under load in L2).
        page.locator("#adaptive-enable").click()
        expect(page.locator("#notice")).to_contain_text("acknowledgement required")
        expect(page.locator("#adaptive-enable")).not_to_be_checked(timeout=2000)
        expect(page.locator("#adaptive-status")).to_contain_text("Off.")
        page.locator("#adaptive-ack").check()
        page.locator("#adaptive-enable").check()
        expect(page.locator("#adaptive-status")).to_contain_text("On · normal")
        page.locator("#adaptive-game").check()
        expect(page.locator("#adaptive-status")).to_contain_text("you paused sharing")
        expect(page.locator("#adaptive-line")).to_have_text("Adaptive sharing: paused - you paused sharing")
        page.locator("#adaptive-game").uncheck()
        page.locator("#adaptive-stop").click()
        expect(page.locator("#adaptive-status")).to_contain_text("you pressed Stop")
        text = page.locator("#adaptive").inner_text().lower()
        assert not FORBIDDEN.search(text.replace("running work", ""))
        # the only console error is the deliberate 400 for the un-acknowledged opt-in above
        assert len(errors) == 1 and "400" in errors[0]
        browser.close()
