"""Production clients for the shared request-gateway session contract."""
from __future__ import annotations

import ipaddress
import json
from typing import Any, Iterator, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .contracts import (
    AdmissionError,
    InferenceSubmission,
    QualificationBinding,
    StreamEvent,
    is_safe_error_code,
    is_valid_request_id,
)
from .service import RequestGatewayService


class GatewayClientError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code if is_safe_error_code(code) else "invalid_gateway_error"
        super().__init__(self.code)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GatewayClient(Protocol):
    def current_qualification(self) -> QualificationBinding: ...

    def submit(self, submission: InferenceSubmission) -> str: ...

    def events(
        self,
        request_id: str,
        *,
        last_event_id: int | None = None,
    ) -> Iterator[StreamEvent]: ...

    def cancel(self, request_id: str) -> bool: ...


class ServiceGatewayClient:
    """In-process production client; exercises the same session service as ASGI."""

    def __init__(self, service: RequestGatewayService) -> None:
        self._service = service

    def current_qualification(self) -> QualificationBinding:
        projection = self._service.current_qualification()
        binding = projection.get("binding")
        if not isinstance(binding, Mapping):
            raise GatewayClientError("invalid_qualification_projection")
        try:
            return QualificationBinding.from_dict(binding)
        except AdmissionError as exc:
            raise GatewayClientError(exc.code) from None

    def submit(self, submission: InferenceSubmission) -> str:
        try:
            return self._service.submit(submission)
        except AdmissionError as exc:
            raise GatewayClientError(exc.code) from None

    def events(
        self,
        request_id: str,
        *,
        last_event_id: int | None = None,
    ) -> Iterator[StreamEvent]:
        try:
            subscription = self._service.subscribe(
                request_id,
                last_event_id=last_event_id,
            )
        except AdmissionError as exc:
            raise GatewayClientError(exc.code) from None
        try:
            while True:
                event = subscription.next_event(timeout=None)
                if event is None:
                    return
                yield event
                subscription.ack(event.sequence)
        finally:
            subscription.close()

    def cancel(self, request_id: str) -> bool:
        try:
            return self._service.cancel(request_id)
        except AdmissionError as exc:
            raise GatewayClientError(exc.code) from None


