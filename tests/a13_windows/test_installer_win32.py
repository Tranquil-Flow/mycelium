# SPDX-License-Identifier: AGPL-3.0-only
"""A13 Windows per-user self-installer and the frozen-app build modes (fake win32 API).

The frozen one-folder app is a stand-in tree (PyInstaller runs on the Windows
device); everything around it -- finalize, signing, verification before any
write, versioned install, rollback, uninstall retaining identity -- is real.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mycelium_a13_windows import installer
from mycelium_a13_windows.installer import HostSeams, InstallerError

from tests.host_platform.fake_win32 import simulate_win32

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import build_a13_windows  # noqa: E402

ISSUED = 1_800_000_000.0
PLATFORM = {"machine": "x86_64", "windows_version": "10.0", "windows_build": "22631"}


def _key(path: Path) -> Path:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path.write_bytes(Ed25519PrivateKey.generate().private_bytes_raw())
    return path


def _frozen(root: Path, marker: bytes = b"app") -> Path:
    for relative in build_a13_windows.FROZEN_REQUIRED:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"MZ" + marker + relative.encode())
    (root / "_internal" / "base_library.zip").write_bytes(b"pyz")
    return root


def _bundle(tmp: Path, version: str, *, key: Path, floor: str = "0.1.0", marker: bytes = b"app") -> Path:
    out = tmp / f"bundle-{version}"
    build_a13_windows.finalize_windows_bundle(
        frozen_dir=_frozen(tmp / f"frozen-{version}", marker), output_root=out, signing_key_path=key,
        version=version, rollback_floor=floor, issued_at=ISSUED, root=REPO)
    return out


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
def fake(monkeypatch, bundles):  # bundles are built on the (POSIX) build host first
    return simulate_win32(monkeypatch)


@pytest.fixture
def seams(tmp_path):
    record = {"registry": None, "shortcut": None, "deferred": [], "runs": []}
    local = tmp_path / "LocalAppData"

    def shortcut(link, target, workdir):
        link.parent.mkdir(parents=True, exist_ok=True)
        link.write_text(str(target))
        record["shortcut"] = (link, target, workdir)

    value = HostSeams(
        programs_root=local / "Programs" / "Mycelium",
        state_root=local / "Mycelium" / "A13",
        service_package_root=local / "Mycelium" / "service",
        shortcut=tmp_path / "StartMenu" / "Mycelium.lnk",
        observe_platform=lambda: dict(PLATFORM),
        set_uninstall_entry=lambda values: record.__setitem__("registry", dict(values)),
        delete_uninstall_entry=lambda: record.__setitem__("registry", None),
        create_shortcut=shortcut,
        remove_directory_after_exit=lambda path: record["deferred"].append(path),
        running_executable=lambda: tmp_path / "elsewhere" / "python.exe",
        run=lambda argv: record["runs"].append(argv) or 0,
        now=lambda: ISSUED + 100,
    )
    value.record = record  # type: ignore[attr-defined]
    return value


def _code(call, *args, **kwargs):
    with pytest.raises(InstallerError) as refused:
        call(*args, **kwargs)
    return refused.value.code


def test_install_verifies_then_installs_per_user_without_identity(fake, bundles, seams):
    result = installer.install(bundles["0.1.0"], seams=seams)
    assert result["version"] == "0.1.0" and result["identity_created"] is False
    assert result["publisher_pin_source"] == "first_use_owner_private_media"
    assert result["unattended_task_registered"] is False
    installed = seams.programs_root / "0.1.0" / "Mycelium"
    assert installer.payload_digest(installed) == installer.payload_digest(bundles["0.1.0"] / "Mycelium")
    assert fake.security_of(seams.programs_root).dacl_protected
    assert fake.security_of(seams.programs_root / "versions.json").dacl_protected
    assert fake.security_of(seams.state_root).dacl_protected and not any(seams.state_root.iterdir())
    entry = seams.record["registry"]
    assert entry["DisplayVersion"] == "0.1.0" and entry["UninstallString"].endswith('Mycelium.exe" --uninstall')
    assert entry["NoModify"] == 1 and "HKLM" not in json.dumps(entry)
    assert seams.record["shortcut"][1] == installed / "Mycelium.exe"
    assert installer.status(seams=seams)["current"] == "0.1.0"


@pytest.mark.parametrize("mutation,code", [
    ("payload_byte", "package_digest_mismatch"),
    ("extra_payload_file", "package_digest_mismatch"),
    ("missing_manifest", "bundle_incomplete"),
    ("arm64", "platform_unsupported"),
    ("old_build", "platform_unsupported"),
    ("expired", "package_expired"),
    ("manifest_tampered", "package_signature_invalid"),
])
def test_every_refusal_happens_before_any_write(fake, bundles, seams, mutation, code):
    bundle = bundles["0.1.0"]
    if mutation == "payload_byte":
        exe = bundle / "Mycelium" / "Mycelium.exe"
        exe.write_bytes(exe.read_bytes() + b"!")
    elif mutation == "extra_payload_file":
        (bundle / "Mycelium" / "_internal" / "evil.dll").write_bytes(b"MZ")
    elif mutation == "missing_manifest":
        (bundle / "installation-package-manifest.json").unlink()
    elif mutation == "arm64":
        seams.observe_platform = lambda: {**PLATFORM, "machine": "ARM64"}
    elif mutation == "old_build":
        seams.observe_platform = lambda: {**PLATFORM, "windows_build": "19044"}
    elif mutation == "expired":
        seams.now = lambda: ISSUED + 91 * 86400
    else:
        path = bundle / "installation-package-manifest.json"
        value = json.loads(path.read_text())
        value["update_channel"] = "public_store"
        path.write_text(json.dumps(value))
    assert _code(installer.install, bundle, seams=seams) == code
    assert not seams.programs_root.exists() and not seams.state_root.exists()
    assert seams.record["registry"] is None and seams.record["shortcut"] is None


def test_update_rollback_and_publisher_pin(fake, bundles, seams):
    installer.install(bundles["0.1.0"], seams=seams)
    pinned = installer.status(seams=seams)["publisher_key_digest"]
    # A bundle signed by any other key is refused against the first-use pin.
    assert _code(installer.install, bundles["foreign"], seams=seams) == "package_publisher_mismatch"
    update = installer.install(bundles["0.1.1"], seams=seams)
    assert update["previous"] == "0.1.0" and update["publisher_key_digest"] == pinned
    assert sorted(p.name for p in seams.programs_root.iterdir() if p.is_dir()) == ["0.1.0", "0.1.1"]
    back = installer.rollback(seams=seams)
    assert back["version"] == "0.1.0" and seams.record["registry"]["DisplayVersion"] == "0.1.0"
    # The retained previous version is re-verified before it becomes current again.
    installer.rollback(seams=seams)  # forward to 0.1.1
    tampered = seams.programs_root / "0.1.0" / "Mycelium" / "Mycelium.exe"
    tampered.write_bytes(b"MZ-tampered")
    assert _code(installer.rollback, seams=seams) == "previous_version_tampered"


def test_rollback_floor_blocks_downgrade_by_install_or_rollback(fake, bundles, seams):
    installer.install(bundles["0.1.1"], seams=seams)
    installer.install(bundles["0.2.0"], seams=seams)  # raises the floor to 0.2.0
    assert _code(installer.install, bundles["0.1.0"], seams=seams) == "package_downgrade_rejected"
    assert _code(installer.rollback, seams=seams) == "package_downgrade_rejected"
    assert installer.status(seams=seams)["current"] == "0.2.0"


def test_uninstall_keeps_identity_unless_asked_and_defers_the_running_version(fake, bundles, seams, tmp_path):
    installer.install(bundles["0.1.0"], seams=seams)
    seams.state_root.joinpath("identity.json").write_text("{}")  # what the app would have created
    seams.service_package_root.mkdir(parents=True)  # an unattended package that cannot be read
    seams.running_executable = lambda: seams.programs_root / "0.1.0" / "Mycelium" / "Mycelium.exe"
    result = installer.uninstall(seams=seams)
    assert result["state"] == "retained" and seams.state_root.joinpath("identity.json").exists()
    assert result["unattended_task"] == "service_package_root_unsafe"  # recorded, never blocking
    assert result["running_version_removed_after_exit"] is True
    assert seams.record["deferred"] == [seams.programs_root]
    assert seams.record["registry"] is None and not seams.shortcut.exists()
    assert result["coordinator_revocation_claimed"] is False
    installer.install(bundles["0.1.0"], seams=seams)
    seams.running_executable = lambda: tmp_path / "elsewhere" / "python.exe"
    deleted = installer.uninstall(delete_identity_and_state=True, seams=seams)
    assert deleted["state"] == "deleted" and not seams.state_root.exists()
    assert not seams.programs_root.exists()


def test_app_entry_dispatches_installer_and_windows_state_root(fake, bundles, seams, monkeypatch, capsys):
    from mycelium_a13_macos import __main__ as app_main

    assert app_main.default_state_root(platform="win32", environ={"LOCALAPPDATA": r"C:\Users\p\AppData\Local"}) \
        == Path(r"C:\Users\p\AppData\Local") / "Mycelium" / "A13"
    assert app_main.default_state_root(platform="darwin", environ={}).name == "Mycelium A13 macOS Demo"
    monkeypatch.setattr(installer, "HostSeams", lambda: seams)
    monkeypatch.setattr(sys, "argv", ["Mycelium.exe", "--install", "--bundle", str(bundles["0.1.0"])])
    assert app_main.main() == 0
    assert json.loads(capsys.readouterr().out)["version"] == "0.1.0"
    monkeypatch.setattr(sys, "argv", ["Mycelium.exe", "--rollback"])
    assert app_main.main() == 2
    assert json.loads(capsys.readouterr().err)["refused"] == "no_previous_version"


def test_frozen_input_is_deterministic_and_finalize_needs_the_native_parts(tmp_path, keys):
    first = build_a13_windows.build_frozen_input(output_root=tmp_path / "in1", root=REPO)
    second = build_a13_windows.build_frozen_input(output_root=tmp_path / "in2", root=REPO)
    assert first["input_digest"] == second["input_digest"] and first["signed"] is False
    for relative in ("mycelium_a13_macos/__main__.py", "mycelium_a13_windows/installer.py",
                     "mycelium_a13_macos/web/index.html", "mycelium_service_runner.py"):
        assert relative in first["files"], relative
    spec = (tmp_path / "in1" / "mycelium-windows.spec").read_text()
    assert "name='Mycelium'" in spec and "mycelium_a13_windows.installer" in spec
    frozen = _frozen(tmp_path / "frozen")
    (frozen / "_internal/mycelium_a13_macos/native/cloudflared.exe").unlink()
    with pytest.raises(SystemExit, match="frozen_app_incomplete"):
        build_a13_windows.finalize_windows_bundle(frozen_dir=frozen, output_root=tmp_path / "out",
                                                  signing_key_path=keys[0], version="0.1.0", root=REPO)
    report_bundle = _bundle(tmp_path / "ok", "0.1.0", key=keys[0])
    identity = json.loads((report_bundle / "artifact-identity.json").read_text())
    assert identity["install_mode"] == "windows_service_per_user"
    assert (report_bundle.parent / "mycelium-0.1.0-windows-x86_64.zip").is_file()
    assert keys[0].read_bytes().hex() not in "".join(
        p.read_text(errors="ignore") for p in report_bundle.iterdir() if p.is_file())
