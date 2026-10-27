# SPDX-License-Identifier: AGPL-3.0-only
"""Optional native test inputs: absence skips, broken installed inputs fail."""
import os
from pathlib import Path

import pytest


def require_native(path, label):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        pytest.skip(f'{label} prerequisite absent: run tools/worktree_test_setup.sh INTEGRATION_CHECKOUT')
    assert path.is_file() and os.access(path, os.X_OK), f'{label} prerequisite is present but not an executable file'
    return path
