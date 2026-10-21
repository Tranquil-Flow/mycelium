# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-side producer of fresh managed native hello observations.

Bounded owner API: obtain native hello through existing signed managed sessions
(``ManagedSessionFactory`` over ``ManagedControlOwner``) for the exact current
enrolled members of one run/deployment, validate node/run/deployment identity
and run-scoped host/boot identities from the *native* hello result, reject
duplicates/stale/missing/malformed/failed responses, and emit a
``mycelium.managed_hello_observations.v1`` document acceptable to
``scripts.build_qwen_live_route._managed_hello_observations`` (whose canonical
checks are mirrored here without importing the heavier builder module).

Hard boundaries:
- Caller-supplied host/boot values are NEVER admitted as observations.
- Only the actual ``ManagedSessionFactory`` is accepted; test fakes must be
  injected by monkeypatching the session boundary, never by passing an
  arbitrary factory into this API.
- No SSH, no argv, no process launch; the signed managed session is the only
  transport (never turn native hello success into physical/full acceptance).
- Python API only. No CLI/UI/filesystem publication/framework.
- One real host cannot demonstrate distinct hosts; the producer only checks
  shape/freshness/distinctness and fails closed on anything else.
"""
from __future__ import annotations

import json
import math
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from .managed_control import CONTROL_COMMAND_PROTOCOL, _binding_from_member
from .session_factory import ManagedSessionFactory

OBSERVATIONS_PROTOCOL = "mycelium.managed_hello_observations.v1"
RUN_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,80}-qwen-[0-9]{8}T[0-9]{6}Z\Z")
_RUN_TIMESTAMP_RE = re.compile(r"-qwen-([0-9]{8}T[0-9]{6}Z)\Z")
_IDENTITY_RE = re.compile(r"(host|boot)-[0-9a-f]{32}\Z")
_MAX_STALE_SECONDS = 600.0
_MAX_SKEW_SECONDS = 60.0
# Bounded hello budget per member, matching ordinary control timeouts.
_DEFAULT_TIMEOUT_SECONDS = 30.0
_RESPONSE_FIELDS_OK = {"protocol", "command_id", "node_id", "ok", "route_ready", "result"}


class ManagedObservationsError(RuntimeError):
    """Stable, sanitized producer failure code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _clock_value(clock: Any) -> float:
    if not callable(clock):
        raise ManagedObservationsError("managed_observation_clock_invalid")
    value = clock()
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ManagedObservationsError("managed_observation_clock_invalid")
    return float(value)


def _valid_run_id(run_id: Any) -> str:
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        raise ManagedObservationsError("managed_observation_run_id_invalid")
    return run_id


def _run_started_unix(run_id: str) -> float:
    match = _RUN_TIMESTAMP_RE.search(run_id)
    if match is None:  # unreachable when RUN_ID_RE passed; kept fail-closed
        raise ManagedObservationsError("managed_observation_run_id_invalid")
    try:
        stamp = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
    except ValueError:
        raise ManagedObservationsError("managed_observation_run_id_invalid") from None
    return stamp.replace(tzinfo=timezone.utc).timestamp()


def _fresh_enough(now: float, then: float) -> bool:
    """Mirror of the builder's canonical freshness window."""
    return -_MAX_SKEW_SECONDS <= now - then <= _MAX_STALE_SECONDS


def _native_hello_identity(
    response: Any, *, node_id: str, run_id: str, deployment_id: str, command_id: str
) -> tuple[str, str]:
    """Validate one native hello response envelope; return (host, boot)."""

    if not isinstance(response, Mapping):
        raise ManagedObservationsError("managed_observation_response_malformed")
    if response.get("ok") is False:
        raise ManagedObservationsError("managed_observation_response_failed")
    if (
        set(response) != _RESPONSE_FIELDS_OK
        or response.get("protocol") != CONTROL_COMMAND_PROTOCOL
        or response.get("command_id") != command_id
        or response.get("node_id") != node_id
        or response.get("route_ready") is not False
        or response.get("ok") is not True
    ):
        raise ManagedObservationsError("managed_observation_response_malformed")
    result = response.get("result")
    if not isinstance(result, Mapping):
        raise ManagedObservationsError("managed_observation_response_malformed")
    if (
        result.get("protocol") != CONTROL_COMMAND_PROTOCOL
        or result.get("node_id") != node_id
        or result.get("run_id") != run_id
        or result.get("deployment_id") != deployment_id
        or result.get("route_ready") is not False
        or result.get("state") != "NEW"
    ):
        raise ManagedObservationsError("managed_observation_identity_mismatch")
    host: Any = result.get("host_id")
    boot: Any = result.get("boot_id")
    for kind, value in (("host", host), ("boot", boot)):
        if value is None:
            raise ManagedObservationsError(
                f"managed_observation_{kind}_identity_missing"
            )
        if not isinstance(value, str) or _IDENTITY_RE.fullmatch(value) is None:
            raise ManagedObservationsError("managed_observation_identity_synthetic")
        if value.startswith("boot-" if kind == "host" else "host-"):
            raise ManagedObservationsError("managed_observation_identity_synthetic")
    return str(host), str(boot)


