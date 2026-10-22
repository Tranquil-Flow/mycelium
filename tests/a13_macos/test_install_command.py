# SPDX-License-Identifier: AGPL-3.0-only
"""``Install Mycelium.command``: the one script the user approves in Gatekeeper.

The documented flow (docs/user/QUICKSTART-MACOS.md, docs/design/MACOS-INSTALL-FLOW.md) is one
Open Anyway, for this script.  It must therefore make the *app* runnable without a second
Gatekeeper block -- but only after a shell-level check of the documents and of every file of the
app, and it must never touch anything it did not check.

SAFETY: all attributes exist only in FakeXattrs memory. No real quarantine is set,
even on directories, links or data. The shell's XATTR function dependency is replaced
in the script copy with a private loopback adapter into that fake. RUNNER points at
an out-of-bundle stub; nothing in the app is ever executed. Hash/file validation and
shell ordering remain real. These tests do not qualify Finder or Gatekeeper.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import finalize_a13_macos  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not Path("/usr/bin/ditto").exists(),
                                reason="needs macOS ditto/xattr/shasum")

@pytest.fixture(autouse=True)
def attributes(fake_xattrs):
    """Require fake attributes for every test in this module."""


ISSUED = 1_800_000_000.0
from fake_xattrs import QUARANTINE
QVALUE = "0083;6700f000;Safari;F00DF00D-0000-0000-0000-000000000000"
APP = "Mycelium A13 Demo.app"
EXE = "Contents/MacOS/Mycelium A13 Demo"
RUNNER_LINE = 'RUNNER="$EXE"\n'

#: Records what the installer would have been started with and what state the app was in at that moment
#: (the app folder and a data file inside it).  Lives outside the bundle, so it never carries a quarantine.
STUB = """#!/bin/sh
BUNDLE="$3"
OUT="$BUNDLE/ran.txt"
{ printf 'args:'; for a in "$@"; do printf ' [%s]' "$a"; done; echo; } > "$OUT"
for item in "$BUNDLE/Mycelium A13 Demo.app" "$BUNDLE/Mycelium A13 Demo.app/Contents/Resources/data.bin"; do
  if "__FAKE_XATTR__" -p __ATTRIBUTE__ "$item" >/dev/null 2>&1; then echo quarantined >> "$OUT"; else echo clean >> "$OUT"; fi
done
echo '{"action": "install", "version": "0.1.0"}'
exit "${FAKE_APP_EXIT:-0}"
"""


def _xattr(path: Path, value: str | None = None) -> str | None:
    raise AssertionError("the fake_xattrs fixture must inject this function")


def _quarantine_tree(root: Path, *, skip: tuple[Path, ...]) -> None:
    """Quarantine every file, folder and link below root, like Archive Utility does (data only; nothing is run)."""

    for path in [root, *root.rglob("*")]:
        if path not in skip:
            _xattr(path, QVALUE)


def _quarantined(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in [root, *root.rglob("*")] if _xattr(p) is not None)


@pytest.fixture
def bundle(tmp_path, fake_xattrs, fake_xattr_command):
    """A finalized bundle, zipped and extracted the way a user's Mac does it, then marked in fake memory only."""

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = tmp_path / "release.key"
    key.write_bytes(Ed25519PrivateKey.generate().private_bytes_raw())
    app = tmp_path / "build" / APP
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Frameworks").mkdir()
    (app / "Contents" / "Frameworks" / "libdemo.dylib").write_bytes(b"lib")
    (app / "Contents" / "Resources").mkdir()
    (app / "Contents" / "Resources" / "data.bin").write_bytes(b"data")
    (app / "Contents" / "Resources" / "libdemo").symlink_to("../Frameworks/libdemo.dylib")
    (app / EXE).write_bytes(b"stub-executable-never-run")  # data, mode 0644: nothing in this bundle is ever spawned
    (app / EXE).chmod(0o644)
    report = finalize_a13_macos.finalize_macos_bundle(
        app_dir=app, output_root=tmp_path / "out", signing_key_path=key, version="0.1.0", issued_at=ISSUED, root=REPO)
    extract = tmp_path / "Down loads"  # a space on purpose
    subprocess.run(["/usr/bin/ditto", "-x", "-k", report["zip"], str(extract)], check=True)
    folder = extract / "mycelium-0.1.0-macos-arm64"
    # the script under test: the real text with the one runner line pointed at an out-of-bundle stub
    text = (folder / "Install Mycelium.command").read_text()
    assert text.count(RUNNER_LINE) == 1
    stub = tmp_path / "stub" / "app-stub"
    stub.parent.mkdir()
    stub.write_text(STUB.replace("__FAKE_XATTR__", str(fake_xattr_command)).replace("__ATTRIBUTE__", QUARANTINE))
    stub.chmod(0o755)
    assert text.count("XATTR=/usr/bin/xattr\n") == 1
    text = text.replace("XATTR=/usr/bin/xattr\n", f'XATTR="{fake_xattr_command}"\n')
    (folder / "run-under-test.command").write_text(text.replace(RUNNER_LINE, f'RUNNER="{stub}"\n'))
    (folder / "Install Mycelium.command").chmod(0o644)  # the real file is only hashed here, never run
    _quarantine_tree(folder, skip=(folder / "run-under-test.command",))
    return folder


