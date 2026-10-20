# SPDX-License-Identifier: AGPL-3.0-only
"""PV26 diagnostics: owner and member views of clock, liveness, command health, errors, relay and host.

Everything here is additive and read-only: it never changes a route, a deadline or a protocol message.  The
local app server answers ``GET /api/diagnostics`` (JSON) and ``GET /api/diagnostics/bundle`` (redacted zip)
behind the same local session as ``/api/status``; neither is reachable through the public relay.
"""
from __future__ import annotations

import time
from typing import Any

from .state import state_for

ROUTES = frozenset({"/api/diagnostics", "/api/diagnostics/bundle"})


def ensure_installed(app: Any) -> None:
    """Start reading the timestamps the control protocol already carries (idempotent, never raises)."""
    try:
        from .clock import install_client_probe, install_mailbox_probe
        state = state_for(app)
        if getattr(app, "installation_role", "owner") == "target":
            install_client_probe(state)
            return
        mailbox = getattr(getattr(app, "authority", None), "control_mailbox", None)
        if mailbox is not None:
            install_mailbox_probe(mailbox, state)
    except Exception:
        pass


def handle_get(app: Any, path: str) -> tuple[int, str, Any, dict[str, str]]:
    """``(status, content_type, body, extra_headers)``; ``body`` is a JSON value or the zip bytes."""
    ensure_installed(app)
    if path == "/api/diagnostics/bundle":
        from .bundle import build_bundle, file_stamp
        now = time.time()
        return (200, "application/zip", build_bundle(app, now=now),
                {"Content-Disposition": f'attachment; filename="mycelium-support-{file_stamp(now)}.zip"'})
    from .collect import collect
    return 200, "application/json", collect(app), {}
