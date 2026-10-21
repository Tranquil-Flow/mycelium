#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Switch a live serve's A10 batch policy: the paired benchmark's mode command.

``run_a10_physical_gate.py benchmark`` runs one operator command per mode
(``--baseline-command`` / ``--candidate-command``).  This client is that command:
it posts the policy to the serve's grant-gated loopback batch-policy control
(``--a10-batch-policy-control-port``) and exits 0 only when every node of the
deployment returned its own receipt for exactly that policy.  ``candidate
--window-ms N`` also sets every node's live hop collection window to N ms.

The fleet grant is read from ``MYCELIUM_A10_FLEET_GRANT``, never from the
command line.  On success it prints one JSON line naming the policy and each
node's runtime batch cap; on refusal it prints the control's stable code to
standard error and exits non-zero.
"""
# ruff: noqa: E402 -- direct script execution needs the repository on sys.path.
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mycelium_a10_batch_policy_control import CONTROL_PATH, MAXIMUM_WINDOW_MS, POLICIES
from mycelium_a10_verify_control import grant_from_environment

_GRANT_HEADER = "X-Mycelium-A10-Fleet-Grant"


def _opener() -> urllib.request.OpenerDirector:
    # Loopback only: never route the grant through an environment proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def switch_policy(
    *,
    port: int,
    policy: str,
    timeout_s: float,
    grant: str,
    window_ms: int | None = None,
    opener: Any = None,
) -> Mapping[str, Any]:
    """Post one policy and return the verified result, else raise ``SystemExit``."""

    document_out: dict[str, Any] = {"policy": policy, "timeout_s": timeout_s}
    if window_ms is not None:
        document_out["window_ms"] = window_ms
    body = json.dumps(document_out).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{CONTROL_PATH}",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", _GRANT_HEADER: grant},
    )
    try:
        with (opener or _opener()).open(request, timeout=timeout_s + 10.0) as response:
            document = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            try:
                code = json.loads(error.read().decode("utf-8")).get("code")
            except (ValueError, AttributeError):
                code = None
        finally:
            error.close()
        raise SystemExit(f"a10_batch_policy_refused:{code or error.code}") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise SystemExit("a10_batch_policy_control_unreachable") from None
    result = document.get("result") if isinstance(document, dict) else None
    nodes = result.get("nodes") if isinstance(result, dict) else None
    if (
        not isinstance(result, dict)
        or result.get("policy") != policy
        or not isinstance(nodes, dict)
        or not nodes
    ):
        raise SystemExit("a10_batch_policy_response_invalid")
    expected_baseline_cap = 1 if policy == "baseline" else None
    for receipt in nodes.values():
        cap = receipt.get("maximum_runtime_batch_size") if isinstance(receipt, dict) else None
        if type(cap) is not int or cap < 1:
            raise SystemExit("a10_batch_policy_response_invalid")
        if expected_baseline_cap is not None and cap != expected_baseline_cap:
            raise SystemExit("a10_batch_policy_response_invalid")
        if window_ms is not None and receipt.get("collection_window_ms") != window_ms:
            raise SystemExit("a10_batch_policy_response_invalid")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("policy", choices=POLICIES)
    parser.add_argument(
        "--port",
        type=int,
        required=True,
        help="the serve's --a10-batch-policy-control-port",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--window-ms",
        type=int,
        help=(
            "candidate only: live hop collection window in whole milliseconds "
            f"(0 through {MAXIMUM_WINDOW_MS}); start from the measured cross-host hop RTT"
        ),
    )
    args = parser.parse_args(argv)
    if args.window_ms is not None and (
        args.policy != "candidate" or not 0 <= args.window_ms <= MAXIMUM_WINDOW_MS
    ):
        raise SystemExit(
            f"--window-ms is valid only with candidate, from 0 through {MAXIMUM_WINDOW_MS}"
        )
    if not 1 <= args.port <= 65_535:
        raise SystemExit("port must be from 1 through 65535")
    if not 0 < args.timeout <= 120:
        raise SystemExit("--timeout must be in (0, 120]")
    grant = grant_from_environment()
    if grant is None:
        raise SystemExit("explicit_fleet_grant_required")
    result = switch_policy(
        port=args.port,
        policy=args.policy,
        timeout_s=args.timeout,
        grant=grant,
        window_ms=args.window_ms,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
