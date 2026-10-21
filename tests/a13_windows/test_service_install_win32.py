# SPDX-License-Identifier: AGPL-3.0-only
"""Unattended Windows managed host: per-user Task Scheduler service on the fake win32 API."""

from __future__ import annotations

import json
import threading

import pytest

from mycelium_a13_windows import service_install
from mycelium_a13_windows.service_install import ServiceInstallError, execute, load_plan, render

from tests.host_platform.fake_win32 import OTHER_USER_SID, owner_only, simulate_win32

WIN_PACKAGE = "C:\\Users\\pilot\\AppData\\Local\\Mycelium\\service"
WIN_HOST = "C:\\Users\\pilot\\AppData\\Local\\Mycelium\\host"
PYTHON = "C:\\Users\\pilot\\AppData\\Local\\Programs\\Python\\Python314\\python.exe"
PRINCIPAL = "PILOT-PC\\pilot"
CONSENT = {"policy": b'{"thermal": "stop_on_serious"}', "limits": b'{"max_commands": 100}',
           "preview": b'{"consent_digest": "sha256:abc"}'}


@pytest.fixture
def fake(monkeypatch):
    return simulate_win32(monkeypatch)


def _render(tmp_path, **overrides):
    options = dict(package_root=tmp_path / "service", host_root=WIN_HOST, principal=PRINCIPAL,
                   acknowledged_unattended=True, python_executable=PYTHON,
                   windows_package_root=WIN_PACKAGE, environ={"MYCELIUM_A13_RUN_MODEL": "a9", "API_TOKEN": "x"},
                   **CONSENT)
    options.update(overrides)
    return render(**options)


def test_render_binds_the_previewed_work_into_an_owner_only_package(fake, tmp_path):
    result = _render(tmp_path)
    plan = result["plan"]
    assert plan["mechanism"] == "task_scheduler_logon_task" and plan["elevation_required"] is False
    assert plan["known_blockers"] == [] and result["executed"] is False
    root = tmp_path / "service"
    assert fake.security_of(root).dacl_protected
    for name in result["files"]:
        assert fake.security_of(root / name).dacl_protected
    assert (root / "preview.json").read_bytes() == CONSENT["preview"]
    config = json.loads((root / "service-config.json").read_bytes())
    assert config["argv"] == [PYTHON, "-m", "mycelium_a13_windows.managed_host", "--root", WIN_HOST, "run",
                              "--policy", WIN_PACKAGE + "\\policy.json", "--limits", WIN_PACKAGE + "\\limits.json",
                              "--preview", WIN_PACKAGE + "\\preview.json", "--acknowledge"]
    # Only the fixed non-secret pass-through names reach the service environment.
    assert config["environment"] == {"MYCELIUM_A13_RUN_MODEL": "a9"}
    task = (root / "mycelium-mycelium-managed-host.task.xml").read_bytes().decode("utf-16")
    assert "<LogonType>InteractiveToken</LogonType>" in task and "<RunLevel>LeastPrivilege</RunLevel>" in task
    assert PRINCIPAL in task
    assert load_plan(root) == plan


@pytest.mark.parametrize("overrides,code", [
    ({"acknowledged_unattended": False}, "unattended_acknowledgement_required"),
    ({"install_mode": "windows_service"}, "windows_service_scm_mode_unsupported"),
    ({"preview": b"not json"}, "bound_consent_invalid"),
    ({"principal": "NT AUTHORITY\\SYSTEM"}, "service_windows_principal_invalid"),
    ({"host_root": "/posix/host"}, "service_working_directory_invalid"),
])
def test_render_refusals(fake, tmp_path, overrides, code):
    with pytest.raises(ServiceInstallError) as refused:
        _render(tmp_path, **overrides)
    assert refused.value.code == code


