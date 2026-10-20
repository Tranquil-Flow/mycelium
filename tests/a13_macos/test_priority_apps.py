"""PV3 Priority apps: while a listed app runs, sharing pauses through the existing Pause sharing path.

Opt-in only: nothing looks at which apps run until the owner lists at least one and acknowledges it.
The wire keeps the reason ``owner_game_mode`` and never carries an app name; the name appears only in
the local one-line status ``Adaptive sharing: paused - <app> is running``.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from mycelium_a13_macos import adaptive_host as ah
from mycelium_a13_macos import priority_apps as pa
from mycelium_a13_macos.app import DemoError
from mycelium_router import adaptive_overlay as ov

from test_adaptive_host import Clock as HostClock, make, settle_authority, tick  # noqa: F401
from test_adaptive_owner import GIB, _close_apps, adaptive, make_app  # noqa: F401

ROOT = Path(pa.__file__).resolve().parents[1]
WEB = Path(pa.__file__).parent / "web"


class Fake:
    """A priority-app observer double: ``running`` is a list of (bundle_id, name, frontmost)."""

    platform = "fake"
    supported = True

    def __init__(self, running=()):
        self.running = list(running)
        self.calls = 0
        self.boom = False

    def running_apps(self):
        self.calls += 1
        if self.boom:
            raise RuntimeError("observer broke")
        return [pa.AppObservation(*row) for row in self.running]


ZOOM = ("us.zoom.xos", "zoom.us", False)
XCODE = ("com.apple.dt.Xcode", "Xcode", True)


def setup_app(tmp_path, apps=("zoom.us",), scope="running", observer=None):
    app, clock = make_app(tmp_path)
    observer = observer or Fake()
    app._priority_observer = observer
    if apps is not None:
        app.action("/api/priority-apps", {"apps": list(apps), "scope": scope, "acknowledged": True})
    return app, clock, observer


def tick_app(app, clock, seconds=3.0):
    clock.now += seconds
    return adaptive(app)


# ------------------------------------------------------------------ policy file
def test_policy_defaults_to_no_apps_and_roundtrips_privately(tmp_path):
    assert pa.read_policy(tmp_path) == {"schema": 1, "apps": [], "scope": "running"}
    assert pa.has_apps(tmp_path) is False
    pa.write_policy(tmp_path, {"schema": 1, "apps": ["zoom.us", " Final Cut Pro ", "ZOOM.US"], "scope": "frontmost"})
    assert (tmp_path / pa.POLICY_FILENAME).stat().st_mode & 0o777 == 0o600
    policy = pa.read_policy(tmp_path)
    assert policy == {"schema": 1, "apps": ["zoom.us", "Final Cut Pro"], "scope": "frontmost"}  # trimmed, deduped (case-insensitive)
    assert pa.has_apps(tmp_path) is True


@pytest.mark.parametrize("policy", [
    {"schema": 1, "apps": "zoom.us", "scope": "running"},
    {"schema": 1, "apps": [1], "scope": "running"},
    {"schema": 1, "apps": [""], "scope": "running"},
    {"schema": 1, "apps": ["a\nb"], "scope": "running"},
    {"schema": 1, "apps": ["x" * 81], "scope": "running"},
    {"schema": 1, "apps": [f"app{n}" for n in range(21)], "scope": "running"},
    {"schema": 1, "apps": ["a"], "scope": "always"},
    {"schema": 2, "apps": ["a"], "scope": "running"},
    {"schema": 1, "apps": ["a"], "scope": "running", "extra": 1},
    {"schema": 1, "apps": ["a"]},
])
def test_invalid_policies_are_refused(tmp_path, policy):
    with pytest.raises(pa.PriorityAppsError, match="priority_apps_invalid"):
        pa.write_policy(tmp_path, policy)
    assert not (tmp_path / pa.POLICY_FILENAME).exists()


def test_unsafe_or_corrupt_policy_file_reads_as_error_and_has_apps_is_false(tmp_path):
    path = tmp_path / pa.POLICY_FILENAME
    for raw, mode in ((b"{", 0o600), (b'{"schema":1,"apps":["a"],"scope":"running"}', 0o644)):
        path.write_bytes(raw)
        path.chmod(mode)
        with pytest.raises(pa.PriorityAppsError, match="priority_apps_invalid"):
            pa.read_policy(tmp_path)
        assert pa.has_apps(tmp_path) is False
        path.unlink()


# ------------------------------------------------------------------ matching
def obs(bundle=None, name=None, front=False):
    return pa.AppObservation(bundle, name, front)


def test_match_is_exact_on_bundle_id_or_name_case_insensitively_never_a_substring():
    policy = {"schema": 1, "apps": ["us.zoom.xos", "Final Cut Pro", "Code"], "scope": "running"}
    assert pa.match(policy, [obs("us.zoom.xos", "zoom.us")]) == "zoom.us"
    assert pa.match(policy, [obs("US.ZOOM.XOS", None)]) == "US.ZOOM.XOS"  # no name known: the id is shown
    assert pa.match(policy, [obs(None, "final cut pro")]) == "final cut pro"
    assert pa.match(policy, [obs(None, "Final Cut Pro.app")]) == "Final Cut Pro.app".removesuffix(".app")
    assert pa.match(policy, [obs("com.apple.dt.Xcode", "Xcode")]) is None  # "Code" is not a substring match
    assert pa.match(policy, []) is None
    assert pa.match({"schema": 1, "apps": [], "scope": "running"}, [obs("us.zoom.xos", "zoom.us")]) is None


def test_frontmost_scope_matches_only_the_frontmost_app():
    policy = {"schema": 1, "apps": ["Xcode"], "scope": "frontmost"}
    assert pa.match(policy, [obs(None, "Xcode", False)]) is None
    assert pa.match(policy, [obs(None, "Xcode", True)]) == "Xcode"
    assert pa.match({**policy, "scope": "running"}, [obs(None, "Xcode", False)]) == "Xcode"


def test_the_name_shown_is_bounded_printable_text():
    policy = {"schema": 1, "apps": ["evil"], "scope": "running"}
    shown = pa.match(policy, [obs(None, "evil", False)])
    assert shown == "evil"
    long = pa.match({"schema": 1, "apps": ["x" * 80], "scope": "running"}, [obs(None, "x" * 80, False)])
    assert len(long) <= 40 and re.fullmatch(r"[ -~]+…?", long)


# ------------------------------------------------------------------ observers
class FakeNSApp:
    def __init__(self, bundle, name, active, policy=0):
        self._b, self._n, self._a, self._p = bundle, name, active, policy

    def bundleIdentifier(self):  # noqa: N802 - Cocoa spelling
        return self._b

    def localizedName(self):  # noqa: N802
        return self._n

    def isActive(self):  # noqa: N802
        return self._a

    def activationPolicy(self):  # noqa: N802
        return self._p


def test_macos_observer_reads_ns_workspace_regular_apps_only():
    workspace = type("W", (), {"runningApplications": lambda self: [
        FakeNSApp("us.zoom.xos", "zoom.us", False), FakeNSApp("com.apple.Dock", "Dock", False, policy=1),
        FakeNSApp("com.apple.dt.Xcode", "Xcode", True)]})()
    observer = pa.MacOSObserver(workspace=lambda: workspace)
    rows = observer.running_apps()
    assert [(r.bundle_id, r.name, r.frontmost) for r in rows] == [
        ("us.zoom.xos", "zoom.us", False), ("com.apple.dt.Xcode", "Xcode", True)]
    assert observer.platform == "macos" and observer.supported is True


def test_macos_observer_falls_back_to_ps_when_ns_workspace_is_unavailable():
    def no_workspace():
        raise ImportError("no AppKit")

    seen = []

    def runner(argv):
        seen.append(argv)
        return ("/sbin/launchd\n/Applications/zoom.us.app/Contents/MacOS/zoom.us\n"
                "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal\n/usr/bin/ssh\n")

    observer = pa.MacOSObserver(workspace=no_workspace, runner=runner)
    rows = observer.running_apps()
    assert [(r.bundle_id, r.name, r.frontmost) for r in rows] == [(None, "zoom.us", False), (None, "Terminal", False)]
    assert seen == [["ps", "-axo", "comm="]]


def test_macos_observer_errors_propagate_as_a_bounded_error():
    def boom(_argv):
        raise OSError("ps gone")

    observer = pa.MacOSObserver(workspace=lambda: (_ for _ in ()).throw(ImportError()), runner=boom)
    with pytest.raises(pa.PriorityAppsError, match="priority_apps_observer_failed"):
        observer.running_apps()


def test_linux_and_windows_observers_are_stubs_with_the_same_interface():
    for cls, platform in ((pa.LinuxObserver, "linux"), (pa.WindowsObserver, "windows")):
        observer = cls()
        assert observer.platform == platform and observer.supported is False
        assert observer.running_apps() == []
    assert isinstance(pa.observer_for_platform("darwin"), pa.MacOSObserver)
    assert isinstance(pa.observer_for_platform("linux"), pa.LinuxObserver)
    assert isinstance(pa.observer_for_platform("win32"), pa.WindowsObserver)
    assert pa.observer_for_platform("plan9").supported is False


# ------------------------------------------------------------------ controller (Pause sharing path)
def test_a_priority_app_drains_through_the_pause_sharing_path_with_the_pause_reason():
    ctl, clock, _ = make()
    tick(ctl, clock)
    ctl.set_priority_app("zoom.us")
    tick(ctl, clock)
    assert ctl.mode == "drain" and ctl.reason == "owner_game_mode"  # same wire reason as Pause sharing
    assert ctl.priority_app == "zoom.us"
    assert ctl.admission() == "adaptive_host_draining"
    assert "zoom" not in json.dumps(ctl.publication()) + json.dumps(ctl.status())
    assert "zoom" not in json.dumps(ctl.overlay(wall_now=1.0))


def test_leaving_needs_the_app_gone_then_the_ordinary_calm_period():
    ctl, clock, _ = make()
    tick(ctl, clock)
    ctl.set_priority_app("zoom.us")
    tick(ctl, clock)
    for _ in range(80):
        tick(ctl, clock)
        settle_authority(ctl, clock)
    assert ctl.mode in {"drain", "withdraw"}  # still running: never recovers
    ctl.set_priority_app(None)
    tick(ctl, clock)
    assert ctl.mode in {"drain", "withdraw"}  # recovery window not yet elapsed
    for _ in range(80):
        tick(ctl, clock)
        settle_authority(ctl, clock)
        if ctl.mode == "normal":
            break
        if ctl.mode == "withdraw":
            ctl.mark_requalified(ctl.generation)
    assert ctl.mode == "normal" and ctl.priority_app is None


def test_switching_the_controller_off_forgets_the_app():
    ctl, clock, _ = make()
    tick(ctl, clock)
    ctl.set_priority_app("zoom.us")
    tick(ctl, clock)
    ctl.apply_policy(dict(ah.DEFAULT_POLICY))
    assert ctl.mode == "normal" and ctl.priority_app is None


# ------------------------------------------------------------------ the app
def test_listing_apps_needs_acknowledgement_and_exact_keys(tmp_path):
    app, _ = make_app(tmp_path)
    app._priority_observer = Fake()
    good = {"apps": ["zoom.us"], "scope": "running", "acknowledged": True}
    for body in ({**good, "acknowledged": False}, {"apps": ["zoom.us"], "scope": "running"}, {**good, "extra": 1},
                 {**good, "acknowledged": 1}, {**good, "scope": "always"}, {**good, "apps": "zoom.us"},
                 {**good, "apps": ["x" * 81]}, [], None):
        with pytest.raises(DemoError):
            app.action("/api/priority-apps", body)
    assert not (app.root / pa.POLICY_FILENAME).exists()
    assert app._priority_observer.calls == 0
    result = app.action("/api/priority-apps", good)
    assert result["apps"] == ["zoom.us"] and result["scope"] == "running"
    assert (app.root / pa.POLICY_FILENAME).stat().st_mode & 0o777 == 0o600
    # clearing the list needs no acknowledgement
    cleared = app.action("/api/priority-apps", {"apps": [], "scope": "running", "acknowledged": False})
    assert cleared["apps"] == [] and not pa.has_apps(app.root)


def test_nothing_looks_at_running_apps_until_the_owner_lists_one(tmp_path):
    app, clock, observer = setup_app(tmp_path, apps=None)
    for _ in range(3):
        tick_app(app, clock)
        app._resource_check()
        app.status()
    assert observer.calls == 0
    assert app.status()["priority_apps"]["apps"] == [] and app.status()["priority_apps"]["active"] is None
    assert adaptive(app)["state"] == "off"


def test_a_running_priority_app_pauses_sharing_with_the_plain_status_line(tmp_path):
    app, clock, observer = setup_app(tmp_path)
    assert adaptive(app)["state"] != "drain"  # nothing running yet
    assert app._resource_check() is None
    observer.running = [ZOOM, XCODE]
    status = tick_app(app, clock)
    assert status["line"] == "Adaptive sharing: paused - zoom.us is running"
    assert status["mode"] == "drain" and status["reason"] == "owner_game_mode"
    assert status["pause_sharing"] is False and status["adaptive_sharing"] is False  # the owner's switches are untouched
    assert status["new_admissions_allowed"] is False
    assert app._resource_check() == "adaptive_host_draining"
    block = app.status()["priority_apps"]
    assert block["active"] == "zoom.us" and block["apps"] == ["zoom.us"] and block["supported"] is True
    assert "zoom.us is running" in block["sentence"]


def test_the_app_name_never_reaches_the_publication_or_the_node_overlay(tmp_path):
    app, clock, observer = setup_app(tmp_path)
    observer.running = [ZOOM]
    tick_app(app, clock)
    publication = app.status()["adaptive_host_publication"]
    overlay = app._adaptive_overlay(wall_clock=lambda: 5000.0)
    assert set(publication) == ah.PUBLICATION_KEYS and set(overlay) == ov.OVERLAY_KEYS
    assert overlay["reason"] == "owner_game_mode" and overlay["admit"] is False
    assert "zoom" not in (json.dumps(publication) + json.dumps(overlay)).lower()


def test_when_the_app_quits_the_status_says_so_and_waits_for_the_calm_period(tmp_path):
    app, clock, observer = setup_app(tmp_path)
    observer.running = [ZOOM]
    tick_app(app, clock)
    observer.running = []
    status = tick_app(app, clock)
    assert status["mode"] in {"drain", "withdraw"}
    assert status["line"] == "Adaptive sharing: paused - zoom.us was running"
    assert app.status()["priority_apps"]["active"] is None


def test_a_manual_pause_keeps_its_own_wording(tmp_path):
    app, clock, observer = setup_app(tmp_path)
    observer.running = [ZOOM]
    app.action("/api/adaptive/policy", {"adaptive_sharing": False, "pause_sharing": True, "acknowledged": False})
    status = tick_app(app, clock)
    assert status["pause_sharing"] is True and "you paused sharing" in status["line"]
    assert "zoom" not in status["line"]


def test_frontmost_scope_pauses_only_while_the_listed_app_is_in_front(tmp_path):
    app, clock, observer = setup_app(tmp_path, apps=("Xcode",), scope="frontmost")
    observer.running = [("com.apple.dt.Xcode", "Xcode", False)]
    assert tick_app(app, clock)["mode"] == "normal"
    observer.running = [("com.apple.dt.Xcode", "Xcode", True)]
    assert tick_app(app, clock)["line"] == "Adaptive sharing: paused - Xcode is running"


def test_a_broken_observer_does_not_pause_and_is_reported(tmp_path):
    app, clock, observer = setup_app(tmp_path)
    observer.boom = True
    status = tick_app(app, clock)
    assert status["mode"] == "normal"
    block = app.status()["priority_apps"]
    assert block["active"] is None and block["observer_error"] == "priority_apps_observer_failed"


def test_clearing_the_list_releases_the_hold_and_goes_fully_off(tmp_path):
    app, clock, observer = setup_app(tmp_path)
    observer.running = [ZOOM]
    assert tick_app(app, clock)["mode"] == "drain"
    app.action("/api/priority-apps", {"apps": [], "scope": "running", "acknowledged": False})
    status = tick_app(app, clock)
    assert status["state"] == "off" and status["line"] == "Adaptive sharing: off"
    calls = observer.calls
    tick_app(app, clock)
    assert observer.calls == calls


def test_the_existing_adaptive_policy_file_is_never_rewritten_by_priority_apps(tmp_path):
    app, clock, observer = setup_app(tmp_path)
    observer.running = [ZOOM]
    tick_app(app, clock)
    assert not (app.root / ah.POLICY_FILENAME).exists()
    app.action("/api/adaptive/policy", {"adaptive_sharing": True, "pause_sharing": False, "acknowledged": True})
    assert ah.read_adaptive_policy(app.root)["enabled"] is True
    app.action("/api/adaptive/policy", {"adaptive_sharing": False, "pause_sharing": False, "acknowledged": False})
    assert ah.read_adaptive_policy(app.root)["enabled"] is False  # file says off ...
    assert tick_app(app, clock)["mode"] in {"drain", "withdraw"}  # ... a running priority app still holds


def test_queued_batch_jobs_wait_while_a_priority_app_runs(tmp_path):
    from mycelium_a13_macos import service_mode_owner as smo
    app, clock, observer = setup_app(tmp_path)
    app.action("/api/service-mode", {"mode": "batch"})
    observer.running = [ZOOM]
    tick_app(app, clock)
    code, line = smo.batch_hold(app)
    assert code == "sharing_not_allowed" and line == "Adaptive sharing: paused - zoom.us is running"


def test_the_page_has_a_priority_apps_card_outside_the_adaptive_card_and_the_privacy_text_is_honest():
    html = (WEB / "index.html").read_text()
    js = (WEB / "app.js").read_text()
    for ident in ("priority-apps", "priority-apps-list", "priority-apps-scope", "priority-apps-ack",
                  "priority-apps-save", "priority-apps-line"):
        assert re.search(rf'id="{ident}"', html), ident
    adaptive_card = html[html.index('<section id="adaptive"'):]
    adaptive_card = adaptive_card[:adaptive_card.index("</section>")]
    assert "priority-apps" not in adaptive_card
    assert "never which apps you run" not in html
    assert "unless you list Priority apps" in adaptive_card
    assert js.count("fetch(") == 1 and "/api/priority-apps" in js and "renderPriorityApps" in js
    for doc in ("PRIVACY.md", "WHAT-MYCELIUM-DOES.md"):
        assert "Priority apps" in (ROOT / "docs" / "user" / doc).read_text(), doc
