# SPDX-License-Identifier: AGPL-3.0-only
"""Top-level legacy modules must import cleanly as the FIRST import of a process.

mycelium_platform_neutral loads mycelium_qualification, which imports layer_assignment and
weight_provisioning; those (and model_manifest) must therefore defer their offline-mode import.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('module', ['layer_assignment', 'model_manifest', 'weight_provisioning', 'model_adapters'])
def test_module_imports_first_in_a_fresh_process(module):
    done = subprocess.run([sys.executable, '-c', f'import {module}'], cwd=ROOT, capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr[-800:]
