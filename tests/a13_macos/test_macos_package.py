# SPDX-License-Identifier: AGPL-3.0-only
"""macOS A13 packaging parity: finalize, link-aware digest, quarantine-preserving install.

Runs natively on this Mac: the real ``/usr/bin/ditto`` and ``xattr``.  The
finalize test reads the real L2 ``0.1.0-preview.2`` app read-only when it is
present; the builder (``scripts/build_a13_macos.py``) is never invoked.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mycelium_a13_macos import installer
from mycelium_a13_macos.installer import InstallerError, MacSeams
from mycelium_a13_macos.package_manifest import DEVELOPER_ID_UNSIGNED
from mycelium_a13_packaging import build

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import finalize_a13_macos  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not Path("/usr/bin/ditto").exists(),
                                reason="needs macOS ditto/xattr")

ISSUED = 1_800_000_000.0
MAC = {"system": "Darwin", "machine": "arm64", "macos_version": "26.5.1"}
QUARANTINE = "com.apple.quarantine"
#: The L2-qualified build output, next to this lane under runs/v2 (absent elsewhere: skipped).
PREVIEW2 = REPO.parents[1] / "l2-batch13-package" / "out" / "0.1.0-preview.2" / "Mycelium A13 Demo.app"


def _key(path: Path) -> Path:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path.write_bytes(Ed25519PrivateKey.generate().private_bytes_raw())
    return path


def _app(root: Path, marker: bytes = b"v1") -> Path:
    app = root / "Mycelium A13 Demo.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Frameworks").mkdir()
    (app / "Contents" / "Resources").mkdir()
    exe = app / "Contents" / "MacOS" / "Mycelium A13 Demo"
    exe.write_bytes(b"\xcf\xfa\xed\xfe" + marker)
    exe.chmod(0o755)
    (app / "Contents" / "Frameworks" / "libdemo.dylib").write_bytes(b"lib" + marker)
    (app / "Contents" / "Resources" / "libdemo").symlink_to("../Frameworks/libdemo.dylib")
    (app / "Contents" / "Info.plist").write_text("<plist/>")
    return app


def _xattr(path: Path, value: str | None = None) -> str | None:
    if value is not None:
        subprocess.run(["/usr/bin/xattr", "-w", QUARANTINE, value, str(path)], check=True)
        return value
    done = subprocess.run(["/usr/bin/xattr", "-p", QUARANTINE, str(path)], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else None


def _finalize(tmp: Path, version: str, *, key: Path, floor: str = "0.1.0", marker: bytes = b"v1",
              quarantine: str | None = None, copy=None) -> Path:
    app = _app(tmp / f"build-{version}", marker)
    if quarantine:
        _xattr(app, quarantine)
        _xattr(app / "Contents" / "MacOS" / "Mycelium A13 Demo", quarantine)
    out = tmp / f"bundle-{version}"
    finalize_a13_macos.finalize_macos_bundle(app_dir=app, output_root=out, signing_key_path=key,
                                             version=version, rollback_floor=floor, issued_at=ISSUED, root=REPO,
                                             copy=copy)
    return out


@pytest.fixture
def key(tmp_path):
    return _key(tmp_path / "release.key")


@pytest.fixture
def seams(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return MacSeams(home=home, observe_platform=lambda: dict(MAC),
                    running_executable=lambda: tmp_path / "elsewhere" / "python3", now=lambda: ISSUED + 100)


# ------------------------------------------------------------------ link-aware digest


@pytest.mark.parametrize("target,code", [
    ("/etc/hosts", "payload_link_absolute"),
    ("../../../outside", "payload_link_escapes_bundle"),
    ("../../..", "payload_link_escapes_bundle"),
    ("missing-target", "payload_link_dangling"),
])
def test_absolute_escaping_and_dangling_links_are_refused(tmp_path, key, target, code):
    app = _app(tmp_path / "b")
    (tmp_path / "outside").write_text("x")
    (app / "Contents" / "Resources" / "bad").symlink_to(target)
    with pytest.raises(build.PayloadError) as refused:
        build.bundle_digest(app)
    assert refused.value.code == code
    with pytest.raises(build.PayloadError):
        finalize_a13_macos.finalize_macos_bundle(app_dir=app, output_root=tmp_path / "out", signing_key_path=key,
                                                 version="0.1.0", issued_at=ISSUED, root=REPO)
    assert not (tmp_path / "out").exists()  # refused before any write


def test_an_escaping_link_planted_after_finalize_is_refused_at_install(tmp_path, key, seams):
    bundle = _finalize(tmp_path, "0.1.0", key=key)
    (bundle / "Mycelium A13 Demo.app" / "Contents" / "Resources" / "evil").symlink_to("../../../../home")
    with pytest.raises(InstallerError) as refused:
        installer.install(bundle, seams=seams)
    assert refused.value.code == "payload_link_escapes_bundle"
    assert not seams.programs_root.exists() and not seams.applications_entry.exists()


def test_links_are_part_of_the_digest(tmp_path):
    app = _app(tmp_path / "b")
    tree = build.bundle_tree(app)
    assert tree["Contents/Resources/libdemo"] == "link:../Frameworks/libdemo.dylib"
    before = build.bundle_digest(app)
    link = app / "Contents" / "Resources" / "libdemo"
    link.unlink()
    link.symlink_to("../Info.plist")  # retargeted inside the bundle: still a different package
    assert build.bundle_digest(app) != before


# ------------------------------------------------------------------ quarantine preservation


WRITTEN_QUARANTINE = "0083;6700f000;Safari;F00DF00D-0000-0000-0000-000000000000"
#: What this host once read back from finalize's ditto copy of WRITTEN_QUARANTINE (2026-10-11):
#: flag 0x0200 added, timestamp and agent cleared, the quarantine event UUID kept.
OS_REWRITTEN_QUARANTINE = "0283;00000000;;F00DF00D-0000-0000-0000-000000000000"


def _ditto_then_os_rewrite(src: Path, dst: Path) -> int:
    code = subprocess.run(["/usr/bin/ditto", str(src), str(dst)], check=False).returncode
    _xattr(dst, OS_REWRITTEN_QUARANTINE)
    return code


def _keeps_quarantine(observed: str | None, written: str) -> bool:
    """The copy kept the quarantine: same event UUID and every written flag still set.

    The flags may gain bits and the timestamp and agent fields may be rewritten by the OS
    (OS_REWRITTEN_QUARANTINE); none of that is the installer's to preserve or prevent.
    """
    if observed is None:
        return False
    seen, want = observed.split(";"), written.split(";")
    flags = int(want[0], 16)
    return len(seen) == 4 and seen[3] == want[3] and int(seen[0], 16) & flags == flags


@pytest.mark.parametrize("observed,kept", [
    (WRITTEN_QUARANTINE, True),
    (OS_REWRITTEN_QUARANTINE, True),
    (None, False),  # dropped, as shutil does
    ("0083;6700f000;Safari;0BADF00D-0000-0000-0000-000000000000", False),  # a different quarantine event
    ("0003;6700f000;Safari;F00DF00D-0000-0000-0000-000000000000", False),  # a written flag cleared
])
def test_kept_quarantine_means_same_event_and_no_cleared_flag(observed, kept):
    assert _keeps_quarantine(observed, WRITTEN_QUARANTINE) is kept


@pytest.mark.parametrize("copy", [None, _ditto_then_os_rewrite], ids=["ditto", "ditto_then_os_rewrite"])
def test_ditto_install_keeps_quarantine_where_shutil_would_drop_it(tmp_path, key, seams, copy):
    value = WRITTEN_QUARANTINE
    bundle = _finalize(tmp_path, "0.1.0", key=key, quarantine=value, copy=copy)
    staged = bundle / "Mycelium A13 Demo.app"
    assert _keeps_quarantine(_xattr(staged), value)  # finalize's ditto copy kept it
    naive = tmp_path / "naive.app"
    shutil.copytree(staged, naive, symlinks=True)
    assert _xattr(naive) is None  # the bypass the installer must not commit
    result = installer.install(bundle, seams=seams)
    installed = seams.programs_root / "0.1.0" / "Mycelium A13 Demo.app"
    assert _keeps_quarantine(_xattr(installed), value)
    assert _keeps_quarantine(_xattr(installed / "Contents" / "MacOS" / "Mycelium A13 Demo"), value)
    assert result["developer_id_team"] == DEVELOPER_ID_UNSIGNED and result["identity_created"] is False


# ------------------------------------------------------------------ install / rollback / uninstall


def test_install_links_applications_and_keeps_state_on_uninstall(tmp_path, key, seams):
    v1 = _finalize(tmp_path, "0.1.0", key=key)
    v2 = _finalize(tmp_path, "0.1.1", key=key, marker=b"v2")
    first = installer.install(v1, seams=seams)
    assert first["publisher_pin_source"] == "first_use_owner_private_media"
    entry = seams.applications_entry
    assert entry.is_symlink() and Path(os.readlink(entry)) == seams.programs_root / "0.1.0" / "Mycelium A13 Demo.app"
    assert (entry / "Contents" / "MacOS" / "Mycelium A13 Demo").is_file()
    assert oct(seams.state_root.stat().st_mode & 0o777) == "0o700"
    assert installer.install(v2, seams=seams)["previous"] == "0.1.0"
    assert "/0.1.1/" in os.readlink(entry)
    assert installer.rollback(seams=seams)["version"] == "0.1.0" and "/0.1.0/" in os.readlink(entry)
    (seams.state_root / "identity.json").write_text("{}")
    result = installer.uninstall(seams=seams)
    assert result["state"] == "retained" and (seams.state_root / "identity.json").exists()
    assert not entry.exists() and not entry.is_symlink() and not seams.programs_root.exists()


@pytest.mark.parametrize("mutation,code", [
    ("payload_byte", "package_digest_mismatch"),
    ("intel", "platform_unsupported"),
    ("old_macos", "package_platform_unsupported"),
    ("expired", "package_expired"),
])
def test_refusals_happen_before_any_write(tmp_path, key, seams, mutation, code):
    bundle = _finalize(tmp_path, "0.1.0", key=key)
    if mutation == "payload_byte":
        lib = bundle / "Mycelium A13 Demo.app" / "Contents" / "Frameworks" / "libdemo.dylib"
        lib.write_bytes(lib.read_bytes() + b"!")
    elif mutation == "intel":
        seams.observe_platform = lambda: {**MAC, "machine": "x86_64"}
    elif mutation == "old_macos":
        seams.observe_platform = lambda: {**MAC, "macos_version": "14.0"}
    else:
        seams.now = lambda: ISSUED + 91 * 86400
    with pytest.raises(InstallerError) as refused:
        installer.install(bundle, seams=seams)
    assert refused.value.code == code
    assert not (seams.home / "Library").exists() and not (seams.home / "Applications").exists()


def test_a_users_own_app_in_applications_is_never_replaced(tmp_path, key, seams):
    bundle = _finalize(tmp_path, "0.1.0", key=key)
    own = seams.applications_entry
    own.mkdir(parents=True)
    (own / "mine").write_text("keep")
    with pytest.raises(InstallerError, match="register_target_not_ours"):
        installer.install(bundle, seams=seams)
    assert (own / "mine").read_text() == "keep"


def test_zip_keeps_links_and_extracts_with_ditto_to_an_installable_bundle(tmp_path, key, seams):
    bundle = _finalize(tmp_path, "0.1.0", key=key)
    archive = bundle.parent / "mycelium-0.1.0-macos-arm64.zip"
    again = finalize_a13_macos.finalize_macos_bundle(
        app_dir=tmp_path / "build-0.1.0" / "Mycelium A13 Demo.app", output_root=tmp_path / "again" / "bundle",
        signing_key_path=key, version="0.1.0", issued_at=ISSUED, root=REPO)
    assert Path(again["zip"]).read_bytes() != b"" and archive.is_file()
    extract = tmp_path / "extract"
    subprocess.run(["/usr/bin/ditto", "-x", "-k", str(archive), str(extract)], check=True)
    extracted = extract / "mycelium-0.1.0-macos-arm64"
    link = extracted / "Mycelium A13 Demo.app" / "Contents" / "Resources" / "libdemo"
    assert link.is_symlink() and os.readlink(link) == "../Frameworks/libdemo.dylib"
    assert os.access(extracted / "Install Mycelium.command", os.X_OK)
    assert installer.install(extracted, seams=seams)["version"] == "0.1.0"


def test_app_entry_dispatches_the_macos_installer(tmp_path, key, seams, monkeypatch, capsys):
    from mycelium_a13_macos import __main__ as app_main

    bundle = _finalize(tmp_path, "0.1.0", key=key)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(installer, "MacSeams", lambda: seams)
    monkeypatch.setattr(sys, "argv", ["Mycelium A13 Demo", "--install", "--bundle", str(bundle)])
    assert app_main.main() == 0
    assert json.loads(capsys.readouterr().out)["version"] == "0.1.0"


# ------------------------------------------------------------------ the real L2 preview.2 app, read only


@pytest.mark.skipif(not PREVIEW2.is_dir(), reason="L2 preview.2 app not present on this host")
def test_finalize_reads_the_real_preview2_app_without_modifying_it(tmp_path, key):
    before = build.bundle_digest(PREVIEW2)
    tree = build.bundle_tree(PREVIEW2)
    assert sum(1 for value in tree.values() if value.startswith("link:")) >= 1  # real relative links
    report = finalize_a13_macos.finalize_macos_bundle(app_dir=PREVIEW2, output_root=tmp_path / "bundle",
                                                      signing_key_path=key, version="0.1.0-preview.2",
                                                      issued_at=ISSUED, root=REPO)
    assert report["bundle_digest"] == before
    assert build.bundle_digest(PREVIEW2) == before  # the L2 artifact is untouched
    manifest = json.loads((tmp_path / "bundle" / "installation-package-manifest.json").read_text())
    assert manifest["package_digest"] == before
    assert manifest["publisher_identity"]["developer_id_team"] == DEVELOPER_ID_UNSIGNED
    assert manifest["service_descriptor"]["install_mode"] == "launchd_user"
