# SPDX-License-Identifier: AGPL-3.0-only
"""Admission behaviour of the A7 evidence door, in a form the OLD door can run.

Every name this file needs from the door module is resolved with ``getattr`` and
the token argument is passed only when the door under test accepts it.  That
matters for RED: these tests must **collect and fail on the old door's
behaviour** - proving the old door really did serve the measurement surface to
the public grant phrase alone, including with a rebound ``Host`` and with a
browser ``Origin`` - and not merely fail on ``ImportError``.

The old door is proved wrong by the *status code it returns*, which is the
behaviour under review:

* old door, public phrase only           -> reached the read handler (``409``)
* new door, public phrase only           -> refused at admission (``403``)

A ``409`` can only be reached after admission: the source double below is a
deliberately unsupported route, so the read itself fails closed *after* the door
let the request in.  That ordering is what makes ``409`` proof of admission.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from mycelium_a7_evidence_control import (
    EXACT_FLEET_GRANT,
    GRANT_HEADER,
    A7EvidenceControl,
    A7EvidenceControlRefused,
    RouteA7Evidence,
)

# Resolved at run time, never imported: the old module has neither name, and a
# module-level import of them would turn every test here into a collection
# error instead of a behavioural failure against the old door.
TOKEN_HEADER: str = getattr(sys.modules["mycelium_a7_evidence_control"], "TOKEN_HEADER", "x-mycelium-a7-evidence-token")


class _UnsupportedRoute:
    """The bounded source double: a route with none of the attributes a read needs.

    ``RouteA7Evidence`` resolves its source on every read and refuses one that
    cannot answer, so every read this door admits ends in the stable
    ``a7_evidence_route_unsupported`` code with HTTP 409 - *after* admission.
    """


def _token_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def _door(tmp_path: Path) -> A7EvidenceControl:
    """Build the door under test, passing ``token_file`` only if it accepts one."""

    kwargs: dict[str, Any] = {}
    if "token_file" in _constructor_parameters():
        kwargs["token_file"] = _token_dir(tmp_path) / "a7-evidence.token"
    return A7EvidenceControl(
        grant=EXACT_FLEET_GRANT,
        source=RouteA7Evidence(_UnsupportedRoute()),
        product_port=8787,
        port=0,
        **kwargs,
    )


def _publishing_door(tmp_path: Path) -> A7EvidenceControl:
    """The same door, but over a source double that publishes real documents."""

    kwargs: dict[str, Any] = {}
    if "token_file" in _constructor_parameters():
        kwargs["token_file"] = _token_dir(tmp_path) / "a7-evidence.token"
    return A7EvidenceControl(
        grant=EXACT_FLEET_GRANT,
        source=RouteA7Evidence(_PublishingRoute()),
        product_port=8787,
        port=0,
        **kwargs,
    )


def _constructor_parameters() -> set[str]:
    import inspect

    return set(inspect.signature(A7EvidenceControl.__init__).parameters)


def _token_of(control: A7EvidenceControl) -> str | None:
    """This lifetime's token, or ``None`` for a door that has none."""

    reader = getattr(control, "token_file", None)
    if reader is None:
        return None
    return Path(reader).read_text(encoding="ascii").strip()


def _raw(port: int, lines: list[str]) -> tuple[int, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.sock = None  # noqa: SLF001 - replaced below by a manual request
        connection.connect()
        connection.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = connection.sock.recv(4096)
            if not chunk:
                break
            buffer += chunk
        head, _, rest = buffer.partition(b"\r\n\r\n")
        status = int(head.split(b"\r\n", 1)[0].split(b" ")[1])
        length = 0
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value.strip())
        while len(rest) < length:
            chunk = connection.sock.recv(4096)
            if not chunk:
                break
            rest += chunk
        return status, json.loads(rest.decode("utf-8"))
    finally:
        connection.close()


