# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit API-only (headless) registry serving; no model execution.

These tests compose the real production HTTP supervisor on loopback with
temporary ports.  Static/UI mode defaults are preserved; ``api_only`` is an
explicit opt-in that removes the static ``index.html`` requirement while
serving the existing request/status/stream routes.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

import mycelium_live.supervisor as supervisor
from tests.a13_macos.test_serving import _free_dynamic_port, _operator_plan, _prepared, _approval, seed  # noqa: F401


# ---------------------------------------------------------------------------
# Model-free production composition helpers (same pattern as
# tests/a13_macos/test_registry_app_lifecycle.py).
# ---------------------------------------------------------------------------


def _compose_registry(monkeypatch, tmp_path: Path, *, api_only: bool):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text(
        "<html>lifecycle fixture, not product build</html>", encoding="utf-8"
    )
    (tmp_path / "product").mkdir(mode=0o700)
    monkeypatch.setattr(
        supervisor,
        "native_deployment_projection",
        lambda plan: SimpleNamespace(
            deployment_id="fixture",
            model_id="fixture-model",
            model_revision="fixture-revision",
            quantization="int8-weight-only",
            topology_size=2,
        ),
    )

    def unavailable(*args, **kwargs):
        raise ValueError("fixture_model_execution_denied")

    monkeypatch.setattr(supervisor, "_qualified_runtime", unavailable)
    monkeypatch.setattr(
        supervisor.signal,
        "signal",
        lambda *args: pytest.fail(
            "application worker must not install global signal handlers"
        ),
    )
    stop = threading.Event()
    ready = threading.Event()
    started: list = []
    outcomes: list = []

    def observed(document):
        started.append(document)
        ready.set()

    kwargs = dict(
        operator_plans=[tmp_path / "fixture-plan"],
        host="127.0.0.1",
        port=0,
        static_root=static,
        seed_state_root=tmp_path / "seed",
        product_state_root=tmp_path / "product",
        stop_event=stop,
        on_started=observed,
        # This fixture checks HTTP/UI mode, not repository audit timing. The
        # production default and the dedicated governance suites stay intact.
        governance_root=None,
    )

    def run():
        try:
            outcomes.append(
                supervisor.run_registry_server(**kwargs, api_only=api_only)
            )
        except BaseException as exc:  # noqa: BLE001
            outcomes.append(exc)
            ready.set()

    return stop, ready, started, outcomes, run


def _get(url: str, timeout: int = 3) -> tuple[int, dict | str]:
    try:
        with urlopen(url, timeout=timeout) as response:
            body = response.read()
            status = response.status
    except HTTPError as error:
        body = error.read()
        status = error.code
    text = body.decode("utf-8")
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, text


# ---------------------------------------------------------------------------
# create_server boundary
# ---------------------------------------------------------------------------


def test_create_server_api_only_skips_static_index_requirement(tmp_path: Path):
    static = tmp_path / "static"
    static.mkdir(exist_ok=True)
    # No index.html anywhere in the static root.
    server = supervisor.create_server(
        app=SimpleNamespace(),
        route=SimpleNamespace(),
        static_root=static,
        host="127.0.0.1",
        port=0,
        api_only=True,
    )
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_create_server_ui_default_still_requires_index_html(tmp_path: Path):
    static = tmp_path / "static"
    static.mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="live_ui_build_missing"):
        supervisor.create_server(
            app=SimpleNamespace(),
            route=SimpleNamespace(),
            static_root=static,
            host="127.0.0.1",
            port=0,
        )


def test_create_server_rejects_non_loopback_even_when_api_only(tmp_path: Path):
    static = tmp_path / "static"
    static.mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="live_mvp_requires_loopback"):
        supervisor.create_server(
            app=SimpleNamespace(),
            route=SimpleNamespace(),
            static_root=static,
            host="0.0.0.0",
            port=0,
            api_only=True,
        )


