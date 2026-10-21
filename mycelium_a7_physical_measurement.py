# SPDX-License-Identifier: AGPL-3.0-only
"""A7 physical case measurement (the owner-authorised case-runner plug-in).

``scripts/run_a7_kv_successor_case_runner.py`` calls :func:`measure_case` for
one frozen gate case when the A7 fleet grant is present.  For that case this
module, and nothing else:

1. optionally launches the case's product serve (the run-day window supplies a
   per-case command that stages the case's A7 plan and execs the ordinary
   ``mycelium_demo serve``) and waits for its own ``live_server_started`` line
   with ``route_ready`` true and ``simulated`` false;
2. pins the source process group (exactly one match of the configured process
   pattern, locally or over ssh) before any request runs;
3. drives one unfaulted ordinary request (the reference output and the
   baseline time per output token) and reads the product's A7 resource
   evidence;
4. drives the measured ordinary request, waits until at least the configured
   number of tokens are committed *and* acknowledged by the standby (read from
   the entry's signed ``a7_snapshot``), then kills the source process group;
5. reads, from the product's own evidence only, the entry's cutover record,
   the request-scoped A4 incidents, the token stream to its terminal event and
   the terminal resource counters, and derives the closed case fields.

Nothing is simulated: every field is computed from an observation this module
made, and a missing observation refuses the measurement
(:class:`MeasurementRefused`) instead of defaulting.  The case's *expected*
result is never consulted; positive and fallback cases are measured
identically and the gate judges them.  Host specifics (ssh target, process
pattern, serve command, ports) come only from the measurement configuration.
"""

from __future__ import annotations

import hashlib
import http.client
import http.cookiejar
import json
import os
import re
import signal
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

CONFIG_PROTOCOL = "mycelium.a7_physical_measurement_config.v1"
CONFIG_ENV = "MYCELIUM_A7_PHYSICAL_CONFIG"
MEASUREMENT_PROTOCOL = "mycelium.a7_physical_measurement.v1"
EXACT_FLEET_GRANT = "FLEET GRANTED TO A7"
GRANT_HEADER = "X-Mycelium-A7-Fleet-Grant"
TOKEN_HEADER = "X-Mycelium-A7-Evidence-Token"
FAULT = "source_process_kill"
FAULT_UNCONFIRMED = "source_process_kill_unconfirmed"
TERMINAL_KINDS = frozenset({"completed", "cancelled", "failed"})
CLEANUP_NOT_MEASURED = "not_measured"
ENTRY_PROCESS_REPLACED = "entry_process_replaced"
ENTRY_PROCESS_IDENTITY_UNOBSERVED = "entry_process_identity_unobserved"
ENTRY_RESOURCES_UNOBSERVED = "entry_resources_unobserved"
COLD_ROUTE_RESTART_REFUSAL = "cold_route_restart_in_progress"
PROCESS_EXIT_PROOF = "process_exit"
CLEANUP_FIELDS = (
    "active_state_count_delta",
    "active_kv_bytes_delta",
    "shadow_bytes_delta",
    "reservation_delta",
    "fence_records_delta",
)
# The A7 recovery workspaces read one product evidence entity; a workspace is
# "checked" only when every attribute its panel renders is present.  Keys are
# those of ui/web/src/features/a7Recovery/A7RecoveryStatusBar.tsx.
A7_EVIDENCE_ENTITY = ("recovery", "a7-kv-successor-runtime")
WORKSPACE_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "inference": ("committed_token_count", "sync_ack_wait_median_ms", "recovery_prefill_operations", "duplicate_token_count"),
    "device_lab": ("standby_placement_fingerprint", "runtime_backend", "kv_reservation_bytes", "measured_free_bytes"),
    "network": ("last_checkpoint_bytes_per_sec", "last_ack_rtt_ms", "fence_advance_sequence", "transport_rtt_to_standby_ms"),
    "nodes": ("entry_node_binding", "source_node_binding", "standby_node_binding", "process_exited_incident_count"),
    "plans": ("standby_plan_digest", "deployment_id_epoch", "qualification_age_s", "parity_challenge_digest"),
    "readiness": ("fence_ledger_state", "durable_fence_generation", "parity_challenge_status", "qualification_ttl_remaining_s"),
    "incidents": ("scoped_incident_count_1h", "incident_reason_code", "recovery_path_chosen", "terminal_blocked_count"),
    "settings": ("collection_window_ms", "command_timeout_ms", "ack_timeout_s", "receipt_timeout_s"),
}
_CODE = re.compile(r"[a-z][a-z0-9_:.-]{0,120}\Z")


class MeasurementRefused(ValueError):
    """The measurement could not be completed honestly; ``code`` is stable."""

    def __init__(self, code: str, *, observations: Mapping[str, Any] | None = None) -> None:
        if not isinstance(code, str) or _CODE.fullmatch(code) is None:
            code = "a7_measurement_refused"
        super().__init__(code)
        self.code = code
        self.observations = None if observations is None else dict(observations)


def _sha_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha_json(value: Any) -> str:
    return _sha_bytes(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def _int(value: Any) -> int | None:
    return value if type(value) is int else None


# ---------------------------------------------------------------------------
# Fault injection: the source process group (local or over ssh)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessGroup:
    pgid: int
    pids: tuple[int, ...]


@dataclass(frozen=True)
class KillObservation:
    pgid: int
    kill_started_monotonic_s: float
    kill_returned_monotonic_s: float
    kill_exit_status: int


@dataclass(frozen=True)
class SourceProcessGroupTarget:
    """Find and kill exactly one process group whose command matches ``process_pattern``.

    ``transport`` is ``local`` (this host) or ``ssh`` (``ssh_target``, with an
    optional ``ssh_identity_file``).  Discovery reads the full process table
    once and matches the regular expression here, so the discovering shell and
    ``ps`` itself never match.  Zero or more than one matching group refuses.
    """

    transport: str
    process_pattern: str
    ssh_target: str | None = None
    ssh_identity_file: str | None = None
    connect_timeout_s: int = 5

    def __post_init__(self) -> None:
        if self.transport not in {"local", "ssh"}:
            raise MeasurementRefused("a7_fault_transport_invalid")
        if not isinstance(self.process_pattern, str) or not self.process_pattern:
            raise MeasurementRefused("a7_fault_pattern_invalid")
        try:
            re.compile(self.process_pattern)
        except re.error as exc:
            raise MeasurementRefused("a7_fault_pattern_invalid") from exc
        if self.transport == "ssh" and (not isinstance(self.ssh_target, str) or not self.ssh_target or self.ssh_target.startswith("-")):
            raise MeasurementRefused("a7_fault_ssh_target_invalid")
        if self.transport == "local" and self.ssh_target is not None:
            raise MeasurementRefused("a7_fault_ssh_target_invalid")

    # -- execution ---------------------------------------------------------------------

    def _ssh_argv(self, script: str) -> list[str]:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(self.connect_timeout_s)}", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=6"]
        if self.ssh_identity_file:
            argv += ["-i", self.ssh_identity_file, "-o", "IdentitiesOnly=yes"]
        return [*argv, str(self.ssh_target), script]

    def _run(self, script: str, *, timeout_s: float = 20.0) -> subprocess.CompletedProcess[str]:
        argv = self._ssh_argv(script) if self.transport == "ssh" else ["/bin/sh", "-c", script]
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise MeasurementRefused("a7_fault_command_failed") from exc

    def _table(self) -> tuple[int, list[tuple[int, int, int, str]]]:
        completed = self._run('echo "a7shell $$"; ps -axo pid=,ppid=,pgid=,command=')
        if completed.returncode != 0:
            raise MeasurementRefused("a7_fault_process_table_unavailable")
        lines = completed.stdout.splitlines()
        if not lines or not lines[0].startswith("a7shell "):
            raise MeasurementRefused("a7_fault_process_table_unavailable")
        shell = int(lines[0].split()[1])
        rows: list[tuple[int, int, int, str]] = []
        for line in lines[1:]:
            parts = line.strip().split(None, 3)
            if len(parts) < 4 or not all(part.isdigit() for part in parts[:3]):
                continue
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
        return shell, rows

    def discover(self) -> ProcessGroup:
        shell, rows = self._table()
        pattern = re.compile(self.process_pattern)
        excluded = {shell}
        if self.transport == "local":
            excluded |= {os.getpid(), os.getppid()}
        matches = [row for row in rows if row[0] not in excluded and row[1] != shell and pattern.search(row[3])]
        groups = sorted({row[2] for row in matches})
        if not groups:
            raise MeasurementRefused("a7_fault_target_not_found")
        if len(groups) != 1:
            raise MeasurementRefused("a7_fault_target_not_unique")
        pgid = groups[0]
        if pgid <= 1 or (self.transport == "local" and pgid == os.getpgrp()):
            raise MeasurementRefused("a7_fault_target_unsafe")
        return ProcessGroup(pgid=pgid, pids=tuple(sorted(row[0] for row in matches)))

    def _signal(self, group: ProcessGroup, name: str) -> int:
        if group.pgid <= 1 or (self.transport == "local" and group.pgid == os.getpgrp()):
            raise MeasurementRefused("a7_fault_target_unsafe")
        if self.transport == "local":
            try:
                os.killpg(group.pgid, getattr(signal, f"SIG{name}"))
                return 0
            except ProcessLookupError:
                return 1
            except OSError as exc:
                raise MeasurementRefused("a7_fault_signal_failed") from exc
        return self._run(f"kill -{name} -- -{int(group.pgid)}").returncode

    def pause(self, group: ProcessGroup) -> int:
        """Freeze the group (SIGSTOP) so its watermark can be read without racing it."""

        return self._signal(group, "STOP")

    def resume(self, group: ProcessGroup) -> int:
        return self._signal(group, "CONT")

    def kill(self, group: ProcessGroup) -> KillObservation:
        started = time.monotonic()
        status = self._signal(group, "KILL")
        return KillObservation(group.pgid, started, time.monotonic(), status)

    def group_gone(self, group: ProcessGroup, *, timeout_s: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while True:
            _shell, rows = self._table()
            if not any(row[2] == group.pgid for row in rows):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)


