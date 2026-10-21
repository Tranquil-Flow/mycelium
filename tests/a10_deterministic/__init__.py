# SPDX-License-Identifier: AGPL-3.0-only
"""A10 deterministic-half suite: fail-closed negatives and the batch admission path.

Every test in this package is deterministic: in-process, no child process, no
listen socket, no model artifact and no physical route.  The physical positives of
the frozen inventory (``real_runtime_batch_membership``, ``causal_pipeline_overlap``,
``continuous_late_arrival``) stay RED in ``tests/a10_scaffold`` by design: they need
a live route, and this package never claims one.

Import hygiene
--------------
``tests/router/test_instrument_and_analyze.py`` (landed before this lane) inserts
the standing repository at ``sys.path[0]`` while it is imported.  In one pytest
process that leaves ``mycelium_router`` bound to the standing checkout, so this
lane's own modules look absent and every test module here must re-assert the
worktree before it imports them:

* the worktree root moves to the front of ``sys.path``, and
* the ``mycelium_router`` package's ``__path__`` is extended with this worktree's
  package directory, so modules that exist only in the lane (``a10_batch_admission``
  and friends) resolve here.

Already-imported submodules are deliberately left alone: replacing them would
create dual class identities and break unrelated suites, and at the lane base the
shared modules are identical anyway.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: The lane worktree this suite must exercise.
LANE_WORKTREE_ROOT = Path(__file__).resolve().parents[2]


def ensure_worktree_imports() -> Path:
   """Make the lane worktree's ``mycelium_router`` importable for this suite."""

   root = str(LANE_WORKTREE_ROOT)
   if root in sys.path:
      sys.path.remove(root)
   sys.path.insert(0, root)

   package_dir = LANE_WORKTREE_ROOT / "mycelium_router"
   try:
      import mycelium_router
   except ImportError:
      return LANE_WORKTREE_ROOT
   package_path = getattr(mycelium_router, "__path__", None)
   if package_path is None:
      return LANE_WORKTREE_ROOT
   if str(package_dir) not in list(package_path):
      package_path.insert(0, str(package_dir))
   return LANE_WORKTREE_ROOT


ensure_worktree_imports()
