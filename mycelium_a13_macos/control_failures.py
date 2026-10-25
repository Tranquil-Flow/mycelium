# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded HTTP diagnostics and transport classification, not replay permission."""
from typing import Any

from .managed_control import ManagedControlError

TRANSIENT_CONTROL_CODES = frozenset({
    'managed_control_unreachable', 'managed_control_remote_error',
    'managed_control_response_timeout', 'relay_backend_unavailable',
})

# Diagnostic disclosure is independent of permission to replay a request.
# Exact routes only: never retain a URL, query, fragment or dynamic path.
_ERROR_HTTP_PATHS = frozenset({
    '/managed-control/enqueue', '/managed-control/poll', '/managed-control/ack',
    '/managed-control/result', '/managed-control/token-progress',
    '/managed-control/staging/read', '/managed-control/staging/ack',
    '/managed-control/staging/cleanup/read', '/managed-control/staging/cleanup/ack',
    '/managed-control/demo-run/read', '/managed-control/demo-run/report',
    '/managed-control/model-source/read', '/managed-control/model-source/manifest',
})


def error_http(error: Exception, code: str) -> dict[str, Any] | None:
    """Return detached metadata; callers supply their sanitized public code."""
    if not isinstance(error, ManagedControlError):
        return None
    status = error.status
    status = status if type(status) is int and 100 <= status <= 599 else None
    path = error.request_path
    path = path if isinstance(path, str) and path in _ERROR_HTTP_PATHS else None
    if status is not None or path is not None:
        return {'status': status, 'path': path, 'code': code}
    return None
