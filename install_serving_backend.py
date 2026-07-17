#!/usr/bin/env python3
"""Explicit, platform-aware serving-backend bootstrap for Mycelium nodes.

The core Mycelium modules remain stdlib-only. This optional helper prints an
installation plan by default and changes the local device only with --execute.
It never installs onto a remote profile by accident.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


PROTOCOL = "mycelium.serving_bootstrap.v1"
SERVING_BACKENDS = {"llama_cpp", "mlx_lm", "mlc_llm"}


@dataclass
class InstallCommand:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class BootstrapPlan:
    backend: str
    target_platform: str
    commands: list[InstallCommand]
    verify_argv: list[str]
    reason: str
    already_installed: list[str]


def _profile_root(document: dict[str, Any]) -> dict[str, Any]:
    return document.get("profile") or document


def _capabilities(document: dict[str, Any]) -> dict[str, Any]:
    return _profile_root(document).get("capabilities") or {}


def profile_platform(document: dict[str, Any]) -> str:
    root = _profile_root(document)
    cap = _capabilities(document)
    return str(cap.get("platform") or root.get("platform") or "unknown")


def profile_arch(document: dict[str, Any]) -> str:
    root = _profile_root(document)
    cap = _capabilities(document)
    return str(cap.get("arch") or root.get("arch") or "unknown")


def profile_backends(document: dict[str, Any]) -> list[str]:
    root = _profile_root(document)
    cap = _capabilities(document)
    return list(cap.get("backends") or root.get("backends") or [])


def profile_has_nvidia(document: dict[str, Any]) -> bool:
    root = _profile_root(document)
    cap = _capabilities(document)
    if cap.get("primary_gpu_backend") in {"cuda", "torch_cuda"}:
        return True
    if set(profile_backends(document)) & {"cuda", "cuda_toolkit", "torch_cuda"}:
        return True
    return any(str(gpu.get("vendor", "")).lower() == "nvidia" for gpu in root.get("gpus") or [])


def local_platform() -> str:
    prefix = os.environ.get("PREFIX", "")
    if os.environ.get("TERMUX_VERSION") or "com.termux" in prefix:
        return "Android"
    return platform.system()


def local_profile() -> dict[str, Any]:
    backends = []
    if importlib.util.find_spec("mlx_lm") is not None:
        backends.append("mlx_lm")
    if importlib.util.find_spec("llama_cpp") is not None:
        backends.append("llama_cpp")
    if shutil.which("llama-cli") or shutil.which("llama-server"):
        if "llama_cpp" not in backends:
            backends.append("llama_cpp")
    if shutil.which("nvcc"):
        backends.append("cuda_toolkit")
    return {
        "platform": local_platform(),
        "arch": platform.machine(),
        "backends": backends,
    }


def choose_backend(document: dict[str, Any], requested: str = "auto") -> str:
    if requested != "auto":
        return requested
    target = profile_platform(document)
    arch = profile_arch(document).lower()
    if target == "Android":
        return "llama_cpp_termux"
    if target == "Darwin" and arch in {"arm64", "aarch64"}:
        return "mlx_lm"
    if profile_has_nvidia(document):
        return "llama_cpp_cuda"
    return "llama_cpp"


def build_plan(
    document: dict[str, Any],
    *,
    requested: str = "auto",
    force: bool = False,
    python_executable: str = "python3",
) -> BootstrapPlan:
    installed = sorted(set(profile_backends(document)) & SERVING_BACKENDS)
    target = profile_platform(document)
    backend = choose_backend(document, requested=requested)
    if installed and not force:
        return BootstrapPlan(
            backend=backend,
            target_platform=target,
            commands=[],
            verify_argv=[],
            reason="serving_backend_already_installed",
            already_installed=installed,
        )

    if backend == "mlx_lm":
        commands = [InstallCommand([python_executable, "-m", "pip", "install", "--upgrade", "mlx-lm"])]
        verify = [python_executable, "-c", "import mlx_lm"]
        reason = "apple_silicon_prefers_mlx_lm"
    elif backend == "llama_cpp_cuda":
        commands = [InstallCommand(
            [python_executable, "-m", "pip", "install", "--upgrade", "llama-cpp-python[server]"],
            env={"CMAKE_ARGS": "-DGGML_CUDA=on"},
        )]
        verify = [python_executable, "-c", "import llama_cpp; import llama_cpp.server.app"]
        reason = "nvidia_device_prefers_cuda_llama_cpp_server"
    elif backend == "llama_cpp":
        commands = [InstallCommand(
            [python_executable, "-m", "pip", "install", "--upgrade", "llama-cpp-python[server]"]
        )]
        verify = [python_executable, "-c", "import llama_cpp; import llama_cpp.server.app"]
        reason = "portable_cpu_llama_cpp_server"
    elif backend == "llama_cpp_termux":
        commands = [
            InstallCommand(["apt", "update"]),
            InstallCommand(["apt", "install", "-y", "llama-cpp"]),
        ]
        verify = ["llama-server", "--help"]
        reason = "android_termux_native_llama_cpp"
    else:
        raise ValueError(f"unsupported backend: {backend}")
    return BootstrapPlan(
        backend=backend,
        target_platform=target,
        commands=commands,
        verify_argv=verify,
        reason=reason,
        already_installed=installed,
    )


def execute_plan(plan: BootstrapPlan) -> list[dict[str, Any]]:
    results = []
    for command in plan.commands:
        env = os.environ.copy()
        env.update(command.env)
        completed = subprocess.run(command.argv, env=env, check=False)
        results.append({"argv": command.argv, "returncode": completed.returncode})
        if completed.returncode != 0:
            raise subprocess.CalledProcessError(completed.returncode, command.argv)
    if plan.verify_argv:
        verified = subprocess.run(plan.verify_argv, check=False)
        results.append({"argv": plan.verify_argv, "returncode": verified.returncode, "verification": True})
        if verified.returncode != 0:
            raise subprocess.CalledProcessError(verified.returncode, plan.verify_argv)
    return results


def report_for(plan: BootstrapPlan, *, execute: bool, local_target: str) -> dict[str, Any]:
    return {
        "ok": True,
        "protocol": PROTOCOL,
        "mode": "execute" if execute else "dry_run",
        "target_platform": plan.target_platform,
        "local_platform": local_target,
        "backend": plan.backend,
        "reason": plan.reason,
        "already_installed": plan.already_installed,
        "commands": [asdict(command) for command in plan.commands],
        "verify_argv": plan.verify_argv,
        "claim_boundary": "optional local serving-runtime bootstrap; does not install models or implement distributed execution",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or install a Mycelium serving backend")
    parser.add_argument("--profile-file", help="node profile used for platform-aware selection; defaults to this device")
    parser.add_argument(
        "--backend",
        choices=["auto", "mlx_lm", "llama_cpp", "llama_cpp_cuda", "llama_cpp_termux"],
        default="auto",
    )
    parser.add_argument("--execute", action="store_true", help="perform the installation; default is a dry run")
    parser.add_argument("--force", action="store_true", help="reinstall even when a serving backend is detected")
    args = parser.parse_args(argv)

    document = json.loads(Path(args.profile_file).read_text()) if args.profile_file else local_profile()
    plan = build_plan(
        document,
        requested=args.backend,
        force=args.force,
        python_executable=sys.executable,
    )
    local_target = local_platform()
    report = report_for(plan, execute=args.execute, local_target=local_target)
    if args.execute and plan.target_platform != local_target:
        report.update({
            "ok": False,
            "error": "refusing_to_install_remote_profile_on_local_device",
        })
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2
    if args.execute:
        try:
            report["execution"] = execute_plan(plan)
        except subprocess.CalledProcessError as exc:
            report.update({
                "ok": False,
                "error": "installation_command_failed",
                "failed_argv": list(exc.cmd),
                "returncode": exc.returncode,
            })
            print(json.dumps(report, indent=2, sort_keys=True))
            return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