# ---------------------------------------------------------------------------
# Ordinary request drivers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenEvent:
    index: int
    token_id: int | None
    text_digest: str | None
    publisher_generation: int
    received_monotonic_s: float


@dataclass
class RunningRequest:
    """One driven request's stream as observed by the measurement (thread-safe)."""

    label: str
    started_monotonic_s: float = field(default_factory=time.monotonic)
    request_id: str | None = None
    tokens: list[TokenEvent] = field(default_factory=list)
    terminals: list[tuple[str, float]] = field(default_factory=list)
    error: str | None = None
    done: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _token_seen: threading.Condition = field(init=False)

    def __post_init__(self) -> None:
        self._token_seen = threading.Condition(self._lock)

    def on_token(self, index: int, token_id: int | None, *, text: str | None = None, publisher_generation: int = 0) -> None:
        event = TokenEvent(
            index=int(index),
            token_id=token_id if type(token_id) is int else None,
            text_digest=None if text is None else _sha_bytes(text.encode("utf-8")),
            publisher_generation=int(publisher_generation),
            received_monotonic_s=time.monotonic(),
        )
        with self._token_seen:
            self.tokens.append(event)
            self._token_seen.notify_all()

    def on_terminal(self, kind: str) -> None:
        with self._token_seen:
            self.terminals.append((str(kind), time.monotonic()))
            self._token_seen.notify_all()

    def finish(self, *, error: str | None = None) -> None:
        with self._token_seen:
            if error is not None and self.error is None:
                self.error = error
            self._token_seen.notify_all()
        self.done.set()

    def wait_for_tokens(self, count: int, *, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        with self._token_seen:
            while len(self.tokens) < count and not self.done.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._token_seen.wait(timeout=min(remaining, 0.1))
            return len(self.tokens) >= count

    def snapshot(self) -> tuple[list[TokenEvent], list[tuple[str, float]]]:
        with self._lock:
            return list(self.tokens), list(self.terminals)


class RequestDriver(Protocol):
    ordinary_browser_request: bool

    def start(self, label: str) -> RunningRequest: ...

    def workspaces_checked(self) -> list[str]: ...


class ProductHttpDriver:
    """The ordinary browser gateway path: bootstrap, CSRF session, inference, SSE.

    The same calls a browser session makes against the product origin
    (``/api/v1/bootstrap``, ``/api/v1/qualification/current``,
    ``POST /api/v1/inference`` and the request's event stream).
    """

    ordinary_browser_request = True

    def __init__(self, origin: str, *, prompt: str, max_new_tokens: int, timeout_s: float = 900.0) -> None:
        parsed = urllib.parse.urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise MeasurementRefused("a7_product_origin_invalid")
        if not isinstance(prompt, str) or not prompt:
            raise MeasurementRefused("a7_prompt_invalid")
        if type(max_new_tokens) is not int or not 4 <= max_new_tokens <= 512:
            raise MeasurementRefused("a7_max_new_tokens_invalid")
        self.origin = origin.rstrip("/")
        self._prompt = prompt
        self._max_new_tokens = max_new_tokens
        self._timeout = float(timeout_s)

    def _opener(self) -> urllib.request.OpenerDirector:
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def _open(self, opener: urllib.request.OpenerDirector, method: str, path: str, *, body: Any = None, headers: Mapping[str, str] | None = None, timeout: float = 60.0):
        request_headers = {"Accept": "application/json", **(headers or {})}
        payload = None
        if body is not None:
            request_headers["Content-Type"] = "application/json"
            payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            urllib.parse.urljoin(self.origin + "/", path.lstrip("/")), data=payload, headers=request_headers, method=method
        )
        try:
            return opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raise MeasurementRefused(f"a7_product_http_{method.lower()}_{exc.code}") from exc
        except (OSError, TimeoutError) as exc:
            raise MeasurementRefused(f"a7_product_http_{method.lower()}_unavailable") from exc

    def _json(self, opener: urllib.request.OpenerDirector, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        with self._open(opener, method, path, **kwargs) as response:
            document = json.load(response)
        if not isinstance(document, dict):
            raise MeasurementRefused("a7_product_response_invalid")
        return document

    def start(self, label: str) -> RunningRequest:
        opener = self._opener()
        bootstrap = self._json(opener, "GET", "/api/v1/bootstrap")
        qualification = self._json(opener, "GET", "/api/v1/qualification/current")
        session = bootstrap.get("session") if isinstance(bootstrap.get("session"), Mapping) else None
        if session is None or not isinstance(qualification.get("binding"), Mapping):
            raise MeasurementRefused("a7_product_session_unavailable")
        running = RunningRequest(label=label)
        accepted = self._json(
            opener,
            "POST",
            "/api/v1/inference",
            body={
                "protocol": "mycelium.request_gateway.v2",
                "prompt": self._prompt,
                "max_new_tokens": self._max_new_tokens,
                "qualification": qualification["binding"],
                "workload_profile_id": "interactive_chat_v1",
                "qos_class": "interactive",
            },
            headers={session["csrf_header"]: session["csrf_token"], "Origin": self.origin},
        )
        request_id = accepted.get("request_id")
        event_path = accepted.get("event_path")
        if not isinstance(request_id, str) or not request_id or not isinstance(event_path, str):
            raise MeasurementRefused("a7_product_accept_invalid")
        running.request_id = request_id

        def stream() -> None:
            try:
                with self._open(opener, "GET", event_path, headers={"Accept": "text/event-stream"}, timeout=self._timeout) as response:
                    kind: str | None = None
                    for raw in response:
                        line = raw.decode("utf-8", "strict").rstrip("\r\n")
                        if line.startswith("event: "):
                            kind = line[7:]
                        elif line.startswith("data: ") and kind is not None:
                            document = json.loads(line[6:])
                            if kind == "token":
                                running.on_token(
                                    document["token_index"],
                                    document.get("token_id"),
                                    text=document.get("text"),
                                    publisher_generation=int(document.get("publisher_generation", 0)),
                                )
                            elif kind in TERMINAL_KINDS:
                                running.on_terminal(kind)
                        elif line == "":
                            kind = None
                running.finish()
            except MeasurementRefused as exc:
                running.finish(error=exc.code)
            except Exception as exc:  # noqa: BLE001 - recorded, never raised from the thread
                running.finish(error="a7_product_stream_failed:" + type(exc).__name__.lower())

        threading.Thread(target=stream, name=f"a7-stream-{label}", daemon=True).start()
        return running

    # Why the last ``workspaces_checked`` call checked no workspace (a product
    # refusal code, or the A7 entity/attributes being absent); None otherwise.
    workspaces_unchecked_reason: str | None = None

    def workspaces_checked(self) -> list[str]:
        """Workspaces whose every rendered attribute is present in the product evidence.

        Like a browser, the read first bootstraps a product session: every
        product route except ``/api/v1/bootstrap`` refuses a request without
        the session cookie (401 ``session_required``).
        """

        self.workspaces_unchecked_reason = None
        opener = self._opener()
        try:
            self._json(opener, "GET", "/api/v1/bootstrap")
            document = self._json(opener, "GET", "/api/v1/product/snapshot")
        except MeasurementRefused as exc:
            self.workspaces_unchecked_reason = exc.code
            return []
        snapshot = document.get("snapshot") if isinstance(document.get("snapshot"), Mapping) else document
        entities = snapshot.get("entities") if isinstance(snapshot, Mapping) else None
        entity = next(
            (
                item
                for item in entities or ()
                if isinstance(item, Mapping) and (item.get("kind"), item.get("entity_id")) == A7_EVIDENCE_ENTITY
            ),
            None,
        )
        if not isinstance(entity, Mapping):
            self.workspaces_unchecked_reason = "a7_workspace_entity_missing"
            return []
        attributes = entity.get("attributes")
        if not isinstance(attributes, Mapping):
            self.workspaces_unchecked_reason = "a7_workspace_attributes_missing"
            return []
        reasons = attributes.get("unavailable_reasons")
        reasons = reasons if isinstance(reasons, Mapping) else {}
        # A workspace is checked when every field it renders is product evidence:
        # a measured value, or an explicit product-stated reason why the value
        # is unobserved (rendered as "unavailable"), never a silent gap.
        checked = sorted(
            workspace
            for workspace, keys in WORKSPACE_ATTRIBUTES.items()
            if all(
                key in attributes
                and (attributes[key] is not None or isinstance(reasons.get(key), str))
                for key in keys
            )
        )
        if not checked:
            self.workspaces_unchecked_reason = "a7_workspace_attributes_incomplete"
        return checked


# ---------------------------------------------------------------------------
# Evidence: the product's own A7 evidence door (or the same reader in-process)
# ---------------------------------------------------------------------------


class EvidenceSource(Protocol):
    def request(self, request_id: str) -> Mapping[str, Any]: ...

    def resources(self) -> Mapping[str, Any]: ...


TOKEN_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _read_evidence_token(path: Path) -> str:
    """The door's current per-lifetime token, read from its owner-only file.

    Read fresh for every client so a case that runs against a restarted serve
    picks up the new lifetime's token, and refused (never guessed) when the
    file is missing, not a regular file, or not exactly one 256-bit hex value.
    """

    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("not a regular file")
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise MeasurementRefused("a7_evidence_token_unreadable") from exc
    if TOKEN_HEX.fullmatch(value) is None:
        raise MeasurementRefused("a7_evidence_token_invalid")
    return value


class HttpEvidence:
    """Client of the serve's grant-gated ``/a7/evidence`` door.

    The door's per-lifetime token is read from the owner-only file the serve
    published the path of (in its ``live_server_started`` line) and is sent in
    a header only: never in the URL, never in a query parameter, never in a
    refusal.  ``http.client`` sends ``Host: 127.0.0.1:<port>``, which is
    exactly what the door requires.
    """

    def __init__(self, url: str, *, grant: str, token_file: str | Path, timeout_s: float = 10.0) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
            raise MeasurementRefused("a7_evidence_url_invalid")
        self._host = parsed.hostname
        self._port = parsed.port
        self._path = parsed.path or "/a7/evidence"
        self._grant = grant
        self._token = _read_evidence_token(Path(token_file))
        self._timeout = float(timeout_s)

    def _get(self, query: Mapping[str, str]) -> Mapping[str, Any]:
        connection = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        try:
            connection.putrequest("GET", self._path + "?" + urllib.parse.urlencode(query), skip_accept_encoding=True)
            connection.putheader("Accept", "application/json")
            connection.putheader(GRANT_HEADER, self._grant)
            connection.putheader(TOKEN_HEADER, self._token)
            connection.endheaders()
            response = connection.getresponse()
            body = response.read()
            status = response.status
        except OSError as exc:
            raise MeasurementRefused("a7_evidence_unreachable") from exc
        finally:
            connection.close()
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise MeasurementRefused("a7_evidence_response_invalid") from exc
        if status != 200 or not isinstance(document, Mapping) or not isinstance(document.get("evidence"), Mapping):
            code = document.get("code") if isinstance(document, Mapping) else None
            raise MeasurementRefused(code if isinstance(code, str) else f"a7_evidence_http_{status}")
        return document["evidence"]

    def request(self, request_id: str) -> Mapping[str, Any]:
        return self._get({"kind": "request", "request_id": request_id})

    def resources(self) -> Mapping[str, Any]:
        return self._get({"kind": "resources"})


# ---------------------------------------------------------------------------
# The measurement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasurementSettings:
    kill_after_committed: int = 2
    max_pause_attempts: int = 40
    first_tokens_timeout_s: float = 120.0
    ack_timeout_s: float = 20.0
    result_timeout_s: float = 60.0
    terminal_timeout_s: float = 900.0
    settle_timeout_s: float = 30.0
    poll_s: float = 0.05
    # SIGSTOP freezes the source process, not a token or checkpoint it already
    # handed to its transport.  Under the freeze the watermark is re-read at
    # this interval until two consecutive reads agree.
    pause_quiescence_s: float = 0.15

    def __post_init__(self) -> None:
        if type(self.kill_after_committed) is not int or self.kill_after_committed < 1:
            raise MeasurementRefused("a7_kill_after_invalid")


def _request_state(evidence: Mapping[str, Any]) -> Mapping[str, Any] | None:
    a7 = evidence.get("a7")
    request = a7.get("request") if isinstance(a7, Mapping) else None
    return request if isinstance(request, Mapping) else None


def _watermark(state: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(state.get(key) for key in ("committed", "acknowledged", "commits_sent", "ack_lag_events"))


def _quiescent_request(
    evidence: Any, request_id: str, current: Mapping[str, Any], settings: MeasurementSettings, *, reads: int = 3
) -> Mapping[str, Any] | None:
    """Re-read a frozen source's request until two consecutive reads agree; ``None`` if it never settles."""

    for _ in range(reads):
        time.sleep(settings.pause_quiescence_s)
        again = evidence.request(request_id)
        previous, state = _request_state(current), _request_state(again)
        if previous is not None and state is not None and _watermark(state) == _watermark(previous):
            return again
        current = again
    return None


def _await_unfrozen_settle(evidence: Any, request_id: str, running: RunningRequest, settings: MeasurementSettings) -> None:
    """Wait (bounded) until an unfrozen read shows every commit acknowledged or recorded ack lag."""

    deadline = time.monotonic() + settings.ack_timeout_s
    while time.monotonic() < deadline and not running.done.is_set():
        state = _request_state(evidence.request(request_id))
        if state is None or state.get("result") is not None:
            return
        lag = _int(state.get("ack_lag_events"))
        if state.get("acknowledged") == state.get("committed") or (lag is not None and lag > 0):
            return
        time.sleep(settings.poll_s / 5)


def _restored_from(state: Mapping[str, Any]) -> str | None:
    """The durable-record marker of request evidence served by a restarted entry."""

    marker = state.get("restored_from")
    return marker if isinstance(marker, str) and marker else None


def _node_with_role(resources: Mapping[str, Any], role: str) -> str | None:
    nodes = resources.get("nodes")
    if not isinstance(nodes, Mapping):
        return None
    matches = sorted(node for node, item in nodes.items() if isinstance(item, Mapping) and role in (item.get("a7_roles") or ()))
    return matches[0] if len(matches) == 1 else None


def _tpot_ms(tokens: Sequence[TokenEvent]) -> float | None:
    if len(tokens) < 2:
        return None
    return round((tokens[-1].received_monotonic_s - tokens[0].received_monotonic_s) * 1000.0 / (len(tokens) - 1), 3)


def _output(tokens: Sequence[TokenEvent]) -> list[Any]:
    """The published output in index order (first publication of each index)."""

    first: dict[int, Any] = {}
    for event in tokens:
        first.setdefault(event.index, event.token_id if event.token_id is not None else event.text_digest)
    return [first[index] for index in sorted(first)]


def _standby_counter(evidence: Mapping[str, Any], name: str) -> int | None:
    a7 = evidence.get("a7")
    runtime = a7.get("standby_runtime") if isinstance(a7, Mapping) else None
    return _int(runtime.get(name)) if isinstance(runtime, Mapping) else None


def _wait_done(running: RunningRequest, timeout_s: float) -> None:
    if not running.done.wait(timeout=timeout_s):
        raise MeasurementRefused("a7_request_terminal_timeout")


def _stable_resources(evidence: EvidenceSource, *, timeout_s: float, poll_s: float) -> Mapping[str, Any]:
    """Two consecutive identical resource reads (asynchronous release has finished)."""

    def counters(document: Mapping[str, Any]) -> Any:
        return {key: document.get(key) for key in ("nodes", "exited_nodes", "route_request_registrations", "gateway_reservations")}

    deadline = time.monotonic() + timeout_s
    previous = evidence.resources()
    while True:
        time.sleep(max(poll_s, 0.2))
        current = evidence.resources()
        if counters(current) == counters(previous) or time.monotonic() >= deadline:
            return current
        previous = current


# Refusals of a post-kill resources read while the route is being replaced (or
# its door is briefly unreachable): retried until the settle window ends.
_SETTLE_RETRYABLE = frozenset({"a7_evidence_route_unavailable", COLD_ROUTE_RESTART_REFUSAL, "a7_evidence_unreachable"})
_ROUTE_REPLACED_REFUSALS = frozenset({"a7_evidence_route_unavailable", COLD_ROUTE_RESTART_REFUSAL})


def _settled_resources(
    evidence: EvidenceSource,
    before: Mapping[str, Any],
    *,
    timeout_s: float,
    poll_s: float,
    reads: dict[str, Any] | None = None,
) -> Mapping[str, Any] | None:
    """Read the resources until they equal the pre-request values or the window ends.

    The last read is returned as observed; the window only waits out
    asynchronous release, it never chooses a value.  A read refused because
    the route is being replaced is retried until the window ends; ``None``
    means no read was answered in the window.  ``reads`` (when given)
    receives the attempt and refusal counts.
    """

    reads = {} if reads is None else reads
    reads.update(attempts=0, refusals={})
    deadline = time.monotonic() + timeout_s
    latest: Mapping[str, Any] | None = None
    while True:
        reads["attempts"] += 1
        try:
            latest = evidence.resources()
        except MeasurementRefused as exc:
            if exc.code not in _SETTLE_RETRYABLE:
                raise
            reads["refusals"][exc.code] = reads["refusals"].get(exc.code, 0) + 1
        if latest is not None and all(value == 0 for value in _cleanup_deltas(before, latest, strict=False).values()):
            return latest
        if time.monotonic() >= deadline:
            return latest
        time.sleep(max(poll_s, 0.2))


def _entry_process(document: Mapping[str, Any], entry_node: str, *, request: bool = False) -> Any:
    """The verified process identity of the entry in one door document (``None`` if absent)."""

    if request:
        return document.get("entry_process")
    nodes = document.get("nodes")
    node = nodes.get(entry_node) if isinstance(nodes, Mapping) else None
    return node.get("process") if isinstance(node, Mapping) else None


def _entry_process_replaced(
    entry_node: str,
    before: Mapping[str, Any],
    pre_kill: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    restored_seen: bool,
) -> str | None:
    """Why the entry's cleanup cannot be measured as one process's delta (``None``: it can)."""

    if restored_seen:
        return ENTRY_PROCESS_REPLACED
    nodes = after.get("nodes")
    if not isinstance(nodes, Mapping) or not isinstance(nodes.get(entry_node), Mapping):
        if any(isinstance(item, Mapping) for item in (after.get("retired_entry_processes") or ())):
            return ENTRY_PROCESS_REPLACED  # the route was retired and none replaced it yet
        return None  # no live entry to read: the delta refuses exactly as before
    identities = [
        _entry_process(before, entry_node),
        _entry_process(pre_kill, entry_node, request=True),
        _entry_process(after, entry_node),
    ]
    if all(identity is None for identity in identities):
        return None  # a door without process identity: the single-process reading as before
    if any(not isinstance(identity, Mapping) for identity in identities):
        return ENTRY_PROCESS_IDENTITY_UNOBSERVED
    if any(identity != identities[0] for identity in identities[1:]):
        return ENTRY_PROCESS_REPLACED
    return None


def _from_replacement(document: Mapping[str, Any], serving_process: Any) -> bool:
    """True when a request read was signed by another process than the serving entry."""

    process = document.get("entry_process")
    return isinstance(serving_process, Mapping) and isinstance(process, Mapping) and process != serving_process


_REPLACEMENT_STATE_FIELDS = ("active_state_count", "active_kv_bytes", "shadow_kv_bytes", "shadow_checkpoint_count", "entry_fence_record_count")


def _process_exit_evidence(
    entry_node: str,
    before: Mapping[str, Any],
    pre_kill: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    replacement_request_live: bool,
) -> dict[str, Any]:
    """The observed facts the gate's process-exit cleanup proof is checked against.

    Every value is as observed; nothing is concluded here.  ``old_entry_process``
    is set only when the before and at-kill reads name the same verified process.
    """

    at_before = _entry_process(before, entry_node)
    at_kill = _entry_process(pre_kill, entry_node, request=True)
    old = at_before if isinstance(at_before, Mapping) and at_before == at_kill else None
    retired = [item for item in (after.get("retired_entry_processes") or ()) if isinstance(item, Mapping)]
    exit_record = next(
        (
            {key: item.get(key) for key in ("host_id", "process_id", "process_transport", "exit_status", "observed_by")}
            for item in retired
            if old is not None
            and item.get("node_id") == entry_node
            and item.get("host_id") == old.get("host_id")
            and item.get("process_id") == old.get("process_id")
        ),
        None,
    )
    nodes = after.get("nodes") if isinstance(after.get("nodes"), Mapping) else {}
    entry = nodes.get(entry_node) if isinstance(nodes.get(entry_node), Mapping) else {}
    live_processes = [node.get("process") for node in nodes.values() if isinstance(node, Mapping)]
    return {
        "old_entry_process": None if old is None else dict(old),
        "old_entry_exit": exit_record,
        "replacement_entry_process": entry.get("process") if isinstance(entry.get("process"), Mapping) else None,
        "replacement_terminal_state": {
            **{name: entry.get(name) for name in _REPLACEMENT_STATE_FIELDS},
            "route_request_registrations": after.get("route_request_registrations"),
            "gateway_reservations": after.get("gateway_reservations"),
            "request_live": replacement_request_live,
        },
        "old_entry_process_live": old is not None and any(process == old for process in live_processes),
    }


def _cleanup_observation(
    entry_node: str,
    before: Mapping[str, Any],
    pre_kill: Mapping[str, Any],
    after: Mapping[str, Any],
    reason: str | None,
    restored_from: Any,
) -> dict[str, Any]:
    if reason is None:
        return {"cleanup_measurement": "measured"}
    return {
        "cleanup_measurement": CLEANUP_NOT_MEASURED,
        "cleanup_unmeasured_reason": reason,
        "restored_from": restored_from,
        "entry_process_before": _entry_process(before, entry_node),
        "entry_process_at_kill": _entry_process(pre_kill, entry_node, request=True),
        "entry_process_after": _entry_process(after, entry_node),
        "exited_nodes_before": list(before.get("exited_nodes") or ()),
        "exited_nodes_after": list(after.get("exited_nodes") or ()),
    }


def _cleanup_deltas(before: Mapping[str, Any], after: Mapping[str, Any], *, strict: bool = True) -> dict[str, int]:
    entry = before.get("entry_node_id")
    if entry != after.get("entry_node_id") or not isinstance(entry, str):
        if strict:
            raise MeasurementRefused("a7_cleanup_entry_changed")
        return {"unreadable": 1}
    first = before.get("nodes", {}).get(entry) if isinstance(before.get("nodes"), Mapping) else None
    last = after.get("nodes", {}).get(entry) if isinstance(after.get("nodes"), Mapping) else None
    if not isinstance(first, Mapping) or not isinstance(last, Mapping):
        if strict:
            raise MeasurementRefused("a7_cleanup_entry_unmeasured")
        return {"unreadable": 1}

    def reservations(document: Mapping[str, Any]) -> int | None:
        registrations = _int(document.get("route_request_registrations"))
        gateway = document.get("gateway_reservations")
        if registrations is None or (gateway is not None and _int(gateway) is None):
            return None
        return registrations + (gateway or 0)

    pairs = {
        "active_state_count_delta": (_int(first.get("active_state_count")), _int(last.get("active_state_count"))),
        "active_kv_bytes_delta": (_int(first.get("active_kv_bytes")), _int(last.get("active_kv_bytes"))),
        "shadow_bytes_delta": (_int(first.get("shadow_kv_bytes")), _int(last.get("shadow_kv_bytes"))),
        "reservation_delta": (reservations(before), reservations(after)),
        "fence_records_delta": (_int(first.get("entry_fence_record_count")), _int(last.get("entry_fence_record_count"))),
    }
    deltas: dict[str, int] = {}
    for name, (start, end) in pairs.items():
        if start is None or end is None:
            if strict:
                raise MeasurementRefused(f"a7_cleanup_unmeasured:{name}")
            deltas[name] = 1
            continue
        deltas[name] = end - start
    return deltas


def measure(
    *,
    driver: RequestDriver,
    evidence: EvidenceSource,
    fault: SourceProcessGroupTarget,
    settings: MeasurementSettings,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Measure one case; returns (case fields, privacy-reduced observations).

    The case fields are every ``mycelium.a7_physical_case.v1`` field except the
    plan binding (protocol, case, candidate, plan digest, evidence digest),
    which the case runner owns.  A refusal raised after the kill without its
    own observations carries what was observed up to it.
    """

    trace: dict[str, Any] = {}
    try:
        return _measure(driver=driver, evidence=evidence, fault=fault, settings=settings, trace=trace)
    except MeasurementRefused as refused:
        if refused.observations is not None or "fault" not in trace:
            raise
        raise MeasurementRefused(refused.code, observations=_post_kill_observations(trace)) from refused


def _post_kill_observations(trace: Mapping[str, Any]) -> dict[str, Any]:
    """The privacy-reduced observations of a measurement refused after the kill."""

    running: RunningRequest = trace["running"]
    tokens, terminals = running.snapshot()
    result = trace.get("a7_result")
    return {
        "protocol": MEASUREMENT_PROTOCOL,
        "refused_after_kill": True,
        "fault": dict(trace["fault"]),
        "result_poll": trace.get("result_poll"),
        "evidence_reads": trace.get("evidence_reads"),
        "settle_reads": trace.get("settle_reads"),
        "a7_result": None if not isinstance(result, Mapping) else dict(result),
        "measured": {
            "token_count": len(tokens),
            "terminals": [kind for kind, _ in terminals],
            "stream_error": running.error,
            "done": running.done.is_set(),
        },
    }


def _measure(
    *,
    driver: RequestDriver,
    evidence: EvidenceSource,
    fault: SourceProcessGroupTarget,
    settings: MeasurementSettings,
    trace: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    group = fault.discover()

    # 1. unfaulted reference request on the same serve, same prompt.
    baseline = driver.start("baseline")
    _wait_done(baseline, settings.terminal_timeout_s)
    baseline_tokens, baseline_terminals = baseline.snapshot()
    if baseline.error is not None or [kind for kind, _ in baseline_terminals] != ["completed"]:
        raise MeasurementRefused(
            "a7_baseline_not_completed",
            observations={
                "baseline": {
                    "terminals": [kind for kind, _ in baseline_terminals],
                    "error": baseline.error,
                    "token_count": len(baseline_tokens),
                }
            },
        )
    reference = _output(baseline_tokens)
    # At least the final decode step must remain for the successor.
    if len(reference) < settings.kill_after_committed + 1:
        raise MeasurementRefused("a7_baseline_too_short")
    baseline_tpot = _tpot_ms(baseline_tokens)

    # The standby must hold no shadow chain before the measured request, so the
    # checkpoint counters read at the kill belong to that request alone.
    release_deadline = time.monotonic() + settings.settle_timeout_s
    while True:
        before = _stable_resources(evidence, timeout_s=settings.settle_timeout_s, poll_s=settings.poll_s)
        held = {
            node_id: item
            for node_id, item in (before.get("nodes") or {}).items()
            if isinstance(item, Mapping) and "standby" in (item.get("a7_roles") or ()) and (item.get("shadow_checkpoint_count"), item.get("shadow_kv_bytes")) != (0, 0)
        }
        if not held:
            break
        if time.monotonic() >= release_deadline:
            # Record what was still held: the refusal is otherwise the only trace
            # of a standby that never released the baseline's shadow chain.
            raise MeasurementRefused(
                "a7_baseline_shadow_not_released",
                observations={
                    "resources_before": dict(before),
                    "held": {
                        node_id: {
                            "shadow_checkpoint_count": item.get("shadow_checkpoint_count"),
                            "shadow_kv_bytes": item.get("shadow_kv_bytes"),
                        }
                        for node_id, item in held.items()
                    },
                    "baseline": {"token_count": len(baseline_tokens)},
                },
            )
        time.sleep(max(settings.poll_s, 0.2))
    entry_node = before.get("entry_node_id")
    standby_node = _node_with_role(before, "standby")
    source_node = _node_with_role(before, "source")
    if not isinstance(entry_node, str) or standby_node is None or source_node is None:
        raise MeasurementRefused("a7_roles_not_staged")
    if standby_node != entry_node:
        # The cutover record and the standby counters are read from one entry
        # snapshot; another placement would need a second signed reader.
        raise MeasurementRefused("a7_standby_not_on_entry_unsupported")
    survivors = sorted(set(before.get("nodes", {})) - {source_node})
    if survivors != [entry_node]:
        raise MeasurementRefused("a7_cleanup_topology_unsupported")
    if fault.discover() != group:
        raise MeasurementRefused("a7_fault_target_changed")

    # 2. the measured request: kill after N committed and acknowledged tokens.
    running = driver.start("measured")
    if not running.wait_for_tokens(settings.kill_after_committed, timeout_s=settings.first_tokens_timeout_s):
        raise MeasurementRefused("a7_request_ended_before_fault" if running.done.is_set() else "a7_first_tokens_timeout")
    if running.request_id is None:
        raise MeasurementRefused("a7_request_id_unavailable")
    request_id = running.request_id
    # Wait until N tokens are committed, then freeze the source (SIGSTOP) and
    # re-read the entry's watermark.  The kill lands only on a frozen source
    # whose standby state is settled: either every committed token is
    # acknowledged, or the entry has itself recorded acknowledgement lag (a
    # standby that lags by configuration).  A source frozen in the middle of
    # one acknowledgement is resumed and retried, so the kill never races the
    # next commit; the entry's own bounded ack window still governs it.
    deadline = time.monotonic() + settings.ack_timeout_s
    ready = False
    while time.monotonic() < deadline and not running.done.is_set():
        state = _request_state(evidence.request(request_id))
        if state is not None and _int(state.get("committed")) is not None and state["committed"] >= settings.kill_after_committed:
            ready = True
            break
        time.sleep(settings.poll_s)
    if not ready:
        raise MeasurementRefused("a7_request_ended_before_fault" if running.done.is_set() else "a7_acknowledgement_timeout")
    pre_kill: Mapping[str, Any] | None = None
    kill_condition: str | None = None
    pause_attempts = 0
    paused_ms = 0.0
    # The last killable watermark leaves one reference token to the successor:
    # the final decode step.  Every stage that already executed its terminal
    # hop retains that KV until the entry's terminal release, so a source lost
    # there is still a fenced KV-successor cutover.
    last_killable = len(reference) - 1
    while pause_attempts < settings.max_pause_attempts and not running.done.is_set():
        # Freeze right after an unfrozen read shows a settled watermark: a
        # freeze that lands while an acknowledgement still waits on the
        # source's own checkpoint can never settle.
        _await_unfrozen_settle(evidence, request_id, running, settings)
        pause_attempts += 1
        paused_at = time.monotonic()
        if fault.pause(group) != 0:
            raise MeasurementRefused("a7_fault_pause_failed")
        current = evidence.request(request_id)
        state = _request_state(current)
        committed_now = None if state is None else _int(state.get("committed"))
        if (
            committed_now is not None
            and settings.kill_after_committed <= committed_now <= last_killable
            and state.get("result") is None
        ):
            lag = _int(state.get("ack_lag_events"))
            if state.get("acknowledged") != committed_now and lag is not None and lag > 0:
                pre_kill, kill_condition = current, "entry_recorded_ack_lag"
                break
            # A token or checkpoint the source sent just before the freeze may
            # still be in flight.  A token that commits without its
            # acknowledgement makes the entry truthfully refuse the fenced
            # cutover (watermark_behind_gateway); a checkpoint that lands
            # completes the pending acknowledgement.  Judge only the
            # watermark that stays put under the freeze.
            settled = _quiescent_request(evidence, request_id, current, settings)
            settled_state = None if settled is None else _request_state(settled)
            if (
                settled_state is not None
                and settled_state.get("result") is None
                and _int(settled_state.get("committed")) is not None
                and settings.kill_after_committed <= settled_state["committed"] <= last_killable
                and settled_state.get("acknowledged") == settled_state["committed"]
            ):
                pre_kill, kill_condition = settled, "acknowledged_equals_committed"
                break
        fault.resume(group)
        paused_ms += (time.monotonic() - paused_at) * 1000.0
        time.sleep(settings.poll_s)
    if pre_kill is None:
        if pause_attempts:
            fault.resume(group)
        raise MeasurementRefused("a7_request_ended_before_fault" if running.done.is_set() else "a7_watermark_not_settled")
    agent = pre_kill.get("a7", {}).get("agent") if isinstance(pre_kill.get("a7"), Mapping) else None
    checkpoint_count = _int(agent.get("shadow_checkpoint_count")) if isinstance(agent, Mapping) else None
    checkpoint_bytes = _int(agent.get("shadow_checkpoint_payload_bytes")) if isinstance(agent, Mapping) else None
    if checkpoint_count is None or checkpoint_bytes is None:
        fault.resume(group)
        raise MeasurementRefused("a7_checkpoint_counters_unavailable")
    tokens_at_kill = len(running.snapshot()[0])
    killed = fault.kill(group)
    trace["running"] = running
    trace["fault"] = {
        "transport": fault.transport,
        "pgid": killed.pgid,
        "pid_count": len(group.pids),
        "kill_exit_status": killed.kill_exit_status,
        "tokens_received_at_kill": tokens_at_kill,
        "pause_attempts": pause_attempts,
        "kill_condition": kill_condition,
    }

    # 3. the entry's cutover record (captured before any A6 restart replaces it).
    result_evidence: Mapping[str, Any] | None = None
    # Process-live fields (standby counters, incidents, release state) come
    # only from reads of the entry process that served the request.  A read
    # whose request evidence is ``restored_from`` a durable record was answered
    # by a restarted entry: it may supply the cutover record, nothing else.
    last_request_evidence: Mapping[str, Any] = pre_kill
    counter_read = "pre_kill"
    result_read: str | None = None
    serving_process = _entry_process(pre_kill, entry_node, request=True)
    replacement_request_live = False
    incidents: list[Mapping[str, Any]] = []
    # Why the cutover record was (not) observed: privacy-reduced counts and codes
    # of every post-kill read, so an unobserved result names its cause.
    result_poll: dict[str, Any] = {
        "reads": 0,
        "refusals": {},
        "stop": "result_timeout",
        "stream_done_at_stop": False,
        "last_state": None,
    }
    trace["result_poll"] = result_poll
    result_deadline = time.monotonic() + settings.result_timeout_s
    while time.monotonic() < result_deadline:
        try:
            current = evidence.request(request_id)
        except MeasurementRefused as exc:
            result_poll["refusals"][exc.code] = result_poll["refusals"].get(exc.code, 0) + 1
            if running.done.is_set():
                result_poll["stop"] = "refused_after_terminal"
                break
            time.sleep(settings.poll_s)
            continue
        result_poll["reads"] += 1
        state = _request_state(current)
        if state is None:
            result_poll["stop"] = "entry_request_absent"
            break  # the entry no longer holds the request (route replaced)
        result_poll["last_state"] = {
            key: state.get(key) for key in ("committed", "finished", "source_lost_accepted", "old_path_fenced", "request_attempt")
        }
        result_poll["last_state"]["result_present"] = isinstance(state.get("result"), Mapping)
        restored = _restored_from(state)
        replacement = _from_replacement(current, serving_process)
        if replacement and restored is None:
            # The replacement entry holds this request live: never its counters,
            # and it refutes a clean replacement state.
            replacement_request_live = True
        if restored is not None:
            result_poll["restored_reads"] = result_poll.get("restored_reads", 0) + 1
            result_poll["restored_from"] = restored
        elif replacement:
            pass
        else:
            last_request_evidence = current
            counter_read = f"poll:{result_poll['reads']}"
            incidents = [item for item in current.get("scoped_incidents", ()) if isinstance(item, Mapping)]
        if isinstance(state.get("result"), Mapping):
            result_evidence = current
            result_read = f"poll:{result_poll['reads']}"
            # A KV-successor cutover is recorded before the successor decodes:
            # keep reading until every reference token has streamed, so the
            # last live counter read follows the successor's decode.
            successor_streamed = state["result"].get("outcome") != "fenced_kv_successor" or len(
                {event.index for event in running.snapshot()[0]}
            ) >= len(reference)
            if running.done.is_set() or (successor_streamed and any(item.get("subject_id") == source_node for item in incidents)):
                result_poll["stop"] = "result_observed"
                break
        time.sleep(settings.poll_s)
    result_poll["stream_done_at_stop"] = running.done.is_set()

    _wait_done(running, settings.terminal_timeout_s)
    tokens, terminals = running.snapshot()
    # Post-terminal standby counters from the same entry process, when it still holds the request.
    # While no cutover record was observed, a refused read (the route is being
    # cold-restarted) is retried until the result deadline, so a restarted
    # entry's durable record can still be read; it supplies the record only.
    final_reads = {"attempts": 0, "refusals": {}}
    while True:
        final_reads["attempts"] += 1
        try:
            final = evidence.request(request_id)
        except MeasurementRefused as exc:
            final_reads["refusals"][exc.code] = final_reads["refusals"].get(exc.code, 0) + 1
            if result_evidence is None and time.monotonic() < result_deadline:
                time.sleep(max(settings.poll_s, 0.2))
                continue
            break
        final_state = _request_state(final)
        final_replacement = _from_replacement(final, serving_process)
        if final_state is not None and final_replacement and _restored_from(final_state) is None:
            replacement_request_live = True
        if final_state is not None and _restored_from(final_state) is not None:
            if result_evidence is None and isinstance(final_state.get("result"), Mapping):
                result_evidence = final
                result_read = "final"
            result_poll["restored_from"] = _restored_from(final_state)
        elif final_state is not None and not final_replacement:
            last_request_evidence = final
            counter_read = "final"
            incidents = [item for item in final.get("scoped_incidents", ()) if isinstance(item, Mapping)] or incidents
        break
    result_poll["final_reads"] = final_reads
    trace["evidence_reads"] = {"result_read": result_read, "counter_read": counter_read}
    trace["a7_result"] = (_request_state(result_evidence or {}) or {}).get("result")
    settle_reads: dict[str, Any] = {}
    trace["settle_reads"] = settle_reads
    settled = _settled_resources(evidence, before, timeout_s=settings.settle_timeout_s, poll_s=settings.poll_s, reads=settle_reads)
    settle_reads["observed"] = settled is not None
    # No resources read was answered in the settle window: the entry's cleanup
    # is not measured.  A route-restart refusal means the entry was replaced.
    after: Mapping[str, Any] = {} if settled is None else settled
    fault_confirmed = killed.kill_exit_status == 0 and fault.group_gone(group)

    # 4. derive the closed fields from the observations.
    result = _request_state(result_evidence or {})
    result = result.get("result") if isinstance(result, Mapping) else None
    if not isinstance(result, Mapping):
        result = None
    standby_before = (
        _int(before["nodes"][entry_node]["standby_runtime"].get("prefill_operation_count"))
        if isinstance(before["nodes"][entry_node].get("standby_runtime"), Mapping)
        else None,
        _int(before["nodes"][entry_node]["standby_runtime"].get("prefill_input_token_count"))
        if isinstance(before["nodes"][entry_node].get("standby_runtime"), Mapping)
        else None,
        _int(before["nodes"][entry_node]["standby_runtime"].get("decode_operation_count"))
        if isinstance(before["nodes"][entry_node].get("standby_runtime"), Mapping)
        else None,
    )
    standby_after = (
        _standby_counter(last_request_evidence, "prefill_operation_count"),
        _standby_counter(last_request_evidence, "prefill_input_token_count"),
        _standby_counter(last_request_evidence, "decode_operation_count"),
    )
    if None in standby_before:
        raise MeasurementRefused("a7_standby_counters_unavailable")
    if None in standby_after:
        # No read of the serving entry process carried the counters; a restarted
        # entry's counters belong to another process and are never compared.
        raise MeasurementRefused(
            "a7_standby_counters_unobserved" if result_poll.get("restored_from") is not None else "a7_standby_counters_unavailable"
        )
    prefill_ops, prefix_tokens, decode_ops = (end - start for start, end in zip(standby_before, standby_after))
    if min(prefill_ops, prefix_tokens, decode_ops) < 0:
        raise MeasurementRefused("a7_standby_counters_regressed")

    seen: set[int] = set()
    duplicates = 0
    stale_after_kill = 0
    for event in tokens:
        if event.index in seen:
            duplicates += 1
            if event.received_monotonic_s >= killed.kill_started_monotonic_s:
                stale_after_kill += 1
        seen.add(event.index)
    output = _output(tokens)
    missing = sum(1 for index in range(len(reference)) if index not in seen)
    after_kill = [event for event in tokens if event.received_monotonic_s >= killed.kill_started_monotonic_s]
    terminal_times = [at for _kind, at in terminals]
    resume_at = after_kill[0].received_monotonic_s if after_kill else (terminal_times[0] if terminal_times else None)
    if resume_at is None:
        raise MeasurementRefused("a7_resume_unobserved")
    candidate_tpot = _tpot_ms(tokens)
    if baseline_tpot is None or candidate_tpot is None:
        raise MeasurementRefused("a7_tpot_unmeasured")
    # Cleanup is a delta of one entry process.  When the process that served
    # the request was replaced (a restarted entry answered, or the verified
    # process identity changed), no delta is computed across the two
    # processes: cleanup is reported as not measured, with the exit evidence.
    if settled is None:
        restarted = result_poll.get("restored_from") is not None or bool(_ROUTE_REPLACED_REFUSALS & set(settle_reads["refusals"]))
        unmeasured_reason: str | None = ENTRY_PROCESS_REPLACED if restarted else ENTRY_RESOURCES_UNOBSERVED
    else:
        unmeasured_reason = _entry_process_replaced(
            entry_node,
            before,
            pre_kill,
            after,
            restored_seen=result_poll.get("restored_from") is not None,
        )
    deltas = None if unmeasured_reason is not None else _cleanup_deltas(before, after)
    # A replaced entry may prove its cleanup only by its verified process exit
    # (the A6 rule); the gate checks every condition of this evidence.
    exit_proof = (
        _process_exit_evidence(entry_node, before, pre_kill, after, replacement_request_live=replacement_request_live)
        if unmeasured_reason == ENTRY_PROCESS_REPLACED
        else None
    )
    acknowledged = None if result is None else result.get("acknowledged_token_count")
    committed = None if result is None else result.get("gateway_committed_token_count")
    source_incidents = [item for item in incidents if item.get("subject_id") == source_node]
    detection_ms = None
    if source_incidents and type(source_incidents[0].get("observed_at_monotonic_ms")) is int:
        detection_ms = source_incidents[0]["observed_at_monotonic_ms"] - int(killed.kill_started_monotonic_s * 1000)

    observations = {
        "protocol": MEASUREMENT_PROTOCOL,
        "request_id_digest": _sha_bytes(request_id.encode("utf-8")),
        "entry_node_id": entry_node,
        "source_node_id": source_node,
        "standby_node_id": standby_node,
        "fault": {
            "transport": fault.transport,
            "pgid": killed.pgid,
            "pid_count": len(group.pids),
            "kill_exit_status": killed.kill_exit_status,
            "kill_round_trip_ms": round((killed.kill_returned_monotonic_s - killed.kill_started_monotonic_s) * 1000.0, 3),
            "group_gone": fault_confirmed,
            "tokens_received_at_kill": tokens_at_kill,
            "pause_attempts": pause_attempts,
            "kill_condition": kill_condition,
            "resumed_pause_ms": round(paused_ms, 3),
            "watermark_at_kill": {
                key: (_request_state(pre_kill) or {}).get(key) for key in ("committed", "acknowledged", "commits_sent", "ack_lag_events")
            },
        },
        "detection_latency_ms": detection_ms,
        "source_incidents": [dict(item) for item in source_incidents],
        "a7_result": None if result is None else dict(result),
        "a7_result_observed": result is not None,
        "result_poll": result_poll,
        "settle_reads": settle_reads,
        "evidence_reads": {
            "result_read": result_read,
            "result_restored_from": None if result_evidence is None else _restored_from(_request_state(result_evidence) or {}),
            "counter_read": counter_read,
        },
        "entry_request_released": _request_state(last_request_evidence) is None,
        "baseline": {
            "token_count": len(baseline_tokens),
            "output_digest": _sha_json(reference),
            "tpot_ms": baseline_tpot,
        },
        "measured": {
            "token_count": len(tokens),
            "token_indices": [event.index for event in tokens],
            "publisher_generations": sorted({event.publisher_generation for event in tokens}),
            "output_digest": _sha_json(output),
            "terminals": [kind for kind, _ in terminals],
            "stream_error": running.error,
            "tpot_ms": candidate_tpot,
            "tokens_after_kill": len(after_kill),
        },
        "standby_counters": {"before": list(standby_before), "after": list(standby_after)},
        "resources_before": dict(before),
        "resources_after": dict(after),
        "cleanup": {
            **_cleanup_observation(entry_node, before, pre_kill, after, unmeasured_reason, result_poll.get("restored_from")),
            **({} if exit_proof is None else {"cleanup_proof": PROCESS_EXIT_PROOF, "cleanup_proof_evidence": exit_proof}),
        },
    }
    if result is None:
        raise MeasurementRefused("a7_result_unobserved", observations=observations)
    if _int(committed) is None:
        raise MeasurementRefused("a7_watermark_unobserved", observations=observations)
    if acknowledged is None:
        # The entry recorded no standby acknowledgement at all for this
        # request (no route watermark): zero committed tokens were acknowledged.
        observations["acknowledged_watermark_absent"] = True
        acknowledged = 0
    elif _int(acknowledged) is None:
        raise MeasurementRefused("a7_watermark_unobserved", observations=observations)
    for name in ("cutover_ms", "max_ack_wait_ms"):
        value = result.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MeasurementRefused(f"a7_result_field_unobserved:{name}", observations=observations)
    fields = {
        "ordinary_browser_request": driver.ordinary_browser_request is True,
        "fault": FAULT if fault_confirmed else FAULT_UNCONFIRMED,
        "outcome": str(result.get("outcome")),
        "reason": str(result.get("reason")),
        "kv_continuity_claimed": result.get("kv_continuity_claimed") is True,
        "committed_before_failure": committed,
        "acknowledged_before_failure": acknowledged,
        "successor_recovery_prefill_operations": prefill_ops,
        "prefix_tokens_sent_to_successor": prefix_tokens,
        "reference_parity": [kind for kind, _ in terminals] == ["completed"] and output == reference,
        "duplicate_tokens": duplicates,
        "missing_tokens": missing,
        "publishers": len({event.publisher_generation for event in tokens}),
        "terminal_results": len(terminals),
        "successor_decode_operations": decode_ops,
        "stale_source_publications_accepted": stale_after_kill,
        "interruption_ms": round((resume_at - killed.kill_started_monotonic_s) * 1000.0, 3),
        "cutover_ms": float(result["cutover_ms"]),
        "checkpoint_bytes_total": checkpoint_bytes,
        "checkpoint_count": checkpoint_count,
        "max_ack_wait_ms": float(result["max_ack_wait_ms"]),
        "baseline_tpot_ms": baseline_tpot,
        "candidate_tpot_ms": candidate_tpot,
        "cleanup": {
            entry_node: {name: deltas[name] for name in CLEANUP_FIELDS}
            if deltas is not None
            else {
                "cleanup_measurement": CLEANUP_NOT_MEASURED,
                "cleanup_unmeasured_reason": unmeasured_reason,
                "cleanup_proof": None if exit_proof is None else PROCESS_EXIT_PROOF,
                "cleanup_proof_evidence": exit_proof,
            }
        },
        "workspaces_checked": driver.workspaces_checked(),
    }
    # The closed case record cannot say why no workspace was checked; the
    # measurement observations do.
    observations["workspaces_unchecked_reason"] = getattr(driver, "workspaces_unchecked_reason", None)
    return fields, observations


# ---------------------------------------------------------------------------
# Serve lifecycle and the case-runner entry point
# ---------------------------------------------------------------------------


class ServeProcess:
    """One case's product serve, launched from the window's per-case command."""

    STARTED = "mycelium.live_server_started.v1"

    def __init__(self, argv: Sequence[str], *, cwd: str, log_path: Path, ready_timeout_s: float, stop_timeout_s: float) -> None:
        if not argv or not all(isinstance(item, str) and item for item in argv) or not Path(argv[0]).is_absolute():
            raise MeasurementRefused("a7_serve_argv_invalid")
        self._argv = list(argv)
        self._cwd = cwd
        self._log_path = log_path
        self._ready_timeout = float(ready_timeout_s)
        self._stop_timeout = float(stop_timeout_s)
        self._process: subprocess.Popen[bytes] | None = None
        self.started_line: dict[str, Any] | None = None

    def start(self) -> dict[str, Any]:
        self._log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._log_path.exists():
            raise MeasurementRefused("a7_serve_log_exists")
        log = open(self._log_path, "xb")  # noqa: SIM115 - owned by the child for its lifetime
        os.chmod(self._log_path, 0o600)
        self._process = subprocess.Popen(self._argv, cwd=self._cwd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise MeasurementRefused("a7_serve_exited_before_ready")
            for line in self._log_path.read_bytes().splitlines():
                if self.STARTED.encode() not in line:
                    continue
                try:
                    document = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                if document.get("protocol") != self.STARTED:
                    continue
                if document.get("route_ready") is not True or document.get("simulated") is not False:
                    raise MeasurementRefused("a7_serve_not_route_ready")
                self.started_line = document
                return document
            time.sleep(1.0)
        raise MeasurementRefused("a7_serve_ready_timeout")

    def stop(self) -> int | None:
        process = self._process
        if process is None:
            return None
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=self._stop_timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=30)
        return process.returncode


_CONFIG_FIELDS = frozenset({"protocol", "origin", "evidence_url", "prompt", "max_new_tokens", "kill_after_committed", "fault", "serve", "timeouts"})
_FAULT_FIELDS = frozenset({"transport", "process_pattern", "ssh_target", "ssh_identity_file"})
_SERVE_FIELDS = frozenset({"argv_by_case", "cwd", "log_dir", "ready_timeout_s", "stop_timeout_s"})


def load_config(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MeasurementRefused("a7_measurement_config_unreadable") from exc
    if not isinstance(document, dict) or set(document) != _CONFIG_FIELDS or document["protocol"] != CONFIG_PROTOCOL:
        raise MeasurementRefused("a7_measurement_config_invalid")
    if not isinstance(document["fault"], dict) or set(document["fault"]) != _FAULT_FIELDS:
        raise MeasurementRefused("a7_measurement_config_invalid")
    serve = document["serve"]
    if serve is not None and (not isinstance(serve, dict) or set(serve) != _SERVE_FIELDS):
        raise MeasurementRefused("a7_measurement_config_invalid")
    if not isinstance(document["timeouts"], dict):
        raise MeasurementRefused("a7_measurement_config_invalid")
    return document


def _serve_evidence_token_file(started: Mapping[str, Any] | None) -> str:
    """The evidence token file the case's own serve published when it started.

    Read from that serve's ``live_server_started`` line, never assumed: a
    serve that mounted no door, or published no path, refuses the case rather
    than reading some other file's token.
    """

    door = started.get("a7_evidence_control") if isinstance(started, Mapping) else None
    token_file = door.get("token_file") if isinstance(door, Mapping) else None
    if not isinstance(token_file, str) or not token_file:
        raise MeasurementRefused("a7_evidence_token_file_unpublished")
    return token_file


def measure_case(
    *,
    plan: Mapping[str, Any],
    case: str,
    grant: str | None,
    config_path: str | None,
    evidence_token_file: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The case-runner plug-in: measure ``case`` under ``plan`` from the configured fleet.

    ``evidence_token_file`` is only needed when the caller drives the door
    itself (no per-case serve): with a serve, the token file is the one the
    serve published in its own ``live_server_started`` line, read after the
    serve is ready so a restarted serve's new lifetime is always the one used.
    """

    if grant != EXACT_FLEET_GRANT:
        raise MeasurementRefused("fleet_grant_missing")
    if not config_path:
        raise MeasurementRefused("a7_measurement_config_missing")
    config = load_config(Path(config_path))
    minimum = plan.get("thresholds", {}).get("min_committed_tokens_before_failure")
    if type(config["kill_after_committed"]) is not int or type(minimum) is not int or config["kill_after_committed"] < minimum:
        raise MeasurementRefused("a7_kill_after_below_threshold")
    timeouts = {key: float(value) for key, value in config["timeouts"].items() if isinstance(value, (int, float)) and not isinstance(value, bool)}
    settings = MeasurementSettings(
        kill_after_committed=config["kill_after_committed"],
        **{key: timeouts[key] for key in ("first_tokens_timeout_s", "ack_timeout_s", "result_timeout_s", "terminal_timeout_s", "settle_timeout_s") if key in timeouts},
    )
    fault_config = config["fault"]
    fault = SourceProcessGroupTarget(
        transport=fault_config["transport"],
        process_pattern=fault_config["process_pattern"],
        ssh_target=fault_config["ssh_target"],
        ssh_identity_file=fault_config["ssh_identity_file"],
    )
    driver = ProductHttpDriver(config["origin"], prompt=config["prompt"], max_new_tokens=config["max_new_tokens"])
    serve: ServeProcess | None = None
    serve_config = config["serve"]
    if serve_config is not None:
        argv = serve_config["argv_by_case"].get(case) if isinstance(serve_config["argv_by_case"], Mapping) else None
        if not isinstance(argv, list):
            raise MeasurementRefused("a7_serve_case_missing")
        serve = ServeProcess(
            argv,
            cwd=str(serve_config["cwd"]),
            log_path=Path(serve_config["log_dir"]) / f"serve-{case}.log",
            ready_timeout_s=float(serve_config["ready_timeout_s"]),
            stop_timeout_s=float(serve_config["stop_timeout_s"]),
        )
    token_file = evidence_token_file
    serve_exit: int | None = None
    try:
        started = serve.start() if serve is not None else None
        if token_file is None:
            token_file = _serve_evidence_token_file(started)
        evidence = HttpEvidence(config["evidence_url"], grant=grant, token_file=token_file)
        fields, observations = measure(driver=driver, evidence=evidence, fault=fault, settings=settings)
    finally:
        if serve is not None:
            serve_exit = serve.stop()
    observations["serve"] = None if serve is None else {"started_line": started, "exit_status": serve_exit}
    observations["config_digest"] = _sha_json(config)
    return fields, observations


__all__ = (
    "CONFIG_ENV",
    "CONFIG_PROTOCOL",
    "EXACT_FLEET_GRANT",
    "HttpEvidence",
    "MeasurementRefused",
    "MeasurementSettings",
    "ProductHttpDriver",
    "RunningRequest",
    "ServeProcess",
    "SourceProcessGroupTarget",
    "load_config",
    "measure",
    "measure_case",
)