# ---------------------------------------------------------------------------
# Full run_registry_server composition (threaded, model-free)
# ---------------------------------------------------------------------------


def test_headless_http_fixture_does_not_run_checkout_governance(tmp_path, monkeypatch):
    def forbidden_governance(*args, **kwargs):
        raise AssertionError("HTTP mode fixture must not run unrelated repository audits")

    monkeypatch.setattr(supervisor, "governance_readiness", forbidden_governance)
    stop, ready, started, outcomes, run = _compose_registry(monkeypatch, tmp_path, api_only=True)
    stop.set()
    run()
    assert outcomes == [0]
    assert ready.is_set() and len(started) == 1


def test_registry_server_api_only_serves_api_and_rejects_static(tmp_path, monkeypatch):
    stop, ready, started, outcomes, run = _compose_registry(
        monkeypatch, tmp_path, api_only=True
    )
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert ready.wait(10), "api-only server did not reach terminal startup outcome"
        document = started[0]
        assert document["api_only"] is True
        base = document["url"]
        # Existing request/status routes must still be served.
        status, bootstrap = _get(base + "api/v1/bootstrap")
        assert status == 200, repr(bootstrap)
        assert bootstrap["protocol"] == "mycelium.product_ui.bootstrap.v1"
        status, live = _get(base + "__mycelium/live-status")
        assert status == 200, repr(live)
        # The registry serves its own public_status projection.
        assert live["protocol"] == "mycelium.live_route_status.v1"
        # Registry projection (unavailable deployment recorded, not hidden).
        status, deployments = _get(base + "__mycelium/deployments")
        assert status == 200, repr(deployments)
        # Static routes return a safe JSON error, never placeholder HTML.
        for path in ("/", "/index.html", "/assets/app.js"):
            status, body = _get(base.rstrip("/") + path)
            assert status == 404, f"{path}: {status} {body!r}"
            assert body == {"error": "static_content_disabled"}
    finally:
        stop.set()
        worker.join(5)
    assert not worker.is_alive()
    assert outcomes == [0]


def test_registry_server_ui_default_still_serves_index(tmp_path, monkeypatch):
    stop, ready, started, outcomes, run = _compose_registry(
        monkeypatch, tmp_path, api_only=False
    )
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert ready.wait(10), "ui server did not reach terminal startup outcome"
        document = started[0]
        assert document["api_only"] is False
        base = document["url"]
        status, body = _get(base.rstrip("/") + "/")
        assert status == 200, repr(body)
        assert "lifecycle fixture" in body
    finally:
        stop.set()
        worker.join(5)
    assert not worker.is_alive()
    assert outcomes == [0]


# ---------------------------------------------------------------------------
# RegistryLaunch.run threading from core
# ---------------------------------------------------------------------------


def test_registry_launch_run_threads_api_only_to_runner(tmp_path: Path, seed):  # noqa: F811
    prepared = _prepared(tmp_path)
    plan = _operator_plan(tmp_path, prepared, seed=seed)
    calls: list = []

    def runner(**kwargs):
        calls.append(kwargs)
        return 5

    launch = __import__("mycelium_a13_macos.serving", fromlist=["prepare_registry_launch"]).prepare_registry_launch(
        prepared=prepared,
        operator_plan=plan,
        private_state_root=tmp_path / "private",
        seed_state_root=seed._seed_root,
        runner=runner,
    )
    port = _free_dynamic_port()
    assert launch.run(approval=_approval(launch), port=port, api_only=True) == 5
    assert calls[0]["api_only"] is True
    assert calls[0]["port"] == port
    # Default remains UI mode: api_only must be absent, not False.
    assert launch.run(approval=_approval(launch), port=port) == 5
    assert "api_only" not in calls[1]
    # Invalid value fails closed before the runner is called.
    with pytest.raises(Exception, match="api_only_invalid"):
        launch.run(approval=_approval(launch), port=port, api_only="yes")
    assert len(calls) == 2
