# SPDX-License-Identifier: AGPL-3.0-only
"""The Windows node closure imports without POSIX-only modules.

Two complementary checks:

1. a child interpreter with ``fcntl``/``resource``/``termios``/``pwd``/``grp``
   made unimportable imports every Windows entry point (the node agent, the
   physical node service, the service runner, the stage loaders);
2. an AST lint over every repository module in that import closure refuses
   import-time use of POSIX-only modules or attributes (an unguarded
   ``import fcntl``, ``os.getuid()`` or ``signal.SIGKILL`` at module scope),
   so a new one cannot slip in behind an unrelated guard.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
POSIX_ONLY_MODULES = frozenset({"fcntl", "resource", "termios", "pwd", "grp", "tty", "pty"})
POSIX_ONLY_ATTRIBUTES = {
    "os": frozenset(
        {
            "O_NOFOLLOW",
            "O_DIRECTORY",
            "getuid",
            "geteuid",
            "getgid",
            "getgroups",
            "killpg",
            "fchdir",
            "setsid",
            "getpgrp",
            "getsid",
            "sysconf",
        }
    ),
    "signal": frozenset({"SIGKILL", "SIGALRM", "setitimer", "ITIMER_REAL", "SIGHUP"}),
    "socket": frozenset({"AF_UNIX"}),
}
ENTRY_POINTS = (
    "mycelium_host_platform",
    "mycelium_host_platform._win32",
    "mycelium_host_platform._win32_job",
    "mycelium_node.__main__",
    "mycelium_node.process",
    "physical_inference_node",
    "mycelium_physical_runner",
    "mycelium_service_runner",
    "mycelium_service_package",
    "mycelium_a13_windows.onboarding",
    # The A13 managed host modules a Windows host agent reuses (M1).
    "mycelium_a13_macos.authority",
    "mycelium_a13_macos.managed_offers",
    "mycelium_a13_macos.managed_control",
    "mycelium_a13_macos.control_sequence",
    "mycelium_a13_macos.control_pump",
    "mycelium_a13_macos.concurrent_pump",
    "mycelium_a13_macos.managed_runtime",
    "mycelium_a13_macos.worker",
    "mycelium_a13_windows.managed_host.staging",
    "mycelium_a13_windows.managed_host.app",
    "mycelium_a13_windows.managed_host.member",
    "mycelium_a13_windows.managed_host.resources",
    "mycelium_a13_windows.managed_host.__main__",
    "mycelium_a7_fence_store",
    "mycelium_a13_macos.target_app",
    "mycelium_a13_macos.workflow",
    # Browser-facing owner/gateway on Windows: app shell, local TLS, owner journal.
    "mycelium_a13_macos.app",
    "mycelium_a13_macos.tls",
    "mycelium_a13_macos.control_session",
    "mycelium_a13_macos.public_relay",
    "mycelium_a13_macos.acquisition",
    "mycelium_a13_macos.child_environment",
    "mycelium_a13_windows.import_model",
    "mycelium_a13_windows.service_install",
    "mycelium_a13_windows.installer",
    "mycelium_a13_packaging.manifest",
    "mycelium_a13_packaging.install_source",
    "mycelium_a13_packaging.build",
    "mycelium_a13_packaging.installer_core",
    "stage_pack",
    "runtime_loader",
    # A9 on a Windows host: the windows-probe / windows-benchmark driver path
    # (local-declaration binds observe_host_platform from seed_migration), the
    # member-sign / member-declare path, and the migration executor they import.
    "mycelium_platform_neutral.migration_executor",
    "mycelium_platform_neutral.migration",
    "mycelium_platform_neutral.seed_migration",
    "mycelium_platform_neutral.native_int8",
    "mycelium_platform_neutral.windows_provider",
    "mycelium_platform_neutral.int8_stage_benchmark",
    "mycelium_platform_neutral.runtime_efficiency",
    "mycelium_platform_neutral.thermal",
    "mycelium_node.durable_membership",
)

_PROBE = """
import json, sys
for name in {blocked!r}:
    sys.modules[name] = None
