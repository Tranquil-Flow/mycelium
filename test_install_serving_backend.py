#!/usr/bin/env python3
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import install_serving_backend as bootstrap


class ServingBootstrapTests(unittest.TestCase):
    def test_auto_selects_mlx_lm_for_apple_silicon(self):
        profile = {"platform": "Darwin", "arch": "arm64", "backends": []}
        plan = bootstrap.build_plan(profile, python_executable="python3")
        self.assertEqual(plan.backend, "mlx_lm")
        self.assertEqual(plan.commands[0].argv[-1], "mlx-lm")
        self.assertEqual(plan.verify_argv, ["python3", "-c", "import mlx_lm"])

    def test_auto_selects_cuda_llama_server_for_nvidia(self):
        profile = {
            "platform": "Linux",
            "arch": "x86_64",
            "gpus": [{"vendor": "nvidia", "backend": "cuda"}],
            "backends": ["cuda_toolkit"],
        }
        plan = bootstrap.build_plan(profile, python_executable="python3")
        self.assertEqual(plan.backend, "llama_cpp_cuda")
        self.assertEqual(plan.commands[0].env["CMAKE_ARGS"], "-DGGML_CUDA=on")
        self.assertIn("llama-cpp-python[server]", plan.commands[0].argv)

    def test_auto_selects_native_llama_cpp_for_android_termux(self):
        profile = {"platform": "Android", "arch": "aarch64", "backends": []}
        plan = bootstrap.build_plan(profile)
        self.assertEqual(plan.backend, "llama_cpp_termux")
        self.assertEqual(plan.commands[1].argv, ["apt", "install", "-y", "llama-cpp"])
        self.assertEqual(plan.verify_argv, ["llama-server", "--help"])

    def test_existing_serving_backend_skips_install_without_force(self):
        profile = {"platform": "Linux", "arch": "x86_64", "backends": ["llama_cpp"]}
        plan = bootstrap.build_plan(profile)
        self.assertEqual(plan.reason, "serving_backend_already_installed")
        self.assertEqual(plan.commands, [])
        self.assertEqual(plan.already_installed, ["llama_cpp"])

    def test_cli_defaults_to_dry_run(self):
        profile = {"platform": "Darwin", "arch": "arm64", "backends": []}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "profile.json"
            path.write_text(json.dumps(profile))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = bootstrap.main(["--profile-file", str(path)])
        report = json.loads(output.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(report["mode"], "dry_run")
        self.assertTrue(report["ok"])

    def test_execute_refuses_remote_profile(self):
        profile = {"platform": "Android", "arch": "aarch64", "backends": []}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "profile.json"
            path.write_text(json.dumps(profile))
            output = io.StringIO()
            with mock.patch.object(bootstrap, "local_platform", return_value="Darwin"):
                with contextlib.redirect_stdout(output):
                    rc = bootstrap.main(["--profile-file", str(path), "--execute"])
        report = json.loads(output.getvalue())
        self.assertEqual(rc, 2)
        self.assertFalse(report["ok"])
        self.assertEqual(report["error"], "refusing_to_install_remote_profile_on_local_device")


if __name__ == "__main__":
    unittest.main()