def _run(bundle: Path, **env: str) -> subprocess.CompletedProcess:
    stub = next(bundle.parent.parent.glob("stub/app-stub"))
    assert _xattr(stub) is None and bundle not in stub.parents, "the stub must live outside the quarantined bundle"
    # The interpreter is executed; the script is only read.  Same code path as double-clicking.
    return subprocess.run(["/bin/sh", str(bundle / "run-under-test.command")], capture_output=True, text=True,
                          env={**os.environ, **env}, cwd="/", timeout=120)


def test_the_script_clears_quarantine_from_the_app_only_then_runs_the_installer(bundle):
    assert _quarantined(bundle / APP)  # the whole download is quarantined, as Archive Utility leaves it
    done = _run(bundle)
    assert done.returncode == 0, done.stderr
    ran = (bundle / "ran.txt").read_text().splitlines()
    assert ran == [f"args: [--install] [--bundle] [{bundle}]", "clean", "clean"]  # app folder and data: clean when it ran
    assert _quarantined(bundle / APP) == []
    assert _xattr(bundle / "installation-package-manifest.json") == QVALUE  # documents: not ours to touch
    assert "installed" in done.stdout.lower() and "Applications" in done.stdout


@pytest.mark.parametrize("tamper", ["manifest_byte", "install_source_byte", "command_edit", "sums_missing",
                                    "sums_without_manifest", "sums_without_install_source", "sums_without_app_files",
                                    "sums_empty", "app_file_byte", "app_file_missing", "app_extra_file",
                                    "app_extra_link_absolute", "app_files_list_edit"])
