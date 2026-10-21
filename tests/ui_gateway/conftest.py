# SPDX-License-Identifier: AGPL-3.0-only
"""Fixtures for the browser gateway tests.

The helpers live in ``ui_gateway_support`` (a unique module name): test modules
import them from there, never from ``conftest``, so this directory's tests can run
in one session with other directories that have their own ``conftest.py``.
"""

from ui_gateway_support import app, bootstrap, coordinator, upstreams  # noqa: F401 - pytest fixtures
