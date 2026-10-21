// SPDX-License-Identifier: AGPL-3.0-only
//! The phone's `infer_cancel_wait` receipt: the owner cancellation fan-out
//! (`mycelium_live/route.py::cancel_node`, route.py:3852) sends the exact
//! request-control identity and expects a signed `inference_cancelled`
//! observation from **every** participant, not only the entry.
//!
//! This module is the pure part: the wire shape, the validation, the
//! generation fence and the receipt assembly. It never touches I/O, never
//! talks to the transport and never invents cleanup: the residue is exactly
//! what [`Residue`] reports, so a receipt can only say `complete: true` when
//! the teardown actually found nothing left.
//!
//! The Python semantics it mirrors (`physical_inference_node.py`):
//!
//! - `_infer_cancel` (PIN:4069): exactly `{request_id}` alone, or the full
//!   control plus `deadline_budget_ms` (PIN:4075-4098);
//! - the control is validated against the bound request control and its
//!   `cancellation_generation` must be exactly `bound + 1`, else
//!   `stale_infer_cancel_generation` (PIN:4150-4153);
//! - a cancellation that arrives before the request's control was bound is a
//!   *pending* cancellation with the same immutable identity; a later bind
//!   must match it exactly (`conflicting_pending_cancellation`, PIN:4125-4131);
//! - `_infer_cancel_wait` (PIN:4299) returns
//!   `{request_id, cancelled, status: "CANCELLED"|"CANCELLING",
//!     cleanup_pending, request_cleanup}` and only sets `cleanup_pending:
//!     false` with a `request_cleanup.complete` receipt.
//! - the receipt itself (PIN:2920-2929) is
//!   `{**subject, runtime_clean, transport_clean,
//!     cancellation_worker_complete, complete}`.
//!
//! The control worker never waits behind CPU execution. A busy runtime and
//! queued/sending/retrying publication remain explicit residue; only the exact
//! consumed subject can seal a delayed receipt once those owners terminate.

use std::collections::BTreeSet;

use serde_json::{Value, json};

/// `mycelium_deadlines.CANCEL_AND_CLEANUP_BUDGET_SECONDS` x 1000
/// (`COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS`): the ceiling a plan may request.
pub const COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS: u64 = 8_000;
/// `physical_inference_node._CANCEL_RECEIPT_FALLBACK_RESERVE_SECONDS`, in ms.
pub const CANCEL_RECEIPT_FALLBACK_RESERVE_MS: u64 = 500;
/// Receipts held at once (the Python node holds 256).
pub const MAX_CLEANUP_RECEIPTS: usize = 256;

/// `_REQUEST_CONTROL_FIELDS`: the immutable identity of one request's control.
pub const SUBJECT_FIELDS: [&str; 12] = [
    "deployment_id",
    "deployment_epoch",
    "qualification_digest",
    "command_id",
    "publisher_generation",
    "absolute_deadline_ms",
    "request_attempt",
    "path_id",
    "path_attempt",
    "path_digest",
    "topology_generation",
    "cancellation_generation",
];

/// The exact cleanup subject: request id plus the immutable control identity.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CancelSubject {
    pub request_id: String,
    pub deployment_id: String,
    pub deployment_epoch: u64,
    pub qualification_digest: String,
    pub command_id: String,
    pub publisher_generation: u64,
    pub absolute_deadline_ms: u64,
    pub request_attempt: u64,
    pub path_id: String,
    pub path_attempt: i64,
    pub path_digest: String,
    pub topology_generation: u64,
    pub cancellation_generation: u64,
}

impl CancelSubject {
    /// The subject as the receipt carries it (PIN:2920 `{**cleanup_subject}`),
    /// ordered as the receipt does so the canonical bytes are stable.
    pub fn to_json(&self) -> Value {
        json!({
            "request_id": self.request_id,
            "deployment_id": self.deployment_id,
            "deployment_epoch": self.deployment_epoch,
            "qualification_digest": self.qualification_digest,
            "command_id": self.command_id,
            "publisher_generation": self.publisher_generation,
            "absolute_deadline_ms": self.absolute_deadline_ms,
            "request_attempt": self.request_attempt,
            "path_id": self.path_id,
            "path_attempt": self.path_attempt,
            "path_digest": self.path_digest,
            "topology_generation": self.topology_generation,
            "cancellation_generation": self.cancellation_generation,
        })
    }