def _current_binding(factory: ManagedSessionFactory, node_id: str) -> None:
    """Re-derive the member binding; membership must still be current."""

    try:
        member = factory.owner.current_member(node_id)
        binding = _binding_from_member(
            json.loads(json.dumps(dict(member), allow_nan=False)),
            factory.run_id,
            factory.deployment_id,
        )
    except Exception:
        raise ManagedObservationsError("managed_observation_member_not_current") from None
    if binding != factory._bindings.get(node_id):
        raise ManagedObservationsError("managed_observation_member_not_current")


def collect_managed_hello_observations(
    factory: Any,
    *,
    run_id: Any,
    deployment_id: Any,
    node_ids: Any,
    clock: Any = time.time,
) -> dict[str, Any]:
    """Produce one fresh managed-hello observations document (Python API).

    Sends ``hello`` through fresh signed managed sessions for exactly the
    caller-listed members (which must equal the factory's enrolled members),
    validates identity/freshness/distinctness and current membership, and
    returns the v1 document dict ready for
    ``build_qwen_live_route._managed_hello_observations``. The caller never
    supplies host/boot observations; they come only from native replies.
    """

    if not isinstance(factory, ManagedSessionFactory):
        raise ManagedObservationsError("managed_observation_factory_invalid")
    run_id = _valid_run_id(run_id)
    if not isinstance(deployment_id, str) or not deployment_id:
        raise ManagedObservationsError("managed_observation_deployment_invalid")
    if run_id != factory.run_id or deployment_id != factory.deployment_id:
        raise ManagedObservationsError("managed_observation_factory_scope_mismatch")
    started_at = _run_started_unix(run_id)
    now = _clock_value(clock)
    if not _fresh_enough(now, started_at):
        raise ManagedObservationsError("managed_observation_run_id_stale")
    if not isinstance(deployment_id, str) or not deployment_id:
        raise ManagedObservationsError("managed_observation_deployment_invalid")
    if (
        not isinstance(node_ids, (tuple, list))
        or not node_ids
        or not all(isinstance(node, str) and node for node in node_ids)
        or len(set(node_ids)) != len(node_ids)
    ):
        raise ManagedObservationsError("managed_observation_nodes_invalid")
    bound = factory._bindings
    if set(bound) != set(node_ids):
        raise ManagedObservationsError("managed_observation_member_not_current")

    observations: list[dict[str, str]] = []
    seen_hosts: set[str] = set()
    seen_boots: set[str] = set()
    for node_id in node_ids:
        command_id = "managed-observation-hello-" + secrets.token_hex(8)
        session = None
        try:
            session = factory(
                argv=(),
                node_id=node_id,
                run_id=run_id,
                deployment_id=deployment_id,
                timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
            )
            response = session.send(
                command_id=command_id,
                command="hello",
                payload={},
            )
        except Exception as error:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass  # The primary send failure stays the reported outcome.
            code = getattr(error, "code", None)
            raise ManagedObservationsError(
                code
                if isinstance(code, str) and code
                else "managed_observation_session_failed"
            ) from None
        # The observation succeeded, so a failed stop/close must fail closed
        # instead of being swallowed and reported as success.
        close_error: BaseException | None = None
        try:
            session.close()
        except Exception as error:
            close_error = error
        cleanup_error = getattr(session, "cleanup_error", None)
        stop_response = getattr(session, "stop_response", None)
        if close_error is None and (
            cleanup_error is not None
            or (stop_response is not None and stop_response.get("ok") is not True)
        ):
            close_error = ManagedObservationsError(
                "managed_observation_session_close_failed"
            )
        if close_error is not None:
            raise ManagedObservationsError(
                "managed_observation_session_close_failed"
            ) from close_error
        host, boot = _native_hello_identity(
            response,
            node_id=node_id,
            run_id=run_id,
            deployment_id=deployment_id,
            command_id=command_id,
        )
        if host in seen_hosts:
            raise ManagedObservationsError("managed_observation_hosts_not_distinct")
        if boot in seen_boots:
            raise ManagedObservationsError("managed_observation_boots_not_distinct")
        seen_hosts.add(host)
        seen_boots.add(boot)
        observations.append({"node_id": node_id, "host_id": host, "boot_id": boot})

    # Membership must still be current after collection, not only before.
    for node_id in node_ids:
        _current_binding(factory, node_id)

    captured = _clock_value(clock)
    if not _fresh_enough(now, captured):
        raise ManagedObservationsError("managed_observation_captured_stale")
    document = {
        "protocol": OBSERVATIONS_PROTOCOL,
        "deployment_id": deployment_id,
        "run_id": run_id,
        "captured_at_unix_ms": int(captured * 1000),
        "observations": observations,
    }
    return document
