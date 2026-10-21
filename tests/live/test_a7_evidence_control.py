# SPDX-License-Identifier: AGPL-3.0-only
"""The grant-gated, read-only A7 evidence door and its product mount."""

from __future__ import annotations

import http.client
import json
import os
import socket
import stat
from pathlib import Path
from typing import Any

import pytest

from mycelium_a7_evidence_control import (
    EXACT_FLEET_GRANT,
    GRANT_HEADER,
    TOKEN_HEADER,
    A7EvidenceControl,
    A7EvidenceControlRefused,
    RouteA7Evidence,
)


def _token_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)  # mkdir's mode is masked by the umask; assert 0700 outright
    return directory


def _door(tmp_path: Path, **kwargs: Any) -> A7EvidenceControl:
    control = A7EvidenceControl(
        grant=EXACT_FLEET_GRANT,
        source=RouteA7Evidence(object()),
        token_file=_token_dir(tmp_path) / "a7-evidence.token",
        product_port=8787,
        port=0,
        **kwargs,
    )
    return control


def _raw(port: int, lines: list[str]) -> tuple[int, Any]:
    """Send exactly these request lines, so duplicates and a wrong Host are testable."""
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


def _get(
    port: int,
    query: str,
    *,
    grant: str | None = EXACT_FLEET_GRANT,
    token: str | None,
    forwarded: bool = False,
    host: str | None = None,
    extra: dict[str, str] | None = None,
) -> tuple[int, Any]:
    lines = [f"GET /a7/evidence?{query} HTTP/1.1", f"Host: {host or f'127.0.0.1:{port}'}", "Accept: application/json"]
    if grant is not None:
        lines.append(f"{GRANT_HEADER}: {grant}")
    if token is not None:
        lines.append(f"{TOKEN_HEADER}: {token}")
    if forwarded:
        lines.append("X-Forwarded-For: 203.0.113.9")
    for name, value in (extra or {}).items():
        lines.append(f"{name}: {value}")
    return _raw(port, lines)


def _token(control: A7EvidenceControl) -> str:
    return control.token_file.read_text(encoding="ascii").strip()


def test_evidence_door_requires_the_grant_and_the_secret_token(tmp_path: Path) -> None:
    with pytest.raises(A7EvidenceControlRefused):
        A7EvidenceControl(grant=None, source=RouteA7Evidence(object()), token_file=tmp_path / "t", product_port=8787, port=0)
    with pytest.raises(A7EvidenceControlRefused):
        A7EvidenceControl(
            grant=EXACT_FLEET_GRANT,
            source=RouteA7Evidence(object()),
            token_file=tmp_path / "t",
            product_port=8787,
            port=0,
            public_origin="https://mycelium.now",
        )
    control = _door(tmp_path)
    control.start()
    try:
        token = _token(control)
        assert len(token) == 64 and token == token.lower()
        assert stat.S_IMODE(control.token_file.stat().st_mode) == 0o600
        # The public grant alone (the old door's only gate) is now refused.
        assert _get(control.port, "kind=resources", token=None) == (403, {"code": "a7_evidence_control_refused"})
        assert _get(control.port, "kind=resources", grant="wrong", token=token) == (403, {"code": "a7_evidence_control_refused"})
        assert _get(control.port, "kind=resources", token="0" * 64) == (403, {"code": "a7_evidence_control_refused"})
        assert _get(control.port, "kind=resources", token=token[:-1]) == (403, {"code": "a7_evidence_control_refused"})
        assert _get(control.port, "kind=resources", token=token, forwarded=True) == (403, {"code": "a7_evidence_control_refused"})
        assert _get(control.port, "kind=request", token=token)[0] == 400
        assert _get(control.port, "kind=resources&request_id=x", token=token)[0] == 400
        assert _get(control.port, "kind=delete", token=token)[0] == 400
        # A granted read on a non-route source fails closed with a stable code.
        assert _get(control.port, "kind=resources", token=token) == (409, {"code": "a7_evidence_route_unsupported"})
        assert control.status()["reads"] == {"request": 0, "resources": 0}
    finally:
        control.close()