    /// Every field except `cancellation_generation`: the identity a request
    /// control is pinned by, so a cancellation cannot move the subject.
    fn immutable(&self, other: &CancelSubject) -> bool {
        other.request_id == self.request_id
            && other.deployment_id == self.deployment_id
            && other.deployment_epoch == self.deployment_epoch
            && other.qualification_digest == self.qualification_digest
            && other.command_id == self.command_id
            && other.publisher_generation == self.publisher_generation
            && other.absolute_deadline_ms == self.absolute_deadline_ms
            && other.request_attempt == self.request_attempt
            && other.path_id == self.path_id
            && other.path_attempt == self.path_attempt
            && other.path_digest == self.path_digest
            && other.topology_generation == self.topology_generation
    }

    /// The receipt's canonical digest input: the identity the receipt commits.
    pub fn identity_json(&self) -> Value {
        let mut m = self.to_json();
        if let Value::Object(m) = &mut m {
            m.remove("cancellation_generation");
        }
        m
    }
}

/// What the teardown actually found. Every field is an observation; an empty
/// `live_paths` with `pending_cancellations == 0` and `inflight == 0` is the
/// only state that can produce `complete: true`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Residue {
    /// Live stage paths still held for this request, oldest registration first.
    pub live_paths: Vec<String>,
    /// Pending cancellation tombstones still naming this request.
    pub pending_cancellations: usize,
    /// Stage steps for this request still executing or queued.
    pub inflight: usize,
    /// Replay results the runtime still holds for this request.
    pub replay_results: usize,
    /// The exact path teardown could not remove (its owner/attempt differs).
    pub path_retained: bool,
    /// The runtime is owned by a compute worker: cleanup cannot inspect or
    /// release even another request's cached values yet.
    pub runtime_busy: bool,
}

impl Residue {
    pub fn is_clean(&self) -> bool {
        self.live_paths.is_empty()
            && self.pending_cancellations == 0
            && self.inflight == 0
            && self.replay_results == 0
            && !self.path_retained
            && !self.runtime_busy
    }

    pub fn to_json(&self) -> Value {
        let mut value = json!({
            "live_paths": self.live_paths,
            "pending_cancellations": self.pending_cancellations,
            "inflight": self.inflight,
            "replay_results": self.replay_results,
            "path_retained": self.path_retained,
        });
        if self.runtime_busy { value["runtime_busy"] = json!(true); }
        value
    }
}

/// The signed `inference_cancelled` details the route reads
/// (`route.py:3898` reads `details.request_cleanup`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CancelReceipt {
    pub request_id: String,
    pub cancelled: bool,
    pub status: &'static str,
    pub cleanup_pending: bool,
    pub subject: CancelSubject,
    pub residue: Residue,
    /// Whether the request control was never bound here (cancel-before-start).
    pub pending_start: bool,
    pub cancellation_worker_complete: bool,
    pub deadline_budget_ms: u64,
}

impl CancelReceipt {
    pub fn cleanup_json(&self) -> Value {
        let mut cleanup = self.subject.to_json();
        if let Value::Object(m) = &mut cleanup {
            m.insert("runtime_clean".into(), json!(!self.residue.runtime_busy && self.residue.inflight == 0 && self.residue.replay_results == 0));
            m.insert("transport_clean".into(), json!(self.residue.inflight == 0 && self.residue.pending_cancellations == 0 && self.residue.live_paths.is_empty() && !self.residue.path_retained));
            m.insert("cancellation_worker_complete".into(), json!(self.cancellation_worker_complete));
            m.insert("complete".into(), json!(!self.cleanup_pending));
            m.insert("residue".into(), self.residue.to_json());
        }
        cleanup
    }

