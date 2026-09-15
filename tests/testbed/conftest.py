"""Testbed-local fixtures.

The iroh sidecar fixture is *reused* from ``tests/e2e_request_iroh/conftest.py``
rather than duplicated: loading that module by path and re-exporting its fixture
object keeps the cargo build flags (``--locked``, ``CARGO_NET_OFFLINE``) and the
canonical binary path in exactly one place.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

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
