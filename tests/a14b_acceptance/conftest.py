"""A14b coarse-region acceptance suite: RED by design.

It freezes the coarse region/evidence vocabulary and then exercises
`mycelium_product_spine.coarse_region` and the region-aware route-explorer
projection, neither of which the A14b gate has implemented yet, so it fails on
purpose. Every test in this directory is marked `scaffold_red`, which the
repository's pytest.ini deselects from default collection. Opt in with:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /opt/homebrew/bin/python3.14 \
      -m pytest -q -p no:cacheprovider -m scaffold_red tests/a14b_acceptance

See docs/operations/red-by-design-suites.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SUITE_ROOT = Path(__file__).resolve().parent


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    marker = pytest.mark.scaffold_red
    for item in items:
        item_path = Path(str(item.path)).resolve()
        if item_path == _SUITE_ROOT or _SUITE_ROOT in item_path.parents:
            item.add_marker(marker)