class HTTPGatewayClient:
    """Stdlib HTTP/SSE client used by the command-line entry point."""

    def __init__(self, *, base_url: str, bearer_token: str, timeout: float = 30.0) -> None:
        parsed = urlparse(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid_gateway_url")
        if (
            not isinstance(bearer_token, str)
            or not bearer_token
            or len(bearer_token.encode("utf-8")) > 4_096
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in bearer_token)
        ):
            raise ValueError("invalid_request_gateway_bearer_token")
        if parsed.scheme == "http" and not self._is_loopback_host(parsed.hostname):
            raise ValueError("insecure_gateway_url")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("invalid_timeout")
        self._base_url = base_url.rstrip("/")
        self._token = bearer_token
        self._timeout = float(timeout)
        self._opener = build_opener(_NoRedirectHandler())

    def current_qualification(self) -> QualificationBinding:
        document = self._json_request("GET", "/v1/qualification/current")
        binding = document.get("binding")
        if not isinstance(binding, Mapping):
            raise GatewayClientError("invalid_qualification_projection")
        try:
            return QualificationBinding.from_dict(binding)
        except AdmissionError as exc:
            raise GatewayClientError(exc.code) from None

    def submit(self, submission: InferenceSubmission) -> str:
        document = self._json_request(
            "POST",
            "/v1/inference",
            body=submission.to_dict(),
        )
        request_id = document.get("request_id")
        if not is_valid_request_id(request_id):
            raise GatewayClientError("invalid_submission_response")
        return request_id

    def events(
        self,
        request_id: str,
        *,
        last_event_id: int | None = None,
    ) -> Iterator[StreamEvent]:
        if not is_valid_request_id(request_id):
            raise GatewayClientError("invalid_request_id")
        headers = self._headers()
        headers["Accept"] = "text/event-stream"
        if last_event_id is not None:
            if (
                not isinstance(last_event_id, int)
                or isinstance(last_event_id, bool)
                or last_event_id < 0
            ):
                raise GatewayClientError("invalid_last_event_id")
            headers["Last-Event-ID"] = str(last_event_id)
        request = Request(
            self._url(f"/v1/inference/{request_id}/events"),
            headers=headers,
            method="GET",
        )
        try:
            response = self._open(request)
        except HTTPError as exc:
            raise GatewayClientError(self._http_error_code(exc)) from None
        except URLError:
            raise GatewayClientError("stream_unavailable") from None
        try:
            with response:
                data_lines: list[str] = []
                event_id: str | None = None
                event_type: str | None = None
                event_bytes = 0
                for raw_line in response:
                    if not isinstance(raw_line, bytes) or len(raw_line) > 1_048_576:
                        raise GatewayClientError("invalid_event_stream")
                    event_bytes += len(raw_line)
                    if event_bytes > 1_048_576:
                        raise GatewayClientError("invalid_event_stream")
                    try:
                        line = raw_line.decode("utf-8").rstrip("\r\n")
                    except UnicodeDecodeError:
                        raise GatewayClientError("invalid_event_stream") from None
                    if line == "":
                        if data_lines:
                            yield self._parse_sse_event(data_lines, event_id, event_type)
                        elif event_id is not None or event_type is not None:
                            raise GatewayClientError("invalid_event_stream")
                        data_lines.clear()
                        event_id = None
                        event_type = None
                        event_bytes = 0
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("id:"):
                        if event_id is not None:
                            raise GatewayClientError("invalid_event_stream")
                        event_id = line[3:].lstrip(" ")
                    elif line.startswith("event:"):
                        if event_type is not None:
                            raise GatewayClientError("invalid_event_stream")
                        event_type = line[6:].lstrip(" ")
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip(" "))
                    else:
                        raise GatewayClientError("invalid_event_stream")
                if data_lines:
                    yield self._parse_sse_event(data_lines, event_id, event_type)
                elif event_id is not None or event_type is not None:
                    raise GatewayClientError("invalid_event_stream")
        except GatewayClientError:
            raise
        except (OSError, TimeoutError):
            raise GatewayClientError("stream_disconnected") from None

    def cancel(self, request_id: str) -> bool:
        if not is_valid_request_id(request_id):
            raise GatewayClientError("invalid_request_id")
        document = self._json_request("DELETE", f"/v1/inference/{request_id}")
        return document.get("status") == "cancelling"

    def _json_request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = None
        headers = self._headers()
        if body is not None:
            payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            self._url(path),
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            with self._open(request) as response:
                raw = response.read(1_048_577)
        except HTTPError as exc:
            raise GatewayClientError(self._http_error_code(exc)) from None
        except URLError:
            raise GatewayClientError("gateway_unavailable") from None
        if len(raw) > 1_048_576:
            raise GatewayClientError("response_too_large")
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            raise GatewayClientError("invalid_gateway_response") from None
        if not isinstance(document, dict):
            raise GatewayClientError("invalid_gateway_response")
        return document

    @classmethod
    def _parse_sse_event(
        cls,
        data_lines: list[str],
        event_id: str | None,
        event_type: str | None,
    ) -> StreamEvent:
        if event_id is None or event_type is None or not event_id.isdigit():
            raise GatewayClientError("invalid_event_stream")
        event = cls._parse_event_data("\n".join(data_lines))
        if event_id != str(event.sequence) or event_type != event.kind:
            raise GatewayClientError("invalid_event_stream")
        return event

    @staticmethod
    def _parse_event_data(data: str) -> StreamEvent:
        try:
            document = json.loads(data)
            if not isinstance(document, dict):
                raise ValueError
            return StreamEvent.from_dict(document)
        except (json.JSONDecodeError, ValueError, AdmissionError, TypeError):
            raise GatewayClientError("invalid_event_stream") from None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Cache-Control": "no-store",
        }

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _open(self, request: Request):
        return self._opener.open(request, timeout=self._timeout)

    @staticmethod
    def _is_loopback_host(hostname: str | None) -> bool:
        if hostname == "localhost":
            return True
        if hostname is None:
            return False
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False

    @staticmethod
    def _http_error_code(error: HTTPError) -> str:
        try:
            raw = error.read(65_537)
            if len(raw) > 65_536:
                return "gateway_error"
            document = json.loads(raw)
            code = document.get("error") if isinstance(document, dict) else None
            return (
                code
                if isinstance(code, str) and is_safe_error_code(code)
                else "gateway_error"
            )
        except Exception:
            return "gateway_error"
