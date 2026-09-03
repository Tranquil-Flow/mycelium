#!/usr/bin/env python3
"""Run the frozen A5 materiality benchmark through the ordinary product HTTP API.

Drives the frozen workload (tests/a5_acceptance/workload_manifest.v1.json)
against the frozen protocol (tests/a5_acceptance/benchmark_protocol.v1.json):
one unscored warmup per mode, then 12 measured ABBA x 3 windows of at least
60 terminal requests each. The run fixture is validated by the frozen
materiality harness (tests/a5_acceptance/materiality_harness.evaluate_benchmark)
and sealed with the decision into the output file.

Mode switching (Evi decision 2026-08-20, no request-path contract change):
- baseline windows: POST /__mycelium/replica-qualification/install with
  {"documents": []} — the replica set is cleared and round-robin degenerates
  to the incumbent A4 default path.
- candidate windows: the same endpoint restores the validated
  replica_qualification.v1 documents passed via --replica-qualification.

Frozen QoS execution mapping:
- interactive -> interactive_chat_v1 / interactive
- background and bulk -> sustained_batch_v1 / batch
The frozen class is still sampled independently and retained in the request
shape; the mapping uses only the two workload classes admitted by product v2.
- Per-window bindings are the frozen manifest constants (15 identical fields)
  plus qualified_track_policy_digest: "incumbent_only" for baseline,
  "round_robin" for candidate (the protocol's allowed per-mode field).

The output contains no prompt, decoded text, token IDs, cookies, CSRF values,
hostnames, addresses, paths, command lines, or exception strings.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import hashlib
import http.cookiejar
import json
import math
import os
from pathlib import Path
import random
import signal
import stat
import tempfile
import threading
import time
from typing import Any, Callable, Mapping
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]

import sys  # noqa: E402

sys.path.insert(0, str(REPO_ROOT))

from tests.a5_acceptance.materiality_harness import (  # noqa: E402
    evaluate_benchmark,
    load_protocol,
    load_workload_manifest,
    protocol_digest,
    workload_manifest_digest,
)

PROTOCOL = load_protocol()
MANIFEST = load_workload_manifest()
SCHEDULE = PROTOCOL["schedule"]
MIN_WINDOW_REQUESTS = SCHEDULE["minimum_requests_per_measured_window"]
MIN_WARMUP_REQUESTS = SCHEDULE["minimum_requests_per_warmup"]
OFFERED_CONCURRENCY = MANIFEST["offered_concurrency"]["per_window"]
ARRIVAL_RATE = MANIFEST["arrival_schedule"]["rate_per_second"]
ARRIVAL_SEED = MANIFEST["arrival_schedule"]["rng_seed"]
WARMUP_SECONDS = MANIFEST["arrival_schedule"]["warmup_seconds"]
BUCKETS = MANIFEST["prompt_output_buckets"]["buckets"]
BUCKET_NAMES = [bucket["name"] for bucket in BUCKETS]
BUCKET_WEIGHTS = [bucket["fraction"] for bucket in BUCKETS]
QOS_CLASSES = MANIFEST["qos_mix"]["classes"]
QOS_NAMES = [item["name"] for item in QOS_CLASSES]
QOS_WEIGHTS = [item["fraction"] for item in QOS_CLASSES]
QOS_SUBMISSION = {
    "interactive": ("interactive_chat_v1", "interactive"),
    "background": ("sustained_batch_v1", "batch"),
    "bulk": ("sustained_batch_v1", "batch"),
}
TOKEN_LIMITS = MANIFEST["token_limits"]
RETAINED_OUTCOMES = PROTOCOL["retained_outcomes"]
POLICY_BY_MODE = {"baseline": "incumbent_only", "candidate": "round_robin"}
TERMINAL_KINDS = {"completed", "cancelled", "failed"}
REQUEST_CANCELLATION_AFTER_SECONDS = 55.0
REQUEST_CLEANUP_SERIALIZATION_SECONDS = 3.0
STREAM_TIMEOUT_SECONDS = 70.0
WINDOW_WORKER_CLEANUP_TIMEOUT_SECONDS = STREAM_TIMEOUT_SECONDS * 2
WINDOW_COLLECTION_TIMEOUT_SECONDS = 900.0
ROUTE_ZERO_RESOURCE_TIMEOUT_SECONDS = 240.0


class GateError(RuntimeError):
    """Stable gate failure without response bodies or private material."""


class ExternalTermination(GateError):
    """Typed OS termination that must be sealed before the process exits."""

    def __init__(self, signal_number: int) -> None:
        self.signal_number = int(signal_number)
        try:
            self.signal_name = signal.Signals(self.signal_number).name
        except ValueError:
            self.signal_name = f"SIGNAL_{self.signal_number}"
        self.exit_code = 128 + self.signal_number
        super().__init__(f"external_{self.signal_name.lower()}")


def _raise_external_termination(signal_number: int, _frame: object) -> None:
    raise ExternalTermination(signal_number)


def _install_termination_handlers() -> dict[int, Any]:
    previous: dict[int, Any] = {}
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        previous[signal_number] = signal.getsignal(signal_number)
        signal.signal(signal_number, _raise_external_termination)
    return previous


def _restore_termination_handlers(previous: Mapping[int, Any]) -> None:
    for signal_number, handler in previous.items():
        signal.signal(signal_number, handler)


@dataclass(frozen=True)
class _RequestShape:
    bucket_name: str
    target_prompt_tokens: int
    prompt: str
    maximum_new_tokens: int
    qos_name: str
    workload_profile_id: str
    qos_class: str


class ProductSession:
    """Minimal ordinary-browser-gateway client (bootstrap + inference)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        jar = http.cookiejar.CookieJar()
        self._cookie_jar = jar
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
        self._bootstrap = self.json("GET", "/api/v1/bootstrap")
        self._qualification = self.json("GET", "/api/v1/qualification/current")
        self._control_opener = self._opener_from_cookies(jar)

    @staticmethod
    def _copy_cookie_jar(
        source: http.cookiejar.CookieJar,
    ) -> http.cookiejar.CookieJar:
        target = http.cookiejar.CookieJar()
        for cookie in source:
            target.set_cookie(copy.copy(cookie))
        return target

    @classmethod
    def _opener_from_cookies(
        cls,
        source: http.cookiejar.CookieJar,
    ) -> urllib.request.OpenerDirector:
        jar = cls._copy_cookie_jar(source)
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def fork(self) -> "ProductSession":
        """Clone browser ownership into request-local data/control channels."""

        child = object.__new__(type(self))
        child.base_url = self.base_url
        child._bootstrap = self._bootstrap
        child._qualification = self._qualification
        child._cookie_jar = self._copy_cookie_jar(self._cookie_jar)
        child._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(child._cookie_jar)
        )
        child._control_opener = self._opener_from_cookies(child._cookie_jar)
        return child

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        mutate: bool = False,
        timeout: float = STREAM_TIMEOUT_SECONDS,
        control: bool = False,
    ):
        headers = {"Accept": "application/json"}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        if mutate:
            session = self._bootstrap["session"]
            headers[session["csrf_header"]] = session["csrf_token"]
            headers["Origin"] = self.base_url
        request = urllib.request.Request(
            urllib.parse.urljoin(self.base_url + "/", path.lstrip("/")),
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            opener = self._control_opener if control else self._opener
            return opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            raise GateError(f"http_{method.lower()}_{error.code}") from error
        except (OSError, TimeoutError) as error:
            raise GateError(f"http_{method.lower()}_unavailable") from error

    def json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        with self.request(method, path, **kwargs) as response:
            document = json.load(response)
        if not isinstance(document, dict):
            raise GateError("http_response_not_object")
        return document

    def submit(
        self,
        *,
        prompt: str,
        maximum_new_tokens: int,
        workload_profile_id: str,
        qos_class: str,
    ) -> dict[str, Any]:
        return self.json(
            "POST",
            "/api/v1/inference",
            body={
                "protocol": "mycelium.request_gateway.v2",
                "prompt": prompt,
                "max_new_tokens": maximum_new_tokens,
                "qualification": self._qualification["binding"],
                "workload_profile_id": workload_profile_id,
                "qos_class": qos_class,
            },
            mutate=True,
        )

    def cancel(self, accepted: dict[str, Any]) -> None:
        with self.request(
            "DELETE",
            accepted["cancel_path"],
            mutate=True,
            timeout=10.0,
            control=True,
        ):
            return

    def stream_summary(
        self, accepted: dict[str, Any]
    ) -> tuple[dict[str, int], str | None]:
        """Return (event_counts, terminal_kind); terminal None = cleanup_failure."""

        event_counts: dict[str, int] = {}
        terminal = None
        deadline = time.monotonic() + STREAM_TIMEOUT_SECONDS
        try:
            with self.request("GET", accepted["event_path"]) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", "strict").rstrip("\r\n")
                    if line.startswith("event: "):
                        kind = line[7:]
                        event_counts[kind] = event_counts.get(kind, 0) + 1
                        if kind in TERMINAL_KINDS:
                            terminal = kind
                            break
                    if time.monotonic() >= deadline:
                        raise GateError("http_get_stream_deadline")
        except (OSError, TimeoutError) as error:
            raise GateError("http_get_stream_unavailable") from error
        return event_counts, terminal


def public_json(base_url: str, path: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(
            base_url.rstrip("/") + path, timeout=10.0
        ) as response:
            document = json.load(response)
    except (OSError, urllib.error.HTTPError, TimeoutError) as error:
        raise GateError("public_status_unavailable") from error
    if not isinstance(document, dict):
        raise GateError("public_status_not_object")
    return document


def install_replica_qualification(
    base_url: str, documents: list[dict[str, Any]]
) -> None:
    """Install (or clear) the live replica-qualification set (operator surface)."""

    operator_token = os.environ.get("MYCELIUM_A5_OPERATOR_TOKEN")
    if not isinstance(operator_token, str) or len(operator_token) < 32:
        raise GateError("operator_authorization_missing")
    body = json.dumps({"documents": documents}, sort_keys=True).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/__mycelium/replica-qualification/install",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": base_url.rstrip("/"),
            "Authorization": f"Bearer {operator_token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            document = json.load(response)
    except urllib.error.HTTPError as error:
        raise GateError(f"qualification_install_http_{error.code}") from error
    except (OSError, TimeoutError) as error:
        raise GateError("qualification_install_unavailable") from error
    if document != {"installed": len(documents)}:
        raise GateError("qualification_install_unexpected_response")


def _zero_live_resources(status: dict[str, Any]) -> bool:
    queue = status["queue"]
    return (
        queue["depth"] == 0
        and queue["active_request_ids"] == []
        and all(item["active_reservations"] == 0 for item in status["placements"])
    )


def wait_for(predicate: Callable[[], Any], *, timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise GateError("gate_state_timeout")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _live_route_binding(status: Mapping[str, Any]) -> str:
    if status.get("route_alive") is not True:
        raise GateError("route_not_alive")
    peers = status.get("peers")
    if not isinstance(peers, list) or not peers:
        raise GateError("route_peer_binding_missing")
    projection = {
        "deployment_id": status.get("deployment_id"),
        "topology_version": status.get("topology_version"),
        "route_identity_digest": status.get("route_identity_digest"),
        "peers": [
            {
                "node_id": peer.get("node_id"),
                "process_id": peer.get("process_id"),
                "sidecar_process_alive": peer.get("sidecar_process_alive"),
                "transport_running": peer.get("transport_running"),
                "transport_fatal": peer.get("transport_fatal"),
                "decode_mode": peer.get("decode_mode"),
            }
            for peer in peers
            if isinstance(peer, Mapping)
        ],
    }
    if len(projection["peers"]) != len(peers) or any(
        peer["sidecar_process_alive"] is not True
        or peer["transport_running"] is not True
        or peer["transport_fatal"] is not False
        for peer in projection["peers"]
    ):
        raise GateError("route_peer_binding_unhealthy")
    return _digest(projection)


def _bindings(
    actual_provenance: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The 15 frozen identical binding fields, derived from the frozen manifest."""

    session = MANIFEST["product_benchmark_session"]
    bindings = {
        "model_id": MANIFEST["model"]["model_id"],
        "model_revision": MANIFEST["model"]["model_revision"],
        "representation_digest": MANIFEST["model"]["representation_digest"],
        "base_deployment_id": MANIFEST["base_deployment_id"],
        "route_generation": MANIFEST["route_generation"],
        "workload_digest": workload_manifest_digest(),
        "arrival_schedule_digest": _digest(MANIFEST["arrival_schedule"]),
        "prompt_output_buckets_digest": _digest(MANIFEST["prompt_output_buckets"]),
        "qos_mix_digest": _digest(MANIFEST["qos_mix"]),
        "offered_concurrency": MANIFEST["offered_concurrency"],
        "token_limits_digest": _digest(MANIFEST["token_limits"]),
        "product_session_digest": _digest(session),
        "software_digest": session["software_digest"],
        "configuration_digest": session["configuration_digest"],
        "instrumentation_digest": session["instrumentation_digest"],
    }
    if actual_provenance is not None:
        if set(actual_provenance) != {
            "software_digest",
            "configuration_digest",
            "instrumentation_digest",
        } or any(
            not isinstance(value, str)
            or len(value) != 71
            or not value.startswith("sha256:")
            for value in actual_provenance.values()
        ):
            raise GateError("benchmark_actual_provenance_invalid")
        bindings.update(actual_provenance)
    return bindings


def _window_bindings(
    mode: str, actual_provenance: Mapping[str, str] | None = None
) -> dict[str, Any]:
    bindings = _bindings(actual_provenance)
    bindings["qualified_track_policy_digest"] = POLICY_BY_MODE[mode]
    return bindings


def _outcomes() -> dict[str, int]:
    return {name: 0 for name in RETAINED_OUTCOMES}


def _join_workers_until(workers: list[threading.Thread], *, deadline: float) -> bool:
    """Join every worker against one shared monotonic deadline."""

    for worker in workers:
        worker.join(timeout=max(0.0, deadline - time.monotonic()))
    return not any(worker.is_alive() for worker in workers)


class _WindowDriver:
    """Drives one measured window: Poisson arrivals, bounded concurrency,
    60+ terminal requests counted after the warmup span."""

    def __init__(self, session: ProductSession, rng: random.Random) -> None:
        self._session = session
        self._rng = rng
        self._lock = threading.Lock()
        self._measured_outcomes = _outcomes()
        self._measured_terminals = 0
        self._measured_tokens = 0
        self._in_flight = 0
        self._accepted: dict[
            int,
            tuple[ProductSession, dict[str, Any], threading.Event],
        ] = {}
        self._cancellation_lock = threading.Lock()
        self._cancellation_watchdogs: list[threading.Thread] = []
        self._cancellation_errors: list[str] = []
        self._stop = threading.Event()
        self._started = time.monotonic()
        self._measured_start: float | None = None
        self._measured_end: float | None = None

    def _next_arrival_delay(self) -> float:
        return -math.log(1.0 - self._rng.random()) / ARRIVAL_RATE

    def _prompt_for_target(self, index: int, target_prompt_tokens: int) -> str:
        # Qwen tokenizes the repeated leading-space lexical unit as one token;
        # reserve eight tokens for the stable request prefix and chat framing.
        repeated_units = max(1, target_prompt_tokens - 8)
        return f"A5 benchmark request {index}:" + " benchmark" * repeated_units

    def _max_tokens_for_bucket(self, bucket_name: str) -> int:
        return int(
            TOKEN_LIMITS["per_bucket_max_overrides"].get(
                bucket_name, TOKEN_LIMITS["maximum_new_tokens_default"]
            )
        )

    def _sample_request(self, index: int) -> _RequestShape:
        bucket = self._rng.choices(BUCKET_NAMES, weights=BUCKET_WEIGHTS, k=1)[0]
        bucket_contract = next(item for item in BUCKETS if item["name"] == bucket)
        target_prompt_tokens = self._rng.randint(
            int(bucket_contract["prompt_token_min"]),
            int(bucket_contract["prompt_token_max"]),
        )
        sampled_output_tokens = self._rng.randint(
            int(bucket_contract["output_token_min"]),
            int(bucket_contract["output_token_max"]),
        )
        maximum_new_tokens = min(
            sampled_output_tokens,
            self._max_tokens_for_bucket(bucket),
        )
        qos_name = self._rng.choices(QOS_NAMES, weights=QOS_WEIGHTS, k=1)[0]
        workload_profile_id, qos_class = QOS_SUBMISSION[qos_name]
        return _RequestShape(
            bucket_name=bucket,
            target_prompt_tokens=target_prompt_tokens,
            prompt=self._prompt_for_target(index, target_prompt_tokens),
            maximum_new_tokens=maximum_new_tokens,
            qos_name=qos_name,
            workload_profile_id=workload_profile_id,
            qos_class=qos_class,
        )

    def _submit_one(
        self,
        index: int,
        request_shape: _RequestShape | None = None,
    ) -> None:
        shape = (
            request_shape if request_shape is not None else self._sample_request(index)
        )
        terminal_at: float | None = None
        terminal: str | None = None
        token_count = 0
        outcome = "error"
        request_session = self._session.fork()
        try:
            accepted = request_session.submit(
                prompt=shape.prompt,
                maximum_new_tokens=shape.maximum_new_tokens,
                workload_profile_id=shape.workload_profile_id,
                qos_class=shape.qos_class,
            )
        except GateError as error:
            code = str(error)
            outcome = "rejected_admission" if "http_post_4" in code else "error"
            terminal_at = time.monotonic()
        else:
            terminal_observed = threading.Event()
            with self._lock:
                self._accepted[index] = (
                    request_session,
                    accepted,
                    terminal_observed,
                )

            def cancel_at_timeout() -> None:
                if not terminal_observed.wait(REQUEST_CANCELLATION_AFTER_SECONDS):
                    self._cancel_and_wait(
                        request_session,
                        accepted,
                        terminal_observed,
                    )

            cancellation_watchdog = threading.Thread(
                target=cancel_at_timeout,
                name=f"a5-benchmark-timeout-{index}",
                daemon=True,
            )
            with self._lock:
                self._cancellation_watchdogs.append(cancellation_watchdog)
            cancellation_watchdog.start()
            try:
                counts, terminal = request_session.stream_summary(accepted)
                token_count = counts.get("token", 0)
                if terminal is None:
                    outcome = "cleanup_failure"
                elif terminal == "completed":
                    outcome = "completed"
                elif terminal == "cancelled":
                    outcome = "cancelled"
                else:
                    outcome = "error"
            except (GateError, OSError, TimeoutError):
                outcome = "timeout"
            except Exception:
                outcome = "error"
            finally:
                if terminal in TERMINAL_KINDS:
                    terminal_observed.set()
                cancellation_watchdog.join(timeout=1.0)
            terminal_at = time.monotonic()
        measured = (
            self._measured_start is not None
            and terminal_at is not None
            and terminal_at >= self._measured_start
        )
        with self._lock:
            self._accepted.pop(index, None)
            self._in_flight -= 1
            if measured and self._measured_end is None:
                self._measured_outcomes[outcome] += 1
                self._measured_terminals += 1
                self._measured_tokens += token_count if outcome == "completed" else 0
                if self._measured_terminals >= MIN_WINDOW_REQUESTS:
                    self._measured_end = time.monotonic()

    def _cancel_and_wait(
        self,
        session: ProductSession,
        accepted: dict[str, Any],
        terminal_observed: threading.Event,
    ) -> None:
        """Serialize control calls while allowing terminal waits to overlap.

        Saturated arrivals start their independent watchdogs close together.
        Sending all DELETE controls simultaneously is an instrumentation
        artifact, not part of the frozen Poisson arrival schedule.  Serialize
        only the short DELETE calls so each request installs its independent
        non-restartable 2,000 ms cleanup authority promptly.  Waiting for one
        stream terminal while holding this lock made a 64-request burst take at
        least 192 seconds to issue, so most clients timed out before their
        cancellation existed.  Stream observation is independent and bounded,
        and therefore belongs outside the shared control lock.
        """

        try:
            with self._cancellation_lock:
                if terminal_observed.is_set():
                    return
                session.cancel(accepted)
        except GateError as error:
            with self._lock:
                self._cancellation_errors.append(str(error))
            return
        terminal_observed.wait(REQUEST_CLEANUP_SERIALIZATION_SECONDS)

    def run(
        self,
        *,
        duration_after_measure: float = WINDOW_COLLECTION_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Drive arrivals until 60 measured terminals, then stop the stream."""

        self._measured_start = self._started + WARMUP_SECONDS
        arrivals: list[threading.Thread] = []
        submitter: threading.Thread | None = None

        def pump() -> None:
            index = 0
            while not self._stop.is_set():
                delay = self._next_arrival_delay()
                self._stop.wait(delay)
                if self._stop.is_set():
                    break
                with self._lock:
                    if self._in_flight >= OFFERED_CONCURRENCY:
                        continue
                    self._in_flight += 1
                index += 1
                request_shape = self._sample_request(index)
                worker = threading.Thread(
                    target=self._submit_one,
                    args=(index, request_shape),
                    daemon=True,
                )
                worker.start()
                arrivals.append(worker)

        submitter = threading.Thread(target=pump, daemon=True)
        submitter.start()
        assert self._measured_start is not None
        deadline = self._measured_start + duration_after_measure
        while self._measured_end is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self._stop.set()
        submitter.join(timeout=10.0)
        with self._lock:
            accepted_in_flight = sorted(self._accepted.items())
        for _, (session, accepted, terminal_observed) in accepted_in_flight:
            self._cancel_and_wait(session, accepted, terminal_observed)
        workers_stopped = _join_workers_until(
            arrivals,
            deadline=(time.monotonic() + WINDOW_WORKER_CLEANUP_TIMEOUT_SECONDS),
        )
        with self._lock:
            cancellation_watchdogs = list(self._cancellation_watchdogs)
        cancellation_watchdogs_stopped = _join_workers_until(
            cancellation_watchdogs,
            deadline=(time.monotonic() + WINDOW_WORKER_CLEANUP_TIMEOUT_SECONDS),
        )
        if self._measured_end is None:
            raise GateError("window_measured_terminals_timeout")
        measured_start = self._measured_start
        measured_end = self._measured_end
        if not workers_stopped:
            raise GateError("window_worker_cleanup_timeout")
        if not cancellation_watchdogs_stopped:
            raise GateError("window_cancellation_cleanup_timeout")
        with self._lock:
            cancellation_errors = tuple(self._cancellation_errors)
        if cancellation_errors:
            raise GateError(
                f"window_cancellation_control_failed:{cancellation_errors[0]}"
            )
        return {
            "outcomes": dict(sorted(self._measured_outcomes.items())),
            "completed_generated_tokens": self._measured_tokens,
            "wall_clock_seconds": measured_end - measured_start,
            "request_count": sum(self._measured_outcomes.values()),
        }


def _run_isolated_window(
    base_url: str,
    rng: random.Random,
) -> dict[str, Any]:
    """Run one window in a fresh bounded browser-ownership session.

    The product gateway intentionally retains request ownership for the life of
    a browser session.  A benchmark window can consume that bounded history,
    so sharing one session across windows would turn later arrivals into
    request-capacity rejections instead of measuring the selected route mode.
    """

    return _WindowDriver(ProductSession(base_url), rng).run()


def _load_replica_documents(paths: list[Path]) -> list[dict[str, Any]]:
    from mycelium_replica_contracts import validate_replica_qualification

    documents: list[dict[str, Any]] = []
    for path in paths:
        candidate = Path(path)
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or candidate.stat().st_size > 4 * 1024 * 1024
        ):
            raise GateError("replica_qualification_unsafe")
        try:
            document = json.loads(candidate.read_text("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise GateError("replica_qualification_invalid") from exc
        documents.append(validate_replica_qualification(document))
    return documents


def run_benchmark(
    base_url: str,
    replica_paths: list[Path],
    *,
    source_manifest_digest: str,
) -> dict[str, Any]:
    replica_documents = _load_replica_documents(replica_paths)
    if not replica_documents:
        raise GateError("replica_qualification_missing")

    before = public_json(base_url, "/__mycelium/live-status")
    if before.get("route_alive") is not True:
        raise GateError("route_not_alive")
    if not before.get("replica_track_qualification"):
        raise GateError("replica_track_not_qualified")
    before_runtime = public_json(base_url, "/__mycelium/runtime/admission-status")
    if not _zero_live_resources(before_runtime):
        raise GateError("route_not_clean_and_alive")
    route_binding_digest = _live_route_binding(before)
    actual_provenance = {
        "software_digest": source_manifest_digest,
        "configuration_digest": route_binding_digest,
        "instrumentation_digest": "sha256:"
        + hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }

    rng = random.Random(ARRIVAL_SEED)
    fixture_bindings = _bindings(actual_provenance)
    warmups: list[dict[str, Any]] = []
    windows: list[dict[str, Any]] = []
    for mode in SCHEDULE["warmup_modes"]:
        if (
            _live_route_binding(public_json(base_url, "/__mycelium/live-status"))
            != route_binding_digest
        ):
            raise GateError("benchmark_route_binding_changed")
        install_replica_qualification(
            base_url, replica_documents if mode == "candidate" else []
        )
        warmup = _run_isolated_window(base_url, rng)
        warmups.append(
            {
                "mode": mode,
                "scored": False,
                "request_count": warmup["request_count"],
                "outcomes": warmup["outcomes"],
                "bindings": _window_bindings(mode, actual_provenance),
                "invalidations": [],
            }
        )
        if warmup["request_count"] < MIN_WARMUP_REQUESTS:
            raise GateError("warmup_below_minimum_requests")
        wait_for(
            lambda: (
                status
                if _zero_live_resources(
                    status := public_json(
                        base_url, "/__mycelium/runtime/admission-status"
                    )
                )
                else None
            ),
            timeout=ROUTE_ZERO_RESOURCE_TIMEOUT_SECONDS,
        )
        if (
            _live_route_binding(public_json(base_url, "/__mycelium/live-status"))
            != route_binding_digest
        ):
            raise GateError("benchmark_route_binding_changed")

    for expected in SCHEDULE["measured_windows"]:
        mode = expected["mode"]
        install_replica_qualification(
            base_url, replica_documents if mode == "candidate" else []
        )
        window = _run_isolated_window(base_url, rng)
        if window["request_count"] < MIN_WINDOW_REQUESTS:
            raise GateError("window_below_minimum_requests")
        windows.append(
            {
                "index": expected["index"],
                "block": expected["block"],
                "pair_id": expected["pair_id"],
                "mode": mode,
                **window,
                "bindings": _window_bindings(mode, actual_provenance),
                "invalidations": [],
            }
        )
        wait_for(
            lambda: (
                status
                if _zero_live_resources(
                    status := public_json(
                        base_url, "/__mycelium/runtime/admission-status"
                    )
                )
                else None
            ),
            timeout=ROUTE_ZERO_RESOURCE_TIMEOUT_SECONDS,
        )
        if (
            _live_route_binding(public_json(base_url, "/__mycelium/live-status"))
            != route_binding_digest
        ):
            raise GateError("benchmark_route_binding_changed")

    fixture = {
        "protocol": "mycelium.a5_benchmark_run_fixture.v1",
        "benchmark_protocol_digest": protocol_digest(PROTOCOL),
        "workload_manifest_digest": workload_manifest_digest(),
        "warmups": warmups,
        "windows": windows,
    }
    decision = evaluate_benchmark(fixture)
    after_status = public_json(base_url, "/__mycelium/live-status")
    return {
        "protocol": "mycelium.a5_benchmark_evidence.v1",
        "qualification_claim": False,
        "promotion_authorized": False,
        "decision": decision,
        "run_fixture": fixture,
        "observed": {
            "identical_bindings": fixture_bindings,
            "policy_by_mode": POLICY_BY_MODE,
            "arrival_rng_seed": ARRIVAL_SEED,
            "arrival_rate_per_second": ARRIVAL_RATE,
            "offered_concurrency": OFFERED_CONCURRENCY,
            "warmup_seconds": WARMUP_SECONDS,
            "qos_submission_mapping": QOS_SUBMISSION,
            "live_route_binding_digest": route_binding_digest,
            "final_live_status_protocol": after_status.get("protocol"),
            "final_replica_loss_placement_ids": after_status.get(
                "replica_loss_placement_ids", []
            ),
        },
    }


def _failure_live_observation(base_url: str) -> dict[str, Any]:
    """Capture a bounded privacy-reduced route/runtime snapshot after failure."""

    live: Mapping[str, Any] = {}
    runtime: Mapping[str, Any] = {}
    try:
        live = public_json(base_url, "/__mycelium/live-status")
    except Exception:
        pass
    try:
        runtime = public_json(base_url, "/__mycelium/runtime/admission-status")
    except Exception:
        pass

    incidents = live.get("incidents")
    reason_counts: dict[str, int] = {}
    if isinstance(incidents, list):
        for incident in incidents[-64:]:
            if not isinstance(incident, Mapping):
                continue
            reason = incident.get("reason")
            if not isinstance(reason, str):
                # Product-spine projections rename the route-owned ``reason``
                # field to ``reason_code``; accept that bounded projection as a
                # compatibility fallback while preferring the live-route schema.
                reason = incident.get("reason_code")
            if (
                isinstance(reason, str)
                and 0 < len(reason) <= 128
                and all(
                    character.islower() or character.isdigit() or character == "_"
                    for character in reason
                )
            ):
                reason_counts[reason] = reason_counts.get(reason, 0) + 1

    requests = runtime.get("requests")
    request_rows = requests if isinstance(requests, list) else []
    placements = runtime.get("placements")
    placement_rows = placements if isinstance(placements, list) else []
    active_reservations = sum(
        value
        for row in placement_rows
        if isinstance(row, Mapping)
        and isinstance((value := row.get("active_reservations")), int)
        and not isinstance(value, bool)
        and value >= 0
    )
    losses = live.get("replica_loss_placement_ids")
    safe_losses = (
        [value for value in losses if isinstance(value, str) and len(value) <= 128]
        if isinstance(losses, list)
        else []
    )
    counters = live.get("counters")
    counter_row = counters if isinstance(counters, Mapping) else {}
    return {
        "live_status_protocol": live.get("protocol"),
        "runtime_status_protocol": runtime.get("protocol"),
        "route_alive": live.get("route_alive") is True,
        "route_identity_digest": live.get("route_identity_digest"),
        "deployment_id": live.get("deployment_id"),
        "route_counters": {
            key: counter_row.get(key)
            for key in (
                "frames_sent",
                "frames_received",
                "applied_operation_count",
                "fatal",
            )
        },
        "replica_loss_placement_ids": safe_losses,
        "incident_reason_counts": reason_counts,
        "total_requests": len(request_rows),
        "nonterminal_requests": sum(
            1
            for row in request_rows
            if isinstance(row, Mapping) and row.get("terminal_state") is None
        ),
        "active_reservations": active_reservations,
    }


def _failure_observation(
    *,
    base_url: str,
    output: Path,
    source_manifest_digest: str,
    error: Exception,
) -> dict[str, Any]:
    external_termination = isinstance(error, ExternalTermination)
    if external_termination:
        # A process-tree reaper may escalate SIGTERM to SIGKILL after a short
        # grace period. Do not spend that window on HTTP probes: seal the
        # source-bound termination receipt first, with an explicit statement
        # that live state was not sampled.
        live_observation = {
            "capture_status": "skipped_external_termination",
            "live_status_protocol": None,
            "runtime_status_protocol": None,
            "route_alive": False,
            "route_identity_digest": None,
            "deployment_id": None,
            "route_counters": {
                "frames_sent": None,
                "frames_received": None,
                "applied_operation_count": None,
                "fatal": None,
            },
            "replica_loss_placement_ids": [],
            "incident_reason_counts": {},
            "total_requests": 0,
            "nonterminal_requests": 0,
            "active_reservations": 0,
        }
    else:
        live_observation = _failure_live_observation(base_url)
    document = {
        "protocol": "mycelium.a5_benchmark_failure.v1",
        "qualification_claim": False,
        "promotion_authorized": False,
        "captured_at_unix_ms": int(time.time() * 1_000),
        "source_manifest_digest": source_manifest_digest,
        "benchmark_protocol_digest": protocol_digest(PROTOCOL),
        "workload_manifest_digest": workload_manifest_digest(),
        "reason_code": str(error)
        if isinstance(error, GateError)
        else "benchmark_unexpected_failure",
        "error_type": type(error).__name__,
        "benchmark_artifact_created": output.exists(),
        "live_observation": live_observation,
    }
    if external_termination:
        assert isinstance(error, ExternalTermination)
        document["termination_signal"] = error.signal_name
        document["termination_signal_number"] = error.signal_number
    return document


def atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def source_manifest_digest(path: Path) -> str:
    """Hash one exact regular, non-symlink frozen source manifest."""

    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise GateError("source_manifest_invalid")
        if metadata.st_size <= 0 or metadata.st_size > 16 * 1024 * 1024:
            raise GateError("source_manifest_invalid")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
    except GateError:
        raise
    except OSError as error:
        raise GateError("source_manifest_invalid") from error
    return "sha256:" + digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--base-url", default="http://127.0.0.1:8791")
    parser.add_argument(
        "--replica-qualification",
        type=Path,
        action="append",
        default=[],
        help="validated replica_qualification.v1 to restore for candidate windows",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--failure-output",
        type=Path,
        help="owner-private failure observation written before re-raising",
    )
    parser.add_argument(
        "--source-manifest-digest",
        help="optional claimed sha256; must equal the manifest bytes",
    )
    parser.add_argument("--source-manifest", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    frozen_source_digest = source_manifest_digest(args.source_manifest)
    if (
        args.source_manifest_digest is not None
        and args.source_manifest_digest != frozen_source_digest
    ):
        raise GateError("source_manifest_digest_mismatch")
    previous_handlers = _install_termination_handlers()
    handlers_restored = False
    try:
        report = run_benchmark(
            args.base_url,
            list(args.replica_qualification),
            source_manifest_digest=frozen_source_digest,
        )
        atomic_json(output, report)
    except Exception as error:
        # Restore before evidence I/O: a second signal must retain the host's
        # normal escalation semantics rather than recursively interrupting an
        # atomic failure write.
        _restore_termination_handlers(previous_handlers)
        handlers_restored = True
        if args.failure_output is not None:
            atomic_json(
                args.failure_output.resolve(),
                _failure_observation(
                    base_url=args.base_url,
                    output=output,
                    source_manifest_digest=frozen_source_digest,
                    error=error,
                ),
            )
        raise
    finally:
        if not handlers_restored:
            _restore_termination_handlers(previous_handlers)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ExternalTermination as error:
        raise SystemExit(error.exit_code) from None
