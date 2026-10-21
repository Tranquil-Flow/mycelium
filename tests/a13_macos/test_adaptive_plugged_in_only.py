# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV25: 'plugged-in only' - on battery the member pauses through the Adaptive sharing / Pause sharing path."""
from __future__ import annotations

import json

import pytest

from mycelium_a13_macos import adaptive_host as ah
from mycelium_a13_macos.app import DemoError
from mycelium_router import adaptive_overlay as overlay

from test_adaptive_owner import GIB, SAFE, Clock, _APPS, _close_apps, adaptive, make_app  # noqa: F401

PLUG = {"adaptive_sharing": False, "pause_sharing": False, "plugged_in_only": True, "acknowledged": False}
BATTERY = {**SAFE, "power": "battery"}
AC = {**SAFE, "power": "external"}


def set_power(app, observation):
    app._policy_observer = lambda: dict(observation)


def test_the_new_reason_is_known_to_every_closed_reason_list():
    assert "host_on_battery" in ah.REASON_CATEGORIES and "host_on_battery" in ah._MARKER_REASONS
    assert "host_on_battery" in overlay._REASONS


def test_on_battery_pauses_sharing_with_the_exact_status_line(tmp_path):
    app, clock = make_app(tmp_path)
    set_power(app, BATTERY)
    app.action("/api/adaptive/policy", PLUG)
    status = adaptive(app)
    assert status["line"] == "Adaptive sharing: paused - on battery"
    assert status["mode"] == "drain" and status["reason"] == "host_on_battery"
    assert status["plugged_in_only"] is True and status["new_admissions_allowed"] is False
    assert app._resource_check() == "adaptive_host_draining"


def test_on_external_power_nothing_pauses(tmp_path):
    app, clock = make_app(tmp_path)
    set_power(app, AC)
    app.action("/api/adaptive/policy", PLUG)
    status = adaptive(app)
    assert status["mode"] == "normal" and status["plugged_in_only"] is True
    assert app._resource_check() is None


def test_unplugging_pauses_and_replugging_recovers_through_the_normal_recovery_window(tmp_path):
    app, clock = make_app(tmp_path)
    set_power(app, AC)
    app.action("/api/adaptive/policy", PLUG)
    assert adaptive(app)["mode"] == "normal"
    set_power(app, BATTERY)
    clock.now += 3
    assert adaptive(app)["reason"] == "host_on_battery"
    set_power(app, AC)
    clock.now += 3
    controller = app._adaptive_runtime.controller
    assert adaptive(app)["mode"] == "drain"          # not instantly: the usual healthy window applies
    assert controller.recovery_ready() is False
    clock.now += 125
    adaptive(app)
    assert controller.recovery_ready() is True


def test_without_the_option_battery_does_not_pause(tmp_path):
    app, clock = make_app(tmp_path)
    set_power(app, BATTERY)
    app.action("/api/adaptive/policy", {"adaptive_sharing": True, "pause_sharing": False, "acknowledged": True})
    status = adaptive(app)
    assert status["mode"] == "normal" and "plugged_in_only" not in status


def test_works_with_adaptive_sharing_off_and_never_samples_memory(tmp_path):
    app, clock = make_app(tmp_path)
    app._adaptive_sampler = lambda: pytest.fail("sampled while Adaptive sharing is off")
    set_power(app, BATTERY)
    app.action("/api/adaptive/policy", PLUG)
    status = adaptive(app)
    assert status["adaptive_sharing"] is False and status["enabled"] is True
    assert status["line"] == "Adaptive sharing: paused - on battery"


def test_unknown_power_does_not_pause(tmp_path):
    """Desktops and stub platforms report unknown: the option cannot guess a battery that may not exist."""
    app, clock = make_app(tmp_path)
    set_power(app, {**SAFE, "power": "unknown"})
    app.action("/api/adaptive/policy", PLUG)
    assert adaptive(app)["mode"] == "normal"


def test_a_failing_observer_does_not_pause_or_crash(tmp_path):
    app, clock = make_app(tmp_path)
    app.action("/api/adaptive/policy", PLUG)
    def boom():
        raise OSError("probe")
    app._policy_observer = boom
    assert adaptive(app)["mode"] == "normal"


def test_policy_file_round_trip_and_validation(tmp_path):
    app, clock = make_app(tmp_path)
    app.action("/api/adaptive/policy", PLUG)
    stored = json.loads((app.root / ah.POLICY_FILENAME).read_text())
    assert stored["plugged_in_only"] is True and stored["enabled"] is True and stored["automatic"] is False
    bad = {**stored, "enabled": False, "automatic": False}
    with pytest.raises(ah.AdaptiveHostError):
        ah.validate_policy(bad)
    with pytest.raises(ah.AdaptiveHostError):
        ah.validate_policy({**stored, "plugged_in_only": 1})
    app.action("/api/adaptive/policy", {**PLUG, "plugged_in_only": False})
    stored = json.loads((app.root / ah.POLICY_FILENAME).read_text())
    assert "plugged_in_only" not in stored and stored["enabled"] is False
    assert adaptive(app)["state"] == "off"


def test_other_policy_shapes_keep_the_option(tmp_path):
    app, clock = make_app(tmp_path)
    set_power(app, AC)
    app.action("/api/adaptive/policy", PLUG)
    app.action("/api/adaptive/policy", {"enabled": False, "game_mode": False, "acknowledged": False})
    assert adaptive(app)["plugged_in_only"] is True and adaptive(app)["enabled"] is True
    app.action("/api/adaptive/policy", {"adaptive_sharing": True, "pause_sharing": False, "acknowledged": True})
    assert adaptive(app)["plugged_in_only"] is True and adaptive(app)["adaptive_sharing"] is True


def test_exact_keys_are_enforced_on_the_new_shape(tmp_path):
    app, clock = make_app(tmp_path)
    for body in ({**PLUG, "foreground_app": "x"}, {**PLUG, "plugged_in_only": 1},
                 {"adaptive_sharing": False, "plugged_in_only": True, "acknowledged": False}):
        with pytest.raises(DemoError):
            app.action("/api/adaptive/policy", body)


def test_a_battery_withdraw_marker_uses_the_same_marker_machinery(tmp_path):
    app, clock = make_app(tmp_path)
    set_power(app, BATTERY)
    app.action("/api/adaptive/policy", PLUG)
    adaptive(app)
    controller = app._adaptive_runtime.controller
    assert controller.status()["reason"] == "host_on_battery"
    controller.restore_withdrawn("host_on_battery")
    assert controller.status()["reason"] in ("host_on_battery",)


def test_page_has_a_plugged_in_only_control_wired_to_the_policy_call():
    from pathlib import Path
    web = Path(ah.__file__).parent / "web"
    html, js = (web / "index.html").read_text(), (web / "app.js").read_text()
    assert 'id="adaptive-plugged"' in html and "Plugged in only" in html
    assert "plugged_in_only" in js and "adaptive-plugged" in js
    assert "on battery" in js.lower()