def test_lifecycle_runs_only_the_rendered_plan(fake, tmp_path):
    _render(tmp_path)
    root = tmp_path / "service"
    seen = []
    codes = {}

    def runner(argv):
        seen.append(argv)
        return codes.get(argv[0] if argv[0] != PYTHON else "runner", 0)

    plan = load_plan(root)
    assert execute("register", root, runner=runner)["commands"][0]["argv"] == plan["install_argv"]
    assert seen[-1][:3] == ["schtasks.exe", "/Create", "/TN"]
    execute("start", root, runner=runner)
    assert seen[-1] == ["schtasks.exe", "/Run", "/TN", "\\Mycelium\\mycelium-mycelium-managed-host"]
    execute("stop", root, runner=runner)
    assert seen[-1][-1] == "--stop" and "mycelium_service_runner" in seen[-1]
    codes["runner"] = 1  # the runner no longer answers: schtasks /End fallback
    result = execute("stop", root, runner=runner)
    assert [c["argv"][0] for c in result["commands"]] == [PYTHON, "schtasks.exe"]
    assert seen[-1][:2] == ["schtasks.exe", "/End"]
    result = execute("unregister", root, runner=runner)
    assert result["commands"][-1]["argv"][:2] == ["schtasks.exe", "/Delete"]
    codes["schtasks.exe"] = 1
    with pytest.raises(ServiceInstallError, match="service_status_failed"):
        execute("status", root, runner=runner)
    with pytest.raises(ServiceInstallError, match="service_stop_failed"):
        execute("stop", root, runner=runner)


def test_tampered_or_foreign_package_is_refused(fake, tmp_path):
    _render(tmp_path)
    root = tmp_path / "service"
    fake.set_security(root / "preview.json", owner_only(OTHER_USER_SID))
    with pytest.raises(ServiceInstallError, match="service_package_incomplete"):
        execute("register", root, runner=lambda argv: 0)
    fake.set_security(root, owner_only(OTHER_USER_SID))
    with pytest.raises(ServiceInstallError, match="service_package_root_unsafe"):
        load_plan(root)


def test_cli_principal_and_refusal_json(fake, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("USERDOMAIN", raising=False)
    files = {}
    for name, raw in CONSENT.items():
        files[name] = tmp_path / f"{name}.json"
        files[name].write_bytes(raw)
    argv = ["--package-root", str(tmp_path / "service"), "render", "--host-root", WIN_HOST,
            "--policy", str(files["policy"]), "--limits", str(files["limits"]),
            "--preview", str(files["preview"]), "--acknowledge-unattended"]
    assert service_install.main(argv) == 2
    assert json.loads(capsys.readouterr().err)["refused"] == "windows_principal_unavailable"
    assert service_install.main(["--package-root", str(tmp_path / "none"), "status"]) == 2


# ----------------------------------------------------------- the host drains on the stop event


def test_managed_host_run_drains_on_the_supervisor_stop_event(monkeypatch, tmp_path, capsys):
    import mycelium_host_platform
    from mycelium_a13_macos import work_acceptance
    from mycelium_a13_windows.managed_host import __main__ as host_cli

    events = {"stopped_control": 0}

    class App:
        def __init__(self, root):
            pass

        def start(self):
            return self

        def stop(self):
            pass

        def pending_stage(self):
            return {"offer": 1}

        def pending_cleanup(self):
            return None

        def status(self):
            return {"native_control": {"state": "running"}}

        def stop_control(self):
            events["stopped_control"] += 1

        def _observe_policy(self):
            return {"power": "external", "network": "unmetered", "thermal": "nominal"}

    def watcher(on_stop, **_kwargs):
        cancelled = threading.Event()
        threading.Timer(0.2, on_stop).start()  # the runner signals the named event
        return cancelled

    monkeypatch.setattr(host_cli, "WindowsTargetApplication", App)
    monkeypatch.setattr(work_acceptance, "accept_work", lambda *a, **k: None)
    monkeypatch.setattr(mycelium_host_platform, "start_stop_event_watcher", watcher)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"peer_visibility": True, "power": "ac_only", "storage_bytes": 200_000_000,
                                  "network": "metered_allowed", "background": False, "diagnostics": False,
                                  "thermal": "stop_on_serious"}))
    limits = tmp_path / "limits.json"
    limits.write_text(json.dumps({"max_rss_bytes": 1 << 30, "max_disk_bytes": 100_000_000,
                                  "max_wall_seconds": 600, "max_commands": 100}))
    preview = tmp_path / "preview.json"
    preview.write_text("{}")
    code = host_cli.main(["--root", str(tmp_path / "host"), "run", "--policy", str(policy),
                          "--limits", str(limits), "--preview", str(preview), "--acknowledge"])
    assert code == 0 and events["stopped_control"] == 1
    assert "draining (staging kept)" in capsys.readouterr().out
