# SPDX-License-Identifier: AGPL-3.0-only
"""Swarm-entry ingress: the owner app's ``/swarm-entry/v1/*`` routes behind its relay.

This is the access check for web requests into a published swarm (see
``mycelium_swarm_directory.entry``): a browser that found the swarm in the
directory sends a requester-signed prompt carrying an owner-signed grant here,
to the swarm's own entry, and only an admitted request reaches the existing
chat service (``chat.ChatService``: same prompt/token caps, single flight,
bounded failure codes, server-side SSE from the registry API).  Bypassing the
directory changes nothing: without a valid grant nothing passes this module.

Cross-origin by design: the directory page lives on another origin, so the
relay answers the CORS preflight for these routes with ``*`` and no
credentials.  No cookie or session is involved; authority is the signature.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from mycelium_swarm_directory.entry import PREFIX

CORS_HEADERS = {'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': 'POST',
                'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Max-Age': '600'}


def is_entry_path(path: str) -> bool:
    return isinstance(path, str) and path.startswith(PREFIX)


def handle(app: Any, method: str, path: str, headers: Mapping[str, str], body: bytes, *,
           via_relay: bool) -> tuple[int, str, bytes]:
    if getattr(app, 'installation_role', None) != 'owner' or not is_entry_path(path):
        return _json(404, {'error': 'entry_route_unknown'})
    if method != 'POST':
        return _json(405, {'error': 'method_not_allowed'})
    if (headers.get('Content-Type') or '').split(';')[0].strip() != 'application/json':
        return _json(415, {'error': 'entry_content_type_required'})
    from .swarm_publish import existing
    publisher = existing(app)
    if publisher is None:
        return _json(503, {'error': 'entry_not_published'})
    from .chat import client_key
    status, value = publisher.gateway.handle(path, body, client_key(headers, via_relay=via_relay))
    return _json(status, value)


def bind(app: Any, proxy: Any) -> None:
    """Route the relay's swarm-entry paths to this app (no-op on older relays)."""
    bind_entry = getattr(proxy, 'bind_swarm_entry', None)
    if bind_entry is not None:
        bind_entry(lambda method, path, headers, body: handle(app, method, path, headers, body, via_relay=True))
    try:
        from .swarm_publish import resume
        resume(app)
    except Exception:
        pass  # publishing is optional; its status shows the error on the next tick


def _json(status: int, value: Any) -> tuple[int, str, bytes]:
    return status, 'application/json', json.dumps(value, separators=(',', ':')).encode()
