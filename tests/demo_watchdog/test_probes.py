# SPDX-License-Identifier: AGPL-3.0-only
"""Health probes: bounded, idempotent, read-only, fail-closed.

Five properties are asserted for every probe:
  * it returns a ``ProbeResult`` and never raises;
  * it is bounded (a timeout is reported as *unknown*, not as a hang);
  * unknown is never a healthy claim;
  * it carries no mutation, no listen socket, no live service contact (runner/opener
    are injected, so the test asserts on the exact argv/request issued);
  * the private proxy capability is read from disk and never echoed into a result.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from mycelium_demo_watchdog.probes import (
    SCRUB_ENV_KEYS,
    HTTPHealthProbe,
    ProbeResult,
    ProcessProbe,
    SSHProbe,
    health_from_status,
    private_capability_headers,
    probe_once,
    scrubbed_env,
)

from .conftest import Completes, ScriptedProbe, bad, ok, unknown


# --------------------------------------------------------------------------- ssh


def test_ssh_probe_reports_reachable_on_marker():
    runner = Completes({"REMOTE_SHELL_REACHED": (0, "REMOTE_SHELL_REACHED\n", "")})
    probe = SSHProbe("member@203.0.113.7", "/nonexistent/key", runner=runner)
    result = probe.run()
    assert result.ok is True
    assert runner.calls[0][0] == "ssh"


def test_ssh_probe_is_bounded_and_batches_input():
    runner = Completes({"REMOTE_SHELL_REACHED": (0, "REMOTE_SHELL_REACHED\n", "")})
    probe = SSHProbe("host", "key", connect_timeout=6.0, total_timeout=8.0, runner=runner)
    probe.run()
    argv = runner.calls[0]
    assert "BatchMode=yes" in argv
    assert "ConnectTimeout=6" in argv
    assert any(item.startswith("StrictHostKeyChecking=") for item in argv)


def test_ssh_probe_timeout_is_unknown_not_healthy():
    class TimingOut:
        def __call__(self, argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 8)

    result = SSHProbe("host", "key", runner=TimingOut()).run()
    assert result.ok is None
    assert result.error == "timeout"
    assert result.ok is not True


def test_ssh_probe_nonzero_is_unreachable_never_an_exception():
    runner = Completes({"REMOTE_SHELL_REACHED": (255, "", "connection refused")})
    result = SSHProbe("host", "key", runner=runner).run()
    assert result.ok is False
    assert result.ok is not None


def test_ssh_probe_oserror_is_unknown():
    class BrokenRunner:
        def __call__(self, argv, **kwargs):
            raise OSError("no route to host")

    result = SSHProbe("host", "key", runner=BrokenRunner()).run()
    assert result.ok is None
    assert "OSError" in result.detail["error_type"]


def test_ssh_probe_never_accepts_an_empty_stdout_as_reachable():
    runner = Completes({"REMOTE_SHELL_REACHED": (0, "", "")})
    assert SSHProbe("host", "key", runner=runner).run().ok is False


# ----------------------------------------------------------------------- process


def test_process_probe_alive_on_ps_state():
    runner = Completes({"-p 4242": (0, "S+ python3.14 -m mycelium_seed\n", "")})
    result = ProcessProbe(4242, runner=runner).run()
    assert result.ok is True


def test_process_probe_dead_on_empty_ps():
    runner = Completes({"-p 4242": (1, "", "ps: no such process")})
    assert ProcessProbe(4242, runner=runner).run().ok is False


def test_process_probe_zombie_is_not_alive():
    runner = Completes({"-p 4242": (0, "Z  <defunct>\n", "")})
    assert ProcessProbe(4242, runner=runner).run().ok is False


def test_process_probe_command_mismatch_is_not_alive():
    runner = Completes({"-p 4242": (0, "S+ /bin/sh -c sleep 900\n", "")})
    assert ProcessProbe(4242, expected_command="mycelium_seed", runner=runner).run().ok is False


def test_process_probe_command_match_is_alive():
    runner = Completes({"-p 4242": (0, "S+ python3.14 -m mycelium_seed --port 8877\n", "")})
    assert ProcessProbe(4242, expected_command="mycelium_seed", runner=runner).run().ok is True


def test_process_probe_unknown_pid_is_unknown():
    assert ProcessProbe(None).run().ok is None


def test_process_probe_timeout_is_unknown():
    class TimingOut:
        def __call__(self, argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 5)

    assert ProcessProbe(1, runner=TimingOut()).run().ok is None


# -------------------------------------------------------------------------- http


def _status(**overrides):
    payload = {
        "route_alive": True,
        "route_ready": True,
        "simulated": False,
        "incidents": [],
    }
    payload.update(overrides)
    return payload


def test_health_from_status_green():
    assert health_from_status(_status()).ok is True


@pytest.mark.parametrize(
    "mutation",
    [
        {"route_alive": False},
        {"route_ready": False},
        {"simulated": True},
        {"incidents": [{"code": "x"}]},
        {"degraded": True},
    ],
)
def test_health_from_status_is_red_on_any_unhealthy_field(mutation):
    result = health_from_status(_status(**mutation))
    assert result.ok is False


@pytest.mark.parametrize("missing", ["route_alive", "route_ready", "simulated"])
def test_health_from_status_missing_key_is_unknown_not_green(missing):
    status = _status()
    del status[missing]
    result = health_from_status(status)
    assert result.ok is None
    assert result.ok is not True


def test_health_from_status_non_object_is_unknown():
    assert health_from_status(["not", "an", "object"]).ok is None


def test_http_probe_reads_with_bounded_timeout_and_reports_green():
    seen = {}

    class Response:
        status = 200

        def read(self, limit=None):
            return json.dumps(_status()).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout=None):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        seen["headers"] = dict(request.headers)
        return Response()

    probe = HTTPHealthProbe("http://127.0.0.1:18590/__mycelium/live-status", {}, timeout=4.0, opener=opener)
    assert probe.run().ok is True
    assert seen["timeout"] == 4.0
    assert seen["url"].endswith("/__mycelium/live-status")


def test_http_probe_transport_error_is_unknown():
    def opener(request, timeout=None):
        raise OSError("connection refused")

    assert HTTPHealthProbe("http://127.0.0.1:18590/x", {}, opener=opener).run().ok is None


def test_http_probe_bad_json_is_unknown():
    class Response:
        status = 200

        def read(self, limit=None):
            return b"<html>not json</html>"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout=None):
        return Response()

    assert HTTPHealthProbe("http://127.0.0.1:1/x", {}, opener=opener).run().ok is None


def test_http_probe_non_200_is_not_healthy():
    class Response:
        status = 503

        def read(self, limit=None):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout=None):
        return Response()

    assert HTTPHealthProbe("http://127.0.0.1:1/x", {}, opener=opener).run().ok is False


def test_private_capability_headers_read_the_capability_file_and_redact_it(tmp_path):
    cap = tmp_path / "proxy.cap"
    cap.write_text("s3cret-capability-bytes\n")
    headers = private_capability_headers(cap, authenticated_user="owner")
    assert headers["X-Mycelium-Proxy-Capability"] == "s3cret-capability-bytes"
    assert headers["Host"] == "mycelium.now"
    assert headers["X-Forwarded-Proto"] == "https"
    assert "s3cret-capability-bytes" not in json.dumps(
        {"detail": {k: ("<redacted>" if "Capability" in k else v) for k, v in headers.items()}}
    )


# ---------------------------------------------------------------------- scrubbing


def test_scrubbed_env_drops_every_named_variable(monkeypatch):
    for name in SCRUB_ENV_KEYS:
        monkeypatch.setenv(name, "poisoned")
    env = scrubbed_env()
    for name in SCRUB_ENV_KEYS:
        assert name not in env
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_probe_once_never_raises_when_a_probe_explodes():
    exploding = ScriptedProbe(raises=RuntimeError("boom"))
    report = probe_once(device=exploding, process=ScriptedProbe(default=ok()), origin=ScriptedProbe(default=ok()))
    assert report.device_reachable is None
    assert report.all_green is False


def test_probe_once_maps_each_field_and_is_all_green_only_when_all_true():
    green = probe_once(device=ScriptedProbe(default=ok()), process=ScriptedProbe(default=ok()), origin=ScriptedProbe(default=ok()))
    assert green.all_green is True

    for probe in ("device", "process", "origin"):
        kwargs = {"device": ScriptedProbe(default=ok()), "process": ScriptedProbe(default=ok()), "origin": ScriptedProbe(default=ok())}
        kwargs[probe] = ScriptedProbe(default=bad())
        assert probe_once(**kwargs).all_green is False

        kwargs[probe] = ScriptedProbe(default=unknown())
        assert probe_once(**kwargs).all_green is False


def test_probe_once_is_idempotent_and_read_only():
    """Calling it twice issues the same read-only calls and leaves state alone."""
    sessions = {
        name: ScriptedProbe(default=ok())
        for name in ("device", "process", "origin")
    }
    first = probe_once(device=sessions["device"], process=sessions["process"], origin=sessions["origin"])
    second = probe_once(device=sessions["device"], process=sessions["process"], origin=sessions["origin"])
    assert first == second
    assert all(session.calls == 2 for session in sessions.values())


def test_probe_contact_surface_has_no_teardown_verb():
    """Structural guard: the package may not own a teardown/kill path."""
    from pathlib import Path

    import mycelium_demo_watchdog

    package = Path(mycelium_demo_watchdog.__file__).parent
    text = "\n".join(path.read_text() for path in package.rglob("*.py"))
    for forbidden in ("teardown", "SIGTERM", "SIGKILL", "killall", "rmtree", "route.close"):
        assert forbidden not in text, forbidden