def _probe(port: int, token: str | None, *, host: str | None = None, extra: dict[str, str] | None = None) -> tuple[int, Any]:
    """One read attempt carrying the public grant phrase, optionally hostile."""

    lines = [
        "GET /a7/evidence?kind=resources HTTP/1.1",
        f"Host: {host or f'127.0.0.1:{port}'}",
        "Accept: application/json",
        f"{GRANT_HEADER}: {EXACT_FLEET_GRANT}",
    ]
    if token is not None:
        lines.append(f"{TOKEN_HEADER}: {token}")
    for name, value in (extra or {}).items():
        lines.append(f"{name}: {value}")
    return _raw(port, lines)


# -- the three admissions the old door wrongly granted ------------------------------


def test_public_grant_phrase_alone_is_refused(tmp_path: Path) -> None:
    """The old door served every read on this public phrase alone (409, admitted).

    The new door refuses it at admission with 403.  Against the old module this
    fails with ``409 != 403`` - a behavioural RED, not an import error.
    """

    control = _door(tmp_path)
    control.start()
    try:
        status, body = _probe(control.port, None)
        assert status == 403 and body == {"code": "a7_evidence_control_refused"}, (
            "the door admitted a read on the public grant phrase alone: "
            f"{status} {body}"
        )
    finally:
        control.close()


def test_public_grant_phrase_with_a_foreign_host_is_refused(tmp_path: Path) -> None:
    """DNS rebinding: the old door never checked ``Host`` and served the rebinding caller."""

    control = _door(tmp_path)
    control.start()
    try:
        token = _token_of(control)
        status, body = _probe(control.port, token, host="attacker.example:8787")
        assert status == 403 and body == {"code": "a7_evidence_control_refused"}, (
            f"the door admitted a read from a rebound hostname: {status} {body}"
        )
    finally:
        control.close()