    /// `details` of the signed observation.
    pub fn to_json(&self) -> Value {
        let mut details = json!({
            "request_id": self.request_id,
            "cancelled": self.cancelled,
            "status": self.status,
            "cleanup_pending": self.cleanup_pending,
            "request_cleanup": self.cleanup_json(),
        });
        if self.pending_start {
            details["pending_start"] = json!(true);
        }
        details
    }
}

/// What the request-control table decided about an arriving cancellation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Fence {
    /// The subject advanced the bound control's cancellation generation.
    Applied,
    /// No control was bound yet: the cancellation is recorded as pending.
    Pending,
    /// Idempotent repeat of a cancellation this node already applied.
    Duplicate,
    /// The exact same immutable identity again at the bound generation.
    Refused(&'static str),
}

/// Validate `infer_cancel_wait`'s payload and return its exact subject.
/// Mirrors `_infer_cancel`'s field set (PIN:4075) and the bound control's
/// validation (`_validated_request_control`).
pub fn subject_of(payload: &Value, code: &str) -> Result<(CancelSubject, u64), String> {
    parse_subject(payload, code, false)
}

/// Existing bound controls may be at generation zero, unlike a cancellation.
pub(crate) fn subject_of_bound(payload: &Value, code: &str) -> Result<(CancelSubject, u64), String> {
    parse_subject(payload, code, true)
}

fn parse_subject(payload: &Value, code: &str, bound: bool) -> Result<(CancelSubject, u64), String> {
    let map = payload.as_object().ok_or_else(|| code.to_owned())?;
    let mut keys: BTreeSet<&str> = map.keys().map(String::as_str).collect();
    let request_id = map.get("request_id").and_then(Value::as_str).filter(|s| !s.is_empty()).ok_or_else(|| code.to_owned())?;
    keys.remove("request_id");
    keys.remove("deadline_budget_ms");
    let fields: BTreeSet<&str> = SUBJECT_FIELDS.iter().copied().collect();
    if keys != fields {
        return Err(code.to_owned());
    }
    let text = |k: &str| map[k].as_str().filter(|s| !s.is_empty()).ok_or_else(|| code.to_owned());
    let uint = |k: &str| map[k].as_u64().ok_or_else(|| code.to_owned());
    let subject = CancelSubject {
        request_id: request_id.to_owned(),
        deployment_id: text("deployment_id")?.to_owned(),
        deployment_epoch: uint("deployment_epoch")?,
        qualification_digest: text("qualification_digest")?.to_owned(),
        command_id: text("command_id")?.to_owned(),
        publisher_generation: uint("publisher_generation")?,
        absolute_deadline_ms: uint("absolute_deadline_ms")?,
        request_attempt: uint("request_attempt")?,
        path_id: text("path_id")?.to_owned(),
        path_attempt: map["path_attempt"].as_i64().ok_or_else(|| code.to_owned())?,
        path_digest: text("path_digest")?.to_owned(),
        topology_generation: uint("topology_generation")?,
        cancellation_generation: uint("cancellation_generation")?,
    };
    // Current Python _validated_request_control, including UTF-8 byte bound.
    let digest = |s: &str| s.len() == 71 && s.starts_with("sha256:")
        && s.as_bytes()[7..].iter().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b));
    if subject.deployment_epoch == 0 || subject.request_attempt == 0
        || subject.publisher_generation == 0 || subject.path_attempt < 0
        || subject.command_id.len() > 256 || !digest(&subject.qualification_digest) || !digest(&subject.path_digest) {
        return Err(code.to_owned());
    }
    // The same bounds the Python cleanup subject carries (PIN:2615-2633).
    if (!bound && subject.publisher_generation < 1)
        || subject.absolute_deadline_ms == 0
        || subject.topology_generation < 1
        || !subject.path_digest.starts_with("sha256:")
        || subject.path_digest.len() != 71
    {
        return Err(code.to_owned());
    }
    let budget = map.get("deadline_budget_ms").and_then(Value::as_u64).ok_or_else(|| code.to_owned())?;
    if !(1..=COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS).contains(&budget) {
        return Err(code.to_owned());
    }
    if !bound && subject.cancellation_generation < 1 {
        return Err(code.to_owned());
    }
    Ok((subject, budget))
}

