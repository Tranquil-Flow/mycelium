#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Fire N concurrent requests at a live Mycelium origin and capture whatever
A10 runtime-batch observation evidence exists into one JSON artifact.

This is a *consumer*, not a producer: it never claims a runtime batch formed
just because it fired concurrent requests. It fires the requests, attempts to
read back ``mycelium.runtime_batch_observation.v1`` records from
``--observation-url``, and evaluates the exact A10 physical-gate predicate
(``mycelium_router/a10_runtime_batch.py:67-68`` --
``MINIMUM_PHYSICAL_BATCH_MEMBERS = 2``,
``BACKEND_INVOCATIONS_PER_OBSERVED_BATCH = 1``) against whatever it actually
found.

HONESTY NOTE (read this before wiring this into a real run): as of this
lane's work (2026-09-27, ``runs/v2/pm-a10/FINDINGS.md``), the shipped live
route exposes no HTTP endpoint that publishes A10 observation records --
``--observation-url`` names an endpoint that does not exist yet. Running this
script against the real demo origin today will honestly report
``observation_source_status: "unavailable"`` and ``gate.satisfied: false``.
That is the correct, non-fabricated answer for the current deployment, not a
bug in this script. Once a live observation endpoint exists (or once this
script is pointed at a per-node file/SSH-based observation source via a
future ``--observation-url`` scheme), the same evaluation logic applies
unchanged.

Expected observation document shape at ``--observation-url`` (a GET
returning JSON): ``{"observations": [{"backend_invocations": int,
"actual_member_count": int, "proposed_member_count": int,
"membership_authority": str, ...}, ...]}`` -- one object per
``mycelium.runtime_batch_observation.v1`` record, using that record's own
field names.

Never touches any protected process by name or port; it only ever speaks
plain HTTP to whatever origin URL it is given. It never kills, restarts, or
reconfigures anything.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

PROTOCOL = "mycelium.a10_concurrency_probe.v1"

#: Mirrors mycelium_router/a10_runtime_batch.py:67-68 exactly -- this script
#: never invents its own threshold.
MINIMUM_PHYSICAL_BATCH_MEMBERS = 2
BACKEND_INVOCATIONS_PER_OBSERVED_BATCH = 1

DEFAULT_REQUEST_PATH = "/v1/generate"
DEFAULT_OBSERVATION_PATH = "/__mycelium/a10/observations"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_CONCURRENCY = 6
DEFAULT_PROMPT = "What is 6 plus 7? Answer with only the number."

PostFn = Callable[..., dict[str, Any]]
GetFn = Callable[..., dict[str, Any]]


def _try_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def _post_json(url: str, payload: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
    """The real network call. Never raises: every fault is reported as data."""

    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
            return {
                "status": "ok",
                "http_status": response.status,
                "latency_seconds": time.monotonic() - started,
                "body": _try_json(body),
            }
    except urllib.error.HTTPError as error:
        return {
            "status": "http_error",
            "http_status": error.code,
            "latency_seconds": time.monotonic() - started,
            "detail": str(error),
        }
    except Exception as error:  # noqa: BLE001 - a probe must never crash on a network fault
        return {
            "status": "error",
            "latency_seconds": time.monotonic() - started,
            "detail": f"{type(error).__name__}: {error}",
        }


def _get_json(url: str, *, timeout_seconds: float) -> dict[str, Any]:
    """The real network call for the observation source. Never raises."""

    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
            return {
                "status": "ok",
                "http_status": response.status,
                "document": _try_json(body),
            }
    except urllib.error.HTTPError as error:
        return {
            "status": "unavailable" if error.code == 404 else "http_error",
            "http_status": error.code,
            "detail": str(error),
        }
    except Exception as error:  # noqa: BLE001
        return {"status": "error", "detail": f"{type(error).__name__}: {error}"}


def fire_concurrent_requests(
    origin_url: str,
    *,
    concurrency: int,
    prompt: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    request_path: str = DEFAULT_REQUEST_PATH,
    post: PostFn = _post_json,
) -> list[dict[str, Any]]:
    """Fire ``concurrency`` requests at once; return one outcome per request,
    in request-index order regardless of completion order."""

    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
        raise ValueError("invalid_concurrency")
    url = origin_url.rstrip("/") + request_path
    outcomes: list[dict[str, Any] | None] = [None] * concurrency

    def _one(index: int) -> tuple[int, dict[str, Any]]:
        return index, post(url, {"prompt": prompt, "request_index": index}, timeout_seconds=timeout_seconds)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_one, index) for index in range(concurrency)]
        for future in as_completed(futures):
            index, outcome = future.result()
            outcomes[index] = outcome
    return [outcome for outcome in outcomes if outcome is not None]


