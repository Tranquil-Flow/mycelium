# SPDX-License-Identifier: AGPL-3.0-only
"""The one User-Agent every Mycelium HTTP client sends toward a public origin (PV18).

Cloudflare's Browser Integrity Check answers ``403 error 1010`` to the default ``Python-urllib``
agent, so a client that reaches a public origin names itself:
``User-Agent: Mycelium/<version> (+https://mycelium.now)``.  Route request headers through
:func:`with_user_agent`; tests/a13_macos/test_http_user_agent.py fails any ``Request(...)`` or
``http.client`` call in the app, directory, seed and acquisition packages that does not.

Stdlib only and import-light on purpose: the hosted directory, the seed and the macOS app all use it.
"""
from __future__ import annotations

from collections.abc import Mapping

#: The product version; tests pin it to ``mycelium_a13_macos.__version__`` so the two cannot drift.
PRODUCT_VERSION = "0.1.0"
USER_AGENT = f"Mycelium/{PRODUCT_VERSION} (+https://mycelium.now)"


def with_user_agent(headers: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of ``headers`` whose ``User-Agent`` is the Mycelium one (any other casing is replaced)."""
    merged = {name: value for name, value in (headers or {}).items() if name.lower() != "user-agent"}
    merged["User-Agent"] = USER_AGENT
    return merged
