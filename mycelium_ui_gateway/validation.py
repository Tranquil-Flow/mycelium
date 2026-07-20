"""Closed validators for browser-visible product gateway documents."""
from __future__ import annotations

import ipaddress
import json
import math
import re
from typing import Any, Mapping, Sequence


MAX_SAFE_INTEGER = 9_007_199_254_740_991
REQUEST_PROTOCOL = "mycelium.request_gateway.v1"
REQUEST_EVENT_PROTOCOL = "mycelium.request_event.v1"
OBSERVATORY_PROTOCOL = "mycelium.observatory_stream.v1"
OBSERVATORY_PROJECTION_PROTOCOL = "mycelium.observatory.request_projection.v1"
OBSERVATORY_STATUS_PROTOCOL = "mycelium.observatory.event_adapter_status.v1"
ROUTE_QUALIFICATION_PROTOCOL = "mycelium.route_qualification.v1"
SWARM_PROTOCOL = "mycelium.product_ui.swarm.v1"
MAX_OBSERVATORY_ITEMS = 256

_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_PUBLIC_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}\Z")
_HOST_PORT = re.compile(r"(?:[A-Za-z0-9.-]+):[0-9]{1,5}\Z")
_CREDENTIAL = re.compile(
    r"(?:\bbearer\s+|\bsk-[a-z0-9_-]{12,}|\bgh[pousr]_[a-z0-9]{20,}|"
    r"\bgithub_pat_[a-z0-9_]{20,}|-----BEGIN[ A-Z0-9_-]{0,48}PRIVATE KEY-----)",
    re.IGNORECASE,
)
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DENIED_KEYS = {
    "activation",
    "activations",
    "api_key",
    "auth_token",
    "bearer_token",
    "completion",
    "completions",
    "credential",
    "credentials",
    "decoded_output",
    "hidden_state",
    "hidden_states",
    "input_ids",
    "kv_cache",
    "logits",
    "message",
    "messages",
    "model_weights",
    "output_ids",
    "password",
    "passphrase",
    "private_key",
    "prompt",
    "prompts",
    "refresh_token",
    "secret",
    "state_dict",
    "tensor",
    "tensors",
    "token",
    "token_ids",
    "tokens",
}


