# SPDX-License-Identifier: AGPL-3.0-only
"""R24(2)/R26: manual 'Pause sharing' (wire key game_mode kept, pause_sharing alias) and the
'Adaptive sharing' setting (automatic controller on/off, independent of the manual pause)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mycelium_a13_macos import adaptive_host as ah
from mycelium_a13_macos.app import DemoApp, DemoError

from test_adaptive_owner import GIB, ON, SAFE, Clock, _APPS, _close_apps, adaptive, make_app  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
EXPLAINER = ("Mycelium watches this Mac's memory, CPU, heat and battery. When other apps need resources, "
             "it automatically shares less and pauses sharing. Work in progress may stop; "
             "fast mode cannot recover an answer if a Mac drops. "
             "It resumes once the Mac has been healthy for a while. Your own apps always come first.")


def test_pause_sharing_alias_equivalent_to_game_mode(tmp_path):
    app, _ = make_app(tmp_path)
    app.action("/api/adaptive/policy", ON)
    app.action("/api/adaptive/policy", {"enabled": True, "pause_sharing": True, "acknowledged": True})
    status = adaptive(app)
    assert status["mode"] == "drain" and status["reason"] == "owner_game_mode"
    assert ah.read_adaptive_policy(app.root)["game_mode"] is True
    assert app._resource_check() == "adaptive_host_draining"


def test_both_keys_or_extra_key_rejected(tmp_path):
    app, _ = make_app(tmp_path)
    for body in ({"enabled": True, "game_mode": False, "pause_sharing": False, "acknowledged": True},
                 {"enabled": True, "pause_sharing": False, "acknowledged": True, "foreground_app": "x"},
                 {"enabled": True, "pause_sharing": 1, "acknowledged": True}):
        with pytest.raises(DemoError):
            app.action("/api/adaptive/policy", body)
    assert not (app.root / ah.POLICY_FILENAME).exists()


def test_status_mirrors_pause_sharing_and_game_mode(tmp_path):
    app, _ = make_app(tmp_path)
    app.action("/api/adaptive/policy", {**ON, "game_mode": True})
    status = adaptive(app)
    assert status["game_mode"] is True and status["pause_sharing"] is True
    assert adaptive(make_app(tmp_path / "other")[0])["pause_sharing"] is False


def test_wire_reason_stays_owner_game_mode(tmp_path):
    app, _ = make_app(tmp_path)
    app.action("/api/adaptive/policy", {"adaptive_sharing": False, "pause_sharing": True, "acknowledged": False})
    assert adaptive(app)["reason"] == "owner_game_mode"
    assert "owner_game_mode" in ah._MARKER_REASONS and "pause_sharing" not in ah._MARKER_REASONS


def test_pause_works_with_adaptive_sharing_off_and_never_samples(tmp_path):
    app, _ = make_app(tmp_path)
    app._adaptive_sampler = lambda: pytest.fail("sampled while Adaptive sharing is off")
    app.action("/api/adaptive/policy", {"adaptive_sharing": False, "pause_sharing": True, "acknowledged": False})
    status = adaptive(app)
    assert status["adaptive_sharing"] is False and status["pause_sharing"] is True
    assert status["mode"] == "drain" and status["new_admissions_allowed"] is False
    assert app._resource_check() == "adaptive_host_draining"


def test_off_means_no_automatic_drain_under_critical_memory(tmp_path):
    app, clock = make_app(tmp_path)
    app._adaptive_sampler = lambda: pytest.fail("sampler must not run while Adaptive sharing is off")
    app.action("/api/adaptive/policy", {"adaptive_sharing": False, "pause_sharing": True, "acknowledged": False})
    app.action("/api/adaptive/policy", {"adaptive_sharing": False, "pause_sharing": False, "acknowledged": False})
    clock.now += 500
    status = adaptive(app)  # nothing left that needs the controller: fully off, never sampled
    assert status["state"] == "off" and status["adaptive_sharing"] is False and status["pause_sharing"] is False
    # pause released while a stale pressure state could linger: still no automatic action
    app.action("/api/adaptive/policy", {"adaptive_sharing": False, "pause_sharing": True, "acknowledged": False})
    clock.now += 500
    assert adaptive(app)["reason"] == "owner_game_mode"


def test_on_still_drains_under_critical_memory(tmp_path):
    app, clock = make_app(tmp_path)
    app._adaptive_sampler = lambda: {"observed_at": clock.now, "pressure": "critical", "available_bytes": GIB // 100}
    app.action("/api/adaptive/policy", {"adaptive_sharing": True, "pause_sharing": False, "acknowledged": True})
    assert adaptive(app)["mode"] == "drain" and adaptive(app)["reason"] == "host_memory_pressure"


def test_off_still_honours_pause_sharing_and_stop(tmp_path):
    app, _ = make_app(tmp_path)
    app.action("/api/adaptive/policy", {"adaptive_sharing": False, "pause_sharing": True, "acknowledged": False})
    assert app._resource_check() == "adaptive_host_draining"
    # Stop while only the manual controller exists (Adaptive sharing off) is still enforced
    app.action("/api/adaptive/stop", {})
    assert adaptive(app)["stopped"] is True and adaptive(app)["reason"] in {"owner_game_mode", "owner_stop"}
    app.action("/api/adaptive/policy", {"adaptive_sharing": False, "pause_sharing": False, "acknowledged": False})
    assert adaptive(app)["state"] == "off"


def test_off_never_disables_a13_hard_gates(tmp_path):
    app, _ = make_app(tmp_path)
    app.action("/api/adaptive/policy", {"adaptive_sharing": False, "pause_sharing": True, "acknowledged": False})
    app.action("/api/adaptive/policy", {"adaptive_sharing": False, "pause_sharing": False, "acknowledged": False})
    policy = json.loads((app.root / "resource-policy.json").read_text())
    policy["power"] = "ac_only"
    (app.root / "resource-policy.json").write_text(json.dumps(policy))
    app._policy_observer = lambda: {**SAFE, "power": "battery"}
    assert app._resource_check() == "resource_power_policy_violation"
    app._policy_observer = lambda: {**SAFE, "thermal": "serious"}
    assert app._resource_check() == "resource_thermal_policy_violation"


def test_missing_automatic_key_back_compat(tmp_path):
    app, clock = make_app(tmp_path)
    policy = {k: v for k, v in {**ah.DEFAULT_POLICY, "enabled": True}.items()}
    assert "automatic" not in policy
    ah.write_adaptive_policy(app.root, policy, owner_authorized=True)
    status = adaptive(app)
    assert status["enabled"] is True and status["adaptive_sharing"] is True and status["mode"] == "normal"


def test_default_policy_and_publication_keys_unchanged():
    assert ah.DEFAULT_POLICY["enabled"] is False and "automatic" not in ah.DEFAULT_POLICY
    assert "automatic" not in ah.PUBLICATION_KEYS and "pause_sharing" not in ah.PUBLICATION_KEYS


def test_status_line_reduced_paused_reasons(tmp_path):
    app, clock = make_app(tmp_path)
    app.action("/api/adaptive/policy", {"adaptive_sharing": True, "pause_sharing": False, "acknowledged": True})
    assert adaptive(app)["line"] == "Adaptive sharing: on"
    app._adaptive_sampler = lambda: {"observed_at": clock.now, "pressure": "critical", "available_bytes": 1}
    clock.now += 10
    assert adaptive(app)["line"] == "Adaptive sharing: paused - memory is low"
    app.action("/api/adaptive/policy", {"adaptive_sharing": False, "pause_sharing": True, "acknowledged": False})
    assert adaptive(app)["line"] == "Adaptive sharing: off - you paused sharing"
    assert len(adaptive(app)["line"]) <= 120


def test_status_line_reduced_cpu(tmp_path):
    app, clock = make_app(tmp_path)
    ah.write_adaptive_policy(app.root, {**ah.DEFAULT_POLICY, "enabled": True, "cpu_saturation_fraction": 0.5,
                                        "timings": {"cpu_sustain_s": 5, "sample_interval_s": 1,
                                                    "stale_after_s": 30}}, owner_authorized=True)
    app._adaptive_sampler = lambda: {"observed_at": clock.now, "pressure": "normal", "available_bytes": 8 * GIB,
                                     "cpu_utilization": 0.99}
    adaptive(app)
    clock.now += 6
    adaptive(app)
    clock.now += 6
    assert adaptive(app)["line"] == "Adaptive sharing: reduced - the CPU is busy"


# ---- first-consent default ON -------------------------------------------------------------------
CONSENT = {"peer_visibility": True, "power": "battery_allowed", "storage_bytes": 10**9, "network": "metered_allowed",
           "background": False, "diagnostics": False, "thermal": "stop_on_serious"}


def fresh_owner(tmp_path):
    app = DemoApp(tmp_path / "app", check_platform=False).start()
    _APPS.append(app)
    return app


def test_fresh_consent_enables_adaptive_sharing_by_default(tmp_path):
    app = fresh_owner(tmp_path)
    app._adaptive_sampler = lambda: pytest.fail("no sampling before consent")
    assert adaptive(app)["enabled"] is False
    app._adaptive_sampler = lambda: {"observed_at": 1000.0, "pressure": "normal", "available_bytes": 8 * GIB}
    app._policy_observer = lambda: dict(SAFE)
    app.action("/api/create", {"consent": CONSENT, "adaptive_sharing": True})
    policy = ah.read_adaptive_policy(app.root)
    assert policy["enabled"] is True and policy["game_mode"] is False
    assert adaptive(app)["adaptive_sharing"] is True


def test_consent_without_tick_leaves_off(tmp_path):
    app = fresh_owner(tmp_path)
    app.action("/api/create", {"consent": CONSENT, "adaptive_sharing": False})
    assert adaptive(app)["enabled"] is False
    app2 = fresh_owner(tmp_path / "b")
    app2.action("/api/create", {"consent": CONSENT})  # legacy body (A16 harness): untouched
    assert not (app2.root / ah.POLICY_FILENAME).exists()


def test_existing_policy_file_not_flipped(tmp_path):
    app = fresh_owner(tmp_path)
    ah.write_adaptive_policy(app.root, {**ah.DEFAULT_POLICY, "reserve_bytes": GIB}, owner_authorized=True)
    app.action("/api/create", {"consent": CONSENT, "adaptive_sharing": True})
    policy = ah.read_adaptive_policy(app.root)
    assert policy["enabled"] is False and policy["reserve_bytes"] == GIB


def test_adaptive_sharing_must_be_bool(tmp_path):
    app = fresh_owner(tmp_path)
    with pytest.raises(DemoError):
        app.action("/api/create", {"consent": CONSENT, "adaptive_sharing": "yes"})


# ---- user-visible copy ---------------------------------------------------------------------------
def test_no_user_visible_game_mode_string():
    import re
    files = [ROOT / "mycelium_a13_macos/web" / n for n in ("index.html", "app.js", "swarm.js", "run.js")]
    files += sorted((ROOT / "docs/user").glob("*.md"))
    for path in files:
        for line_no, line in enumerate(path.read_text().splitlines(), 1):
            visible = re.sub(r"owner_game_mode|game_mode|a16_game_mode_two_mac", "", line)
            assert not re.search(r"(?i)game[ -]mode", visible), f"{path.name}:{line_no}"


def test_explainer_text_in_ui_and_privacy_doc():
    html = (ROOT / "mycelium_a13_macos/web/index.html").read_text().replace("&#39;", "'").replace("’", "'")
    assert EXPLAINER.replace("Mac's", "Mac's") in html
    assert "Adaptive sharing" in html and "Pause sharing" in html
    assert "Adaptive sharing" in (ROOT / "docs/user/PRIVACY.md").read_text()


def test_ui_states_what_off_means():
    html = (ROOT / "mycelium_a13_macos/web/index.html").read_text()
    assert "memory limit" in html.lower() and "disk" in html.lower() and "Stop" in html