/// The generation fence against the node's own request controls.
pub fn fence(bound: Option<&CancelSubject>, pending: Option<&CancelSubject>, arriving: &CancelSubject) -> Fence {
    // A pending cancellation is the authoritative identity for the request:
    // only its exact repeat is idempotent. Anything else — a different
    // immutable identity, or the same identity at another generation — is a
    // conflict and must never overwrite it (`_infer_cancel`, PIN:4125-4131:
    // `previous is None or previous == dict(data)`).
    if let Some(pending) = pending {
        if *pending == *arriving {
            return Fence::Duplicate;
        }
        return Fence::Refused("conflicting_pending_cancellation");
    }
    match bound {
        None => Fence::Pending,
        Some(bound) => {
            if !bound.immutable(arriving) {
                return Fence::Refused("stale_infer_cancel_generation");
            }
            // Python is strict (`_infer_cancel`, PIN:4150-4153): against a
            // bound control only `bound + 1` applies. An equal generation is
            // `stale_infer_cancel_generation`; idempotent repeats are served
            // by the session's sealed receipt, never by re-applying.
            match arriving.cancellation_generation.cmp(&bound.cancellation_generation) {
                std::cmp::Ordering::Greater if Some(arriving.cancellation_generation) == bound.cancellation_generation.checked_add(1) => Fence::Applied,
                _ => Fence::Refused("stale_infer_cancel_generation"),
            }
        }
    }
}

