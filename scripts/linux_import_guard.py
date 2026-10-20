# SPDX-License-Identifier: AGPL-3.0-only
"""Linux import guard: import the node/seed/membership/gateway modules as a Linux host would.

usage: python -B scripts/linux_import_guard.py [--root CHECKOUT] [--json-only]

Runs in a child interpreter on any host (it needs no Linux). Before importing anything it

* sets ``sys.platform = "linux"`` and ``platform.system()``/``os.uname()``-style probes to Linux values,
* makes the macOS-only modules unimportable (PyObjC bridges, ``mlx``, ``rumps``, ...), the way
  they are absent on a Linux machine.

It then imports every module under the guarded packages (plus a few top-level node entry
points). A module that fails at import time on this path is a Linux defect. It prints one JSON
line per failure and a summary line; exit 1 on any failure.

Third-party packages that are simply not installed in the checking interpreter are reported
separately as ``missing_third_party`` and do not fail the guard: only platform causes do.

Result is UNVERIFIED-on-hardware: it proves import-time behaviour under a simulated platform, not
that the module works on a real Linux machine.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.abc
import importlib.util
import json
import os
import pkgutil
import platform
import sys
import traceback
from pathlib import Path

# Modules that exist only on macOS (or Apple Silicon). Unimportable on the simulated host.
MACOS_ONLY_MODULES = (
    "objc",
    "Foundation",
    "AppKit",
    "Cocoa",
    "Quartz",
    "CoreFoundation",
    "CoreServices",
    "SystemConfiguration",
    "IOKit",
    "LaunchServices",
    "Security",
    "rumps",
    "mlx",
    "mlx_lm",
    "_scproxy",
)
# Windows-only stdlib modules must stay unimportable too: a Linux host has none of them.
WINDOWS_ONLY_MODULES = ("winreg", "msvcrt", "_winapi", "nt", "win32api", "win32con", "win32file")

PACKAGES = (
    "mycelium_node",
    "mycelium_seed",
    "mycelium_membership",
    "mycelium_request_gateway",
    "mycelium_platform_neutral",
    "mycelium_host_platform",
    "mycelium_a13_linux",
)
MODULES = (
    "physical_inference_node",
    "mycelium_service_runner",
    "mycelium_service_package",
    "mycelium_member_runtime",
)
# Modules that import ``mlx`` unconditionally at module scope. They are the MLX (Apple Silicon) backend and the
# Apple-only qualification/demo harnesses built on it; they are not part of any Linux node path. ``--all`` lists
# them as expected so a *new* unconditional macOS-only import anywhere else is a visible failure.
MLX_ONLY_BY_DESIGN = frozenset({
    "mycelium_router.mlx_runtime",
    "mycelium_interactive.runtime",
    "mycelium_interactive.server",
    "mycelium_a11_draft_runtime",
    "qwen35_mlx",
    "physical_pixel_host_stage",
    "distributed_inference_qualification",
    "two_process_inference_qualification",
    "two_process_runtime_qualification",
})
# Platform-specific implementations selected at runtime; importing them off-platform is not expected to work.
SKIP_LEAVES = frozenset({"_darwin", "_macos", "_win32", "_win32_api", "_win32_job"})
THIRD_PARTY_HINTS = ("numpy", "cryptography", "nacl", "websockets", "uvicorn", "starlette", "fastapi", "httpx",
                     "pydantic", "yaml", "psutil", "torch", "transformers", "safetensors", "huggingface_hub")


class _Blocker(importlib.abc.MetaPathFinder):
    def __init__(self, names):
        self._names = frozenset(names)

    def find_spec(self, fullname, path, target=None):
        if fullname.split(".")[0] in self._names:
            raise ModuleNotFoundError(f"No module named {fullname!r} (blocked: not present on Linux)", name=fullname)
        return None


def simulate_linux() -> None:
    blocked = MACOS_ONLY_MODULES + WINDOWS_ONLY_MODULES
    for name in blocked:
        sys.modules[name] = None  # import -> ModuleNotFoundError
    sys.meta_path.insert(0, _Blocker(blocked))
    sys.platform = "linux"
    platform.system = lambda: "Linux"  # type: ignore[assignment]
    platform.machine = lambda: "x86_64"  # type: ignore[assignment]
    platform.mac_ver = lambda *a, **k: ("", ("", "", ""), "")  # type: ignore[assignment]


def run(root: Path, packages=PACKAGES, modules=MODULES) -> dict:
    root = root.resolve()
    sys.path.insert(0, str(root))
    simulate_linux()
    failures: list[dict] = []
    missing_third_party: list[dict] = []
    ok: list[str] = []

    def attempt(label: str, fn) -> None:
        try:
            fn()
        except SystemExit:
            # a ``__main__`` module that runs its CLI on import: it imported fine, it just exited.
            ok.append(label)
        except Exception as exc:  # noqa: BLE001 - report every import failure
            frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename.startswith(str(root))]
            where = f"{Path(frames[-1].filename).relative_to(root)}:{frames[-1].lineno}" if frames else "?"
            entry = {"module": label, "error": f"{type(exc).__name__}: {exc}", "at": where}
            missing = getattr(exc, "name", None)
            if (isinstance(exc, ModuleNotFoundError) and missing
                    and missing.split(".")[0] in THIRD_PARTY_HINTS):
                missing_third_party.append(entry)
            else:
                failures.append(entry)
        else:
            ok.append(label)

    for pkg in packages:
        if not (root / pkg).is_dir():
            continue
        attempt(pkg, lambda p=pkg: importlib.import_module(p))
        mod = sys.modules.get(pkg)
        if mod is None or not hasattr(mod, "__path__"):
            continue
        for info in pkgutil.walk_packages(mod.__path__, prefix=pkg + "."):
            if info.name.rsplit(".", 1)[-1] in SKIP_LEAVES:
                continue
            attempt(info.name, lambda n=info.name: importlib.import_module(n))
    for name in modules:
        if (root / f"{name}.py").is_file():
            attempt(name, lambda n=name: importlib.import_module(n))
    return {
        "check": "linux_import_guard",
        "simulated_platform": "linux",
        "blocked": list(MACOS_ONLY_MODULES + WINDOWS_ONLY_MODULES),
        "imported_ok": len(ok),
        "failed": len(failures),
        "missing_third_party": len(missing_third_party),
        "status": "UNVERIFIED-on-hardware",
        "failures": failures,
        "missing_third_party_detail": missing_third_party,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--packages", help="comma-separated package override")
    parser.add_argument("--modules", help="comma-separated top-level module override")
    parser.add_argument("--all", action="store_true",
                        help="every mycelium_* package and top-level module; MLX-only modules are reported as expected")
    args = parser.parse_args(argv)
    root = Path(args.root)
    packages, modules = PACKAGES, MODULES
    if args.all:
        packages = tuple(sorted(d for d in os.listdir(root)
                                if d.startswith("mycelium") and (root / d / "__init__.py").is_file()))
        modules = tuple(sorted(f[:-3] for f in os.listdir(root)
                               if f.endswith(".py") and not f.startswith("test_") and f != "conftest.py"))
    if args.packages is not None:
        packages = tuple(x for x in args.packages.split(",") if x)
    if args.modules is not None:
        modules = tuple(x for x in args.modules.split(",") if x)
    result = run(root, packages, modules)
    if args.all:
        expected = [f for f in result["failures"] if f["module"] in MLX_ONLY_BY_DESIGN and "mlx" in f["error"]]
        result["failures"] = [f for f in result["failures"] if f not in expected]
        result["failed"] = len(result["failures"])
        result["expected_mlx_only"] = sorted(f["module"] for f in expected)
    for failure in result["failures"]:
        print(json.dumps(failure))
    summary = {k: v for k, v in result.items() if k not in ("failures", "missing_third_party_detail")}
    print(json.dumps(summary))
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