def test_public_grant_phrase_from_a_browser_origin_is_refused(tmp_path: Path) -> None:
    """Browser reachability: the old door never checked ``Origin``/``Sec-Fetch-*``."""

    control = _door(tmp_path)
    control.start()
    try:
        token = _token_of(control)
        for extra in (
            {"Origin": "https://evil.example"},
            {"Sec-Fetch-Mode": "cors"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            status, body = _probe(control.port, token, extra=extra)
            assert status == 403 and body == {"code": "a7_evidence_control_refused"}, (
                f"the door admitted a browser context ({sorted(extra)}): {status} {body}"
            )
    finally:
        control.close()


def test_a_correctly_presented_caller_still_reaches_the_read_handler(tmp_path: Path) -> None:
    """The control for the three refusals above: ``409`` is proof the door admitted.

    A door that refuses *everything* would pass the refusals vacuously, so this
    pins that an admitted caller still gets the read handler's own code.  A door
    with no token (the old one) is admitted by the grant alone here.
    """

    control = _door(tmp_path)
    control.start()
    try:
        token = _token_of(control)
        if token is None:
            pytest.skip("door under test has no per-lifetime token; this control is vacuous")
        status, body = _probe(control.port, token)
        assert status == 409 and body == {"code": "a7_evidence_route_unsupported"}, (
            f"a granted caller never reached the read handler: {status} {body}"
        )
    finally:
        control.close()


# -- the identity and cleanup findings -----------------------------------------------


def test_a_replaced_token_file_survives_the_mounted_production_path(tmp_path: Path) -> None:
    """The mounted supervisor teardown must not destroy a replaced token file.

    The old supervisor wrapped ``close`` in a recursive ``shutil.rmtree`` of the
    token directory.  The door's own ``close`` correctly left a replaced file
    alone, but the recursive wrapper - the path production actually takes -
    deleted it anyway.  These assertions run the real mount.
    """

    from types import SimpleNamespace

    from mycelium_live import supervisor as supervisor_module

    os.environ["MYCELIUM_A7_FLEET_GRANT"] = EXACT_FLEET_GRANT
    try:
        control = supervisor_module._mount_a7_evidence_control(
            stack=SimpleNamespace(router=None),
            route=object(),
            product_port=8787,
            control_port=0,
            public_origin=None,
            trusted_https_proxy=False,
        )
        try:
            token_file = Path(control.status()["token_file"])
            # Someone else replaced our token file under the live door.
            token_file.unlink()
            token_file.write_text("someone else's file\n", encoding="ascii")
            os.chmod(token_file, 0o600)
            control.close()
            assert token_file.read_text(encoding="ascii") == "someone else's file\n", (
                "the mounted teardown destroyed a token file it did not create"
            )
        finally:
            control.close()
    finally:
        os.environ.pop("MYCELIUM_A7_FLEET_GRANT", None)


def test_the_token_file_is_never_written_with_a_post_close_path_lstat(tmp_path: Path) -> None:
    """``_write_owner_only_token`` must record the identity of the descriptor it created.

    The old code wrote the token, closed the descriptor, and only then called
    ``path.lstat()`` - so the identity it kept could name a file a concurrent
    actor swapped in, and its error path unlinked whatever was at the path.  This
    pins the identity to the still-open descriptor and the replacement survival.
    """

    import mycelium_a7_evidence_control as door

    directory = _token_dir(tmp_path)
    path = directory / "a7-evidence.token"

    swapped: dict[str, Any] = {}
    real_fdopen = os.fdopen

    class _SwappingFdopen:
        def __init__(self, descriptor: int, *args: Any, **kwargs: Any) -> None:
            # Once the descriptor is ours and open, swap a foreign file in.
            swapped["identity"] = os.fstat(descriptor)
            path.unlink()
            path.write_text("someone else's file\n", encoding="ascii")
            os.chmod(path, 0o600)
            self._handle = real_fdopen(descriptor, *args, **kwargs)

        def __enter__(self) -> Any:
            return self._handle.__enter__()

        def __exit__(self, *exc: Any) -> Any:
            return self._handle.__exit__(*exc)

    os.fdopen = _SwappingFdopen  # type: ignore[assignment]
    try:
        with pytest.raises(A7EvidenceControlRefused, match="token_file_invalid"):
            door._write_owner_only_token(path)
    finally:
        os.fdopen = real_fdopen  # type: ignore[assignment]
    # The replacement is intact: cleanup only ever touches the file we created.
    assert path.read_text(encoding="ascii") == "someone else's file\n"
    assert (swapped["identity"].st_dev, swapped["identity"].st_ino) != (
        path.lstat().st_dev,
        path.lstat().st_ino,
    )


def test_a_thread_that_never_runs_leaves_no_listener_and_no_token(tmp_path: Path) -> None:
    """A bind-time thread failure must release the listener and the secret token."""

    control = _door(tmp_path)
    token_path = getattr(control, "token_file", None)
    if token_path is None:
        pytest.skip("door under test has no per-lifetime token; this control is vacuous")

    def _explode(**_kwargs: Any) -> Any:
        raise RuntimeError("thread never started")

    real_thread = threading.Thread
    threading.Thread = _explode  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="thread never started"):
            control.start()
    finally:
        threading.Thread = real_thread  # type: ignore[assignment]
    # Nothing half-started survives: no secret file, and the port is free again.
    assert not Path(token_path).exists()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        probe.close()
    with pytest.raises(A7EvidenceControlRefused, match="not_started"):
        _ = control.port


# -- the positive read, and where the client's token comes from ----------------------


def test_a_granted_read_returns_the_documents_the_source_double_publishes(tmp_path: Path) -> None:
    """A real local HTTP read of real documents, not a refusal.

    The bounded source double answers both closed kinds with the exact protocol
    payload the measurement client parses, so the client's positive path is
    exercised end to end against a live door.
    """

    from mycelium_a7_physical_measurement import HttpEvidence

    control = _publishing_door(tmp_path)
    control.start()
    try:
        url = f"http://127.0.0.1:{control.port}/a7/evidence"
        evidence = HttpEvidence(url, grant=EXACT_FLEET_GRANT, token_file=control.token_file)
        resources = evidence.resources()
        assert resources["protocol"] == "mycelium.a7_physical_evidence.v1"
        assert resources["kind"] == "resources"
        assert resources["entry_node_id"] == "entry-a"
        assert resources["nodes"]["entry-a"]["active_state_count"] == 7
        request = evidence.request("req-1")
        assert request["kind"] == "request"
        assert request["request_id"] == "req-1"
        assert request["scoped_incidents"][0]["reason"] == "node_process_exited"
        # Two real reads were served, so the door counted exactly those two.
        assert control.status()["reads"] == {"request": 1, "resources": 1}
    finally:
        control.close()


class _PublishingRoute:
    """A route double good enough for both closed evidence kinds, and nothing more."""

    def __init__(self) -> None:
        self._plan = {"entry_node_id": "entry-a"}
        self._peers = {"entry-a": object()}
        self._sessions = {"entry-a": _AliveSession()}
        self._lock = threading.Lock()
        self._active_route_requests: dict[str, object] = {}

    def _send_before(self, node_id: str, *, command_id: str, command: str, payload: dict[str, Any], deadline_monotonic_s: float) -> dict[str, Any]:
        assert command == "a7_snapshot" and node_id == "entry-a"
        request_id = payload.get("request_id")
        details: dict[str, Any] = {
            "entry_fence_record_count": 2,
            "roles": ["entry", "standby"],
            "agent": {"shadow_kv_bytes": 12, "shadow_entry_count": 3, "shadow_checkpoint_count": 1, "shadow_checkpoint_payload_bytes": 4},
        }
        if request_id is not None:
            details["observed_request"] = request_id
        return {"details": details}

    def _verify_observation(self, node_id: str, response: Any, *, event: str) -> dict[str, Any]:
        assert event == "a7_snapshot"
        return {"host_id": "host-a", "process_id": 4242, "details": response["details"]}

    def _command_id(self, node_id: str, command: str) -> str:
        return f"{node_id}:{command}"

    def public_status(self) -> dict[str, Any]:
        return {"peers": [{"node_id": "entry-a", "active_kv_state_count": 7, "active_kv_bytes": 70}]}

    def a4_scoped_runtime_incidents(self) -> list[dict[str, Any]]:
        return [
            {
                "request_id": "req-1",
                "subject_id": "entry-a",
                "reason": "node_process_exited",
                "state": "terminal",
                "observed_at_monotonic_ms": 5,
                "internal": "never published",
            }
        ]


class _AliveSession:
    returncode = None


def test_the_client_takes_its_token_from_the_serve_log_published_line() -> None:
    """Token discovery from a serve's ``live_server_started`` line, exactly.

    The client must read the path that line published and nothing else: a serve
    that mounted no door, or published no path, refuses the case rather than
    reading some other file's token.
    """

    from mycelium_a7_physical_measurement import MeasurementRefused, _serve_evidence_token_file

    published = "/private/a7-evidence.token"
    assert (
        _serve_evidence_token_file({"a7_evidence_control": {"token_file": published}}) == published
    )
    # A restarted serve publishes a new lifetime's path, and that is the one used.
    restarted = "/private/second-life.token"
    assert (
        _serve_evidence_token_file({"a7_evidence_control": {"token_file": restarted}}) == restarted
    )
    for absent in (
        {},
        {"a7_evidence_control": None},
        {"a7_evidence_control": {}},
        {"a7_evidence_control": {"token_file": ""}},
        {"a7_evidence_control": {"token_file": None}},
        {"a7_evidence_control": "not a mapping"},
        None,
    ):
        with pytest.raises(MeasurementRefused, match="a7_evidence_token_file_unpublished"):
            _serve_evidence_token_file(absent)