import importlib
for name in {entries!r}:
    importlib.import_module(name)
import pathlib
# Compare resolved paths, not string prefixes: on Windows the separator is "\\".
root = pathlib.Path({root!r}).resolve()
def inside(value):
    try:
        relative = pathlib.Path(value).resolve().relative_to(root)
    except ValueError:
        return False
    return ".venv" not in relative.parts and "tests" not in relative.parts
files = sorted(
    str(pathlib.Path(module.__file__).resolve())
    for module in list(sys.modules.values())
    if getattr(module, "__file__", None) and inside(module.__file__)
)
print(json.dumps(files))
"""


@pytest.fixture(scope="module")
def closure() -> list[Path]:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE.format(
                blocked=sorted(POSIX_ONLY_MODULES),
                entries=list(ENTRY_POINTS),
                root=str(ROOT),
            ),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    files = [Path(item) for item in json.loads(completed.stdout.strip().splitlines()[-1])]
    assert files
    return files


def test_every_windows_entry_point_imports_without_posix_modules(closure) -> None:
    names = {path.relative_to(ROOT).as_posix() for path in closure}
    for required in (
        "physical_inference_node.py",
        "mycelium_node/__main__.py",
        "mycelium_node/process.py",
        "mycelium_physical_runner/lock.py",
        "mycelium_service_runner.py",
        "mycelium_host_platform/_win32_job.py",
    ):
        assert required in names


def _guarded_by_import_error(stack: list[ast.AST]) -> bool:
    for node in stack:
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                caught = handler.type
                names = (
                    [caught]
                    if isinstance(caught, (ast.Name, ast.Attribute))
                    else list(getattr(caught, "elts", []))
                )
                if any(
                    isinstance(item, ast.Name)
                    and item.id in {"ImportError", "ModuleNotFoundError"}
                    for item in names
                ):
                    return True
    return False


def import_time_violations(source: str, filename: str) -> list[str]:
    """POSIX-only imports or attributes evaluated when the module is imported."""

    tree = ast.parse(source, filename=filename)
    violations: list[str] = []

    def visit(node: ast.AST, stack: list[ast.AST]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # Only defaults and decorators run at import time.
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                if default is not None:
                    visit(default, stack)
            for decorator in getattr(node, "decorator_list", []):
                visit(decorator, stack)
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in POSIX_ONLY_MODULES and not _guarded_by_import_error(stack):
                    violations.append(f"{filename}:{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            if node.level == 0 and module in POSIX_ONLY_MODULES and not _guarded_by_import_error(stack):
                violations.append(f"{filename}:{node.lineno}: from {node.module} import")
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.attr in POSIX_ONLY_ATTRIBUTES.get(node.value.id, ())
        ):
            violations.append(f"{filename}:{node.lineno}: {node.value.id}.{node.attr}")
        for child in ast.iter_child_nodes(node):
            visit(child, [*stack, node])

    visit(tree, [])
    return violations


def test_the_import_closure_has_no_import_time_posix_dependency(closure) -> None:
    violations: list[str] = []
    for path in closure:
        violations.extend(
            import_time_violations(path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix())
        )
    assert violations == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import fcntl\n", 1),
        ("import resource as r\n", 1),
        ("from pwd import getpwuid\n", 1),
        ("try:\n    import fcntl\nexcept ImportError:\n    fcntl = None\n", 0),
        ("def f():\n    import fcntl\n", 0),
        ("import os\nUID = os.getuid()\n", 1),
        ("import os\nFLAGS = os.O_RDONLY | os.O_NOFOLLOW\n", 1),
        ("import os\nFLAGS = getattr(os, 'O_NOFOLLOW', 0)\n", 0),
        ("import signal\ndef stop():\n    return signal.SIGKILL\n", 0),
        ("import signal\nclass A:\n    KILL = signal.SIGKILL\n", 1),
        ("import os\ndef f(uid=os.getuid()):\n    return uid\n", 1),
    ],
)
def test_the_lint_itself_catches_import_time_uses(source, expected) -> None:
    assert len(import_time_violations(source, "probe.py")) == expected