/// The budget the teardown may spend: the received one, less the single
/// fallback reserve (`_apply_cancellation_until_deadline`, PIN:3923-3931).
pub fn teardown_budget_ms(budget_ms: u64) -> u64 {
    budget_ms.saturating_sub(CANCEL_RECEIPT_FALLBACK_RESERVE_MS)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn payload() -> Value {
        json!({
            "request_id": "r-1",
            "deployment_id": "dep-1",
            "deployment_epoch": 2,
            "qualification_digest": format!("sha256:{}", "b".repeat(64)),
            "command_id": "cmd-1",
            "publisher_generation": 3,
            "absolute_deadline_ms": 90_000,
            "request_attempt": 1,
            "path_id": "path-1",
            "path_attempt": 4,
            "path_digest": format!("sha256:{}", "a".repeat(64)),
            "topology_generation": 7,
            "cancellation_generation": 1,
            "deadline_budget_ms": 4_000,
        })
    }

    fn subject(generation: u64) -> CancelSubject {
        subject_of(&payload(), "invalid_infer_cancel_control").unwrap().0.cancellation_generation_eq(generation)
    }

    impl CancelSubject {
        fn cancellation_generation_eq(mut self, g: u64) -> Self {
            self.cancellation_generation = g;
            self
        }
    }

    #[test]
    fn the_exact_control_identity_and_budget_validate() {
        let (s, budget) = subject_of(&payload(), "invalid_infer_cancel_control").unwrap();
        assert_eq!(s.request_id, "r-1");
        assert_eq!(s.path_attempt, 4);
        assert_eq!((s.publisher_generation, s.topology_generation, s.cancellation_generation), (3, 7, 1));
        assert_eq!(budget, 4_000);
        assert_eq!(teardown_budget_ms(budget), 3_500);
        // The receipt carries the subject and the bounds the Python one does.
        let cleanup = CancelReceipt {
            request_id: s.request_id.clone(),
            cancelled: true,
            status: "CANCELLED",
            cleanup_pending: false,
            subject: s.clone(),
            residue: Residue::default(),
            pending_start: false,
            cancellation_worker_complete: true,
            deadline_budget_ms: budget,
        }
        .cleanup_json();
        assert_eq!(cleanup["cancellation_generation"], 1);
        assert_eq!(cleanup["complete"], true);
        assert_eq!(cleanup["runtime_clean"], true);
        assert_eq!(cleanup["residue"]["live_paths"], json!([]));
    }

    #[test]
    fn a_missing_extra_or_out_of_bound_field_is_refused() {
        let code = |f: &dyn Fn(&mut Value)| {
            let mut p = payload();
            f(&mut p);
            subject_of(&p, "invalid_infer_cancel_control").unwrap_err()
        };
        let mut p = payload();
        p.as_object_mut().unwrap().remove("path_digest");
        assert_eq!(subject_of(&p, "invalid_infer_cancel_control").unwrap_err(), "invalid_infer_cancel_control");
        assert_eq!(code(&|p| { p.as_object_mut().unwrap().insert("extra".into(), json!(1)); }), "invalid_infer_cancel_control");
        assert_eq!(code(&|p| { p.as_object_mut().unwrap().remove("deadline_budget_ms"); }), "invalid_infer_cancel_control");
        // Out of the Python bounds: publisher generation 0, topology 0,
        // deadline 0, a non-digest path digest, cancellation generation 0.
        assert_eq!(code(&|p| p["publisher_generation"] = json!(0)), "invalid_infer_cancel_control");
        assert_eq!(code(&|p| p["topology_generation"] = json!(0)), "invalid_infer_cancel_control");
        assert_eq!(code(&|p| p["absolute_deadline_ms"] = json!(0)), "invalid_infer_cancel_control");
        assert_eq!(code(&|p| p["path_digest"] = json!("p")), "invalid_infer_cancel_control");
        assert_eq!(code(&|p| p["cancellation_generation"] = json!(0)), "invalid_infer_cancel_control");
        // The budget is bounded by the one authority window.
        assert_eq!(code(&|p| p["deadline_budget_ms"] = json!(9_000)), "invalid_infer_cancel_control");
        assert_eq!(code(&|p| p["deadline_budget_ms"] = json!(0)), "invalid_infer_cancel_control");
        assert_eq!(code(&|p| p["deadline_budget_ms"] = json!(1.5)), "invalid_infer_cancel_control");
        // A bare `{request_id}` is the entry's data-plane cancel, not this one.
        assert!(subject_of(&json!({"request_id": "r-1"}), "invalid_infer_cancel_control").is_err());
    }

    #[test]
    fn only_the_next_cancellation_generation_of_the_same_identity_is_applied() {
        let bound = subject(1);
        assert_eq!(fence(Some(&bound), None, &subject(2)), Fence::Applied);
        // Python refuses everything except bound + 1: a repeat of an applied
        // generation is stale here; idempotency is the sealed receipt's job.
        assert_eq!(fence(Some(&subject(2)), None, &subject(2)), Fence::Refused("stale_infer_cancel_generation"));
        // Skipping a generation, going backwards, or moving the identity are
        // all fence violations.
        assert_eq!(fence(Some(&bound), None, &subject(4)), Fence::Refused("stale_infer_cancel_generation"));
        assert_eq!(fence(Some(&bound), None, &subject(0)), Fence::Refused("stale_infer_cancel_generation"));
        let mut moved = subject(2);
        moved.path_id = "path-2".into();
        assert_eq!(fence(Some(&bound), None, &moved), Fence::Refused("stale_infer_cancel_generation"));
        let mut moved = subject(2);
        moved.path_attempt = 5;
        assert_eq!(fence(Some(&bound), None, &moved), Fence::Refused("stale_infer_cancel_generation"));
        // Cancel-before-start: no control bound, so it is a pending identity.
        assert_eq!(fence(None, None, &subject(1)), Fence::Pending);
        let pending = subject(1);
        assert_eq!(fence(None, Some(&pending), &subject(1)), Fence::Duplicate);
        let other = subject(3);
        assert_eq!(fence(None, Some(&pending), &other), Fence::Refused("conflicting_pending_cancellation"));
    }

    #[test]
    fn a_receipt_reports_the_residue_it_found_and_never_synthesises_zero() {
        let s = subject(1);
        let clean = CancelReceipt {
            request_id: s.request_id.clone(),
            cancelled: true,
            status: "CANCELLED",
            cleanup_pending: false,
            subject: s.clone(),
            residue: Residue::default(),
            pending_start: false,
            cancellation_worker_complete: true,
            deadline_budget_ms: 4_000,
        };
        assert_eq!(clean.to_json()["cleanup_pending"], false);
        assert_eq!(clean.to_json()["status"], "CANCELLED");

        // A successor path still live, a pending tombstone and a queued step:
        // the receipt must say CANCELLING/cleanup_pending and name each one.
        for residue in [
            Residue { live_paths: vec!["path-2".into()], ..Residue::default() },
            Residue { pending_cancellations: 1, ..Residue::default() },
            Residue { inflight: 2, ..Residue::default() },
            Residue { replay_results: 1, ..Residue::default() },
            Residue { path_retained: true, ..Residue::default() },
        ] {
            assert!(!residue.is_clean());
            let receipt = CancelReceipt {
                request_id: s.request_id.clone(),
                cancelled: true,
                status: "CANCELLING",
                cleanup_pending: true,
                subject: s.clone(),
                residue,
                pending_start: false,
                cancellation_worker_complete: true,
                deadline_budget_ms: 4_000,
            };
            let details = receipt.to_json();
            assert_eq!(details["cleanup_pending"], true, "{details}");
            assert_eq!(details["status"], "CANCELLING");
            assert_eq!(details["request_cleanup"]["complete"], false, "{details}");
            assert_ne!(
                details["request_cleanup"]["residue"],
                json!({"live_paths": [], "pending_cancellations": 0, "inflight": 0, "replay_results": 0, "path_retained": false})
            );
        }
        // A teardown that never ran is not a cancellation.
        let refused = CancelReceipt {
            request_id: s.request_id.clone(),
            cancelled: false,
            status: "CANCELLING",
            cleanup_pending: true,
            subject: s,
            residue: Residue::default(),
            pending_start: true,
            cancellation_worker_complete: true,
            deadline_budget_ms: 4_000,
        };
        assert_eq!(refused.to_json()["cancelled"], false);
        assert_eq!(refused.to_json()["pending_start"], true);
        assert_eq!(refused.to_json()["request_cleanup"]["complete"], false);
    }

    #[test]
    fn the_subject_field_set_is_exactly_the_python_request_control() {
        let p = payload();
        let keys: BTreeSet<&str> = p.as_object().unwrap().keys().map(String::as_str).collect();
        let mut expected: BTreeSet<&str> = SUBJECT_FIELDS.iter().copied().collect();
        expected.insert("request_id");
        expected.insert("deadline_budget_ms");
        assert_eq!(keys, expected);
        assert!(MAX_CLEANUP_RECEIPTS > 0);
    }

    #[test]
    fn identity_omits_only_the_cancellation_generation() {
        let s = subject(3);
        let identity = s.identity_json();
        assert!(identity.get("cancellation_generation").is_none());
        assert_eq!(identity["request_id"], "r-1");
        assert_eq!(identity.as_object().unwrap().len(), 12);
    }

    #[test]
    fn a_control_map_is_fenced_like_a_subject() {
        // The control channel stores the bound control as the Python node's
        // 12-field map; this is the projection into a subject.
        let bound = payload();
        let mut bound = bound.clone();
        bound.as_object_mut().unwrap().remove("request_id");
        bound.as_object_mut().unwrap().remove("deadline_budget_ms");
        assert_eq!(bound.as_object().unwrap().len(), 12);
        let s = subject(1);
        assert_eq!(fence(Some(&s), None, &subject(2)), Fence::Applied);
        // Map and subject agree on the identity fields.
        for k in SUBJECT_FIELDS {
            if k == "cancellation_generation" {
                continue;
            }
            assert_eq!(&bound[k], s.to_json().get(k).unwrap(), "{k}");
        }
    }
}
