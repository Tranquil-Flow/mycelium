# SPDX-License-Identifier: AGPL-3.0-only
"""A7 route-side authority for covered peer process exits.

The route owns detection of a peer process exit.  A7 can claim that exit only
when the live entry node signs an accept-first ``a7_source_lost`` observation for
that exact request, lost source node, and request-control identity.  This module
installs only that small authority closure; staged-role detection lives in the
supervisor so the closure never reaches back into route status or supervisor
state.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

SendToEntry = Callable[[str, Mapping[str, Any], float], Mapping[str, Any]]
PeerExitSuccessorAuthority = Callable[..., bool]


def _details(observation: Any) -> Mapping[str, Any] | None:
    if not isinstance(observation, Mapping):
        return None
    value = observation.get("details")
    return value if isinstance(value, Mapping) else None


def _accepted_details_match(
    details: Mapping[str, Any],
    *,
    request_id: str,
    lost_node_id: str,
    control: Mapping[str, Any],
) -> bool:
    return (
        details.get("accepted") is True
        and details.get("request_id") == request_id
        and details.get("lost_node_id") == lost_node_id
        and details.get("control") == dict(control)
    )


def install_route_authority(
    route: Any,
    *,
    node_id_for_command: str,
    accept_source_lost: Callable[..., Mapping[str, Any]],
    cut_over_lost_source: Callable[..., bool],
) -> None:
    """Install the A7 peer-exit successor authority on ``route``.

    ``accept_source_lost`` and ``cut_over_lost_source`` are required metadata for
    the entry-node command path: the actual authority must not call them in this
    process because the per-node wiring lives inside the entry node process.  The
    closure instead uses the route-provided ``send_to_entry`` command seam, which
    signs and verifies the returned observation.
    """

    if not isinstance(node_id_for_command, str) or not node_id_for_command:
        raise ValueError("a7_entry_node_id_invalid")
    if not callable(accept_source_lost) or not callable(cut_over_lost_source):
        raise ValueError("a7_entry_callbacks_required")
    setter = getattr(route, "set_peer_exit_successor_authority", None)
    if not callable(setter):
        raise RuntimeError("a7_route_authority_unsupported")

    def authority(
        *,
        request_id: str,
        lost_node_id: str,
        control: Mapping[str, Any],
        send_to_entry: SendToEntry,
    ) -> bool:
        if (
            not isinstance(request_id, str)
            or not request_id
            or not isinstance(lost_node_id, str)
            or not lost_node_id
            or not isinstance(control, Mapping)
            or not callable(send_to_entry)
        ):
            return False
        control_document = dict(control)
        try:
            observation = send_to_entry(
                "a7_source_lost",
                {
                    "request_id": request_id,
                    "control": control_document,
                    "lost_node_id": lost_node_id,
                },
                time.monotonic() + 1.0,
            )
        except Exception:
            return False
        details = _details(observation)
        return details is not None and _accepted_details_match(
            details,
            request_id=request_id,
            lost_node_id=lost_node_id,
            control=control_document,
        )

    authority.a7_entry_node_id = node_id_for_command  # type: ignore[attr-defined]
    authority.a7_accept_source_lost = accept_source_lost  # type: ignore[attr-defined]
    authority.a7_cut_over_lost_source = cut_over_lost_source  # type: ignore[attr-defined]
    setter(authority)