def test_a_document_that_does_not_match_the_sums_stops_everything_before_any_trust_is_extended(bundle, tamper):
    sums = bundle / "SHA256SUMS"
    if tamper == "manifest_byte":
        (bundle / "installation-package-manifest.json").write_text("{}\n")
    elif tamper == "install_source_byte":
        with (bundle / "install-source.json").open("a") as handle:
            handle.write(" ")
    elif tamper == "command_edit":
        with (bundle / "Install Mycelium.command").open("a") as handle:
            handle.write("# edited\n")
    elif tamper == "sums_missing":
        sums.unlink()
    elif tamper == "sums_without_manifest":
        sums.write_text("".join(line for line in sums.read_text().splitlines(True)
                                if not line.rstrip().endswith("installation-package-manifest.json")))
    elif tamper == "sums_without_install_source":
        sums.write_text("".join(line for line in sums.read_text().splitlines(True)
                                if not line.rstrip().endswith("install-source.json")))
    elif tamper == "sums_without_app_files":
        sums.write_text("".join(line for line in sums.read_text().splitlines(True)
                                if not line.rstrip().endswith("APP-FILES.sha256")))
    elif tamper == "app_file_byte":
        with (bundle / APP / "Contents" / "Resources" / "data.bin").open("ab") as handle:
            handle.write(b"x")
    elif tamper == "app_file_missing":
        (bundle / APP / "Contents" / "Resources" / "data.bin").unlink()
    elif tamper == "app_extra_file":
        (bundle / APP / "Contents" / "Resources" / "extra.bin").write_bytes(b"extra")
    elif tamper == "app_extra_link_absolute":
        (bundle / APP / "Contents" / "Resources" / "out").symlink_to("/etc/hosts")
    elif tamper == "app_files_list_edit":
        with (bundle / "APP-FILES.sha256").open("a") as handle:
            handle.write("\n")
    else:
        sums.write_text("")
    before = _quarantined(bundle / APP)  # app folder, data, links: quarantined; the stub executable never is
    done = _run(bundle)
    assert done.returncode == 2
    assert "not installed" in done.stderr.lower()
    assert not (bundle / "ran.txt").exists()  # the app never ran
    assert _quarantined(bundle / APP) == before and before  # and nothing was un-quarantined


@pytest.mark.parametrize("damage", ["app_is_a_link", "executable_is_a_link", "executable_missing"])
def test_an_app_that_is_not_a_plain_bundle_inside_this_folder_is_refused(bundle, tmp_path, damage):
    app = bundle / APP
    if damage == "app_is_a_link":
        elsewhere = tmp_path / "elsewhere.app"
        app.rename(elsewhere)
        app.symlink_to(elsewhere)
    elif damage == "executable_is_a_link":
        target = tmp_path / "other-exe"
        target.write_text("data, never run")
        (app / EXE).unlink()
        (app / EXE).symlink_to(target)
    else:
        (app / EXE).unlink()
    done = _run(bundle)
    assert done.returncode == 2 and not (bundle / "ran.txt").exists()
    assert "not installed" in done.stderr.lower()


def test_the_script_refuses_when_fake_quarantine_removal_fails(bundle, fake_xattrs):
    fake_xattrs.fail_delete = True
    before = _quarantined(bundle / APP)
    done = _run(bundle)
    assert done.returncode == 2
    assert "download mark could not be removed" in done.stderr
    assert not (bundle / "ran.txt").exists()
    assert _quarantined(bundle / APP) == before and before


def test_fake_marks_include_the_link_itself(bundle, fake_xattrs):
    link = bundle / APP / "Contents/Resources/libdemo"
    assert fake_xattrs.attribute(link, link=True) == QVALUE
    assert _run(bundle).returncode == 0
    assert fake_xattrs.attribute(link, link=True) is None


def test_the_installer_refusal_is_passed_through_and_never_reported_as_installed(bundle):
    done = _run(bundle, FAKE_APP_EXIT="2")
    assert done.returncode == 2
    assert "is installed" not in done.stdout.lower()


def test_the_script_uses_no_network_no_admin_and_no_global_trust_changes():
    text = finalize_a13_macos._INSTALL_COMMAND
    for forbidden in ("sudo", "curl", "wget", "nc ", "spctl", "csrutil", "defaults write", "chmod -R", "rm -r"):
        assert forbidden not in text
    assert text.count("xattr") == 1 and QUARANTINE in text  # one targeted removal, of the app only


def test_sums_cover_the_documents_the_script_checks(bundle):
    listed = {line.split("  ", 1)[1] for line in (bundle / "SHA256SUMS").read_text().splitlines()}
    assert {"installation-package-manifest.json", "install-source.json", "Install Mycelium.command",
            "APP-FILES.sha256"} <= listed
    # every regular file of the app, relative to it, hashed; links are not hashed here (the installer's
    # signed digest covers them) but the script refuses absolute ones
    files = {line.split("  ", 1)[1] for line in (bundle / "APP-FILES.sha256").read_text().splitlines()}
    assert files == {"Contents/Frameworks/libdemo.dylib", "Contents/Resources/data.bin", EXE}
