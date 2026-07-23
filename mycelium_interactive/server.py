from __future__ import annotations

import argparse
import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import mimetypes
from pathlib import Path
import secrets
import ssl
from typing import Any
from urllib.parse import unquote, urlsplit

from .runtime import InteractiveRuntime, InteractiveRuntimeError
from .swarm import SwarmError, normalize_public_origin

MAX_JSON_BYTES = 2 * 1024 * 1024
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)


class InteractiveHTTPError(RuntimeError):
    def __init__(self, status: HTTPStatus, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(code)


def _operator_token_digest(token: str) -> bytes:
    if not isinstance(token, str) or not 32 <= len(token) <= 512:
        raise InteractiveRuntimeError("operator_token_invalid")
    try:
        encoded = token.encode("ascii")
    except UnicodeEncodeError as exc:
        raise InteractiveRuntimeError("operator_token_invalid") from exc
    if any(
        not (
            ord("0") <= byte <= ord("9")
            or ord("A") <= byte <= ord("Z")
            or ord("a") <= byte <= ord("z")
            or byte in {ord("-"), ord("_")}
        )
        for byte in encoded
    ):
        raise InteractiveRuntimeError("operator_token_invalid")
    return hashlib.sha256(encoded).digest()


def _read_operator_token_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise InteractiveRuntimeError("operator_token_file_invalid")
    if path.stat().st_mode & 0o077:
        raise InteractiveRuntimeError("operator_token_file_permissions_invalid")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InteractiveRuntimeError("operator_token_file_unreadable") from exc
    if len(raw) > 1024:
        raise InteractiveRuntimeError("operator_token_file_invalid")
    try:
        token = raw.decode("ascii").rstrip("\r\n")
    except UnicodeDecodeError as exc:
        raise InteractiveRuntimeError("operator_token_file_invalid") from exc
    _operator_token_digest(token)
    return token


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _strict_json(raw: bytes) -> Any:
    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise InteractiveHTTPError(HTTPStatus.BAD_REQUEST, "duplicate_json_key")
            result[key] = value
        return result

    def reject_nonfinite(_value: str) -> Any:
        raise InteractiveHTTPError(HTTPStatus.BAD_REQUEST, "nonfinite_json_number")

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate,
            parse_constant=reject_nonfinite,
        )
    except InteractiveHTTPError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise InteractiveHTTPError(HTTPStatus.BAD_REQUEST, "invalid_json") from exc


