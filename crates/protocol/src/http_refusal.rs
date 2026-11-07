// SPDX-License-Identifier: AGPL-3.0-only
//! v0.1.4 HTTP refusal bytes for the two surfaces that carry signed-envelope refusals to
//! a client: the seed coordinator (`mycelium_seed/http.py`) and the managed-control
//! origin (`mycelium_a13_macos/control_origin.py`). Pure mapping only: no listener, no
//! clock, no I/O. A server mounts these so a refusal reaches a peer byte-for-byte as it
//! did in v0.1.4.
use crate::canonical;
use serde_json::json;

/// One refusal response: status plus canonical-JSON body (headers: see the `*_headers`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Refusal {
    pub status: u16,
    pub body: Vec<u8>,
}

const SEED_ERROR_PROTOCOL: &str = "mycelium.seed.http_error.v1";
const SEED_INTERNAL: &str = "seed_http_internal_error";
const ORIGIN_INTERNAL: &str = "managed_control_internal_error";
/// `JOIN_ROUTE_ERROR_STATUSES` (`mycelium_seed/http.py`): the only codes `/seed/join`
/// forwards; any other join failure is answered as an internal error.
const JOIN_STATUSES: &[(&str, u16)] = &[
    ("invite_expired", 400),
    ("invite_malformed", 400),
    ("invite_protocol_invalid", 400),
    ("invite_replayed", 409),
    ("invite_signature_invalid", 401),
    ("join_request_protocol_required", 400),
    ("membership_endpoint_addr_invalid", 400),
    ("membership_endpoint_id_mismatch", 400),
    ("membership_envelope_invalid", 400),
    ("membership_field_unusable", 400),
    ("membership_fields_invalid", 400),
    ("membership_generation_invalid", 400),
    ("membership_identifier_invalid", 400),
    ("membership_join_generation_invalid", 400),
    ("membership_key_pin_invalid", 401),
    ("membership_message_expired", 400),
    ("membership_message_from_future", 400),
    ("membership_message_invalid", 400),
    ("membership_contract_upgrade_required", 426),
    ("membership_peer_class_invalid", 400),
    ("membership_peer_profile_binding_mismatch", 400),
    ("membership_peer_profile_invalid", 400),
    ("membership_v2_before_cutover", 400),
    ("membership_v2_peer_profile_invalid", 400),
    ("membership_v2_profile_generation_mismatch", 400),
    ("membership_v2_profile_has_no_consumer_projection", 400),
    ("membership_protocol_invalid", 400),
    ("membership_runtime_capability_invalid", 400),
    ("membership_runtime_capability_mismatch", 400),
    ("membership_signature_invalid", 401),
    ("membership_text_invalid", 400),
    ("membership_time_invalid", 400),
    ("membership_ttl_invalid", 400),
    ("membership_verifier_invalid", 400),
    ("seed_join_key_invalid", 400),
    ("seed_join_mismatch", 400),
    ("seed_join_retry_mismatch", 400),
    ("seed_member_identity_reused", 400),
    ("seed_node_endpoint_conflict", 400),
    ("seed_node_key_conflict", 409),
];

fn seed_body(code: &str) -> Vec<u8> {
    canonical::to_bytes(&json!({"protocol": SEED_ERROR_PROTOCOL, "error": {"code": code}}))
        .expect("fixed-shape body is canonical")
}
fn origin_body(code: &str) -> Vec<u8> {
    canonical::to_bytes(&json!({"error": {"code": code}})).expect("fixed-shape body is canonical")
}

/// `_error_status`: the status for any code on `/seed/resume` and `/seed/message`.
fn seed_status(code: &str) -> u16 {
    if let Some((_, status)) = JOIN_STATUSES.iter().find(|(c, _)| *c == code) {
        return *status;
    }
    match code {
        "seed_http_frame_too_large" => 413,
        "invite_replayed"
        | "seed_node_key_conflict"
        | "seed_assignment_exists"
        | "seed_message_replayed" => 409,
        "seed_member_unknown" | "seed_assignment_unknown" => 404,
        _ if code.contains("signature") || code.contains("key_pin") => 401,
        _ => 400,
    }
}

/// `/seed/join`: a failure whose code is not in the join table is an internal error.
pub fn seed_join(code: &str) -> Refusal {
    match JOIN_STATUSES.iter().find(|(c, _)| *c == code) {
        Some((_, status)) => Refusal {
            status: *status,
            body: seed_body(code),
        },
        None => seed_internal_error(),
    }
}
/// `/seed/resume` and `/seed/message`: the code of a listed exception type.
pub fn seed_message(code: &str) -> Refusal {
    Refusal {
        status: seed_status(code),
        body: seed_body(code),
    }
}
/// Any exception type the seed handler does not list.
pub fn seed_internal_error() -> Refusal {
    Refusal {
        status: 500,
        body: seed_body(SEED_INTERNAL),
    }
}
/// Response headers in v0.1.4 send order (`Server` is the handler's `server_version`).
pub fn seed_headers(body_len: usize) -> Vec<(&'static str, String)> {
    vec![
        ("Server", "MyceliumSeed/1 ".to_owned()),
        ("Content-Type", "application/json".to_owned()),
        ("Content-Length", body_len.to_string()),
        ("Cache-Control", "no-store".to_owned()),
    ]
}

/// `_status_for_error` of the managed-control origin.
pub fn managed_control_origin(code: &str) -> Refusal {
    let status = match code {
        "managed_control_origin_capacity_exhausted" => 503,
        "managed_control_origin_host_invalid" => 403,
        "managed_control_method_invalid" => 405,
        "managed_control_route_unknown" => 404,
        "managed_control_conflicting_retry"
        | "managed_control_sequence_replayed"
        | "managed_control_nonce_replayed"
        | "managed_control_member_stale" => 409,
        "managed_control_signature_invalid"
        | "managed_control_key_pin_mismatch"
        | "managed_control_message_expired"
        | "managed_control_message_from_future" => 401,
        _ => 400,
    };
    Refusal {
        status,
        body: origin_body(code),
    }
}
pub fn managed_control_origin_internal_error() -> Refusal {
    Refusal {
        status: 500,
        body: origin_body(ORIGIN_INTERNAL),
    }
}
/// Send order; the stdlib `Server: BaseHTTP/..` banner of the Python handler is a
/// runtime artefact of that implementation, not part of the contract, and is omitted.
pub fn managed_control_origin_headers(body_len: usize) -> Vec<(&'static str, String)> {
    vec![
        ("Content-Type", "application/json".to_owned()),
        ("Content-Length", body_len.to_string()),
        ("Cache-Control", "no-store".to_owned()),
        ("Connection", "close".to_owned()),
    ]
}
