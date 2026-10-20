"""PV9 at the app: pick Interactive or Batch / overnight, queue jobs, shown in /api/status and the page."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from mycelium_a13_macos import adaptive_host as ah
from mycelium_a13_macos import service_mode_owner as smo
from mycelium_a13_macos.app import DemoError
from mycelium_router import service_modes as sm

from test_adaptive_owner import ON, _close_apps, adaptive, make_app  # noqa: F401

WEB = Path(smo.__file__).parent / "web"
ORIGIN = "http://127.0.0.1:43111"


class FakeClient:
    """Stands in for the loopback product API client; records the QoS each request asked for."""

    calls: list = []
    fail_with: str | None = None

    def __init__(self, origin):
        self.origin = origin

    def stream(self, prompt, max_new_tokens, on_token, qos_class="interactive"):
        FakeClient.calls.append({"prompt": prompt, "max_new_tokens": max_new_tokens, "qos_class": qos_class})
        if FakeClient.fail_with:
            return {"terminal": "failed", "code": FakeClient.fail_with}
        for piece in ("ans", "wer"):
            on_token(piece)
        return {"terminal": "completed", "code": None}


@pytest.fixture(autouse=True)
def _reset_client():
    FakeClient.calls = []
    FakeClient.fail_with = None


def serving(app, state="serving"):
    app.owner_run = SimpleNamespace(status=lambda: {
        "state": state, "registry_origin": ORIGIN, "run_id": "r1", "participants": [], "step": "serving"})
    app._batch_client_factory = FakeClient
    app._batch_spawn = lambda fn: fn()  # run inline: deterministic


def mode(app):
    return app.status()["service_mode"]


def test_default_is_interactive_with_one_sentence_and_nothing_written(tmp_path):
    app, _ = make_app(tmp_path)
    document = mode(app)
    assert document["mode"] == "interactive" and document["label"] == "Interactive"
    assert document["knobs"]["live_hop_collection_window_ms"] == 2
    assert "2 ms" in document["sentence"]
    assert document["queue"]["counts"] == {"queued": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0}
    assert document["batch_hold"] == "interactive_mode"
    assert not (app.root / sm.SERVICE_MODE_FILENAME).exists()  # reading status never writes


def test_choosing_batch_persists_shows_the_knobs_and_reaches_local_nodes(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path)
    node_a, node_b = tmp_path / "na", tmp_path / "nb"
    for node in (node_a, node_b):
        node.mkdir(mode=0o700)
    monkeypatch.setattr(ah, "capacity_roots", lambda _app: [node_a, node_b])
    result = app.action("/api/service-mode", {"mode": "batch"})
    assert result["mode"] == "batch" and result["applied_to_local_nodes"] == 2
    document = mode(app)
    assert document["label"] == "Batch / overnight"
    assert document["knobs"]["live_hop_collection_window_ms"] == 46
    assert document["knobs"]["accepts_queued_batch_jobs"] is True
    assert "46 ms" in document["sentence"]
    assert document["kept_at_today"] == list(sm.KEPT_AT_TODAY)
    assert (app.root / sm.SERVICE_MODE_FILENAME).stat().st_mode & 0o777 == 0o600
    for node in (node_a, node_b):
        assert sm.read_service_mode(node) == "batch"
    app.action("/api/service-mode", {"mode": "interactive"})
    assert mode(app)["mode"] == "interactive"
    for node in (node_a, node_b):
        assert sm.read_service_mode(node) == "interactive"


def test_mode_survives_a_restart(tmp_path):
    app, _ = make_app(tmp_path)
    app.action("/api/service-mode", {"mode": "batch"})
    assert smo.current_mode(app) == "batch"
    fresh = SimpleNamespace(root=app.root)
    assert smo.current_mode(fresh) == "batch"


@pytest.mark.parametrize("body", [{}, {"mode": "turbo"}, {"mode": "batch", "x": 1}, {"mode": 1}, [], None, {"mode": None}])
def test_bad_mode_bodies_are_refused_and_change_nothing(tmp_path, body):
    app, _ = make_app(tmp_path)
    with pytest.raises(DemoError):
        app.action("/api/service-mode", body)
    assert mode(app)["mode"] == "interactive"
    assert not (app.root / sm.SERVICE_MODE_FILENAME).exists()


def test_queued_batch_jobs_are_accepted_only_in_batch_mode(tmp_path):
    app, _ = make_app(tmp_path)
    with pytest.raises(DemoError) as caught:
        app.action("/api/batch/submit", {"prompt": "p", "max_new_tokens": 8})
    assert str(caught.value) == "service_mode_batch_required"
    # an interactive-priority request is accepted in either mode
    queued = app.action("/api/batch/submit", {"prompt": "p", "max_new_tokens": 8, "priority": "interactive"})
    assert queued["priority"] == "interactive" and queued["state"] == "queued"
    app.action("/api/service-mode", {"mode": "batch"})
    job = app.action("/api/batch/submit", {"prompt": "later", "max_new_tokens": 8})
    assert job["priority"] == "batch" and job["state"] == "queued"
    assert mode(app)["queue"]["counts"]["queued"] == 2
    listed = app.action("/api/batch/jobs", {})
    assert {row["id"] for row in listed["jobs"]} == {queued["id"], job["id"]}
    assert "later" not in json.dumps(app.status()["service_mode"])  # prompts never ride in status


@pytest.mark.parametrize("body,code", [
    ({"prompt": "p"}, "invalid_request"),
    ({"prompt": "", "max_new_tokens": 8}, "batch_prompt_invalid"),
    ({"prompt": "p", "max_new_tokens": 0}, "batch_max_new_tokens_invalid"),
    ({"prompt": "p", "max_new_tokens": 8, "priority": "urgent"}, "batch_priority_invalid"),
    ({"prompt": "p", "max_new_tokens": 8, "extra": 1}, "invalid_request"),
])
def test_submit_validation_codes(tmp_path, body, code):
    app, _ = make_app(tmp_path)
    app.action("/api/service-mode", {"mode": "batch"})
    with pytest.raises(DemoError) as caught:
        app.action("/api/batch/submit", body)
    assert str(caught.value) == code


def test_batch_job_runs_with_batch_qos_and_the_result_is_delivered_later(tmp_path):
    app, _ = make_app(tmp_path)
    serving(app)
    app.action("/api/service-mode", {"mode": "batch"})
    job = app.action("/api/batch/submit", {"prompt": "write a poem", "max_new_tokens": 12})
    assert smo.service_tick(app) == 1
    assert FakeClient.calls == [{"prompt": "write a poem", "max_new_tokens": 12, "qos_class": "batch"}]
    result = app.action("/api/batch/result", {"id": job["id"]})
    assert result["state"] == "done" and result["result"] == "answer"
    assert mode(app)["queue"]["counts"]["done"] == 1


def test_interactive_priority_job_jumps_queued_batch_jobs_and_asks_for_interactive_qos(tmp_path):
    app, _ = make_app(tmp_path)
    serving(app)
    app.action("/api/service-mode", {"mode": "batch"})
    app.action("/api/batch/submit", {"prompt": "b1", "max_new_tokens": 4})
    app.action("/api/batch/submit", {"prompt": "b2", "max_new_tokens": 4})
    app.action("/api/batch/submit", {"prompt": "now", "max_new_tokens": 4, "priority": "interactive"})
    while smo.service_tick(app):
        pass
    assert [(c["prompt"], c["qos_class"]) for c in FakeClient.calls] == [
        ("now", "interactive"), ("b1", "batch"), ("b2", "batch")]


def test_interactive_priority_job_runs_even_in_interactive_mode(tmp_path):
    app, _ = make_app(tmp_path)
    serving(app)
    app.action("/api/batch/submit", {"prompt": "hi", "max_new_tokens": 4, "priority": "interactive"})
    assert smo.service_tick(app) == 1
    assert FakeClient.calls[0]["qos_class"] == "interactive"


def test_batch_jobs_do_not_run_in_interactive_mode(tmp_path):
    app, _ = make_app(tmp_path)
    serving(app)
    app.action("/api/service-mode", {"mode": "batch"})
    app.action("/api/batch/submit", {"prompt": "b", "max_new_tokens": 4})
    app.action("/api/service-mode", {"mode": "interactive"})
    assert smo.service_tick(app) == 0
    assert FakeClient.calls == [] and mode(app)["batch_hold"] == "interactive_mode"


def test_batch_jobs_wait_while_sharing_is_paused_and_the_status_says_why(tmp_path):
    app, _ = make_app(tmp_path)
    serving(app)
    app.action("/api/service-mode", {"mode": "batch"})
    app.action("/api/batch/submit", {"prompt": "b", "max_new_tokens": 4})
    app.action("/api/adaptive/policy", {"adaptive_sharing": False, "pause_sharing": True, "acknowledged": False})
    assert smo.service_tick(app) == 0 and FakeClient.calls == []
    document = mode(app)
    assert document["batch_hold"] == "sharing_not_allowed"
    assert document["batch_hold_line"] == adaptive(app)["line"]
    assert "you paused sharing" in document["batch_hold_line"]
    # released and fully off again: the queued job runs
    app.action("/api/adaptive/policy", {"adaptive_sharing": False, "pause_sharing": False, "acknowledged": False})
    assert smo.service_tick(app) == 1
    assert mode(app)["batch_hold"] is None


def test_batch_jobs_wait_for_a_route_and_while_a_chat_is_running(tmp_path):
    app, _ = make_app(tmp_path)
    app.action("/api/service-mode", {"mode": "batch"})
    app.action("/api/batch/submit", {"prompt": "b", "max_new_tokens": 4})
    app._batch_client_factory = FakeClient
    app._batch_spawn = lambda fn: fn()
    assert smo.service_tick(app) == 0 and mode(app)["batch_hold"] == "route_not_ready"
    serving(app)
    from mycelium_a13_macos.chat import service as chat_service
    chat = chat_service(app)
    chat._busy = "some-chat"  # an interactive chat is generating right now
    assert smo.service_tick(app) == 0 and mode(app)["batch_hold"] == "interactive_request_running"
    chat._busy = None
    assert smo.service_tick(app) == 1


def test_a_failed_job_keeps_a_bounded_code_and_the_queue_moves_on(tmp_path):
    app, _ = make_app(tmp_path)
    serving(app)
    app.action("/api/service-mode", {"mode": "batch"})
    first = app.action("/api/batch/submit", {"prompt": "a", "max_new_tokens": 4})
    FakeClient.fail_with = "chat_generation_failed"
    smo.service_tick(app)
    FakeClient.fail_with = None
    second = app.action("/api/batch/submit", {"prompt": "b", "max_new_tokens": 4})
    smo.service_tick(app)
    rows = {row["id"]: row for row in app.action("/api/batch/jobs", {})["jobs"]}
    assert rows[first["id"]]["state"] == "failed" and rows[first["id"]]["error"] == "chat_generation_failed"
    assert rows[second["id"]]["state"] == "done"


def test_cancel_a_queued_job(tmp_path):
    app, _ = make_app(tmp_path)
    app.action("/api/service-mode", {"mode": "batch"})
    job = app.action("/api/batch/submit", {"prompt": "a", "max_new_tokens": 4})
    assert app.action("/api/batch/cancel", {"id": job["id"]})["state"] == "cancelled"
    with pytest.raises(DemoError) as caught:
        app.action("/api/batch/cancel", {"id": "nope"})
    assert str(caught.value) == "batch_job_unknown"


def test_concurrency_never_exceeds_the_mode_knob(tmp_path):
    app, _ = make_app(tmp_path)
    serving(app)
    pending = []
    app._batch_spawn = pending.append  # hold the workers: nothing finishes
    app.action("/api/service-mode", {"mode": "batch"})
    for n in range(3):
        app.action("/api/batch/submit", {"prompt": f"b{n}", "max_new_tokens": 4})
    assert smo.service_tick(app) == 1 and smo.service_tick(app) == 0
    assert mode(app)["queue"]["counts"]["running"] == 1 and len(pending) == 1
    pending.pop()()
    assert smo.service_tick(app) == 1


def test_the_page_has_the_toggle_the_sentence_and_the_queue_without_a_new_transport():
    html = (WEB / "index.html").read_text()
    js = (WEB / "app.js").read_text()
    for ident in ("service-mode", "service-mode-interactive", "service-mode-batch", "service-mode-sentence",
                  "service-mode-knobs", "batch-prompt", "batch-submit", "batch-jobs"):
        assert re.search(rf'id="{ident}"', html), ident
    assert "<script>" not in html and "onclick=" not in html
    assert js.count("fetch(") == 1
    for route in ("/api/service-mode", "/api/batch/submit", "/api/batch/cancel", "/api/batch/result"):
        assert route in js
    assert "renderServiceMode" in js
    # the existing Adaptive card keeps its promise text and gains no application wording
    card = html[html.index('id="adaptive"'):html.index('id="adaptive"') + 4000]
    assert "batch" not in card.lower().split("</section>")[0]


def test_registry_client_sends_the_requested_qos():
    from mycelium_a13_macos.chat import RegistryClient
    import inspect
    parameters = inspect.signature(RegistryClient.stream).parameters
    assert parameters["qos_class"].default == "interactive"
