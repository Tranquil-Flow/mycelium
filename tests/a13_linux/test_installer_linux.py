# SPDX-License-Identifier: AGPL-3.0-only
"""A13 Linux per-user self-installer, user unit and build modes (native POSIX backend).

The frozen one-folder app is a stand-in tree (PyInstaller runs on an Ubuntu
build host); finalize, the deterministic tar.gz, extraction, verification,
install, rollback, the systemd user unit, linger and uninstall are real.
"""

from __future__ import annotations

import gzip
import io
import json
import stat
import sys
import tarfile
from pathlib import Path

import pytest

from mycelium_a13_linux import installer, service_install
from mycelium_a13_linux.installer import InstallerError, LinuxSeams

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import build_a13_linux  # noqa: E402

ISSUED = 1_800_000_000.0
UBUNTU = {"system": "Linux", "machine": "x86_64", "distribution_id": "ubuntu", "distribution_version": "24.04"}


def _key(path: Path) -> Path:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path.write_bytes(Ed25519PrivateKey.generate().private_bytes_raw())
    return path


def _frozen(root: Path, marker: bytes) -> Path:
    for relative in build_a13_linux.FROZEN_REQUIRED:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x7fELF" + marker + relative.encode())
        path.chmod(0o755)
    (root / "_internal" / "libpython3.14.so").write_bytes(b"lib")
    return root


def _bundle(tmp: Path, version: str, *, key: Path, floor: str = "0.1.0", marker: bytes = b"app") -> Path:
    """Finalize, then extract the tar.gz exactly as a user would; returns the extracted folder."""
    out = tmp / f"out-{version}"
    report = build_a13_linux.finalize_linux_bundle(
        frozen_dir=_frozen(tmp / f"frozen-{version}", marker), output_root=out, signing_key_path=key,
        version=version, rollback_floor=floor, issued_at=ISSUED, root=REPO)
    extract = tmp / f"extract-{version}"
    with tarfile.open(report["tarball"]) as archive:
        archive.extractall(extract, filter="data")
    return extract / report["artifact_name"]


@pytest.fixture
def keys(tmp_path):
    return _key(tmp_path / "release.key"), _key(tmp_path / "other.key")


@pytest.fixture
def bundles(tmp_path, keys):
    release, other = keys
    return {
        "0.1.0": _bundle(tmp_path / "b", "0.1.0", key=release),
        "0.1.1": _bundle(tmp_path / "b", "0.1.1", key=release, marker=b"v2"),
        "0.2.0": _bundle(tmp_path / "b", "0.2.0", key=release, floor="0.2.0", marker=b"v3"),
        "foreign": _bundle(tmp_path / "f", "0.1.2", key=other, marker=b"evil"),
    }


