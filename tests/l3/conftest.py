# SPDX-License-Identifier: AGPL-3.0-only
"""L3 scenario imports (PLAN v2 §2.1 ladder).

``tests/a13_macos`` holds the shipped-path integration helpers this scenario
reuses (the signed-TLS ``_world``, the owner control session, the staged
bundle).  Those modules import each other by bare name because pytest puts a
test file's own directory on ``sys.path``; a test that lives outside that
directory has to say so explicitly instead of copying them.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_A13_TESTS = _REPOSITORY_ROOT / "tests" / "a13_macos"

for _entry in (str(_REPOSITORY_ROOT), str(_A13_TESTS)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)
