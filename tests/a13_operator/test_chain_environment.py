# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise only the inert shell prefix; never smoke-launch a physical chain."""
from pathlib import Path
import os
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def newest_chain(suffix):
    paths = list((ROOT / "scripts/a13_operator").glob(f"chain-preview*{suffix}.sh"))
    parsed = [(int(m.group(1)), p) for p in paths
              if (m := re.fullmatch(rf"chain-preview(\d+){suffix}\.sh", p.name))]
    if not parsed:
        pytest.skip("the operator chain launchers are not part of the published tree")
    return max(parsed)[1]


@pytest.mark.parametrize("suffix", ["", "e"])
def test_chain_strips_foreign_python_environment_before_any_command(suffix):
    path = newest_chain(suffix)
    source = path.read_text()
    # Explicitly stop at the first repository assignment: no cwd change, build,
    # network operation, cleanup or physical launch is included in this probe.
    prefix = source.split("REPO=", 1)[0]
    assert "REPO=" in source
    env = dict(os.environ, PYTHONPATH="FOREIGN_PATH", PYTHONHOME="FOREIGN_HOME",
               VIRTUAL_ENV="FOREIGN_VENV")
    probe = prefix + '\nprintf "%s|%s|%s\\n" "${PYTHONPATH-unset}" "${PYTHONHOME-unset}" "${VIRTUAL_ENV-unset}"\n'
    result = subprocess.run(["bash", "-c", probe], env=env, text=True,
                            capture_output=True, check=True)
    assert result.stdout.strip() == "unset|unset|unset"


@pytest.mark.parametrize("suffix", ["", "e"])
def test_newest_chain_shell_parses_without_execution(suffix):
    result = subprocess.run(["bash", "-n", str(newest_chain(suffix))],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