def fetch_observation_evidence(
    observation_url: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    get: GetFn = _get_json,
) -> dict[str, Any]:
    """Best-effort read of whatever observation records exist.

    Absence is reported honestly (``source_status`` other than ``"ok"``),
    never smoothed into an empty-but-successful read.
    """

    result = get(observation_url, timeout_seconds=timeout_seconds)
    if result.get("status") != "ok":
        return {
            "source_status": result.get("status", "error"),
            "observations": (),
            "detail": result.get("detail"),
        }
    document = result.get("document")
    observations = document.get("observations") if isinstance(document, dict) else None
    if not isinstance(observations, list):
        return {
            "source_status": "malformed_document",
            "observations": (),
            "detail": "observation document missing an 'observations' list",
        }
    return {"source_status": "ok", "observations": tuple(observations), "detail": None}


def evaluate_gate(observations: tuple[Any, ...]) -> dict[str, Any]:
    """The exact A10 physical-gate predicate, applied to whatever evidence
    was actually found. See ``mycelium_router/a10_runtime_batch.py:67-68``.
    """

    qualifying = [
        observation
        for observation in observations
        if isinstance(observation, dict)
        and observation.get("backend_invocations") == BACKEND_INVOCATIONS_PER_OBSERVED_BATCH
        and isinstance(observation.get("actual_member_count"), int)
        and not isinstance(observation.get("actual_member_count"), bool)
        and observation.get("actual_member_count") >= MINIMUM_PHYSICAL_BATCH_MEMBERS
    ]
    satisfied = len(qualifying) > 0
    return {
        "minimum_physical_batch_members": MINIMUM_PHYSICAL_BATCH_MEMBERS,
        "backend_invocations_per_observed_batch": BACKEND_INVOCATIONS_PER_OBSERVED_BATCH,
        "satisfied": satisfied,
        "qualifying_observation_count": len(qualifying),
        "total_observation_count": len(observations),
        "reason": None if satisfied else "no_observation_claims_a_runtime_batch",
    }


def build_artifact(
    *,
    origin_url: str,
    observation_url: str,
    concurrency: int,
    prompt: str,
    request_outcomes: list[dict[str, Any]],
    observation_evidence: dict[str, Any],
    generated_at: float,
) -> dict[str, Any]:
    gate = evaluate_gate(observation_evidence["observations"])
    return {
        "protocol": PROTOCOL,
        "generated_at": generated_at,
        "origin_url": origin_url,
        "observation_url": observation_url,
        "concurrency": concurrency,
        "prompt": prompt,
        "requests": [
            {"request_index": index, **outcome} for index, outcome in enumerate(request_outcomes)
        ],
        "request_success_count": sum(
            1 for outcome in request_outcomes if outcome.get("status") == "ok"
        ),
        "observation_source_status": observation_evidence["source_status"],
        "observation_source_detail": observation_evidence["detail"],
        "observations": list(observation_evidence["observations"]),
        "gate": gate,
    }


def write_artifact(path: str | Path, artifact: dict[str, Any]) -> None:
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_probe(
    *,
    origin_url: str,
    observation_url: str,
    concurrency: int,
    prompt: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    request_path: str = DEFAULT_REQUEST_PATH,
    post: PostFn = _post_json,
    get: GetFn = _get_json,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    generated_at = clock()
    request_outcomes = fire_concurrent_requests(
        origin_url,
        concurrency=concurrency,
        prompt=prompt,
        timeout_seconds=timeout_seconds,
        request_path=request_path,
        post=post,
    )
    observation_evidence = fetch_observation_evidence(
        observation_url,
        timeout_seconds=timeout_seconds,
        get=get,
    )
    return build_artifact(
        origin_url=origin_url,
        observation_url=observation_url,
        concurrency=concurrency,
        prompt=prompt,
        request_outcomes=request_outcomes,
        observation_evidence=observation_evidence,
        generated_at=generated_at,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin-url", required=True, help="e.g. http://127.0.0.1:4351")
    parser.add_argument(
        "--observation-url",
        default=None,
        help=(
            "defaults to <origin-url>" + DEFAULT_OBSERVATION_PATH + "; this endpoint does "
            "not exist in the shipped live route today (see FINDINGS.md) -- the probe "
            "reports that honestly rather than fabricating zero observations as a pass."
        ),
    )
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--request-path", default=DEFAULT_REQUEST_PATH)
    parser.add_argument("--out", required=True, help="path to write the JSON artifact")
    args = parser.parse_args(argv)

    observation_url = args.observation_url or (args.origin_url.rstrip("/") + DEFAULT_OBSERVATION_PATH)
    artifact = run_probe(
        origin_url=args.origin_url,
        observation_url=observation_url,
        concurrency=args.concurrency,
        prompt=args.prompt,
        timeout_seconds=args.timeout_seconds,
        request_path=args.request_path,
    )
    write_artifact(args.out, artifact)
    print(json.dumps({"gate_satisfied": artifact["gate"]["satisfied"], "out": str(args.out)}))
    return 0 if artifact["gate"]["satisfied"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