@pytest.fixture
def seams(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    runs: list[list[str]] = []
    value = LinuxSeams(
        environ={"HOME": str(home), "USER": "mycelium-clean"},
        observe_platform=lambda: dict(UBUNTU),
        run=lambda argv: runs.append(argv) or 0,
        running_executable=lambda: tmp_path / "elsewhere" / "python3",
        now=lambda: ISSUED + 100,
    )
    value.runs = runs  # type: ignore[attr-defined]
    return value


def _code(call, *args, **kwargs):
    with pytest.raises(InstallerError) as refused:
        call(*args, **kwargs)
    return refused.value.code


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_extracted_tarball_installs_per_user_into_xdg_paths(bundles, seams):
    assert (bundles["0.1.0"] / "install.sh").stat().st_mode & 0o111
    result = installer.install(bundles["0.1.0"], seams=seams)
    assert result["version"] == "0.1.0" and result["identity_created"] is False
    assert result["distribution"] == "ubuntu-24.04" and result["unattended_task_registered"] is False
    home = Path(seams.environ["HOME"])
    app = home / ".local/share/mycelium/0.1.0/mycelium/mycelium"
    assert app.is_file() and _mode(app) & 0o100  # the executable bit survived tar and copy
    launcher = home / ".local/bin/mycelium"
    assert _mode(launcher) == 0o755 and str(app) in launcher.read_text()
    desktop = home / ".local/share/applications/org.mycelium.Mycelium.desktop"
    text = desktop.read_text()
    assert "Terminal=false" in text and f'Exec="{launcher}"' in text and _mode(desktop) == 0o644
    state = home / ".local/state/mycelium/A13"
    assert _mode(state) == 0o700 and not any(state.iterdir())
    assert _mode(home / ".local/share/mycelium") == 0o700
    assert seams.runs == []  # no systemctl, no loginctl, no sudo during install


@pytest.mark.parametrize("mutation,code", [
    ("payload_byte", "package_digest_mismatch"),
    ("missing_manifest", "bundle_incomplete"),
    ("debian", "platform_unsupported"),
    ("jammy", "platform_unsupported"),
    ("aarch64_host", "package_platform_unsupported"),
    ("expired", "package_expired"),
])
def test_every_refusal_happens_before_any_write(bundles, seams, mutation, code):
    bundle = bundles["0.1.0"]
    if mutation == "payload_byte":
        exe = bundle / "mycelium" / "mycelium"
        exe.write_bytes(exe.read_bytes() + b"!")
    elif mutation == "missing_manifest":
        (bundle / "installation-package-manifest.json").unlink()
    elif mutation == "debian":
        seams.observe_platform = lambda: {**UBUNTU, "distribution_id": "debian", "distribution_version": "12"}
    elif mutation == "jammy":
        seams.observe_platform = lambda: {**UBUNTU, "distribution_version": "22.04"}
    elif mutation == "aarch64_host":
        seams.observe_platform = lambda: {**UBUNTU, "machine": "aarch64"}
    else:
        seams.now = lambda: ISSUED + 91 * 86400
    assert _code(installer.install, bundle, seams=seams) == code
    home = Path(seams.environ["HOME"])
    assert not (home / ".local").exists()


def test_update_rollback_floor_and_publisher_pin(bundles, seams):
    installer.install(bundles["0.1.0"], seams=seams)
    assert _code(installer.install, bundles["foreign"], seams=seams) == "package_publisher_mismatch"
    assert installer.install(bundles["0.1.1"], seams=seams)["previous"] == "0.1.0"
    assert installer.rollback(seams=seams)["version"] == "0.1.0"
    installer.rollback(seams=seams)
    installer.install(bundles["0.2.0"], seams=seams)  # floor 0.2.0
    assert _code(installer.install, bundles["0.1.0"], seams=seams) == "package_downgrade_rejected"
    assert _code(installer.rollback, seams=seams) == "package_downgrade_rejected"
    launcher = Path(seams.environ["HOME"]) / ".local/bin/mycelium"
    assert "/0.2.0/" in launcher.read_text()


def test_a_users_own_launcher_is_never_overwritten(bundles, seams):
    launcher = Path(seams.environ["HOME"]) / ".local/bin/mycelium"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\necho mine\n")
    assert _code(installer.install, bundles["0.1.0"], seams=seams) == "register_target_not_ours"
    assert launcher.read_text() == "#!/bin/sh\necho mine\n"


def _consent(tmp: Path) -> dict:
    return {"policy": b'{"thermal": "stop_on_serious"}', "limits": b'{"max_commands": 100}',
            "preview": b'{"consent_digest": "sha256:abc"}'}


def test_user_unit_binds_preview_and_linger_is_a_separate_recorded_opt_in(bundles, seams, tmp_path):
    installer.install(bundles["0.1.0"], seams=seams)
    package = seams.service_package_root
    app = str(Path(seams.environ["HOME"]) / ".local/share/mycelium/0.1.0/mycelium/mycelium")
    with pytest.raises(service_install.ServiceInstallError, match="unattended_acknowledgement_required"):
        service_install.render(package_root=package, host_root=str(tmp_path / "host"),
                               acknowledged_unattended=False, app_executable=app, **_consent(tmp_path))
    rendered = service_install.render(package_root=package, host_root=str(tmp_path / "host"),
                                      acknowledged_unattended=True, app_executable=app,
                                      environ={"MYCELIUM_A13_RUN_MODEL": "a9", "API_TOKEN": "x"},
                                      **_consent(tmp_path))
    unit = (package / service_install.UNIT_NAME).read_text()
    assert "network-online.target" not in unit and "WantedBy=default.target" in unit
    assert "NoNewPrivileges=true" in unit and "User=" not in unit and "API_TOKEN" not in unit
    assert f'"{app}"' in unit and '"-m"' in unit and '"mycelium_service_runner"' in unit
    assert _mode(package) == 0o700 and _mode(package / "preview.json") == 0o600
    config = json.loads((package / "service-config.json").read_text())
    assert config["argv"][:3] == [app, "-m", "mycelium_a13_linux.managed_host"] and config["argv"][-1] == "--acknowledge"
    assert rendered["executed"] is False
    calls = []
    service_install.execute("register", package, unit_directory=seams.unit_directory,
                            runner=lambda argv: calls.append(argv) or 0, user="mycelium-clean")
    assert (seams.unit_directory / service_install.UNIT_NAME).read_text() == unit
    assert calls == [["systemctl", "--user", "daemon-reload"],
                     ["systemctl", "--user", "enable", service_install.UNIT_NAME]]
    assert not any("sudo" in part or "--system" in part for argv in calls for part in argv)
    with pytest.raises(service_install.ServiceInstallError, match="linger_acknowledgement_required"):
        service_install.execute("enable-linger", package, runner=lambda argv: 0, user="mycelium-clean")
    with pytest.raises(service_install.ServiceInstallError, match="linger_not_enabled_by_installer"):
        service_install.execute("disable-linger", package, runner=lambda argv: 0, user="mycelium-clean")
    with pytest.raises(service_install.ServiceInstallError, match="linger_enable_failed"):
        service_install.execute("enable-linger", package, runner=lambda argv: 1, user="mycelium-clean",
                                acknowledged_linger=True)  # polkit refused: reported, never escalated
    service_install.execute("enable-linger", package, runner=lambda argv: 0, user="mycelium-clean",
                            acknowledged_linger=True)
    assert service_install.execute("status", package, runner=lambda argv: 3)["action"] == "status"
    # Uninstall removes the unit and undoes only the linger this tool enabled; state is kept.
    state = Path(seams.environ["HOME"]) / ".local/state/mycelium/A13"
    (state / "identity.json").write_text("{}")
    result = installer.uninstall(seams=seams)
    assert result["unattended_task"] == "removed" and result["state"] == "retained"
    assert ["loginctl", "disable-linger", "mycelium-clean"] in seams.runs
    assert ["systemctl", "--user", "disable", "--now", service_install.UNIT_NAME] in seams.runs
    assert not (seams.unit_directory / service_install.UNIT_NAME).exists()
    assert (state / "identity.json").exists()
    assert not (Path(seams.environ["HOME"]) / ".local/bin/mycelium").exists()
    assert not seams.programs_root.exists()
    assert set(result["removed_entries"]) == {"mycelium", "org.mycelium.Mycelium.desktop"}


def test_uninstall_without_linger_never_touches_loginctl_and_can_delete_state(bundles, seams):
    installer.install(bundles["0.1.0"], seams=seams)
    result = installer.uninstall(delete_identity_and_state=True, seams=seams)
    assert result["unattended_task"] == "absent" and result["state"] == "deleted"
    assert not any(argv[0] == "loginctl" for argv in seams.runs)
    assert result["coordinator_revocation_claimed"] is False


def test_system_unit_rendering_is_unchanged_and_user_unit_drops_network_online(tmp_path):
    from mycelium_service_package import build_service_package

    config = service_install.service_config(package_root="/srv/pkg", host_root="/srv/host",
                                            app_executable="/srv/app/mycelium")
    system = build_service_package(config, package_root=tmp_path / "a")["_payloads"]
    user = build_service_package(config, package_root=tmp_path / "a", systemd_user_unit=True)["_payloads"]
    name = "mycelium-managed-host.service"
    assert b"After=network-online.target" in system[name] and b"Wants=network-online.target" in system[name]
    assert b"network-online" not in user[name]
    assert system[name].replace(b"After=network-online.target\nWants=network-online.target\n", b"") == user[name]


def test_app_entry_linux_state_root_preflight_dispatch_and_frozen_modules(bundles, seams, monkeypatch, capsys):
    from mycelium_a13_macos import __main__ as app_main
    from mycelium_a13_macos.app import DemoError, preflight

    assert app_main.default_state_root(platform="linux", environ={"XDG_STATE_HOME": "/x/state"}) \
        == Path("/x/state/mycelium/A13")
    assert app_main.default_state_root(platform="linux", environ={"XDG_STATE_HOME": "relative"}) \
        == Path.home() / ".local/state/mycelium/A13"
    assert preflight(system="Linux", machine="x86_64", os_version="ubuntu-24.04")["platform"] == "linux"
    assert preflight(system="Linux", machine="aarch64", os_version="ubuntu-26.04")["architecture"] == "aarch64"
    for machine, version in (("x86_64", "ubuntu-22.04"), ("x86_64", "debian-12"), ("riscv64", "ubuntu-24.04")):
        with pytest.raises(DemoError, match="unsupported_platform"):
            preflight(system="Linux", machine=machine, os_version=version)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(installer, "LinuxSeams", lambda: seams)
    monkeypatch.setattr(sys, "argv", ["mycelium", "--install", "--bundle", str(bundles["0.1.0"])])
    assert app_main.main() == 0
    assert json.loads(capsys.readouterr().out)["version"] == "0.1.0"
    monkeypatch.setattr(sys, "argv", ["mycelium", "-m", "os", "system"])
    assert app_main.main() == 2  # only the allow-listed entry points
    assert json.loads(capsys.readouterr().err)["code"] == "frozen_module_not_allowed"
    import mycelium_service_runner

    seen = []
    monkeypatch.setattr(mycelium_service_runner, "main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr(sys, "argv", ["mycelium", "-B", "-m", "mycelium_service_runner", "--config", "/c.json"])
    assert app_main.main() == 0 and seen == [["--config", "/c.json"]]


def test_frozen_input_and_finalize_are_deterministic(tmp_path, keys):
    first = build_a13_linux.build_frozen_input(output_root=tmp_path / "in1", root=REPO)
    second = build_a13_linux.build_frozen_input(output_root=tmp_path / "in2", root=REPO)
    assert first["input_digest"] == second["input_digest"] and first["build_host"] == "ubuntu-24.04"
    for relative in ("mycelium_a13_macos/__main__.py", "mycelium_a13_linux/installer.py",
                     "mycelium_a13_linux/service_install.py", "mycelium_a13_macos/web/index.html"):
        assert relative in first["files"], relative
    frozen = _frozen(tmp_path / "frozen", b"same")
    a = build_a13_linux.finalize_linux_bundle(frozen_dir=frozen, output_root=tmp_path / "o1", signing_key_path=keys[0],
                                              version="0.1.0", issued_at=ISSUED, root=REPO)
    raw = Path(a["tarball"]).read_bytes()
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(raw))) as archive:
        names = archive.getnames()
    assert "mycelium-0.1.0-linux-x86_64/install.sh" in names
    assert all(name.startswith("mycelium-0.1.0-linux-x86_64") for name in names)
    (frozen / "_internal/mycelium_a13_macos/native/cloudflared").unlink()
    with pytest.raises(SystemExit, match="frozen_app_incomplete"):
        build_a13_linux.finalize_linux_bundle(frozen_dir=frozen, output_root=tmp_path / "o2",
                                              signing_key_path=keys[0], version="0.1.0", root=REPO)
    assert keys[0].read_bytes().hex() not in raw.hex()