def test_evidence_door_refuses_a_browser_context_a_foreign_host_and_duplicates(tmp_path: Path) -> None:
    control = _door(tmp_path)
    control.start()
    try:
        token = _token(control)
        port = control.port
        refused = {"code": "a7_evidence_control_refused"}
        assert _get(port, "kind=resources", token=token, host="evil.example") == (403, refused)
        assert _get(port, "kind=resources", token=token, host=f"localhost:{port}") == (403, refused)
        assert _get(port, "kind=resources", token=token, extra={"Origin": f"http://127.0.0.1:{port}"}) == (403, refused)
        assert _get(port, "kind=resources", token=token, extra={"Sec-Fetch-Mode": "cors"}) == (403, refused)
        assert _get(port, "kind=resources", token=token, extra={"Sec-Fetch-Site": "same-origin"}) == (403, refused)
        # Duplicated Host, grant and token headers are a smuggling attempt.
        assert _raw(port, ["GET /a7/evidence?kind=resources HTTP/1.1", f"Host: 127.0.0.1:{port}", f"Host: 127.0.0.1:{port}", f"{GRANT_HEADER}: {EXACT_FLEET_GRANT}", f"{TOKEN_HEADER}: {token}"]) == (403, refused)
        assert _raw(port, ["GET /a7/evidence?kind=resources HTTP/1.1", f"Host: 127.0.0.1:{port}", f"{GRANT_HEADER}: {EXACT_FLEET_GRANT}", f"{GRANT_HEADER}: {EXACT_FLEET_GRANT}", f"{TOKEN_HEADER}: {token}"]) == (403, refused)
        assert _raw(port, ["GET /a7/evidence?kind=resources HTTP/1.1", f"Host: 127.0.0.1:{port}", f"{GRANT_HEADER}: {EXACT_FLEET_GRANT}", f"{TOKEN_HEADER}: {token}", f"{TOKEN_HEADER}: {token}"]) == (403, refused)
        # A duplicated header never becomes a valid request, and reads stay zero.
        assert control.status()["reads"] == {"request": 0, "resources": 0}
    finally:
        control.close()


def test_evidence_door_token_file_lifecycle_is_owner_only_and_per_lifetime(tmp_path: Path) -> None:
    # A group- or world-readable parent can never hold a secret.
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    with pytest.raises(A7EvidenceControlRefused, match="token_dir_invalid"):
        A7EvidenceControl(grant=EXACT_FLEET_GRANT, source=RouteA7Evidence(object()), token_file=loose / "t", product_port=8787, port=0)
    with pytest.raises(A7EvidenceControlRefused, match="token_dir_invalid"):
        A7EvidenceControl(grant=EXACT_FLEET_GRANT, source=RouteA7Evidence(object()), token_file=tmp_path / "absent" / "t", product_port=8787, port=0)

    directory = _token_dir(tmp_path)
    stale = directory / "a7-evidence.token"
    stale.write_text("left over from an earlier life\n", encoding="ascii")
    os.chmod(stale, 0o600)
    control = _door(tmp_path)
    # A stale token file fails closed: it is never overwritten and never read.
    with pytest.raises(A7EvidenceControlRefused, match="token_file_invalid"):
        control.start()
    assert stale.read_text(encoding="ascii") == "left over from an earlier life\n"

    stale.unlink()
    control = _door(tmp_path)
    control.start()
    try:
        first = _token(control)
        # close() removes exactly this lifetime's file, and the token stops matching.
        control.close()
        assert not control.token_file.exists()
        assert control.token_matches(first) is False
        assert control.status()["mounted"] is False
    finally:
        control.close()

    # A token file replaced under a live door belongs to someone else: close
    # must not delete it.
    control = _door(tmp_path)
    control.start()
    replacement = control.token_file
    replacement.unlink()
    replacement.write_text("someone else's file\n", encoding="ascii")
    os.chmod(replacement, 0o600)
    control.close()
    assert replacement.read_text(encoding="ascii") == "someone else's file\n"
    # And because that foreign file is still there, the next lifetime refuses to
    # start rather than overwriting it: no secret is ever handed to a stale file.
    with pytest.raises(A7EvidenceControlRefused, match="token_file_invalid"):
        _door(tmp_path).start()
    assert replacement.read_text(encoding="ascii") == "someone else's file\n"
    replacement.unlink()

    # A new lifetime gets a different token and refuses the old one.
    control = _door(tmp_path)
    control.start()
    try:
        assert _token(control) != first
        assert control.token_matches(first) is False
    finally:
        control.close()

    # A symlinked token file is never followed and never overwritten.
    target = directory / "target"
    target.write_text("target\n", encoding="ascii")
    link = directory / "linked.token"
    link.symlink_to(target)
    control = A7EvidenceControl(
        grant=EXACT_FLEET_GRANT,
        source=RouteA7Evidence(object()),
        token_file=link,
        product_port=8787,
        port=0,
    )
    with pytest.raises(A7EvidenceControlRefused, match="token_file_invalid"):
        control.start()
    assert target.read_text(encoding="ascii") == "target\n"
    assert link.is_symlink()


