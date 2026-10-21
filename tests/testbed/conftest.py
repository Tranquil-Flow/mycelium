# SPDX-License-Identifier: AGPL-3.0-only
"""Testbed-local fixtures.

The iroh sidecar fixture is *reused* from ``tests/e2e_request_iroh/conftest.py``
rather than duplicated: loading that module by path and re-exporting its fixture
object keeps the cargo build flags (``--locked``, ``CARGO_NET_OFFLINE``) and the
canonical binary path in exactly one place.

A second fixture, ``ui_web_toolchain``, models the one repository prerequisite
that ``scripts/pilot_readiness_gate.py`` reads off the *host filesystem* instead
of off a declared artifact -- see its docstring.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Iterator
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
IROH_CONFTEST = ROOT / "tests" / "e2e_request_iroh" / "conftest.py"

_spec = importlib.util.spec_from_file_location(
    "_testbed_e2e_request_iroh_conftest", IROH_CONFTEST
)
if _spec is None or _spec.loader is None:  # pragma: no cover - import-time guard
    raise AssertionError(f"iroh_conftest_unavailable:{IROH_CONFTEST}")
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)

# re-exported so tests/testbed can request it without copying the build logic
native_iroh_sidecar_binary = _module.native_iroh_sidecar_binary


UI_WEB = ROOT / "ui" / "web"


@pytest.fixture()
def ui_web_toolchain() -> Iterator[None]:
    """Model an *installed* ``ui/web`` toolchain for discovery-only tests.

    ``scripts/pilot_readiness_gate.discover_canonical_commands`` reads the npm
    scripts declared in ``ui/web/package.json`` only when
    ``ui/web/node_modules`` exists; otherwise it records
    ``ui_toolchain_absent:ui/web/node_modules`` as a ``skip`` and check ``a``
    aggregates to ``SKIPPED`` rather than discovering ``npm run test`` /
    ``test:contracts`` / ``typecheck``.  That directory is gitignored, so a fresh
    checkout or Git worktree does not have it while the long-lived pilot checkout
    (where this suite was authored and ran 36 passed) does -- discovery then
    depends on the host instead of on the repository.

    This fixture makes that prerequisite deterministic for the tests that assert
    discovery of the declared npm scripts.  It models *presence only*: nothing
    under ``ui/web`` is executed, because every command in those tests is
    answered by ``StubRunner``; no UI, npm or dependency install is simulated or
    claimed to have run.

    It is a no-op when the host already has the toolchain installed (the pilot
    checkout's case).  When it has to create the directory it removes it again,
    so a test run never leaves behind a directory that would make the *real*
    gate believe an uninstalled toolchain is present.
    """

    toolchain = UI_WEB / "node_modules"
    created = not toolchain.is_dir()
    if created:
        toolchain.mkdir(parents=True, exist_ok=True)
    try:
        yield
    finally:
        if created and toolchain.is_dir() and not any(toolchain.iterdir()):
            toolchain.rmdir()
