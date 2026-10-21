# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV25: the app shows and sets the idle policy and shows the per-session energy estimate."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from mycelium_a13_macos import adaptive_host as ah
from mycelium_a13_macos.app import DemoError
from mycelium_idle import node_link
from mycelium_idle import policy as ip

from test_adaptive_owner import GIB, SAFE, Clock, _APPS, _close_apps, make_app  # noqa: F401

WEB = Path(ah.__file__).parent / "web"


def test_status_carries_the_idle_and_energy_block_with_the_default_setting(tmp_path):
    app, _ = make_app(tmp_path)
    block = app.status()["idle_energy"]
    assert block["policy"]["unload_after_minutes"] == 10
    assert block["member"] is None
    assert block["energy"]["is_estimate"] is True and "estimate" in block["energy"]["text"]


def test_setting_the_idle_policy_persists_and_is_reflected(tmp_path):
    app, _ = make_app(tmp_path)
    out = app.action("/api/idle/policy", {"unload_after_minutes": 30})
    assert out["policy"]["unload_after_minutes"] == 30
    assert ip.read_policy(app.root).unload_after_minutes == 30
    out = app.action("/api/idle/policy", {"unload_after_minutes": "never"})
    assert out["policy"]["unload_after_minutes"] == "never"
    assert app.status()["idle_energy"]["policy"]["unload_after_minutes"] == "never"


@pytest.mark.parametrize("body", [{}, {"unload_after_minutes": 0}, {"unload_after_minutes": "soon"},
                                  {"unload_after_minutes": 10, "extra": 1}, {"unload_after_minutes": True},
                                  {"minutes": 10}])
def test_bad_idle_policy_bodies_are_refused_without_writing(tmp_path, body):
    app, _ = make_app(tmp_path)
    with pytest.raises(DemoError):
        app.action("/api/idle/policy", body)
    assert not (app.root / ip.POLICY_FILENAME).exists()


def test_the_member_tick_publishes_the_policy_and_reports_the_node_status(tmp_path):
    app, _ = make_app(tmp_path)
    runtime_root = tmp_path / "managed-run"
    runtime_root.mkdir(mode=0o700)
    (runtime_root / node_link.STATUS_FILENAME).write_text(
        json.dumps({"state": "idle", "line": "idle (weights unloaded)"}))
    (runtime_root / node_link.STATUS_FILENAME).chmod(0o600)
    app.action("/api/idle/policy", {"unload_after_minutes": 15})
    runtime = SimpleNamespace(root=runtime_root, worker=SimpleNamespace(pid=None))
    app._idle_tick(runtime)
    assert ip.read_policy(runtime_root).unload_after_minutes == 15
    member = app.status()["idle_energy"]["member"]
    assert member["state"] == "idle" and member["line"] == "idle (weights unloaded)"


def test_the_target_authority_receives_the_idle_tick_next_to_the_adaptive_hooks(tmp_path):
    source = (Path(ah.__file__).parent / "app.py").read_text()
    assert source.count("self.authority.idle_tick = self._idle_tick") == 2
    target = (Path(ah.__file__).parent / "target_app.py").read_text()
    assert "idle_tick=getattr(self, 'idle_tick', None)" in target


def test_the_managed_runtime_ticks_the_idle_link_from_its_control_loop():
    from mycelium_a13_macos.managed_runtime import ManagedTargetRuntime
    runtime = object.__new__(ManagedTargetRuntime)
    runtime._stop = threading.Event()
    runtime._adaptive = None
    runtime._adaptive_drain = None
    runtime._member = lambda: None
    runtime._replace_worker_if_dead = lambda: None
    runtime._outbound_retry_backoff = .1
    seen = []
    runtime._idle_tick = lambda rt: seen.append(rt)
    runtime.pump = SimpleNamespace(step=lambda: (runtime._stop.set(), {})[1], wait_for_wake=lambda delay: None)
    runtime._lock = threading.RLock()
    runtime._state = 'running'
    ManagedTargetRuntime._serve(runtime)
    assert seen == [runtime]


def test_an_idle_tick_fault_never_stops_the_control_loop():
    from mycelium_a13_macos.managed_runtime import ManagedTargetRuntime
    runtime = object.__new__(ManagedTargetRuntime)
    runtime._stop = threading.Event()
    runtime._adaptive = None
    runtime._adaptive_drain = None
    runtime._member = lambda: None
    runtime._replace_worker_if_dead = lambda: None
    runtime._outbound_retry_backoff = .1
    runtime._idle_tick = lambda rt: (_ for _ in ()).throw(RuntimeError("boom"))
    steps = []
    runtime.pump = SimpleNamespace(step=lambda: steps.append(1) or runtime._stop.set() or {},
                                   wait_for_wake=lambda delay: None)
    runtime._lock = threading.RLock()
    runtime._state = 'running'
    ManagedTargetRuntime._serve(runtime)
    assert steps == [1] and runtime._state == 'running'


def test_the_page_shows_the_idle_setting_state_and_energy_estimate():
    html, js = (WEB / "index.html").read_text(), (WEB / "app.js").read_text()
    for element in ('id="idle-minutes"', 'id="idle-line"', 'id="energy-line"'):
        assert element in html
    assert 'value="never"' in html and 'value="10"' in html
    assert "/api/idle/policy" in js and "idle_energy" in js
    assert "(estimate)" in js or "estimate" in html.lower()