def _string_field(document: dict[str, Any], field: str, code: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise InteractiveHTTPError(HTTPStatus.BAD_REQUEST, code)
    return value


def _float_field(
    document: dict[str, Any], field: str, code: str, *, default: float, maximum: float
) -> float:
    value = document.get(field, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InteractiveHTTPError(HTTPStatus.BAD_REQUEST, code)
    number = float(value)
    if not 0 <= number <= maximum:
        raise InteractiveHTTPError(HTTPStatus.BAD_REQUEST, code)
    return number


def _int_field(
    document: dict[str, Any], field: str, code: str, *, default: int, minimum: int, maximum: int
) -> int:
    value = document.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InteractiveHTTPError(HTTPStatus.BAD_REQUEST, code)
    return value


def _is_loopback_host(host: str) -> bool:
    if not isinstance(host, str) or not host:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _public_origin_from_handler(handler: BaseHTTPRequestHandler, configured: str | None) -> str:
    if configured:
        return configured.rstrip("/")
    host = handler.headers.get("host", "127.0.0.1")
    if not host or any(ord(character) < 0x20 for character in host):
        raise InteractiveHTTPError(HTTPStatus.BAD_REQUEST, "host_header_invalid")
    scheme = "https" if isinstance(handler.connection, ssl.SSLSocket) else "http"
    try:
        return normalize_public_origin(f"{scheme}://{host}")
    except SwarmError as exc:
        raise InteractiveHTTPError(HTTPStatus.BAD_REQUEST, "host_header_invalid") from exc


class InteractiveHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    runtime: InteractiveRuntime
    public_origin: str | None
    static_root: Path | None
    worker_static_root: Path | None
    operator_token_digest: bytes


def make_handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # Narrowed with runtime attributes in create_server().
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send_browser_security_headers(self) -> None:
            self.send_header("cache-control", "no-store")
            self.send_header("x-content-type-options", "nosniff")
            self.send_header("content-security-policy", CONTENT_SECURITY_POLICY)
            self.send_header("referrer-policy", "no-referrer")
            self.send_header("permissions-policy", "camera=(), microphone=(), geolocation=()")
            self.send_header("x-frame-options", "DENY")
            self.send_header("cross-origin-resource-policy", "same-origin")

        def _send_json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = _json_bytes(value)
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self._send_browser_security_headers()
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, status: HTTPStatus, code: str) -> None:
            self._send_json(
                {"ok": False, "error": code, "route_ready": False},
                status=status,
            )

        def _require_operator(self) -> None:
            authorization = self.headers.get("authorization", "")
            scheme, separator, token = authorization.partition(" ")
            if scheme != "Bearer" or separator != " " or not token or " " in token:
                raise InteractiveHTTPError(HTTPStatus.UNAUTHORIZED, "operator_unauthorized")
            try:
                supplied = _operator_token_digest(token)
            except InteractiveRuntimeError as exc:
                raise InteractiveHTTPError(
                    HTTPStatus.UNAUTHORIZED, "operator_unauthorized"
                ) from exc
            if not hmac.compare_digest(
                supplied, getattr(self.server, "operator_token_digest", b"")
            ):
                raise InteractiveHTTPError(HTTPStatus.UNAUTHORIZED, "operator_unauthorized")

        def _read_json_document(self) -> dict[str, Any]:
            raw_length = self.headers.get("content-length")
            if raw_length is None:
                raise InteractiveHTTPError(HTTPStatus.LENGTH_REQUIRED, "content_length_required")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise InteractiveHTTPError(HTTPStatus.BAD_REQUEST, "content_length_invalid") from exc
            if length < 0 or length > MAX_JSON_BYTES:
                raise InteractiveHTTPError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "json_body_too_large")
            media_type = self.headers.get("content-type", "")
            if not media_type.lower().split(";", 1)[0].strip() == "application/json":
                raise InteractiveHTTPError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "content_type_invalid")
            value = _strict_json(self.rfile.read(length))
            if not isinstance(value, dict):
                raise InteractiveHTTPError(HTTPStatus.BAD_REQUEST, "json_document_invalid")
            return value

        def do_GET(self) -> None:  # noqa: N802
            try:
                if self.path == "/api/interactive/status":
                    self._require_operator()
                    self._send_json({"ok": True, "status": self.server.runtime.status()})
                    return
                if self.path.startswith("/api/interactive/requests/"):
                    self._require_operator()
                    request_id = self.path.rsplit("/", 1)[-1]
                    record = self.server.runtime.get_record(request_id)
                    if record is None:
                        raise InteractiveHTTPError(HTTPStatus.NOT_FOUND, "request_not_found")
                    self._send_json({"ok": True, "record": record})
                    return
                if self.path in {"/", "/index.html"} or not self.path.startswith("/api/"):
                    self._serve_static()
                    return
                raise InteractiveHTTPError(HTTPStatus.NOT_FOUND, "not_found")
            except InteractiveHTTPError as exc:
                self._send_error_json(exc.status, exc.code)
            except Exception:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "interactive_server_failed")

        def do_POST(self) -> None:  # noqa: N802
            try:
                if self.path in {
                    "/api/interactive/invite",
                    "/api/interactive/infer",
                    "/api/interactive/cancel",
                }:
                    self._require_operator()
                document = self._read_json_document()
                if self.path == "/api/interactive/invite":
                    ttl = _float_field(document, "ttl_seconds", "invite_ttl_invalid", default=300.0, maximum=300.0)
                    origin = _public_origin_from_handler(self, self.server.public_origin)
                    invite = self.server.runtime.swarm.create_invite(
                        public_origin=origin,
                        ttl_seconds=ttl,
                    )
                    invite_url = invite.url
                    if getattr(self.server, "worker_static_root", None) is not None:
                        invite_url = f"{origin}/device#join/{invite.token}"
                    self._send_json(
                        {
                            "ok": True,
                            "invite": {
                                "url": invite_url,
                                "expires_at": invite.expires_at,
                                "route_ready": False,
                            },
                        }
                    )
                    return
                if self.path == "/api/interactive/join":
                    token = _string_field(document, "token", "invite_invalid_or_consumed")
                    grant = self.server.runtime.swarm.exchange_invite(token)
                    self._send_json(
                        {
                            "ok": True,
                            "grant": {
                                "peer_id": grant.peer_id,
                                "session_token": grant.session_token,
                                "expires_at": grant.expires_at,
                                "stage_pack": grant.stage_pack,
                                "membership_acceptance": grant.membership_acceptance,
                                "route_ready": False,
                            },
                        }
                    )
                    return
                if self.path == "/api/interactive/poll":
                    peer_id = _string_field(document, "peer_id", "peer_unauthorized")
                    session_token = _string_field(document, "session_token", "peer_unauthorized")
                    timeout = _float_field(document, "timeout_seconds", "poll_timeout_invalid", default=15.0, maximum=25.0)
                    work = self.server.runtime.swarm.poll_work(
                        peer_id=peer_id,
                        session_token=session_token,
                        timeout_seconds=timeout,
                    )
                    self._send_json({"ok": True, "work": work, "route_ready": False})
                    return
                if self.path == "/api/interactive/start":
                    peer_id = _string_field(document, "peer_id", "peer_unauthorized")
                    session_token = _string_field(document, "session_token", "peer_unauthorized")
                    started = self.server.runtime.swarm.start_work(
                        peer_id=peer_id,
                        session_token=session_token,
                        job_id=_string_field(document, "job_id", "work_start_job_invalid"),
                        request_id=_string_field(
                            document,
                            "request_id",
                            "work_start_binding_invalid",
                        ),
                        input_digest=_string_field(
                            document,
                            "input_digest",
                            "work_start_binding_invalid",
                        ),
                    )
                    self._send_json({"ok": True, "started": started, "route_ready": False})
                    return
                if self.path == "/api/interactive/result":
                    peer_id = _string_field(document, "peer_id", "peer_unauthorized")
                    session_token = _string_field(document, "session_token", "peer_unauthorized")
                    result = document.get("result")
                    outcome = self.server.runtime.swarm.submit_result(
                        peer_id=peer_id,
                        session_token=session_token,
                        document=result,
                    )
                    self._send_json({"ok": True, "result": outcome, "route_ready": False})
                    return
                if self.path == "/api/interactive/leave":
                    peer_id = _string_field(document, "peer_id", "peer_unauthorized")
                    session_token = _string_field(document, "session_token", "peer_unauthorized")
                    left = self.server.runtime.swarm.leave(peer_id=peer_id, session_token=session_token)
                    self._send_json({"ok": True, "left": left, "route_ready": False})
                    return
                if self.path == "/api/interactive/infer":
                    prompt = _string_field(document, "prompt", "prompt_invalid") if "prompt" in document else ""
                    max_new_tokens = _int_field(
                        document,
                        "max_new_tokens",
                        "max_new_tokens_invalid",
                        default=1,
                        minimum=1,
                        maximum=8,
                    )
                    required_distinct_peers = _int_field(
                        document,
                        "required_distinct_peers",
                        "required_distinct_peers_invalid",
                        default=1,
                        minimum=1,
                        maximum=6,
                    )
                    request_id = document.get("request_id")
                    if request_id is not None and (not isinstance(request_id, str) or not request_id):
                        raise InteractiveHTTPError(HTTPStatus.BAD_REQUEST, "request_id_invalid")
                    record = self.server.runtime.infer(
                        prompt=prompt,
                        max_new_tokens=max_new_tokens,
                        required_distinct_peers=required_distinct_peers,
                        request_id=request_id,
                    )
                    self._send_json({"ok": True, "record": record})
                    return
                if self.path == "/api/interactive/cancel":
                    request_id = _string_field(document, "request_id", "request_id_invalid")
                    cancelled = self.server.runtime.cancel_request(request_id)
                    self._send_json({"ok": True, "cancelled": cancelled, "route_ready": False})
                    return
                raise InteractiveHTTPError(HTTPStatus.NOT_FOUND, "not_found")
            except InteractiveHTTPError as exc:
                self._send_error_json(exc.status, exc.code)
            except (InteractiveRuntimeError, SwarmError) as exc:
                code = getattr(exc, "code", str(exc))
                status = HTTPStatus.REQUEST_TIMEOUT if code.endswith("timeout") else HTTPStatus.BAD_REQUEST
                self._send_error_json(status, code)
            except Exception:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "interactive_server_failed")

        def _request_path(self) -> str:
            try:
                encoded = urlsplit(self.path).path
                index = 0
                while index < len(encoded):
                    if encoded[index] == "%":
                        if (
                            index + 2 >= len(encoded)
                            or encoded[index + 1] not in "0123456789abcdefABCDEF"
                            or encoded[index + 2] not in "0123456789abcdefABCDEF"
                        ):
                            raise ValueError("malformed percent escape")
                        index += 3
                        continue
                    index += 1
                requested = unquote(encoded, encoding="utf-8", errors="strict")
            except (UnicodeError, ValueError) as exc:
                raise InteractiveHTTPError(HTTPStatus.NOT_FOUND, "not_found") from exc
            if (
                not requested.startswith("/")
                or "\\" in requested
                or any(ord(character) < 0x20 or ord(character) == 0x7F for character in requested)
                or any(part in {".", ".."} for part in requested.split("/"))
            ):
                raise InteractiveHTTPError(HTTPStatus.NOT_FOUND, "not_found")
            return requested

        def _static_candidate(self, root: Path, relative: str) -> Path:
            root_resolved = root.resolve()
            candidate = (root_resolved / relative).resolve()
            if root_resolved not in candidate.parents and candidate != root_resolved:
                raise InteractiveHTTPError(HTTPStatus.NOT_FOUND, "not_found")
            return candidate

        def _send_static_file(self, candidate: Path) -> None:
            try:
                body = candidate.read_bytes()
            except OSError as exc:
                raise InteractiveHTTPError(HTTPStatus.NOT_FOUND, "not_found") from exc
            content_type, _encoding = mimetypes.guess_type(candidate.name)
            if candidate.suffix == ".js":
                content_type = "text/javascript"
            if content_type is None:
                content_type = "application/octet-stream"
            if content_type.startswith("text/") or content_type in {
                "application/javascript",
                "application/json",
                "image/svg+xml",
            }:
                content_type += "; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", content_type)
            self._send_browser_security_headers()
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_static_root(
            self,
            root: Path,
            relative: str,
            *,
            spa_fallback: bool,
        ) -> None:
            relative = relative or "index.html"
            candidate = self._static_candidate(root, relative)
            if not candidate.is_file():
                leaf = Path(relative).name
                if not spa_fallback or "." in leaf:
                    raise InteractiveHTTPError(HTTPStatus.NOT_FOUND, "not_found")
                candidate = self._static_candidate(root, "index.html")
            if not candidate.is_file():
                raise InteractiveHTTPError(HTTPStatus.NOT_FOUND, "not_found")
            self._send_static_file(candidate)

        def _serve_static(self) -> None:
            requested = self._request_path()
            worker_root = getattr(self.server, "worker_static_root", None)
            if worker_root is not None and (
                requested == "/device" or requested.startswith("/device/")
            ):
                relative = requested.removeprefix("/device").lstrip("/")
                self._serve_static_root(worker_root, relative, spa_fallback=False)
                return

            root = getattr(self.server, "static_root", None)
            if root is None:
                self._send_json(
                    {
                        "ok": True,
                        "message": "Interactive API online. Build ui/web and pass --static-root ui/web/dist to serve the browser console.",
                        "route_ready": False,
                        "local_evidence_only": True,
                    }
                )
                return

            # Keep the packaged single-root worker functional after its script
            # URL moved to /device, while split mode always uses the worker root.
            if worker_root is None and requested.startswith("/device/"):
                relative = requested.removeprefix("/device/")
            else:
                relative = requested.lstrip("/")
            self._serve_static_root(root, relative, spa_fallback=True)

    return Handler


