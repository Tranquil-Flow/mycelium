# SPDX-License-Identifier: AGPL-3.0-only
"""A11 scaffold suite: RED by design.

It freezes the A11 implementation contract and then exercises the speculative
decode/verify execution that the A11 gate has not implemented yet, so it fails on
purpose. Every test in this directory is marked `scaffold_red`, which the
repository's pytest.ini deselects from default collection. Opt in with:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /opt/homebrew/bin/python3.14 \
      -m pytest -q -p no:cacheprovider -m scaffold_red tests/a11_scaffold

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
