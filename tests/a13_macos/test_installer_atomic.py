# SPDX-License-Identifier: AGPL-3.0-only
"""macOS self-installer: atomic, locked installs that recover from a failed copy.

Reproduces the 2026-10-21 admin-user findings (``09-finding-gatekeeper-flow.txt``):
an installer killed mid-copy (Gatekeeper) or two installers racing on one
``.partial`` left an empty ``programs/<version>`` behind, and every later
install died with an unhandled ``FileNotFoundError``.  The invariants pinned here:

* the payload is copied into a staging directory and the *whole* version
  directory is renamed into place only after the staged digest verifies;
* a version directory without a valid payload is stale: moved aside to
  ``<version>.stale-<n>`` (never deleted), then the install continues;
* an exclusive lock (``programs/.install.lock``) makes a second concurrent
  installer refuse with ``install_already_running``;
* every failure is the structured ``{"refused": ...}`` line, never a traceback;
* quarantine is removed only from the verified payload copy the installer
  itself installs, never earlier and never from anything unverified.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from mycelium_a13_macos import installer
from mycelium_a13_macos.installer import InstallerError, MacSeams

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import finalize_a13_macos  # noqa: E402

# SAFETY: every attribute operation uses FakeXattrs. No real quarantine is ever set.
# The fixture executable is data only and is never spawned.

pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not Path("/usr/bin/ditto").exists(),
                                reason="needs macOS ditto/xattr")

@pytest.fixture(autouse=True)
def attributes(fake_xattrs):
    """Require fake attributes for every test in this module."""


ISSUED = 1_800_000_000.0
MAC = {"system": "Darwin", "machine": "arm64", "macos_version": "26.5.1"}
from fake_xattrs import QUARANTINE
QVALUE = "0083;6700f000;Safari;F00DF00D-0000-0000-0000-000000000000"
APP = "Mycelium A13 Demo.app"
EXE = "Contents/MacOS/Mycelium A13 Demo"


def _key(path: Path) -> Path:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    path.write_bytes(Ed25519PrivateKey.generate().private_bytes_raw())
    return path


def _app(root: Path, marker: bytes = b"v1") -> Path:
    app = root / APP
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Frameworks").mkdir()
    (app / "Contents" / "Resources").mkdir()
    exe = app / EXE
    exe.write_bytes(b"stub-executable-never-run-" + marker)
    exe.chmod(0o644)  # data only; attributes are simulated
    (app / "Contents" / "Frameworks" / "libdemo.dylib").write_bytes(b"lib" + marker)
    (app / "Contents" / "Resources" / "libdemo").symlink_to("../Frameworks/libdemo.dylib")
    (app / "Contents" / "Info.plist").write_text("<plist/>")
    return app


def _xattr(path: Path, *, write: str | None = None, link: bool = False) -> str | None:
    raise AssertionError("the fake_xattrs fixture must inject this function")


def _quarantined(app: Path) -> list[str]:
    """Every path in the tree (links themselves included) that still carries the quarantine."""

    found = [p for p in [app, *app.rglob("*")] if _xattr(p, link=True) is not None]
    return sorted(str(p.relative_to(app.parent)) for p in found)


def _bundle(tmp: Path, version: str, key: Path, *, marker: bytes = b"v1", quarantine: bool = False) -> Path:
    app = _app(tmp / f"build-{version}-{marker.decode()}", marker)
    if quarantine:
        for path in [app, *app.rglob("*")]:
            _xattr(path, write=QVALUE, link=path.is_symlink())
    out = tmp / f"bundle-{version}-{marker.decode()}"
    finalize_a13_macos.finalize_macos_bundle(app_dir=app, output_root=out, signing_key_path=key, version=version,
                                             rollback_floor="0.1.0", issued_at=ISSUED, root=REPO, copy=_ditto)
    return out


def _ditto(source: Path, destination: Path) -> int:
    return subprocess.run(["/usr/bin/ditto", str(source), str(destination)], check=False).returncode


@pytest.fixture
def key(tmp_path):
    return _key(tmp_path / "release.key")


@pytest.fixture
def seams(tmp_path, fake_xattrs):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return MacSeams(home=home, observe_platform=lambda: dict(MAC),
                    running_executable=lambda: tmp_path / "elsewhere" / "python3", now=lambda: ISSUED + 100,
                    copy=fake_xattrs.copy, clear_quarantine=lambda p: installer.clear_quarantine(p, run=fake_xattrs.run))


def _programs(seams: MacSeams) -> Path:
    return seams.programs_root


def _children(seams: MacSeams) -> list[str]:
    return sorted(p.name for p in _programs(seams).iterdir())


def _assert_installed(seams: MacSeams, version: str = "0.1.0") -> None:
    exe = _programs(seams) / version / APP / EXE
    assert exe.is_file()
    assert seams.applications_entry.is_symlink()
    assert os.readlink(seams.applications_entry) == str(_programs(seams) / version / APP)


# ------------------------------------------------------------------ the wedge (F2)


@pytest.mark.parametrize("leftover", ["empty_version_dir", "payload_dir_without_executable", "old_layout_partial",
                                      "version_is_a_file", "version_is_a_dangling_symlink"])
def test_a_stale_version_directory_is_moved_aside_and_the_install_continues(tmp_path, key, seams, leftover):
    bundle = _bundle(tmp_path, "0.1.0", key)
    root = _programs(seams)
    target = root / "0.1.0"
    root.mkdir(parents=True, mode=0o700)
    if leftover == "empty_version_dir":
        target.mkdir(mode=0o700)  # exactly what a killed / raced 0.1.0 install left behind
    elif leftover == "payload_dir_without_executable":
        (target / APP / "Contents").mkdir(parents=True)
    elif leftover == "old_layout_partial":
        (target / (APP + ".partial") / "Contents").mkdir(parents=True)
    elif leftover == "version_is_a_file":
        target.write_text("junk")
    else:
        target.symlink_to(tmp_path / "nowhere")
    result = installer.install(bundle, seams=seams)
    assert result["version"] == "0.1.0" and result["action"] == "install"
    _assert_installed(seams)
    stale = [name for name in _children(seams) if name.startswith("0.1.0.stale-")]
    assert len(stale) == 1  # moved aside, not deleted
    assert not any(name.endswith(".partial") for name in _children(seams))
    assert installer.status(seams=seams)["current"] == "0.1.0"


def test_a_later_install_does_not_prune_or_delete_the_stale_directory(tmp_path, key, seams):
    root = _programs(seams)
    root.mkdir(parents=True, mode=0o700)
    (root / "0.1.0").mkdir(mode=0o700)
    (root / "0.1.0" / "evidence.txt").write_text("keep me")
    installer.install(_bundle(tmp_path, "0.1.0", key), seams=seams)
    installer.install(_bundle(tmp_path, "0.1.1", key, marker=b"v2"), seams=seams)
    stale = next(root.glob("0.1.0.stale-*"))
    assert (stale / "evidence.txt").read_text() == "keep me"


def test_two_stale_directories_of_one_version_get_distinct_names(tmp_path, key, seams):
    root = _programs(seams)
    root.mkdir(parents=True, mode=0o700)
    bundle = _bundle(tmp_path, "0.1.0", key)
    (root / "0.1.0").mkdir(mode=0o700)
    installer.install(bundle, seams=seams)
    shutil.rmtree(root / "0.1.0" / APP)  # wedged again, same clock reading
    installer.install(bundle, seams=seams)
    assert len(list(root.glob("0.1.0.stale-*"))) == 2
    _assert_installed(seams)


def test_a_damaged_installed_copy_of_the_recorded_version_is_repaired_not_wedged(tmp_path, key, seams):
    bundle = _bundle(tmp_path, "0.1.0", key)
    installer.install(bundle, seams=seams)
    lib = _programs(seams) / "0.1.0" / APP / "Contents" / "Frameworks" / "libdemo.dylib"
    lib.write_bytes(b"damaged")
    result = installer.install(bundle, seams=seams)  # the verified bundle's digest equals the recorded one
    assert result["version"] == "0.1.0"
    assert lib.read_bytes() == b"libv1"
    assert len(list(_programs(seams).glob("0.1.0.stale-*"))) == 1


def test_a_different_package_of_an_installed_version_is_still_a_conflict(tmp_path, key, seams):
    installer.install(_bundle(tmp_path, "0.1.0", key), seams=seams)
    other = _bundle(tmp_path, "0.1.0", key, marker=b"evil")  # same version, other signed bytes
    with pytest.raises(InstallerError) as refused:
        installer.install(other, seams=seams)
    assert refused.value.code == "installed_version_conflict"
    assert (_programs(seams) / "0.1.0" / APP / "Contents" / "Frameworks" / "libdemo.dylib").read_bytes() == b"libv1"
    assert not list(_programs(seams).glob("0.1.0.stale-*"))


def test_reinstalling_the_same_verified_package_is_idempotent(tmp_path, key, seams):
    bundle = _bundle(tmp_path, "0.1.0", key)
    installer.install(bundle, seams=seams)
    before = _children(seams)
    installer.install(bundle, seams=seams)
    assert _children(seams) == before


# ------------------------------------------------------------------ atomic staging


def test_the_version_directory_does_not_exist_until_the_copy_verified(tmp_path, key, seams):
    bundle = _bundle(tmp_path, "0.1.0", key)
    seen = {}

    def copy(source, destination):
        seen["version_dir_during_copy"] = (_programs(seams) / "0.1.0").exists()
        seen["staging_during_copy"] = sorted(p.name for p in _programs(seams).iterdir())
        return _ditto(source, destination)

    installer.install(bundle, seams=dataclasses.replace(seams, copy=copy))
    assert seen["version_dir_during_copy"] is False
    assert "0.1.0.partial" in seen["staging_during_copy"]
    assert "0.1.0.partial" not in _children(seams)  # renamed, not copied twice


def test_a_failed_copy_leaves_no_version_directory_and_the_next_install_works(tmp_path, key, seams):
    bundle = _bundle(tmp_path, "0.1.0", key)

    def failing(source, destination):
        _ditto(source, destination)  # a copy that got far, then failed (Gatekeeper kill, "File exists", ...)
        return 1

    with pytest.raises(InstallerError) as refused:
        installer.install(bundle, seams=dataclasses.replace(seams, copy=failing))
    assert refused.value.code == "payload_copy_failed"
    assert _children(seams) == [".install.lock"] or _children(seams) == []
    assert not seams.applications_entry.exists() and not seams.applications_entry.is_symlink()
    installer.install(bundle, seams=seams)
    _assert_installed(seams)


def test_a_copy_that_does_not_match_the_signed_digest_is_never_renamed_into_place(tmp_path, key, seams):
    bundle = _bundle(tmp_path, "0.1.0", key)

    def corrupting(source, destination):
        code = _ditto(source, destination)
        (destination / "Contents" / "Frameworks" / "libdemo.dylib").write_bytes(b"flipped")
        return code

    with pytest.raises(InstallerError) as refused:
        installer.install(bundle, seams=dataclasses.replace(seams, copy=corrupting))
    assert refused.value.code == "payload_copy_mismatch"
    assert not (_programs(seams) / "0.1.0").exists() and not (_programs(seams) / "0.1.0.partial").exists()


def test_an_interrupted_install_leaves_only_staging_and_the_next_install_cleans_it(tmp_path, key, seams):
    bundle = _bundle(tmp_path, "0.1.0", key)

    def interrupted(source, destination):
        _ditto(source, destination)
        raise KeyboardInterrupt  # not an Exception: nothing may catch it as a refusal

    with pytest.raises(KeyboardInterrupt):
        installer.install(bundle, seams=dataclasses.replace(seams, copy=interrupted))
    assert not (_programs(seams) / "0.1.0").exists()
    (_programs(seams) / "0.1.0.partial").mkdir(exist_ok=True)  # as a hard kill would leave it
    (_programs(seams) / "0.1.0.partial" / "junk").write_text("x")
    installer.install(bundle, seams=seams)
    _assert_installed(seams)
    assert "0.1.0.partial" not in _children(seams)


def test_a_leftover_staging_directory_of_another_version_is_discarded(tmp_path, key, seams):
    root = _programs(seams)
    root.mkdir(parents=True, mode=0o700)
    (root / "0.0.9.partial").mkdir(mode=0o700)
    installer.install(_bundle(tmp_path, "0.1.0", key), seams=seams)
    assert "0.0.9.partial" not in _children(seams)


# ------------------------------------------------------------------ the install lock


def test_a_second_concurrent_installer_refuses_with_a_human_code_and_changes_nothing(tmp_path, key, seams, capsys):
    bundle = _bundle(tmp_path, "0.1.0", key)
    entered, release = threading.Event(), threading.Event()

    def slow(source, destination):
        entered.set()
        assert release.wait(60)
        return _ditto(source, destination)

    outcome: dict = {}
    first = threading.Thread(target=lambda: outcome.update(installer.install(
        bundle, seams=dataclasses.replace(seams, copy=slow))))
    first.start()
    try:
        assert entered.wait(60)
        before = _children(seams)
        with pytest.raises(InstallerError) as refused:
            installer.install(bundle, seams=seams)
        assert refused.value.code == "install_already_running"
        assert _children(seams) == before  # the second one touched nothing, not the first one's staging either
        assert installer.main(["--install", "--bundle", str(bundle)], seams=seams) == 2
        refusal = json.loads(capsys.readouterr().err)
        assert refusal["refused"] == "install_already_running"
        assert "another" in refusal["message"].lower()
    finally:
        release.set()
        first.join(60)
    assert outcome["version"] == "0.1.0"
    _assert_installed(seams)
    assert not list(_programs(seams).glob("*.stale-*")) and "0.1.0.partial" not in _children(seams)


def test_uninstall_and_rollback_also_refuse_while_an_install_holds_the_lock(tmp_path, key, seams):
    installer.install(_bundle(tmp_path, "0.1.0", key), seams=seams)
    installer.install(_bundle(tmp_path, "0.1.1", key, marker=b"v2"), seams=seams)
    import mycelium_host_platform

    with mycelium_host_platform.hold_exclusive_lock(_programs(seams), ".install.lock", blocking=False):
        for action in (installer.uninstall, installer.rollback):
            with pytest.raises(InstallerError) as refused:
                action(seams=seams)
            assert refused.value.code == "install_already_running"
    assert installer.status(seams=seams)["current"] == "0.1.1"
    assert installer.rollback(seams=seams)["version"] == "0.1.0"  # the lock was released


def test_the_lock_is_released_after_a_failed_install(tmp_path, key, seams):
    bundle = _bundle(tmp_path, "0.1.0", key)
    with pytest.raises(InstallerError):
        installer.install(bundle, seams=dataclasses.replace(seams, copy=lambda s, d: 1))
    installer.install(bundle, seams=seams)  # would be install_already_running if the lock leaked


def test_verification_refusals_still_precede_the_lock_and_every_write(tmp_path, key, seams):
    bundle = _bundle(tmp_path, "0.1.0", key)
    lib = bundle / APP / "Contents" / "Frameworks" / "libdemo.dylib"
    lib.write_bytes(lib.read_bytes() + b"!")
    with pytest.raises(InstallerError) as refused:
        installer.install(bundle, seams=seams)
    assert refused.value.code == "package_digest_mismatch"
    assert not (seams.home / "Library").exists() and not (seams.home / "Applications").exists()


# ------------------------------------------------------------------ never a traceback


def test_an_unexpected_oserror_in_the_copy_is_a_structured_refusal(tmp_path, key, seams, capsys):
    bundle = _bundle(tmp_path, "0.1.0", key)

    def raising(source, destination):
        raise FileExistsError(17, "File exists")

    assert installer.main(["--install", "--bundle", str(bundle)],
                          seams=dataclasses.replace(seams, copy=raising)) == 2
    captured = capsys.readouterr()
    refusal = json.loads(captured.err)
    assert refusal["refused"] == "install_io_error" and refusal["errno"] == "EEXIST"
    assert "Traceback" not in captured.err and str(tmp_path) not in captured.err  # no paths leak
    assert "0.1.0" not in _children(seams) and "0.1.0.partial" not in _children(seams)


def test_an_unexpected_non_os_error_is_still_a_structured_refusal(tmp_path, key, seams, capsys):
    bundle = _bundle(tmp_path, "0.1.0", key)

    def boom(source, destination):
        raise RuntimeError("secret detail")

    assert installer.main(["--install", "--bundle", str(bundle)], seams=dataclasses.replace(seams, copy=boom)) == 2
    refusal = json.loads(capsys.readouterr().err)
    assert refusal["refused"] == "installer_failed" and refusal["error_type"] == "RuntimeError"
    assert "secret detail" not in json.dumps(refusal)


def test_a_missing_bundle_is_a_refusal_not_a_traceback(tmp_path, seams, capsys):
    assert installer.main(["--install", "--bundle", str(tmp_path / "nope")], seams=seams) == 2
    assert json.loads(capsys.readouterr().err)["refused"] == "bundle_incomplete"


@pytest.mark.parametrize("content", [b"not json", b"[]", b'{"protocol": "mycelium.a13_macos_installer.v1"}'])
def test_a_corrupt_installation_record_is_a_refusal(tmp_path, key, seams, capsys, content):
    bundle = _bundle(tmp_path, "0.1.0", key)
    installer.install(bundle, seams=seams)
    record = _programs(seams) / "versions.json"
    record.write_bytes(content)
    record.chmod(0o600)
    assert installer.main(["--install", "--bundle", str(bundle)], seams=seams) == 2
    assert json.loads(capsys.readouterr().err)["refused"] in {"installation_record_invalid",
                                                              "installation_record_untrusted"}


def test_rollback_to_a_previous_version_that_lost_its_payload_is_a_refusal(tmp_path, key, seams, capsys):
    installer.install(_bundle(tmp_path, "0.1.0", key), seams=seams)
    installer.install(_bundle(tmp_path, "0.1.1", key, marker=b"v2"), seams=seams)
    shutil.rmtree(_programs(seams) / "0.1.0" / APP)
    assert installer.main(["--rollback"], seams=seams) == 2
    assert json.loads(capsys.readouterr().err)["refused"] == "previous_version_missing"
    assert installer.status(seams=seams)["current"] == "0.1.1"


def test_uninstall_removes_stale_directories_and_the_lock_file_too(tmp_path, key, seams):
    root = _programs(seams)
    root.mkdir(parents=True, mode=0o700)
    (root / "0.1.0").mkdir(mode=0o700)
    installer.install(_bundle(tmp_path, "0.1.0", key), seams=seams)
    assert list(root.glob("0.1.0.stale-*")) and (root / ".install.lock").exists()
    installer.uninstall(seams=seams)
    assert not root.exists() and not seams.applications_entry.is_symlink()


# ------------------------------------------------------------------ quarantine (F3)


def test_the_installed_copy_carries_no_quarantine_and_the_source_is_not_touched(tmp_path, key, seams):
    bundle = _bundle(tmp_path, "0.1.0", key, quarantine=True)
    source_app = bundle / APP
    assert _xattr(source_app / EXE) == QVALUE
    result = installer.install(bundle, seams=seams)
    installed = _programs(seams) / "0.1.0" / APP
    assert _quarantined(installed) == []  # app, executable, nested files and the links themselves
    assert result["quarantine_cleared"] is True
    assert _xattr(source_app / EXE) == QVALUE  # the downloaded bundle is the user's to judge, not the installer's
    assert _xattr(seams.applications_entry / EXE) is None


def test_quarantine_is_cleared_only_from_the_verified_staged_copy_and_only_after_the_digest_check(tmp_path, key, seams):
    bundle = _bundle(tmp_path, "0.1.0", key, quarantine=True)
    calls: list[tuple[Path, bool, bool]] = []

    def clear(path: Path) -> bool:
        staged = _programs(seams) / "0.1.0.partial" / APP
        calls.append((path, path == staged, (_programs(seams) / "0.1.0").exists()))
        return True

    installer.install(bundle, seams=dataclasses.replace(seams, clear_quarantine=clear))
    assert calls == [(_programs(seams) / "0.1.0.partial" / APP, True, False)]  # staged, before the rename


def test_nothing_is_cleared_when_the_signed_verification_refuses(tmp_path, key, seams):
    bundle = _bundle(tmp_path, "0.1.0", key, quarantine=True)
    lib = bundle / APP / "Contents" / "Frameworks" / "libdemo.dylib"
    lib.write_bytes(lib.read_bytes() + b"!")
    calls: list[Path] = []
    with pytest.raises(InstallerError) as refused:
        installer.install(bundle, seams=dataclasses.replace(seams, clear_quarantine=lambda p: calls.append(p) or True))
    assert refused.value.code == "package_digest_mismatch" and calls == []
    assert _xattr(bundle / APP / EXE) == QVALUE


def test_nothing_is_cleared_when_the_staged_copy_fails_its_digest(tmp_path, key, seams):
    bundle = _bundle(tmp_path, "0.1.0", key, quarantine=True)
    calls: list[Path] = []

    def corrupting(source, destination):
        code = _ditto(source, destination)
        (destination / "Contents" / "Frameworks" / "libdemo.dylib").write_bytes(b"flipped")
        return code

    with pytest.raises(InstallerError):
        installer.install(bundle, seams=dataclasses.replace(
            seams, copy=corrupting, clear_quarantine=lambda p: calls.append(p) or True))
    assert calls == []


def test_reinstalling_a_verified_package_clears_quarantine_on_the_existing_copy(tmp_path, key, seams):
    bundle = _bundle(tmp_path, "0.1.0", key, quarantine=True)
    installer.install(bundle, seams=dataclasses.replace(seams, clear_quarantine=lambda p: False))
    installed = _programs(seams) / "0.1.0" / APP
    assert _quarantined(installed) != []  # an install by the released v0.1.0 left this behind
    installer.install(bundle, seams=seams)
    assert _quarantined(installed) == []


def test_a_failed_quarantine_removal_is_reported_not_fatal(tmp_path, key, seams):
    bundle = _bundle(tmp_path, "0.1.0", key, quarantine=True)
    result = installer.install(bundle, seams=dataclasses.replace(seams, clear_quarantine=lambda p: False))
    assert result["quarantine_cleared"] is False  # still verified, still installed; macOS just translocates it
    _assert_installed(seams)


def test_the_default_clear_removes_the_attribute_everywhere_and_never_raises(tmp_path, fake_xattrs):
    tree = _app(tmp_path)
    for path in [tree, *tree.rglob("*")]:
        _xattr(path, write=QVALUE, link=path.is_symlink())
    assert installer.clear_quarantine(tree, run=fake_xattrs.run) is True
    assert _quarantined(tree) == []
    assert installer.clear_quarantine(tree, run=fake_xattrs.run) is True  # nothing left to clear is still success
    assert installer.clear_quarantine(tmp_path / "missing", run=fake_xattrs.run) is False


def test_the_default_clear_runs_xattr_without_a_shell_and_maps_failures(monkeypatch, tmp_path):
    ran = []

    def fake_run(argv, **kwargs):
        ran.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 1, "", "denied")

    assert installer.clear_quarantine(tmp_path, run=fake_run) is False
    argv, kwargs = ran[0]
    assert argv[0] == "/usr/bin/xattr" and argv[-2] == QUARANTINE and argv[-1] == str(tmp_path)
    assert not kwargs.get("shell")

    def missing_binary(argv, **kwargs):
        raise FileNotFoundError(2, "no xattr")

    assert installer.clear_quarantine(tmp_path, run=missing_binary) is False
