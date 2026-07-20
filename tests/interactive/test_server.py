from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import pytest

from mycelium_interactive.runtime import InteractiveRuntime, InteractiveRuntimeError
import mycelium_interactive.server as interactive_server
from mycelium_interactive.server import create_server
from mycelium_interactive.swarm import matrix_digest
from mycelium_mobile.pixel_stage import PixelStage

OPERATOR_TOKEN = "test-operator-capability-token-that-is-long-enough"


def _post(
    origin: str,
    path: str,
    document: dict[str, Any],
    *,
    operator: bool = False,
) -> dict[str, Any]:
    body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    headers = {
        "content-type": "application/json",
        "content-length": str(len(body)),
    }
    if operator:
        headers["authorization"] = f"Bearer {OPERATOR_TOKEN}"
    request = Request(
        origin + path,
        data=body,
        method="POST",
        headers=headers,
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - loopback test server
        value = json.loads(response.read().decode("utf-8"))
    assert isinstance(value, dict)
    return value


def _get(origin: str, path: str) -> dict[str, Any]:
    request = Request(
        origin + path,
        headers={"authorization": f"Bearer {OPERATOR_TOKEN}"},
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - loopback test server
        value = json.loads(response.read().decode("utf-8"))
    assert isinstance(value, dict)
    return value


def _get_response(origin: str, path: str):
    return urlopen(origin + path, timeout=30)  # noqa: S310 - loopback test server


def _http_worker(origin: str, grant: dict[str, Any], stop: threading.Event, errors: list[BaseException]) -> threading.Thread:
    stage = PixelStage.from_document(grant["stage_pack"])

    def run() -> None:
        try:
            while not stop.is_set():
                response = _post(
                    origin,
                    "/api/interactive/poll",
                    {
                        "peer_id": grant["peer_id"],
                        "session_token": grant["session_token"],
                        "timeout_seconds": 0.1,
                    },
                )
                assert response["ok"] is True
                work = response["work"]
                if work is None:
                    continue
                permit = _post(
                    origin,
                    "/api/interactive/start",
                    {
                        "peer_id": grant["peer_id"],
                        "session_token": grant["session_token"],
                        "job_id": work["job_id"],
                        "request_id": work["request_id"],
                        "input_digest": work["input_digest"],
                    },
                )
                assert permit == {"ok": True, "started": True, "route_ready": False}
                output = stage.execute(
                    request_id=work["request_id"],
                    assignment_id=work["assignment_id"],
                    stage_id=work["stage_id"],
                    hidden=work["hidden"],
                )
                accepted = _post(
                    origin,
                    "/api/interactive/result",
                    {
                        "peer_id": grant["peer_id"],
                        "session_token": grant["session_token"],
                        "result": {
                            "protocol": "mycelium.browser_stage_result.v1",
                            "job_id": work["job_id"],
                            "request_id": work["request_id"],
                            "assignment_id": work["assignment_id"],
                            "stage_id": work["stage_id"],
                            "pack_digest": work["pack_digest"],
                            "input_digest": work["input_digest"],
                            "output": output,
                            "output_digest": matrix_digest(output),
                            "route_ready": False,
                        },
                    },
                )
                assert accepted["result"] in {"accepted", "duplicate"}
        except BaseException as exc:  # pragma: no cover - asserted by test
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_same_origin_server_invite_join_worker_inference_and_status(tmp_path: Path) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")
    server = create_server(
        runtime=runtime,
        operator_token=OPERATOR_TOKEN,
        host="127.0.0.1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stop = threading.Event()
    errors: list[BaseException] = []
    worker: threading.Thread | None = None
    try:
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        invite = _post(
            origin,
            "/api/interactive/invite",
            {"ttl_seconds": 300},
            operator=True,
        )["invite"]
        assert invite["url"].startswith(origin + "/#join/")
        token = urlsplit(invite["url"]).fragment.removeprefix("join/")
        grant = _post(origin, "/api/interactive/join", {"token": token})["grant"]
        assert grant["route_ready"] is False
        assert grant["session_token"] not in json.dumps(_get(origin, "/api/interactive/status"))
        worker = _http_worker(origin, grant, stop, errors)
        record = _post(
            origin,
            "/api/interactive/infer",
            {"prompt": "web swarm", "max_new_tokens": 1, "request_id": "server-test-request"},
            operator=True,
        )["record"]
        assert record["request_id"] == "server-test-request"
        assert record["route_ready"] is False
        assert record["local_evidence_only"] is True
        assert len(record["generated_tokens"]) == 1
        status = _get(origin, "/api/interactive/status")["status"]
        assert status["route_ready"] is False
        assert status["completed_request_count"] == 1
        serialized_status = json.dumps(status)
        assert "web swarm" not in serialized_status
        assert grant["session_token"] not in serialized_status
        assert "hidden" not in serialized_status
    finally:
        stop.set()
        if worker is not None:
            worker.join(timeout=2)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        runtime.close()
    assert worker is not None and not worker.is_alive()
    assert errors == []


def test_server_rejects_reused_invite_token(tmp_path: Path) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")
    server = create_server(
        runtime=runtime,
        operator_token=OPERATOR_TOKEN,
        host="127.0.0.1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        token = urlsplit(
            _post(origin, "/api/interactive/invite", {}, operator=True)["invite"]["url"]
        ).fragment.removeprefix("join/")
        assert _post(origin, "/api/interactive/join", {"token": token})["ok"] is True
        body = json.dumps({"token": token}).encode("utf-8")
        request = Request(
            origin + "/api/interactive/join",
            data=body,
            method="POST",
            headers={"content-type": "application/json", "content-length": str(len(body))},
        )
        try:
            urlopen(request, timeout=30)  # noqa: S310 - loopback test server
        except HTTPError as exc:
            assert exc.code == 400
            error = json.loads(exc.read().decode("utf-8"))
            assert error["error"] == "invite_invalid_or_consumed"
        else:  # pragma: no cover
            raise AssertionError("reused invite unexpectedly accepted")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        runtime.close()


def test_static_console_has_fail_closed_browser_security_headers(tmp_path: Path) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")
    static_root = Path(__file__).parents[2] / "mycelium_interactive" / "static"
    server = create_server(
        runtime=runtime,
        operator_token=OPERATOR_TOKEN,
        host="127.0.0.1",
        port=0,
        static_root=static_root,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        with _get_response(origin, "/") as response:
            assert response.status == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["content-security-policy"] == (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; img-src 'self'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'self'"
            )
            assert response.headers["referrer-policy"] == "no-referrer"
            assert response.headers["permissions-policy"] == (
                "camera=(), microphone=(), geolocation=()"
            )
            assert response.headers["x-frame-options"] == "DENY"
            assert response.headers["cross-origin-resource-policy"] == "same-origin"
        try:
            urlopen(origin + "/api/interactive/status", timeout=30)  # noqa: S310
        except HTTPError as exc:
            assert exc.code == 401
            error = json.loads(exc.read().decode("utf-8"))
            assert error["error"] == "operator_unauthorized"
            assert error["route_ready"] is False
        else:  # pragma: no cover
            raise AssertionError("operator endpoint allowed without capability")
        assert _get(origin, "/api/interactive/status")["status"]["route_ready"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        runtime.close()


def test_split_static_roots_serve_operator_spa_and_worker_without_cross_root_leaks(
    tmp_path: Path,
) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")
    operator_root = tmp_path / "operator"
    worker_root = tmp_path / "worker"
    (operator_root / "assets").mkdir(parents=True)
    operator_root.joinpath("index.html").write_text(
        "<html>operator-root-marker</html>", encoding="utf-8"
    )
    operator_root.joinpath("assets", "operator.js").write_text(
        "operator-asset-marker", encoding="utf-8"
    )
    operator_root.joinpath("shared.txt").write_text("operator-shared", encoding="utf-8")
    worker_root.mkdir()
    worker_root.joinpath("index.html").write_text(
        "<html>worker-root-marker</html>", encoding="utf-8"
    )
    worker_root.joinpath("interactive.js").write_text(
        "import './pixelStage.js'; // worker-script-marker", encoding="utf-8"
    )
    worker_root.joinpath("pixelStage.js").write_text(
        "// worker-stage-marker", encoding="utf-8"
    )
    worker_root.joinpath("shared.txt").write_text("worker-shared", encoding="utf-8")
    outside_secret = tmp_path / "outside-secret.txt"
    outside_secret.write_text("must-not-leak", encoding="utf-8")
    operator_root.joinpath("operator-link.txt").symlink_to(outside_secret)
    worker_root.joinpath("worker-link.txt").symlink_to(outside_secret)

    server = create_server(
        runtime=runtime,
        operator_token=OPERATOR_TOKEN,
        host="127.0.0.1",
        port=0,
        static_root=operator_root,
        worker_static_root=worker_root,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"http://127.0.0.1:{server.server_address[1]}"

        def get_text(path: str) -> str:
            with _get_response(origin, path) as response:
                assert response.status == 200
                return response.read().decode("utf-8")

        assert "operator-root-marker" in get_text("/")
        assert get_text("/assets/operator.js") == "operator-asset-marker"
        assert get_text("/shared.txt") == "operator-shared"
        assert "operator-root-marker" in get_text("/requests/current")
        assert "operator-root-marker" in get_text("/nested/operator/route?view=graph")

        assert "worker-root-marker" in get_text("/device")
        assert "worker-root-marker" in get_text("/device/")
        assert "worker-script-marker" in get_text("/device/interactive.js")
        assert get_text("/device/pixelStage.js") == "// worker-stage-marker"
        assert get_text("/device/shared.txt") == "worker-shared"

        invite = _post(origin, "/api/interactive/invite", {}, operator=True)["invite"]
        parsed_invite = urlsplit(invite["url"])
        assert parsed_invite.path == "/device"
        assert parsed_invite.fragment.startswith("join/")
        assert invite["route_ready"] is False

        rejected_paths = (
            "/missing.js",
            "/worker-stage-marker.js",
            "/device/missing.js",
            "/device/assets/operator.js",
            "/..%2Foutside-secret.txt",
            "/device/%2e%2e/outside-secret.txt",
            "/operator-link.txt",
            "/device/worker-link.txt",
        )
        for path in rejected_paths:
            with pytest.raises(HTTPError) as captured:
                _get_response(origin, path)
            assert captured.value.code == 404
            body = captured.value.read().decode("utf-8")
            assert "must-not-leak" not in body
            assert json.loads(body)["error"] == "not_found"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        runtime.close()


def test_worker_root_can_be_mounted_while_operator_root_remains_api_only(tmp_path: Path) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")
    worker_root = tmp_path / "worker"
    worker_root.mkdir()
    worker_root.joinpath("index.html").write_text("worker-only-marker", encoding="utf-8")
    server = create_server(
        runtime=runtime,
        operator_token=OPERATOR_TOKEN,
        host="127.0.0.1",
        port=0,
        static_root=None,
        worker_static_root=worker_root,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        with _get_response(origin, "/") as response:
            document = json.loads(response.read().decode("utf-8"))
        assert document["route_ready"] is False
        assert document["local_evidence_only"] is True
        with _get_response(origin, "/device") as response:
            assert response.read().decode("utf-8") == "worker-only-marker"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        runtime.close()


def test_accepted_http_cancel_denies_late_browser_compute_permit(tmp_path: Path) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")
    server = create_server(
        runtime=runtime,
        operator_token=OPERATOR_TOKEN,
        host="127.0.0.1",
        port=0,
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    poll_result: list[dict[str, Any]] = []
    infer_outcome: list[str] = []
    try:
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        invite = _post(origin, "/api/interactive/invite", {}, operator=True)["invite"]
        token = urlsplit(invite["url"]).fragment.removeprefix("join/")
        grant = _post(origin, "/api/interactive/join", {"token": token})["grant"]

        def poll() -> None:
            poll_result.append(
                _post(
                    origin,
                    "/api/interactive/poll",
                    {
                        "peer_id": grant["peer_id"],
                        "session_token": grant["session_token"],
                        "timeout_seconds": 5,
                    },
                )
            )

        poll_thread = threading.Thread(target=poll)
        poll_thread.start()
        for _ in range(100):
            if runtime.status()["ready_peer_count"] == 1:
                break
            threading.Event().wait(0.01)
        assert runtime.status()["ready_peer_count"] == 1

        def infer() -> None:
            try:
                runtime.infer(
                    prompt="cancel before browser compute",
                    max_new_tokens=1,
                    required_distinct_peers=1,
                    request_id="request-http-late-start",
                    timeout_seconds=5,
                )
            except InteractiveRuntimeError as exc:
                infer_outcome.append(exc.code)

        infer_thread = threading.Thread(target=infer)
        infer_thread.start()
        poll_thread.join(timeout=2)
        assert not poll_thread.is_alive()
        work = poll_result[0]["work"]
        assert work is not None

        cancelled = _post(
            origin,
            "/api/interactive/cancel",
            {"request_id": "request-http-late-start"},
            operator=True,
        )
        assert cancelled == {"ok": True, "cancelled": True, "route_ready": False}
        permit = _post(
            origin,
            "/api/interactive/start",
            {
                "peer_id": grant["peer_id"],
                "session_token": grant["session_token"],
                "job_id": work["job_id"],
                "request_id": work["request_id"],
                "input_digest": work["input_digest"],
            },
        )
        assert permit == {"ok": True, "started": False, "route_ready": False}

        infer_thread.join(timeout=2)
        assert not infer_thread.is_alive()
        assert infer_outcome == ["request_cancelled"]
        assert runtime.status()["completed_request_count"] == 0
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        runtime.close()


def test_packaged_worker_html_loads_script_from_device_mount() -> None:
    worker_html = (
        Path(__file__).parents[2] / "mycelium_interactive" / "static" / "index.html"
    ).read_text(encoding="utf-8")
    assert 'src="/interactive.js"' not in worker_html
    assert (
        'src="/device/interactive.js"' in worker_html
        or 'src="./interactive.js"' in worker_html
    )
    worker_script = (
        Path(__file__).parents[2] / "mycelium_interactive" / "static" / "interactive.js"
    ).read_text(encoding="utf-8")
    assert "from './operatorContract.js'" in worker_script
    operator_contract = (
        Path(__file__).parents[2]
        / "mycelium_interactive"
        / "static"
        / "operatorContract.js"
    ).read_text(encoding="utf-8")
    assert "decodeEvidenceRecord" in operator_contract
    assert "decodeOperatorStatus" in operator_contract
    permit_request = worker_script.index("await post('/api/interactive/start'")
    permit_rejection = worker_script.index("if (permit.started !== true)")
    stage_execute = worker_script.index("state.peer.stage.execute")
    assert permit_request < permit_rejection < stage_execute


def test_api_only_root_does_not_publish_operator_status(tmp_path: Path) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")
    server = create_server(
        runtime=runtime,
        operator_token=OPERATOR_TOKEN,
        host="127.0.0.1",
        port=0,
        static_root=None,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        with _get_response(origin, "/") as response:
            document = json.loads(response.read().decode("utf-8"))
        assert document["route_ready"] is False
        assert document["local_evidence_only"] is True
        assert "status" not in document
        assert "peers" not in json.dumps(document)
        assert "recent_requests" not in json.dumps(document)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        runtime.close()


def test_server_rejects_insecure_nonloopback_public_origin(tmp_path: Path) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")
    try:
        with pytest.raises(
            interactive_server.InteractiveRuntimeError,
            match="public_origin_invalid",
        ):
            create_server(
                runtime=runtime,
                operator_token=OPERATOR_TOKEN,
                host="127.0.0.1",
                port=0,
                public_origin="http://192.0.2.10:8787",
            )
    finally:
        runtime.close()


def test_server_requires_explicit_public_origin_before_nonloopback_bind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")

    def unexpected_bind(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("server bound before public-origin validation")

    monkeypatch.setattr(interactive_server, "InteractiveHTTPServer", unexpected_bind)
    try:
        with pytest.raises(
            interactive_server.InteractiveRuntimeError,
            match="public_origin_required",
        ):
            create_server(
                runtime=runtime,
                operator_token=OPERATOR_TOKEN,
                host="0.0.0.0",
                port=8787,
            )
    finally:
        runtime.close()


def test_loopback_server_rejects_untrusted_host_header_for_invites(tmp_path: Path) -> None:
    runtime = InteractiveRuntime(root=tmp_path / "runtime")
    server = create_server(
        runtime=runtime,
        operator_token=OPERATOR_TOKEN,
        host="127.0.0.1",
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        body = b"{}"
        request = Request(
            origin + "/api/interactive/invite",
            data=body,
            method="POST",
            headers={
                "authorization": f"Bearer {OPERATOR_TOKEN}",
                "content-type": "application/json",
                "content-length": str(len(body)),
                "host": "attacker.example",
            },
        )
        with pytest.raises(HTTPError) as captured:
            urlopen(request, timeout=30)  # noqa: S310 - loopback test server
        assert captured.value.code == 400
        error = json.loads(captured.value.read().decode("utf-8"))
        assert error["error"] == "host_header_invalid"
        assert error["route_ready"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        runtime.close()


def test_serve_rejects_partial_tls_configuration_before_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def unexpected_create_server(**_kwargs: Any) -> None:
        raise AssertionError("server bound before TLS configuration validation")

    monkeypatch.setattr(interactive_server, "create_server", unexpected_create_server)
    with pytest.raises(
        interactive_server.InteractiveRuntimeError,
        match="tls_cert_and_key_required",
    ):
        interactive_server.serve_forever(
            runtime=object(),  # type: ignore[arg-type]
            operator_token=OPERATOR_TOKEN,
            host="127.0.0.1",
            port=0,
            public_origin=None,
            static_root=None,
            tls_cert=tmp_path / "certificate.pem",
            tls_key=None,
        )


def test_main_generates_operator_capability_and_passes_it_to_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, Any] = {}

    class FakeRuntime:
        def __init__(self, *, root: Path | None = None) -> None:
            observed["root"] = root

        def close(self) -> None:
            observed["closed"] = True

    def fake_serve_forever(**kwargs: Any) -> None:
        observed.update(kwargs)

    monkeypatch.setattr(interactive_server, "InteractiveRuntime", FakeRuntime)
    monkeypatch.setattr(interactive_server, "serve_forever", fake_serve_forever)

    state_root = tmp_path / "state"
    operator_root = tmp_path / "operator"
    worker_root = tmp_path / "worker"
    assert interactive_server.main(
        [
            "--state-root",
            str(state_root),
            "--port",
            "0",
            "--static-root",
            str(operator_root),
            "--worker-static-root",
            str(worker_root),
        ]
    ) == 0
    token = observed["operator_token"]
    assert isinstance(token, str)
    assert len(token) >= 32
    assert interactive_server._operator_token_digest(token)
    assert observed["root"] == state_root
    assert observed["static_root"] == operator_root
    assert observed["worker_static_root"] == worker_root
    assert observed["closed"] is True


def test_main_reads_operator_capability_from_mode_0600_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, Any] = {}
    token_path = tmp_path / "operator-token"
    token_path.write_text(OPERATOR_TOKEN + "\n", encoding="ascii")
    token_path.chmod(0o600)

    class FakeRuntime:
        def __init__(self, *, root: Path | None = None) -> None:
            observed["root"] = root

        def close(self) -> None:
            observed["closed"] = True

    monkeypatch.setattr(interactive_server, "InteractiveRuntime", FakeRuntime)
    monkeypatch.setattr(
        interactive_server,
        "serve_forever",
        lambda **kwargs: observed.update(kwargs),
    )

    assert interactive_server.main(["--operator-token-file", str(token_path)]) == 0
    assert observed["operator_token"] == OPERATOR_TOKEN
    assert observed["closed"] is True


def test_main_rejects_group_readable_operator_capability_file(tmp_path: Path) -> None:
    token_path = tmp_path / "operator-token"
    token_path.write_text(OPERATOR_TOKEN + "\n", encoding="ascii")
    token_path.chmod(0o640)

    with pytest.raises(
        interactive_server.InteractiveRuntimeError,
        match="operator_token_file_permissions_invalid",
    ):
        interactive_server.main(["--operator-token-file", str(token_path)])