class GatewayValidationError(ValueError):
    """A stable internal validation class that never carries private input."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("gateway_validation_error")


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def strict_json_document(body: bytes, *, code: str = "invalid_json_body") -> Mapping[str, Any]:
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_closed_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise GatewayValidationError(code) from None
    if not isinstance(value, Mapping):
        raise GatewayValidationError(code)
    return value


def canonical_json(document: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise GatewayValidationError("invalid_document") from None


def safe_code(value: object) -> bool:
    return isinstance(value, str) and _SAFE_CODE.fullmatch(value) is not None


def request_id(value: object) -> bool:
    return isinstance(value, str) and _REQUEST_ID.fullmatch(value) is not None


def identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def safe_integer(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= MAX_SAFE_INTEGER
    )


def _safe_integer_min(value: object, minimum: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= MAX_SAFE_INTEGER
    )


def _public_identifier(value: object) -> bool:
    if not isinstance(value, str) or _PUBLIC_IDENTIFIER.fullmatch(value) is None:
        return False
    lowered = value.lower()
    if (
        "/" in value
        or "\\" in value
        or "://" in value
        or _HOST_PORT.fullmatch(value) is not None
        or _CREDENTIAL.search(value) is not None
        or lowered == "localhost"
        or lowered.endswith((".localhost", ".local", ".internal", ".lan"))
    ):
        return False
    try:
        ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return True
    return False


def _exact(document: object, fields: set[str]) -> bool:
    return isinstance(document, Mapping) and set(document) == fields


def _authority_string(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 4_096


def _sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def valid_binding(document: object) -> bool:
    fields = {
        "qualification_id",
        "qualification_digest",
        "deployment_id",
        "deployment_epoch",
        "topology_version",
        "model_id",
        "resolved_commit",
        "manifest_digest",
        "path_manifest_digest",
        "stage_load_proof_digests",
    }
    if not _exact(document, fields):
        return False
    assert isinstance(document, Mapping)
    if not all(
        _public_identifier(document[field])
        for field in ("qualification_id", "deployment_id", "model_id", "resolved_commit")
    ):
        return False
    if not all(
        _sha256(document[field])
        for field in ("qualification_digest", "manifest_digest", "path_manifest_digest")
    ):
        return False
    if not safe_integer(document["deployment_epoch"]) or not safe_integer(
        document["topology_version"]
    ):
        return False
    digests = document["stage_load_proof_digests"]
    return (
        isinstance(digests, list)
        and len(digests) <= MAX_OBSERVATORY_ITEMS
        and all(_sha256(value) for value in digests)
        and all(digests[index] > digests[index - 1] for index in range(1, len(digests)))
    )


def validate_submission(document: Mapping[str, Any]) -> None:
    if not _exact(document, {"protocol", "prompt", "max_new_tokens", "qualification"}):
        raise GatewayValidationError("invalid_inference_request")
    prompt = document["prompt"]
    max_new_tokens = document["max_new_tokens"]
    if (
        document["protocol"] != REQUEST_PROTOCOL
        or not isinstance(prompt, str)
        or not prompt
        or len(prompt.encode("utf-8")) > 131_072
        or not isinstance(max_new_tokens, int)
        or isinstance(max_new_tokens, bool)
        or not 1 <= max_new_tokens <= 4_096
        or not valid_binding(document["qualification"])
    ):
        raise GatewayValidationError("invalid_inference_request")


def validate_qualification(document: Mapping[str, Any]) -> None:
    fields = {
        "protocol",
        "issued_at_unix_ms",
        "evidence_class",
        "route_ready",
        "reason_codes",
        "binding",
    }
    if not _exact(document, fields):
        raise GatewayValidationError("invalid_upstream_qualification")
    reasons = document["reason_codes"]
    ready = document["route_ready"]
    if (
        document["protocol"] != REQUEST_PROTOCOL
        or not safe_integer(document["issued_at_unix_ms"])
        or document["evidence_class"] not in {"physical_qualification", "synthetic_test_fixture"}
        or type(ready) is not bool
        or not isinstance(reasons, list)
        or len(reasons) > 128
        or len(set(reasons)) != len(reasons)
        or not all(safe_code(reason) for reason in reasons)
        or not valid_binding(document["binding"])
    ):
        raise GatewayValidationError("invalid_upstream_qualification")
    assert isinstance(document["binding"], Mapping)
    proof_digests = document["binding"]["stage_load_proof_digests"]
    if ready:
        if (
            document["evidence_class"] != "physical_qualification"
            or reasons
            or not proof_digests
        ):
            raise GatewayValidationError("invalid_upstream_qualification")
    elif not reasons:
        raise GatewayValidationError("invalid_upstream_qualification")


def validate_inference_accept(document: Mapping[str, Any]) -> str:
    if not _exact(document, {"request_id", "stream_path", "cancel_path"}):
        raise GatewayValidationError("invalid_upstream_response")
    value = document["request_id"]
    if (
        not request_id(value)
        or document["stream_path"] != f"/v1/inference/{value}/events"
        or document["cancel_path"] != f"/v1/inference/{value}"
    ):
        raise GatewayValidationError("invalid_upstream_response")
    assert isinstance(value, str)
    return value


def validate_cancel_response(document: Mapping[str, Any], expected_id: str) -> bool:
    if not _exact(document, {"request_id", "status"}) or document["request_id"] != expected_id:
        raise GatewayValidationError("invalid_upstream_response")
    if document["status"] == "cancelling":
        return True
    if document["status"] == "terminal":
        return False
    raise GatewayValidationError("invalid_upstream_response")


def _safe_json_tree(value: object, *, depth: int = 0) -> None:
    if depth > 32:
        raise GatewayValidationError("invalid_observatory_response")
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise GatewayValidationError("invalid_observatory_response")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GatewayValidationError("invalid_observatory_response")
        return
    if isinstance(value, list):
        for item in value:
            _safe_json_tree(item, depth=depth + 1)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key.isascii() or key.lower() in _DENIED_KEYS:
                raise GatewayValidationError("invalid_observatory_response")
            _safe_json_tree(item, depth=depth + 1)
        return
    raise GatewayValidationError("invalid_observatory_response")


def _validate_observatory_semantics(
    snapshot: Mapping[str, Any],
    incidents: list[Any],
    provisioning: Mapping[str, Any],
) -> None:
    session_fields = {
        "request_id",
        "state",
        "last_sequence",
        "event_count",
        "token_count",
        "terminal",
        "qualification_id",
        "started_at_unix_ms",
        "updated_at_unix_ms",
        "quarantine_reason",
    }
    incident_fields = {"protocol", "source_cursor", "reason"}
    sessions = snapshot["sessions"]
    if (
        snapshot["protocol"] != OBSERVATORY_PROJECTION_PROTOCOL
        or not _safe_integer_min(snapshot["source_cursor"], -1)
        or not safe_integer(snapshot["observed_at_unix_ms"])
        or not isinstance(sessions, list)
        or len(sessions) > MAX_OBSERVATORY_ITEMS
        or len(incidents) > MAX_OBSERVATORY_ITEMS
    ):
        raise GatewayValidationError("invalid_observatory_response")

    states = {"accepted", "streaming", "completed", "cancelled", "failed", "quarantined"}
    decoded_sessions: list[Mapping[str, Any]] = []
    for session in sessions:
        if not _exact(session, session_fields):
            raise GatewayValidationError("invalid_observatory_response")
        assert isinstance(session, Mapping)
        state = session["state"]
        terminal_state = state in {"completed", "cancelled", "failed"}
        reason = session["quarantine_reason"]
        if (
            not _public_identifier(session["request_id"])
            or state not in states
            or type(session["terminal"]) is not bool
            or session["terminal"] is not terminal_state
            or not safe_integer(session["last_sequence"])
            or not _safe_integer_min(session["event_count"], 1)
            or not safe_integer(session["token_count"])
            or session["event_count"] != session["last_sequence"] + 1
            or session["token_count"] >= session["event_count"]
            or (state == "accepted" and (session["event_count"] != 1 or session["token_count"] != 0))
            or (state == "streaming" and session["token_count"] < 1)
            or not _public_identifier(session["qualification_id"])
            or not safe_integer(session["started_at_unix_ms"])
            or not safe_integer(session["updated_at_unix_ms"])
            or session["updated_at_unix_ms"] < session["started_at_unix_ms"]
            or session["updated_at_unix_ms"] > snapshot["observed_at_unix_ms"]
            or (reason is not None and not safe_code(reason))
            or ((state == "quarantined") is not (reason is not None))
        ):
            raise GatewayValidationError("invalid_observatory_response")
        decoded_sessions.append(session)
    request_ids = [str(session["request_id"]) for session in decoded_sessions]
    if request_ids != sorted(set(request_ids)):
        raise GatewayValidationError("invalid_observatory_response")

    for incident in incidents:
        if not _exact(incident, incident_fields):
            raise GatewayValidationError("invalid_observatory_response")
        assert isinstance(incident, Mapping)
        if (
            incident["protocol"]
            not in {ROUTE_QUALIFICATION_PROTOCOL, REQUEST_EVENT_PROTOCOL, "unknown"}
            or not _safe_integer_min(incident["source_cursor"], -1)
            or not safe_code(incident["reason"])
        ):
            raise GatewayValidationError("invalid_observatory_response")

    qualification = snapshot["qualification"]
    qualification_ready = False
    qualification_id: str | None = None
    if qualification is not None:
        qualification_fields = {
            "protocol",
            "qualification_id",
            "issued_at_unix_ms",
            "evidence_class",
            "route_ready",
            "reason_codes",
            "binding",
        }
        if not _exact(qualification, qualification_fields):
            raise GatewayValidationError("invalid_observatory_response")
        assert isinstance(qualification, Mapping)
        reasons = qualification["reason_codes"]
        binding = qualification["binding"]
        qualification_ready = qualification["route_ready"]
        qualification_id = qualification["qualification_id"] if isinstance(
            qualification["qualification_id"], str
        ) else None
        if (
            qualification["protocol"] != ROUTE_QUALIFICATION_PROTOCOL
            or not _public_identifier(qualification["qualification_id"])
            or not safe_integer(qualification["issued_at_unix_ms"])
            or qualification["issued_at_unix_ms"] > snapshot["observed_at_unix_ms"]
            or qualification["evidence_class"]
            not in {"physical_qualification", "synthetic_test_fixture"}
            or type(qualification_ready) is not bool
            or not isinstance(reasons, list)
            or len(reasons) > 64
            or len(set(reasons)) != len(reasons)
            or not all(safe_code(reason) for reason in reasons)
            or not valid_binding(binding)
        ):
            raise GatewayValidationError("invalid_observatory_response")
        assert isinstance(binding, Mapping)
        if (
            binding["qualification_id"] != qualification_id
            or (
                qualification_ready
                and (
                    qualification["evidence_class"] != "physical_qualification"
                    or reasons
                    or not binding["stage_load_proof_digests"]
                )
            )
            or (not qualification_ready and not reasons)
        ):
            raise GatewayValidationError("invalid_observatory_response")
    if decoded_sessions and qualification is None:
        raise GatewayValidationError("invalid_observatory_response")
    if qualification_id is not None and any(
        not session["terminal"]
        and session["state"] != "quarantined"
        and session["qualification_id"] != qualification_id
        for session in decoded_sessions
    ):
        raise GatewayValidationError("invalid_observatory_response")

    if (
        provisioning["protocol"] != OBSERVATORY_STATUS_PROTOCOL
        or type(provisioning["route_ready"]) is not bool
        or provisioning["route_ready"] is not qualification_ready
        or not _safe_integer_min(provisioning["source_cursor"], -1)
        or provisioning["source_cursor"] != snapshot["source_cursor"]
        or not safe_integer(provisioning["buffered_sessions"])
        or provisioning["buffered_sessions"] != len(decoded_sessions)
        or not _safe_integer_min(provisioning["quarantine_capacity"], 1)
        or provisioning["quarantine_capacity"] > MAX_OBSERVATORY_ITEMS
        or len(incidents) > provisioning["quarantine_capacity"]
        or not safe_integer(provisioning["dropped_quarantine_count"])
    ):
        raise GatewayValidationError("invalid_observatory_response")


def validate_observatory_envelope(document: Mapping[str, Any]) -> None:
    if not _exact(document, {"protocol", "generation", "bundle"}):
        raise GatewayValidationError("invalid_observatory_response")
    bundle = document["bundle"]
    if (
        document["protocol"] != OBSERVATORY_PROTOCOL
        or not _safe_integer_min(document["generation"], 1)
        or not _exact(bundle, {"snapshot", "incidents", "provisioning"})
    ):
        raise GatewayValidationError("invalid_observatory_response")
    assert isinstance(bundle, Mapping)
    snapshot = bundle["snapshot"]
    incidents = bundle["incidents"]
    provisioning = bundle["provisioning"]
    if (
        not isinstance(snapshot, Mapping)
        or not isinstance(incidents, list)
        or not isinstance(provisioning, Mapping)
        or not _exact(
            snapshot,
            {"protocol", "source_cursor", "observed_at_unix_ms", "qualification", "sessions"},
        )
        or not _exact(
            provisioning,
            {
                "protocol",
                "route_ready",
                "source_cursor",
                "buffered_sessions",
                "quarantine_capacity",
                "dropped_quarantine_count",
            },
        )
    ):
        raise GatewayValidationError("invalid_observatory_response")
    assert isinstance(snapshot, Mapping)
    assert isinstance(incidents, list)
    assert isinstance(provisioning, Mapping)
    _validate_observatory_semantics(snapshot, incidents, provisioning)
    sessions = snapshot["sessions"]
    if not isinstance(sessions, list) or len(sessions) > 4_096:
        raise GatewayValidationError("invalid_observatory_response")
    session_fields = {
        "request_id",
        "state",
        "last_sequence",
        "event_count",
        "token_count",
        "terminal",
        "qualification_id",
        "started_at_unix_ms",
        "updated_at_unix_ms",
        "quarantine_reason",
    }
    incident_fields = {"protocol", "source_cursor", "reason"}
    if any(not _exact(session, session_fields) for session in sessions) or any(
        not _exact(incident, incident_fields) for incident in incidents
    ):
        raise GatewayValidationError("invalid_observatory_response")
    qualification = snapshot["qualification"]
    qualification_ready = False
    if qualification is not None:
        qualification_fields = {
            "protocol",
            "qualification_id",
            "issued_at_unix_ms",
            "evidence_class",
            "route_ready",
            "reason_codes",
            "binding",
        }
        if not _exact(qualification, qualification_fields):
            raise GatewayValidationError("invalid_observatory_response")
        assert isinstance(qualification, Mapping)
        if not valid_binding(qualification["binding"]):
            raise GatewayValidationError("invalid_observatory_response")
        qualification_ready = qualification["route_ready"]
        reasons = qualification["reason_codes"]
        if (
            not isinstance(qualification_ready, bool)
            or not isinstance(reasons, list)
            or any(not safe_code(reason) for reason in reasons)
            or len(set(reasons)) != len(reasons)
            or (
                qualification_ready
                and (
                    qualification["evidence_class"] != "physical_qualification"
                    or len(reasons) != 0
                )
            )
            or (not qualification_ready and len(reasons) == 0)
        ):
            raise GatewayValidationError("invalid_observatory_response")
    if (
        not isinstance(provisioning["route_ready"], bool)
        or provisioning["route_ready"] is not qualification_ready
    ):
        raise GatewayValidationError("invalid_observatory_response")
    _safe_json_tree(document)


def validate_stream_event(document: Mapping[str, Any], expected_id: str) -> tuple[int, str]:
    common = {"protocol", "request_id", "sequence", "type"}
    event_type = document.get("type")
    expected = set(common)
    if event_type == "token":
        expected.update({"token_index", "text"})
    elif event_type == "failed":
        expected.add("code")
    if not _exact(document, expected):
        raise GatewayValidationError("invalid_upstream_event")
    if (
        document["protocol"] != REQUEST_EVENT_PROTOCOL
        or document["request_id"] != expected_id
        or not safe_integer(document["sequence"])
        or event_type not in {"accepted", "token", "completed", "cancelled", "failed"}
    ):
        raise GatewayValidationError("invalid_upstream_event")
    if event_type == "token":
        text = document["text"]
        if (
            not safe_integer(document["token_index"])
            or not isinstance(text, str)
            or len(text) > 65_536
            or len(text.encode("utf-8")) > 262_144
        ):
            raise GatewayValidationError("invalid_upstream_event")
    elif event_type == "failed" and not safe_code(document["code"]):
        raise GatewayValidationError("invalid_upstream_event")
    return int(document["sequence"]), str(event_type)


def validate_last_event_id(values: Sequence[bytes]) -> bytes | None:
    if not values:
        return None
    if len(values) != 1:
        raise GatewayValidationError("invalid_last_event_id")
    try:
        text = values[0].decode("ascii")
    except UnicodeDecodeError:
        raise GatewayValidationError("invalid_last_event_id") from None
    if not text or not text.isdecimal() or int(text) > MAX_SAFE_INTEGER:
        raise GatewayValidationError("invalid_last_event_id")
    return values[0]


def _validate_endpoint_id(value: object) -> bool:
    if value is None:
        return True
    if not _public_identifier(value):
        return False
    assert isinstance(value, str)
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return not value.lower().startswith("localhost")
    return False


def validate_swarm_status(document: Mapping[str, Any]) -> None:
    if not _exact(document, {"protocol", "native_nodes", "browser_workers"}):
        raise GatewayValidationError("invalid_coordinator_response")
    native_nodes = document["native_nodes"]
    browser_workers = document["browser_workers"]
    if (
        document["protocol"] != SWARM_PROTOCOL
        or not isinstance(native_nodes, list)
        or not isinstance(browser_workers, list)
        or len(native_nodes) > 4_096
        or len(browser_workers) > 4_096
    ):
        raise GatewayValidationError("invalid_coordinator_response")
    for node in native_nodes:
        if not _exact(
            node,
            {"member_id", "capability", "membership_state", "connectivity", "endpoint_id"},
        ):
            raise GatewayValidationError("invalid_coordinator_response")
        assert isinstance(node, Mapping)
        if (
            not _public_identifier(node["member_id"])
            or node["capability"] != "native_inference_node"
            or node["membership_state"]
            not in {"invited", "trusted", "reachable", "assigned", "qualified", "revoked"}
            or node["connectivity"]
            not in {"direct", "relayed", "local", "disconnected", "stale", "unknown"}
            or not _validate_endpoint_id(node["endpoint_id"])
        ):
            raise GatewayValidationError("invalid_coordinator_response")
    for worker in browser_workers:
        if not _exact(worker, {"peer_id", "capability", "state", "expires_at_unix_ms"}):
            raise GatewayValidationError("invalid_coordinator_response")
        assert isinstance(worker, Mapping)
        if (
            not _public_identifier(worker["peer_id"])
            or worker["capability"] != "synthetic_browser_probe"
            or worker["state"]
            not in {"invited", "connecting", "ready", "busy", "disconnected", "stale", "revoked"}
            or not safe_integer(worker["expires_at_unix_ms"])
        ):
            raise GatewayValidationError("invalid_coordinator_response")


def swarm_action(document: Mapping[str, Any], action: str) -> None:
    if action == "create_invite":
        if not _exact(document, {"protocol", "action", "capability", "expires_in_seconds"}):
            raise GatewayValidationError("invalid_swarm_request")
        expires = document["expires_in_seconds"]
        valid = (
            document["capability"] in {"native_inference_node", "synthetic_browser_probe"}
            and isinstance(expires, int)
            and not isinstance(expires, bool)
            and 30 <= expires <= 86_400
        )
    elif action == "join":
        if not _exact(document, {"protocol", "action", "invite_code", "display_name"}):
            raise GatewayValidationError("invalid_swarm_request")
        code = document["invite_code"]
        name = document["display_name"]
        valid = (
            isinstance(code, str)
            and 16 <= len(code) <= 2_048
            and isinstance(name, str)
            and 1 <= len(name) <= 128
        )
    else:
        if not _exact(document, {"protocol", "action", "member_id"}):
            raise GatewayValidationError("invalid_swarm_request")
        valid = identifier(document["member_id"])
    if document.get("protocol") != SWARM_PROTOCOL or document.get("action") != action or not valid:
        raise GatewayValidationError("invalid_swarm_request")


def validate_swarm_result(
    document: Mapping[str, Any], action: str, request_document: Mapping[str, Any]
) -> None:
    if action == "create_invite":
        fields = {"protocol", "invite_id", "invite_code", "capability", "expires_at_unix_ms"}
        valid = (
            _exact(document, fields)
            and identifier(document.get("invite_id"))
            and isinstance(document.get("invite_code"), str)
            and 16 <= len(document["invite_code"]) <= 2_048
            and document.get("capability") == request_document["capability"]
            and safe_integer(document.get("expires_at_unix_ms"))
        )
    elif action == "join":
        fields = {"protocol", "joined", "member_id", "capability"}
        valid = (
            _exact(document, fields)
            and document.get("joined") is True
            and identifier(document.get("member_id"))
            and document.get("capability")
            in {"native_inference_node", "synthetic_browser_probe"}
        )
    else:
        fields = {"protocol", "member_id", "left"}
        valid = (
            _exact(document, fields)
            and document.get("member_id") == request_document["member_id"]
            and type(document.get("left")) is bool
        )
    if not valid or document.get("protocol") != SWARM_PROTOCOL:
        raise GatewayValidationError("invalid_coordinator_response")


__all__ = [
    "GatewayValidationError",
    "canonical_json",
    "safe_code",
    "strict_json_document",
    "swarm_action",
    "validate_cancel_response",
    "validate_inference_accept",
    "validate_last_event_id",
    "validate_observatory_envelope",
    "validate_qualification",
    "validate_stream_event",
    "validate_submission",
    "validate_swarm_result",
    "validate_swarm_status",
]
