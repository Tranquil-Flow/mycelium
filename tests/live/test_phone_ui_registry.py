# SPDX-License-Identifier: AGPL-3.0-only
"""Serial run_focused harness for the owned frontend registry (no browser/live service)."""
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2] / 'ui' / 'web'

@pytest.mark.skipif(not (ROOT / 'node_modules' / '.bin' / 'vitest').exists(), reason='frontend npm ci required')
def test_host_ui_registry():
    for command in (["npm", "test", "--", "src/features/liveRoute/routeStatus.test.ts"],
                    ["npm", "run", "typecheck"]):
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)
        print(result.stdout, result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