def test_evidence_door_binds_before_it_writes_its_token(tmp_path: Path) -> None:
    """A bind failure must leave no token file: no secret outlives a door that never opened."""
    directory = _token_dir(tmp_path)
    path = directory / "a7-evidence.token"
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        taken = blocker.getsockname()[1]
        with pytest.raises(OSError):
            A7EvidenceControl(
                grant=EXACT_FLEET_GRANT,
                source=RouteA7Evidence(object()),
                token_file=path,
                product_port=8787,
                port=taken,
            ).start()
    assert not path.exists()
    assert list(directory.iterdir()) == []

    # The same holds when the token file itself cannot be created.
    control = _door(tmp_path)
    control._token_path = directory  # a directory where a file is required
    with pytest.raises(A7EvidenceControlRefused, match="token_file_invalid"):
        control.start()
    assert directory.is_dir()


def test_evidence_door_never_publishes_the_token(tmp_path: Path) -> None:
    control = _door(tmp_path)
    control.start()
    try:
        token = _token(control)
        document = control.status()
        assert document["token_file"] == str(control.token_file)
        assert token not in json.dumps(document)
        _, refused = _get(control.port, "kind=resources", token=None)
        assert "token" not in json.dumps(refused).lower()
    finally:
        control.close()


def test_measurement_client_reads_the_published_token_file(tmp_path: Path) -> None:
    from mycelium_a7_physical_measurement import HttpEvidence, MeasurementRefused

    control = _door(tmp_path)
    control.start()
    try:
        url = f"http://127.0.0.1:{control.port}/a7/evidence"
        evidence = HttpEvidence(url, grant=EXACT_FLEET_GRANT, token_file=control.token_file)
        with pytest.raises(MeasurementRefused, match="a7_evidence_route_unsupported"):
            evidence.resources()
        with pytest.raises(MeasurementRefused, match="a7_evidence_url_invalid"):
            HttpEvidence(f"http://localhost:{control.port}/a7/evidence", grant=EXACT_FLEET_GRANT, token_file=control.token_file)
        missing = tmp_path / "absent.token"
        with pytest.raises(MeasurementRefused, match="a7_evidence_token_unreadable"):
            HttpEvidence(url, grant=EXACT_FLEET_GRANT, token_file=missing)
        bad = tmp_path / "bad.token"
        bad.write_text("not-a-token\n", encoding="ascii")
        with pytest.raises(MeasurementRefused, match="a7_evidence_token_invalid"):
            HttpEvidence(url, grant=EXACT_FLEET_GRANT, token_file=bad)
        with pytest.raises(MeasurementRefused, match="a7_evidence_token_unreadable"):
            HttpEvidence(url, grant=EXACT_FLEET_GRANT, token_file=tmp_path)
        # The token is never put in the URL: only the header carries it.
        assert control.status()["reads"] == {"request": 0, "resources": 0}
    finally:
        control.close()