def create_server(
    *,
    runtime: InteractiveRuntime,
    operator_token: str,
    host: str = "127.0.0.1",
    port: int = 8787,
    public_origin: str | None = None,
    static_root: Path | None = None,
    worker_static_root: Path | None = None,
) -> InteractiveHTTPServer:
    try:
        configured_public_origin = (
            None if public_origin is None else normalize_public_origin(public_origin)
        )
    except SwarmError as exc:
        raise InteractiveRuntimeError(exc.code) from exc
    if configured_public_origin is None and not _is_loopback_host(host):
        raise InteractiveRuntimeError("public_origin_required")
    server = InteractiveHTTPServer((host, port), make_handler())
    server.runtime = runtime
    server.operator_token_digest = _operator_token_digest(operator_token)
    server.public_origin = configured_public_origin
    server.static_root = None if static_root is None else Path(static_root)
    server.worker_static_root = (
        None if worker_static_root is None else Path(worker_static_root)
    )
    return server


def serve_forever(
    *,
    runtime: InteractiveRuntime,
    operator_token: str,
    host: str,
    port: int,
    public_origin: str | None,
    static_root: Path | None,
    worker_static_root: Path | None = None,
    tls_cert: Path | None = None,
    tls_key: Path | None = None,
) -> None:
    if (tls_cert is None) != (tls_key is None):
        raise InteractiveRuntimeError("tls_cert_and_key_required")
    server = create_server(
        runtime=runtime,
        operator_token=operator_token,
        host=host,
        port=port,
        public_origin=public_origin,
        static_root=static_root,
        worker_static_root=worker_static_root,
    )
    try:
        if tls_cert is not None:
            assert tls_key is not None
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(tls_cert), str(tls_key))
            server.socket = context.wrap_socket(server.socket, server_side=True)
        actual_host = str(server.server_address[0])
        actual_port = int(server.server_address[1])
        display_origin = server.public_origin
        if display_origin is None:
            display_host = actual_host
            if display_host in {"0.0.0.0", "::"}:
                display_host = "127.0.0.1"
            if ":" in display_host and not display_host.startswith("["):
                display_host = f"[{display_host}]"
            scheme = "https" if tls_cert is not None else "http"
            display_origin = f"{scheme}://{display_host}:{actual_port}"
        print(
            json.dumps(
                {
                    "protocol": "mycelium.interactive_server_started.v1",
                    "host": actual_host,
                    "port": actual_port,
                    "public_origin": server.public_origin,
                    "operator_url": (
                        f"{display_origin.rstrip('/')}/#lab/operator/{operator_token}"
                        if worker_static_root is not None
                        else f"{display_origin.rstrip('/')}/#operator/{operator_token}"
                    ),
                    "static_root": str(static_root) if static_root else None,
                    "worker_static_root": (
                        str(worker_static_root) if worker_static_root else None
                    ),
                    "route_ready": False,
                    "local_evidence_only": True,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve Mycelium interactive browser swarm console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--public-origin")
    parser.add_argument("--state-root", type=Path)
    parser.add_argument(
        "--operator-token-file",
        type=Path,
        help="Optional mode-0600 file containing a URL-safe operator capability.",
    )
    parser.add_argument(
        "--static-root",
        type=Path,
        help="Static console root (defaults to mycelium_interactive/static).",
    )
    parser.add_argument(
        "--worker-static-root",
        type=Path,
        help="Optional browser-worker static root mounted at /device.",
    )
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    args = parser.parse_args(argv)
    operator_token = (
        _read_operator_token_file(args.operator_token_file)
        if args.operator_token_file is not None
        else secrets.token_urlsafe(32)
    )
    runtime = InteractiveRuntime(root=args.state_root)
    static_root = args.static_root or Path(__file__).with_name("static")
    try:
        serve_forever(
            runtime=runtime,
            operator_token=operator_token,
            host=args.host,
            port=args.port,
            public_origin=args.public_origin,
            static_root=static_root,
            worker_static_root=args.worker_static_root,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
        )
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
