# SPDX-License-Identifier: AGPL-3.0-only
"""The Linux import guard: node/seed/membership/gateway modules import with macOS-only modules absent.

The guard runs in a child interpreter with ``sys.platform == "linux"`` and the PyObjC bridges, ``mlx`` and the
Windows-only modules unimportable. UNVERIFIED-on-hardware: this proves import-time behaviour under a simulated
platform only, not that a real Linux host runs the node.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import textwrap

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "linux_import_guard.py"


def _run(*args: str) -> tuple[int, list[dict], dict]:
    completed = subprocess.run(
        [sys.executable, "-B", str(GUARD), *args],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=240,
        check=False,
    )
    lines = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
    assert lines, completed.stderr
    return completed.returncode, lines[:-1], lines[-1]


def _fake_checkout(tmp_path: Path) -> Path:
    package = tmp_path / "mycelium_node"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "fine.py").write_text("import sys\nVALUE = sys.platform\n")
    (package / "lazy.py").write_text(textwrap.dedent("""\
        import sys

        def probe():
            if sys.platform == "darwin":
                import objc
                return objc
            return None
        """))
    (package / "eager_objc.py").write_text("import objc\n")
    (package / "eager_foundation.py").write_text("from Foundation import NSBundle\n")
    (package / "eager_mlx.py").write_text("import mlx.core as mx\n")
    (package / "eager_msvcrt.py").write_text("import msvcrt\n")
    return tmp_path


def test_guard_reports_seeded_macos_only_imports_and_accepts_lazy_ones(tmp_path):
    root = _fake_checkout(tmp_path)
    code, failures, summary = _run("--root", str(root), "--packages", "mycelium_node", "--modules", "")
    assert code == 1
    assert {f["module"] for f in failures} == {
        "mycelium_node.eager_objc",
        "mycelium_node.eager_foundation",
        "mycelium_node.eager_mlx",
        "mycelium_node.eager_msvcrt",
    }
    assert all(f["at"].startswith("mycelium_node/eager_") for f in failures), failures
    assert summary["failed"] == 4 and summary["imported_ok"] == 3  # package, fine, lazy
    assert summary["simulated_platform"] == "linux"
    assert summary["status"] == "UNVERIFIED-on-hardware"


def test_guard_simulates_linux_platform(tmp_path):
    package = tmp_path / "mycelium_node"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "needs_linux.py").write_text(
        "import platform, sys\nassert sys.platform == 'linux' and platform.system() == 'Linux'\n"
    )
    code, failures, summary = _run("--root", str(tmp_path), "--packages", "mycelium_node", "--modules", "")
    assert (code, failures, summary["failed"]) == (0, [], 0)


def test_node_seed_membership_gateway_modules_import_on_simulated_linux():
    code, failures, summary = _run()
    assert failures == []
    assert code == 0
    assert summary["failed"] == 0
    assert summary["imported_ok"] >= 80  # node, seed, membership, request gateway, neutral, host platform, a13_linux


def test_whole_repo_only_mlx_backends_need_macos_at_import_time():
    code, failures, summary = _run("--all")
    assert failures == [], failures
    assert code == 0
    assert "mycelium_router.mlx_runtime" in summary["expected_mlx_only"]
    assert "qwen35_mlx" in summary["expected_mlx_only"]