def test_supervisor_forwards_and_mounts_the_evidence_door_only_when_requested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import mycelium_live.supervisor as supervisor_module

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(supervisor_module, "run_physical_server", lambda **kwargs: calls.append(kwargs) or 0)
    base = ["--operator-plan", str(tmp_path / "one.json"), "--seed-state-root", str(tmp_path / "seed")]
    assert supervisor_module.main(base) == 0
    assert "a7_evidence_control_port" not in calls[-1]
    assert supervisor_module.main([*base, "--a7-evidence-control-port", "18797"]) == 0
    assert calls[-1]["a7_evidence_control_port"] == 18797
    with pytest.raises(SystemExit):
        supervisor_module.main([*base, "--a7-evidence-control-port", "80"])
    with pytest.raises(SystemExit):
        supervisor_module.main([*base, "--operator-plan", str(tmp_path / "two.json"), "--a7-evidence-control-port", "18797"])

    monkeypatch.delenv("MYCELIUM_A7_FLEET_GRANT", raising=False)
    with pytest.raises(RuntimeError, match="startup_a7_evidence_control_refused:explicit_fleet_grant_required"):
        supervisor_module._mount_a7_evidence_control(stack=SimpleNamespace(router=None), route=object(), product_port=8787, control_port=0, public_origin=None, trusted_https_proxy=False)
    monkeypatch.setenv("MYCELIUM_A7_FLEET_GRANT", EXACT_FLEET_GRANT)
    control = supervisor_module._mount_a7_evidence_control(stack=SimpleNamespace(router=None), route=object(), product_port=8787, control_port=0, public_origin=None, trusted_https_proxy=False)
    try:
        status = control.status()
        assert status["mounted"] is True and status["port"] == control.port
        assert "claim_boundary" in status
        # The mount publishes the token path (the client needs it) and not the token.
        token_file = Path(status["token_file"])
        assert token_file.is_file() and stat.S_IMODE(token_file.stat().st_mode) == 0o600
        assert token_file.read_text(encoding="ascii").strip() not in json.dumps(status)
        assert _get(control.port, "kind=resources", token=token_file.read_text(encoding="ascii").strip()) == (409, {"code": "a7_evidence_route_unsupported"})
    finally:
        control.close()
    # The private token directory goes with the door's lifetime.
    assert not token_file.parent.exists()


def test_node_side_counters_the_door_reads(tmp_path: Path) -> None:
    from mycelium_a7_fence_store import DurableFenceStore
    from mycelium_a7_standby_store import StandbyShadowStore

    store = DurableFenceStore(tmp_path / "fences")
    assert store.record_count() == 0
    begun = store.advance("request-a7", expected_fencing_generation=0)
    store.grant_publication("request-a7", generation=begun["fencing_generation"])
    (tmp_path / "fences" / ".partial.tmp").write_bytes(b"x")
    assert store.record_count() == 1
    store.mark_terminal("request-a7", generation=begun["fencing_generation"], outcome="completed")
    assert store.release("request-a7") is True
    assert store.record_count() == 0

    shadow = StandbyShadowStore(max_entry_bytes=1024, max_entries=4)
    assert shadow.snapshot() == {"shadow_entry_count": 0, "shadow_kv_bytes": 0}
    assert shadow.checkpoint_snapshot() == {"shadow_checkpoint_count": 0, "shadow_checkpoint_payload_bytes": 0}


def test_demo_cli_forwards_the_evidence_port_to_the_live_serve() -> None:
    from mycelium_demo.cli import _fixture_has_live_only_arguments, _live_arguments, build_parser

    plain = build_parser().parse_args(["serve", "--mode", "live"])
    assert "--a7-evidence-control-port" not in _live_arguments(plain)
    args = build_parser().parse_args(["serve", "--mode", "live", "--a7-evidence-control-port", "18797"])
    forwarded = _live_arguments(args)
    assert forwarded[forwarded.index("--a7-evidence-control-port") + 1] == "18797"
    assert _fixture_has_live_only_arguments(args) is True