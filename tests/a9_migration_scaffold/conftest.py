"""A9 migration-executor scaffold suite: RED by design.

This suite pins the migration *executor* seams the A9 re-derivation plan's S3
lane must land (journal, checkpoint/restart, atomic commit, exact and changed
retry, duplicate identity, concurrent resume, rollback before commit,
migration-only reader removal, node/seed write-path integration).  None of them
is implemented by this lane, so every test here fails on purpose with a
``MigrationUnavailable`` code raised by this lane's own boundary functions.

Weak-RED discipline: a failure here is evidence of an *honest boundary*, not
evidence that the product lacked the behaviour at the base tip.  The GREEN
deterministic half of the same four frozen cases lives in
``tests/a9_acceptance/test_identity_preserving_migration.py``.

Every test in this directory is marked ``scaffold_red``, which the repository's
pytest.ini deselects from default collection.  Opt in with::

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /opt/homebrew/bin/python3.14 \\
      -m pytest -q -p no:cacheprovider -m scaffold_red tests/a9_migration_scaffold

See docs/operations/red-by-design-suites.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mycelium_platform_neutral import migration

_SUITE_ROOT = Path(__file__).resolve().parent


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    marker = pytest.mark.scaffold_red
    for item in items:
        item_path = Path(str(item.path)).resolve()
        if item_path == _SUITE_ROOT or _SUITE_ROOT in item_path.parents:
            item.add_marker(marker)
