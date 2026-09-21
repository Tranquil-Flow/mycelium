"""A7 acceptance suite: the KV-successor RED modules are RED by design.

`test_kv_successor_contract.py` and `test_kv_successor_boundaries.py` exercise
the successor/recovery contract core the A7 gate has not implemented yet, so they
fail on purpose. They are marked `scaffold_red`, which the repository's pytest.ini
deselects from default collection. `test_inventory.py` is the already-landed
frozen inventory and stays in default collection: only the two RED modules are
marked here, not the whole directory.

Opt in with:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /opt/homebrew/bin/python3.14 \
      -m pytest -q -p no:cacheprovider -m scaffold_red tests/a7_acceptance

See docs/operations/red-by-design-suites.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SUITE_ROOT = Path(__file__).resolve().parent
_RED_MODULES = ("test_kv_successor_contract.py", "test_kv_successor_boundaries.py")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    marker = pytest.mark.scaffold_red
    for item in items:
        item_path = Path(str(item.path)).resolve()
        if item_path.parent == _SUITE_ROOT and item_path.name in _RED_MODULES:
            item.add_marker(marker)
