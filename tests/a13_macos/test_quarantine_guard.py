# SPDX-License-Identifier: AGPL-3.0-only
"""Regression probes must fail BEFORE reaching the real attribute-writing API."""
import os
import shlex
import subprocess
import sys

import pytest

from fake_xattrs import QUARANTINE


@pytest.mark.parametrize("flags", [["-w"], ["-r", "-w"], ["-rw"], ["-s", "-w"], ["--write"]])
@pytest.mark.parametrize("entry", ["run", "Popen", "shell", "quoted_shell", "system", "script"])
def test_guard_rejects_child_attribute_writes_before_spawn(tmp_path, flags, entry):
    argv = ["/usr/bin/xattr", *flags, QUARANTINE, "fake", str(tmp_path / "never-created")]
    with pytest.raises(RuntimeError, match="REAL QUARANTINE WRITE FORBIDDEN"):
        if entry == "run":
            subprocess.run(argv, check=True)
        elif entry == "Popen":
            subprocess.Popen(argv)
        elif entry == "shell":
            subprocess.run(shlex.join(argv), shell=True, check=True)
        elif entry == "quoted_shell":
            subprocess.run(" ".join('"' + arg + '"' for arg in argv), shell=True, check=True)
        elif entry == "system":
            os.system(shlex.join(argv))
        else:
            script = tmp_path / "unsafe.sh"
            script.write_text(shlex.join(argv))
            subprocess.run(["/bin/sh", str(script)], check=True)


def test_guard_rejects_python_child_attribute_write(tmp_path):
    code = f"import os; os.setxattr({str(tmp_path)!r}, {QUARANTINE!r}, b'fake')"
    with pytest.raises(RuntimeError, match="REAL QUARANTINE WRITE FORBIDDEN"):
        subprocess.run([sys.executable, "-c", code], check=True)


def test_guard_rejects_os_api_when_available(tmp_path):
    if not hasattr(os, "setxattr"):
        pytest.skip("this Python has no os.setxattr")
    with pytest.raises(RuntimeError, match="REAL QUARANTINE WRITE FORBIDDEN"):
        os.setxattr(tmp_path, QUARANTINE, b"fake")


def test_guard_rejects_xattr_module_when_available(tmp_path):
    xattr = pytest.importorskip("xattr")
    with pytest.raises(RuntimeError, match="REAL QUARANTINE WRITE FORBIDDEN"):
        xattr.setxattr(str(tmp_path), QUARANTINE, b"fake")
    attributes = xattr.xattr(str(tmp_path))
    with pytest.raises(RuntimeError, match="REAL QUARANTINE WRITE FORBIDDEN"):
        attributes.set(QUARANTINE, b"fake")
    with pytest.raises(RuntimeError, match="REAL QUARANTINE WRITE FORBIDDEN"):
        attributes[QUARANTINE] = b"fake"
