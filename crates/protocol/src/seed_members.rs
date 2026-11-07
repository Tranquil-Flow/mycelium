// SPDX-License-Identifier: AGPL-3.0-only
//! The member paths of the v0.1.4 `SeedCoordinator` (`mycelium_seed/coordinator.py`, membership contract
//! v1): `receive_member_message` (heartbeat with lease renewal and the committed-heartbeat retry,
//! capability / link-probe / drain / rotation-ack / assignment-result with the replay window),
//! `lease_renewal`, `assignment_offer`, `advance_member_generation`, `member_authority_guard`, the liveness
//! projection, and the join/resume retry shortcuts.
//!
//! Pure over a caller-held [`SeedTable`] (members, replay rows, committed renewals, assignments, authority
//! rotations): no SQLite, no clock, no signing. The caller supplies `now`, the id source (the table counts
//! what was asked of it) and signs the returned unsigned messages. Membership contract v2 (the profile
//! contract and its upgrade gate) is not part of a v1 seed and is not modelled.
use crate::seed_coordinator::{
    Admission, Member, SeedCoordinatorError, SeedFrame, bounded_deadline,
};
use crate::{canonical, membership};
use serde_json::{Map, Value, json};
use std::collections::BTreeSet;

pub const HEARTBEAT_PROTOCOL: &str = "mycelium.membership.heartbeat.v1";
pub const LEASE_RENEWAL_PROTOCOL: &str = "mycelium.membership.lease_renewal.v1";
pub const ASSIGNMENT_OFFER_PROTOCOL: &str = "mycelium.membership.assignment_offer.v1";
pub const ASSIGNMENT_RESULT_PROTOCOL: &str = "mycelium.membership.assignment_result.v1";
pub const SEED_ROTATION_ACK_PROTOCOL: &str = "mycelium.membership.seed_rotation_ack.v1";
const MEMBER_PROTOCOLS: [&str; 6] = [
    "mycelium.membership.capability_report.v1",
    "mycelium.membership.link_probe_report.v1",
    HEARTBEAT_PROTOCOL,
    ASSIGNMENT_RESULT_PROTOCOL,
    "mycelium.membership.drain_ack.v1",
    SEED_ROTATION_ACK_PROTOCOL,
];
/// `_MAX_REPLAY_IDS`: replay rows kept per member generation.
pub const MAX_REPLAY_IDS: usize = 4096;
/// `assignment_offer` accepts at most this many peers.
const MAX_OFFER_PEERS: usize = 256;
const LIFECYCLE_ORDER: [&str; 6] = [
    "NEW",
    "CONFIGURED",
    "RUNNING",
    "DRAINING",
    "STOPPING",
    "STOPPED",
];

type Refusal = SeedCoordinatorError;

fn refuse<T>(code: &'static str) -> Result<T, Refusal> {
    Err(SeedCoordinatorError(code))
}

fn is_segment(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.as_bytes()[0].is_ascii_alphanumeric()
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
}

fn lifecycle_rank(state: &str) -> Option<usize> {
    LIFECYCLE_ORDER.iter().position(|s| *s == state)
}

fn float_value(f: f64) -> Value {
    serde_json::Number::from_f64(f).map_or(Value::Null, Value::Number)
}

/// Python `a // b` for finite `a >= 0`, `b > 0`.
fn floor_div(a: f64, b: f64) -> f64 {
    let modulo = a % b;
    let div = (a - modulo) / b;
    let floor = div.floor();
    if div - floor > 0.5 {
        floor + 1.0
    } else {
        floor
    }
}

/// The two liveness constants of a coordinator.
#[derive(Debug, Clone, Copy)]
pub struct Timing {
    pub keepalive_interval_seconds: f64,
    pub evidence_freshness_seconds: f64,
}

/// What a coordinator knows about itself, for the messages it issues.
pub struct Coordinator<'a> {
    pub frame: SeedFrame<'a>,
    pub timing: Timing,
    /// The seed signer's endpoint id and the seed's own incarnation (both sit in every seed message).
    pub seed_endpoint_id: &'a str,
    pub seed_incarnation: &'a str,
}

/// A coordinator member: the join/resume row plus the liveness fields heartbeats move.
#[derive(Debug, Clone, PartialEq)]
pub struct Tracked {
    pub member: Member,
    pub last_heartbeat_sequence: u64,
    pub last_liveness_at: f64,
    pub next_heartbeat_due_at: f64,
    pub last_activity_receipt_at: Option<f64>,
    pub active_requests: u64,
}

impl Tracked {
    fn admitted(member: Member, now: f64, timing: &Timing) -> Self {
        Self {
            member,
            last_heartbeat_sequence: 0,
            last_liveness_at: now,
            next_heartbeat_due_at: now + timing.keepalive_interval_seconds,
            last_activity_receipt_at: None,
            active_requests: 0,
        }
    }
}

struct ReplayRow {
    node_id: String,
    generation: u64,
    message_id: String,
    expires_at: f64,
}

struct RenewalRow {
    node_id: String,
    generation: u64,
    heartbeat_message_id: String,
    request_digest: String,
    sequence: u64,
    renewal: Value,
}

struct Assignment {
    node_id: String,
    deployment_id: String,
    deployment_epoch: u64,
    membership_generation: u64,
    assignment_id: String,
    accepted: Option<bool>,
}

struct Rotation {
    authority_generation: u64,
    transition_digest: String,
    pending: bool,
    /// The exact transition document (`seed_authority_rotations.transition_json`).
    transition: Value,
}

/// One `seed_rotation_acknowledgements` row: the latest acknowledgement a member sent for one authority generation
/// (primary key `(node_id, authority_generation)`; a later acknowledgement replaces the earlier one).
#[derive(Debug, Clone, PartialEq)]
struct AckRow {
    node_id: String,
    member_generation: u64,
    authority_generation: u64,
    transition_digest: String,
    message_id: String,
    acknowledged_at: f64,
}

/// What `SqliteSeedState.complete_authority_rotation` did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Completion {
    /// The pending transition was promoted: the caller persists the new seed key digest with the table.
    Applied,
    /// The transition was already completed (an idempotent repeat): nothing changes.
    AlreadyCompleted,
}

/// One arriving assignment offer (`SeedCoordinator.assignment_offer` arguments).
pub struct Offer<'a> {
    pub node_id: &'a str,
    pub deployment_id: &'a str,
    pub deployment_epoch: u64,
    pub assignment_id: &'a str,
    pub assignment_digest: &'a str,
    pub stage_pack_digest: &'a str,
    pub graph_digest: &'a str,
    pub load_generation: u64,
    pub peer_node_ids: &'a [String],
    pub placement_provenance: &'a str,
}

/// The coordinator's state: member rows, the durable replay / renewal / assignment / rotation tables and the
/// id source with its emitted-id set.
pub struct SeedTable {
    members: Vec<Tracked>,
    replay: Vec<ReplayRow>,
    renewals: Vec<RenewalRow>,
    join_rows: Vec<(String, String, String)>,
    resume_rows: Vec<(String, String)>,
    assignments: Vec<Assignment>,
    rotations: Vec<Rotation>,
    acks: Vec<AckRow>,
    authority_generation: u64,
    emitted: BTreeSet<String>,
    id_source: Box<dyn FnMut() -> String + Send>,
    drawn: Vec<String>,
    row_kept_by_refusal: bool,
    /// The members the durable state holds right now (set by the caller around one operation; `None` = no check).
    durable: Option<Vec<DurableMember>>,
}

/// The identity of a member as the database holds it (`SqliteSeedState.member_is_current`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DurableMember {
    pub node_id: String,
    pub endpoint_id: String,
    pub verification_key_digest: String,
    pub incarnation: String,
    pub generation: u64,
}

fn digest_of(value: &Value) -> Option<String> {
    canonical::to_bytes(value)
        .ok()
        .map(|b| canonical::sha256_prefixed(&b))
}

impl SeedTable {
    pub fn new(id_source: Box<dyn FnMut() -> String + Send>) -> Self {
        Self {
            members: Vec::new(),
            replay: Vec::new(),
            renewals: Vec::new(),
            join_rows: Vec::new(),
            resume_rows: Vec::new(),
            assignments: Vec::new(),
            rotations: Vec::new(),
            acks: Vec::new(),
            authority_generation: 1,
            emitted: BTreeSet::new(),
            id_source,
            drawn: Vec::new(),
            row_kept_by_refusal: false,
            durable: None,
        }
    }

    /// Whether the last refused [`receive_member_message`] still changed the durable state: a rotation
    /// acknowledgement row that `save_seed_rotation_acknowledgement` committed before `_remember` refused
    /// (`seed_replay_window_full`). The caller persists the table when this is true. Reading clears it.
    pub fn take_row_kept_by_refusal(&mut self) -> bool {
        std::mem::take(&mut self.row_kept_by_refusal)
    }

    /// Set (or clear) the durable view `_ensure_current_member` compares against.
    pub fn set_durable_view(&mut self, view: Option<Vec<DurableMember>>) {
        self.durable = view;
    }

    /// `_ensure_current_member`: the member in memory must still be the member the database holds.
    fn ensure_current(&self, index: usize) -> Result<(), Refusal> {
        let Some(view) = &self.durable else {
            return Ok(());
        };
        let m = &self.members[index].member;
        let current = view.iter().any(|d| {
            d.node_id == m.node_id
                && d.endpoint_id == m.endpoint_id
                && d.verification_key_digest == m.verification_key_digest
                && d.incarnation == m.incarnation
                && d.generation == m.generation
        });
        if current {
            Ok(())
        } else {
            refuse("seed_state_member_stale")
        }
    }

    /// `_ensure_current_member` for the member `node_id` (unknown members are the caller's refusal).
    pub fn ensure_current_member(&self, node_id: &str) -> Result<(), Refusal> {
        match self.index(node_id) {
            Some(index) => self.ensure_current(index),
            None => Ok(()),
        }
    }

    /// The ids the id source was asked for since the last call.
    pub fn take_drawn(&mut self) -> Vec<String> {
        std::mem::take(&mut self.drawn)
    }

    /// The member table the join / resume relation decisions read.
    pub fn rows(&self) -> Vec<Member> {
        self.members.iter().map(|t| t.member.clone()).collect()
    }

    /// `_new_message_id` / the acceptance id: ask the id source and refuse an unusable or reused id.
    fn draw_emitted(&mut self) -> Result<String, Refusal> {
        let id = (self.id_source)();
        self.drawn.push(id.clone());
        if !is_segment(&id) {
            return refuse("seed_message_id_invalid");
        }
        if self.emitted.contains(&id) {
            return refuse("seed_message_id_reused");
        }
        Ok(id)
    }

    fn index(&self, node_id: &str) -> Option<usize> {
        self.members
            .iter()
            .position(|t| t.member.node_id == node_id)
    }

    fn put(&mut self, tracked: Tracked) {
        match self.index(&tracked.member.node_id) {
            Some(i) => self.members[i] = tracked,
            None => self.members.push(tracked),
        }
    }

    fn is_v2_form(message: &Value) -> bool {
        message
            .as_object()
            .is_some_and(|m| m.contains_key("peer_profile"))
    }

    /// `load_join_acceptance`: an exactly repeated join is answered from the committed row; the same invite
    /// nonce with another token or envelope is `seed_join_retry_mismatch`.
    pub fn replays_join(&self, token: &str, envelope: &Value) -> Result<bool, Refusal> {
        let message = &envelope["message"];
        if Self::is_v2_form(message) {
            return Ok(false);
        }
        let (Some(nonce), Some(request_digest)) =
            (message["invite_nonce"].as_str(), digest_of(envelope))
        else {
            return Ok(false);
        };
        let token_digest = canonical::sha256_prefixed(token.as_bytes());
        match self.join_rows.iter().find(|row| row.0 == nonce) {
            None => Ok(false),
            Some(row) if row.1 == token_digest && row.2 == request_digest => Ok(true),
            Some(_) => refuse("seed_join_retry_mismatch"),
        }
    }

    /// `load_resume_acceptance`, keyed by the request message id.
    pub fn replays_resume(&self, envelope: &Value) -> Result<bool, Refusal> {
        let message = &envelope["message"];
        if Self::is_v2_form(message) {
            return Ok(false);
        }
        let (Some(message_id), Some(request_digest)) =
            (message["message_id"].as_str(), digest_of(envelope))
        else {
            return Ok(false);
        };
        match self.resume_rows.iter().find(|row| row.0 == message_id) {
            None => Ok(false),
            Some(row) if row.1 == request_digest => Ok(true),
            Some(_) => refuse("seed_resume_retry_mismatch"),
        }
    }

    /// Commit an admitted join: ask for the acceptance id, record the retry row, replace the member row.
    pub fn admit_join(
        &mut self,
        token: &str,
        envelope: &Value,
        admission: &Admission,
        now: f64,
        timing: &Timing,
    ) -> Result<(), Refusal> {
        let id = self.draw_emitted()?;
        self.commit_join(id, token, envelope, admission, now, timing);
        Ok(())
    }

    /// The id source's next id, judged as a message id (`seed_message_id_invalid` / `seed_message_id_reused`),
    /// not yet recorded as emitted: the caller signs under it and then commits with [`SeedTable::commit_join`]
    /// / [`SeedTable::commit_resume`].
    pub fn draw_message_id(&mut self) -> Result<String, Refusal> {
        self.draw_emitted()
    }

    /// Commit an admitted join under an id from [`SeedTable::draw_message_id`].
    pub fn commit_join(
        &mut self,
        id: String,
        token: &str,
        envelope: &Value,
        admission: &Admission,
        now: f64,
        timing: &Timing,
    ) {
        self.emitted.insert(id);
        if let (Some(nonce), Some(request_digest)) = (
            envelope["message"]["invite_nonce"].as_str(),
            digest_of(envelope),
        ) {
            self.join_rows.push((
                nonce.to_owned(),
                canonical::sha256_prefixed(token.as_bytes()),
                request_digest,
            ));
        }
        self.put(Tracked::admitted(admission.member.clone(), now, timing));
    }

    /// Commit an admitted resume (a new envelope for an already advanced generation included: the member
    /// object is replaced either way).
    pub fn admit_resume(
        &mut self,
        envelope: &Value,
        admission: &Admission,
        now: f64,
        timing: &Timing,
    ) -> Result<(), Refusal> {
        let id = self.draw_emitted()?;
        self.commit_resume(id, envelope, admission, now, timing);
        Ok(())
    }

    /// Commit an admitted resume under an id from [`SeedTable::draw_message_id`].
    pub fn commit_resume(
        &mut self,
        id: String,
        envelope: &Value,
        admission: &Admission,
        now: f64,
        timing: &Timing,
    ) {
        self.emitted.insert(id);
        if let (Some(message_id), Some(request_digest)) = (
            envelope["message"]["message_id"].as_str(),
            digest_of(envelope),
        ) {
            self.resume_rows
                .push((message_id.to_owned(), request_digest));
        }
        self.put(Tracked::admitted(admission.member.clone(), now, timing));
    }

    /// The durable state as one JSON document (members with their liveness fields, replay rows, committed
    /// renewals, join / resume retry rows, assignments, authority rotations, emitted message ids). The id
    /// source is not state. [`SeedTable::restore`] is its inverse.
    pub fn snapshot(&self) -> Value {
        let f = float_value;
        json!({
            "members": self.members.iter().map(|t| json!({
                "node_id": t.member.node_id, "endpoint_id": t.member.endpoint_id,
                "endpoint_addrs": t.member.endpoint_addrs, "peer_class": t.member.peer_class,
                "runtime_capability": t.member.runtime_capability,
                "verification_key_digest": t.member.verification_key_digest,
                "incarnation": t.member.incarnation, "generation": t.member.generation,
                "lease_expires_at": f(t.member.lease_expires_at),
                "lifecycle_state": t.member.lifecycle_state,
                "last_heartbeat_sequence": t.last_heartbeat_sequence,
                "last_liveness_at": f(t.last_liveness_at),
                "next_heartbeat_due_at": f(t.next_heartbeat_due_at),
                "last_activity_receipt_at": t.last_activity_receipt_at.map_or(Value::Null, f),
                "active_requests": t.active_requests,
            })).collect::<Vec<_>>(),
            "replay": self.replay.iter().map(|r| json!({
                "node_id": r.node_id, "generation": r.generation, "message_id": r.message_id,
                "expires_at": f(r.expires_at),
            })).collect::<Vec<_>>(),
            "renewals": self.renewals.iter().map(|r| json!({
                "node_id": r.node_id, "generation": r.generation,
                "heartbeat_message_id": r.heartbeat_message_id, "request_digest": r.request_digest,
                "sequence": r.sequence, "renewal": r.renewal,
            })).collect::<Vec<_>>(),
            "join_rows": self.join_rows.iter().map(|r| json!([r.0, r.1, r.2])).collect::<Vec<_>>(),
            "resume_rows": self.resume_rows.iter().map(|r| json!([r.0, r.1])).collect::<Vec<_>>(),
            "assignments": self.assignments.iter().map(|a| json!({
                "node_id": a.node_id, "deployment_id": a.deployment_id,
                "deployment_epoch": a.deployment_epoch, "membership_generation": a.membership_generation,
                "assignment_id": a.assignment_id, "accepted": a.accepted,
            })).collect::<Vec<_>>(),
            "rotations": self.rotations.iter().map(|r| json!({
                "authority_generation": r.authority_generation,
                "transition_digest": r.transition_digest, "pending": r.pending,
                "transition": r.transition,
            })).collect::<Vec<_>>(),
            "acks": self.acks.iter().map(|a| json!({
                "node_id": a.node_id, "member_generation": a.member_generation,
                "authority_generation": a.authority_generation, "transition_digest": a.transition_digest,
                "message_id": a.message_id, "acknowledged_at": float_value(a.acknowledged_at),
            })).collect::<Vec<_>>(),
            "authority_generation": self.authority_generation,
            "emitted": self.emitted.iter().collect::<Vec<_>>(),
        })
    }

    /// Rebuild a table from [`SeedTable::snapshot`]; any missing or mistyped field is `seed_state_corrupt`.
    pub fn restore(
        snapshot: &Value,
        id_source: Box<dyn FnMut() -> String + Send>,
    ) -> Result<Self, Refusal> {
        const CORRUPT: Refusal = SeedCoordinatorError("seed_state_corrupt");
        fn text(v: &Value, k: &str) -> Result<String, Refusal> {
            v[k].as_str().map(str::to_owned).ok_or(CORRUPT)
        }
        fn num(v: &Value, k: &str) -> Result<f64, Refusal> {
            v[k].as_f64().ok_or(CORRUPT)
        }
        fn int(v: &Value, k: &str) -> Result<u64, Refusal> {
            v[k].as_u64().ok_or(CORRUPT)
        }
        fn rows<'v>(v: &'v Value, k: &str) -> Result<&'v Vec<Value>, Refusal> {
            v[k].as_array().ok_or(CORRUPT)
        }
        let mut table = Self::new(id_source);
        for m in rows(snapshot, "members")? {
            let addrs = rows(m, "endpoint_addrs")?
                .iter()
                .map(|a| a.as_str().map(str::to_owned).ok_or(CORRUPT))
                .collect::<Result<Vec<_>, _>>()?;
            table.members.push(Tracked {
                member: Member {
                    node_id: text(m, "node_id")?,
                    endpoint_id: text(m, "endpoint_id")?,
                    endpoint_addrs: addrs,
                    peer_class: text(m, "peer_class")?,
                    runtime_capability: m["runtime_capability"].clone(),
                    verification_key_digest: text(m, "verification_key_digest")?,
                    incarnation: text(m, "incarnation")?,
                    generation: int(m, "generation")?,
                    lease_expires_at: num(m, "lease_expires_at")?,
                    lifecycle_state: text(m, "lifecycle_state")?,
                },
                last_heartbeat_sequence: int(m, "last_heartbeat_sequence")?,
                last_liveness_at: num(m, "last_liveness_at")?,
                next_heartbeat_due_at: num(m, "next_heartbeat_due_at")?,
                last_activity_receipt_at: match &m["last_activity_receipt_at"] {
                    Value::Null => None,
                    other => Some(other.as_f64().ok_or(CORRUPT)?),
                },
                active_requests: int(m, "active_requests")?,
            });
        }
        for r in rows(snapshot, "replay")? {
            table.replay.push(ReplayRow {
                node_id: text(r, "node_id")?,
                generation: int(r, "generation")?,
                message_id: text(r, "message_id")?,
                expires_at: num(r, "expires_at")?,
            });
        }
        for r in rows(snapshot, "renewals")? {
            table.renewals.push(RenewalRow {
                node_id: text(r, "node_id")?,
                generation: int(r, "generation")?,
                heartbeat_message_id: text(r, "heartbeat_message_id")?,
                request_digest: text(r, "request_digest")?,
                sequence: int(r, "sequence")?,
                renewal: r["renewal"].clone(),
            });
        }
        let cell = |r: &Value, i: usize| r[i].as_str().map(str::to_owned).ok_or(CORRUPT);
        for r in rows(snapshot, "join_rows")? {
            table
                .join_rows
                .push((cell(r, 0)?, cell(r, 1)?, cell(r, 2)?));
        }
        for r in rows(snapshot, "resume_rows")? {
            table.resume_rows.push((cell(r, 0)?, cell(r, 1)?));
        }
        for a in rows(snapshot, "assignments")? {
            table.assignments.push(Assignment {
                node_id: text(a, "node_id")?,
                deployment_id: text(a, "deployment_id")?,
                deployment_epoch: int(a, "deployment_epoch")?,
                membership_generation: int(a, "membership_generation")?,
                assignment_id: text(a, "assignment_id")?,
                accepted: match &a["accepted"] {
                    Value::Null => None,
                    Value::Bool(b) => Some(*b),
                    _ => return Err(CORRUPT),
                },
            });
        }
        for r in rows(snapshot, "rotations")? {
            table.rotations.push(Rotation {
                authority_generation: int(r, "authority_generation")?,
                transition_digest: text(r, "transition_digest")?,
                pending: r["pending"].as_bool().ok_or(CORRUPT)?,
                transition: match &r["transition"] {
                    Value::Object(_) => r["transition"].clone(),
                    _ => return Err(CORRUPT),
                },
            });
        }
        // A snapshot written before acknowledgement rows were kept has no `acks` key: it holds none (a missing
        // key is not corruption); a present key must be well formed.
        if !snapshot["acks"].is_null() {
            for a in rows(snapshot, "acks")? {
                table.acks.push(AckRow {
                    node_id: text(a, "node_id")?,
                    member_generation: int(a, "member_generation")?,
                    authority_generation: int(a, "authority_generation")?,
                    transition_digest: text(a, "transition_digest")?,
                    message_id: text(a, "message_id")?,
                    acknowledged_at: num(a, "acknowledged_at")?,
                });
            }
        }
        table.authority_generation = int(snapshot, "authority_generation")?;
        for id in rows(snapshot, "emitted")? {
            table.emitted.insert(id.as_str().ok_or(CORRUPT)?.to_owned());
        }
        Ok(table)
    }

    /// `_liveness_projection`.
    fn liveness(t: &Tracked, now: f64, timing: &Timing) -> Map<String, Value> {
        let age = (now - t.last_liveness_at).max(0.0);
        let overdue = (now - t.next_heartbeat_due_at).max(0.0);
        let stale = now > t.next_heartbeat_due_at;
        let missed: u64 = if stale {
            1 + floor_div(overdue, timing.keepalive_interval_seconds) as u64
        } else {
            0
        };
        let expired = age > timing.evidence_freshness_seconds;
        let (status, dead, reason): (&str, bool, Value) = if !stale {
            (
                if t.active_requests > 0 {
                    "active_fresh"
                } else {
                    "idle_fresh"
                },
                false,
                Value::Null,
            )
        } else if t.active_requests > 0 {
            (
                "active_decode_transport_failure",
                false,
                json!("in_flight_liveness_lost"),
            )
        } else {
            let dead = missed >= 2 && expired;
            (
                if dead { "dead" } else { "liveness_stale" },
                dead,
                json!(if dead {
                    "idle_keepalive_and_evidence_expired"
                } else {
                    "idle_keepalive_missed"
                }),
            )
        };
        let mut out = Map::new();
        out.insert("liveness_status".into(), json!(status));
        out.insert("liveness_dead".into(), json!(dead));
        out.insert("liveness_reason".into(), reason);
        out.insert("liveness_age_seconds".into(), float_value(age));
        out.insert("heartbeat_overdue_seconds".into(), float_value(overdue));
        out.insert("missed_keepalives".into(), json!(missed));
        out.insert("evidence_freshness_expired".into(), json!(expired));
        out
    }

    /// `_Member.projection()`.
    fn member_projection(t: &Tracked) -> Map<String, Value> {
        let m = &t.member;
        let mut out = Map::new();
        out.insert("node_id".into(), json!(m.node_id));
        out.insert("endpoint_id".into(), json!(m.endpoint_id));
        out.insert("endpoint_addrs".into(), json!(m.endpoint_addrs));
        out.insert("peer_class".into(), json!(m.peer_class));
        out.insert("runtime_capability".into(), m.runtime_capability.clone());
        out.insert("activation_eligible".into(), json!(activation_eligible(m)));
        out.insert(
            "verification_key_digest".into(),
            json!(m.verification_key_digest),
        );
        out.insert("incarnation".into(), json!(m.incarnation));
        out.insert("generation".into(), json!(m.generation));
        out.insert("lease_expires_at".into(), float_value(m.lease_expires_at));
        out.insert(
            "last_heartbeat_sequence".into(),
            json!(t.last_heartbeat_sequence),
        );
        out.insert("last_liveness_at".into(), float_value(t.last_liveness_at));
        out.insert(
            "next_heartbeat_due_at".into(),
            float_value(t.next_heartbeat_due_at),
        );
        out.insert(
            "last_activity_receipt_at".into(),
            t.last_activity_receipt_at.map_or(Value::Null, float_value),
        );
        out.insert("active_requests".into(), json!(t.active_requests));
        out.insert("lifecycle_state".into(), json!(m.lifecycle_state));
        out
    }

    /// `SeedCoordinator.member`: the member projection with its liveness at `now`.
    pub fn projection(&self, node_id: &str, now: f64, timing: &Timing) -> Option<Value> {
        let t = &self.members[self.index(node_id)?];
        let mut out = Self::member_projection(t);
        out.extend(Self::liveness(t, now, timing));
        Some(Value::Object(out))
    }

    /// `SqliteSeedState.begin_authority_rotation`; returns the transition digest the acknowledgements carry.
    /// `seed_key_digest` is the durable `seed_key_digest` metadata (the key the database is bound to).
    pub fn begin_authority_rotation(
        &mut self,
        transition: &Value,
        seed_key_digest: &str,
    ) -> Result<String, Refusal> {
        if transition["previous_generation"] != json!(self.authority_generation)
            || transition["authority_generation"] != json!(self.authority_generation + 1)
            || transition["old_seed_key_digest"] != seed_key_digest
        {
            return refuse("seed_authority_rotation_stale");
        }
        if self.rotations.iter().any(|r| r.pending) {
            return refuse("seed_authority_rotation_pending");
        }
        // The INSERT reads every column of the transition; a missing one is a `KeyError` that the source
        // reports as `seed_state_unavailable` (not vectored: `begin_seed_key_rotation` always builds all ten).
        for field in [
            "new_seed_key_digest",
            "initiated_at",
            "effective_at",
            "overlap_expires_at",
            "reason",
        ] {
            if transition.get(field).is_none() {
                return refuse("seed_state_unavailable");
            }
        }
        let digest = digest_of(transition).ok_or(SeedCoordinatorError("seed_state_unavailable"))?;
        self.rotations.push(Rotation {
            authority_generation: self.authority_generation + 1,
            transition_digest: digest.clone(),
            pending: true,
            transition: transition.clone(),
        });
        Ok(digest)
    }

    /// `SqliteSeedState.complete_authority_rotation`: promote one pending transition. `seed_key_digest` is the
    /// durable metadata digest before the call; on [`Completion::Applied`] the caller stores
    /// `new_seed_key_digest` in the same transaction as this table.
    pub fn complete_authority_rotation(
        &mut self,
        authority_generation: u64,
        new_seed_key_digest: &str,
        seed_key_digest: &str,
    ) -> Result<Completion, Refusal> {
        let Some(slot) = self
            .rotations
            .iter()
            .position(|r| r.authority_generation == authority_generation)
        else {
            return refuse("seed_authority_rotation_unknown");
        };
        let row = &self.rotations[slot];
        let previous = row.transition["previous_generation"].as_u64();
        if row.transition["new_seed_key_digest"] != new_seed_key_digest
            || previous.map(|p| p + 1) != Some(authority_generation)
        {
            return refuse("seed_authority_rotation_stale");
        }
        if !row.pending {
            if self.authority_generation != authority_generation
                || seed_key_digest != new_seed_key_digest
            {
                return refuse("seed_state_corrupt");
            }
            return Ok(Completion::AlreadyCompleted);
        }
        if Some(self.authority_generation) != previous {
            return refuse("seed_authority_rotation_stale");
        }
        self.authority_generation = authority_generation;
        self.rotations[slot].pending = false;
        Ok(Completion::Applied)
    }

    /// `SqliteSeedState.seed_rotation_acknowledgements`: the durable acknowledgement rows of one authority
    /// generation ordered by node id.
    pub fn rotation_acknowledgements(&self, authority_generation: u64) -> Vec<Value> {
        let mut rows: Vec<&AckRow> = self
            .acks
            .iter()
            .filter(|a| a.authority_generation == authority_generation)
            .collect();
        rows.sort_by(|a, b| a.node_id.cmp(&b.node_id));
        rows.iter()
            .map(|a| {
                json!({
                    "node_id": a.node_id,
                    "member_generation": a.member_generation,
                    "authority_generation": a.authority_generation,
                    "transition_digest": a.transition_digest,
                    "message_id": a.message_id,
                    "acknowledged_at": float_value(a.acknowledged_at),
                })
            })
            .collect()
    }

    /// `save_seed_rotation_acknowledgement`'s `INSERT .. ON CONFLICT(node_id, authority_generation) DO UPDATE`.
    fn save_acknowledgement(&mut self, message: &Value) {
        let row = AckRow {
            node_id: message["sender_node_id"]
                .as_str()
                .unwrap_or_default()
                .to_owned(),
            member_generation: message["generation"].as_u64().unwrap_or_default(),
            authority_generation: message["authority_generation"].as_u64().unwrap_or_default(),
            transition_digest: message["transition_digest"]
                .as_str()
                .unwrap_or_default()
                .to_owned(),
            message_id: message["message_id"]
                .as_str()
                .unwrap_or_default()
                .to_owned(),
            acknowledged_at: message["issued_at"].as_f64().unwrap_or_default(),
        };
        match self.acks.iter().position(|a| {
            a.node_id == row.node_id && a.authority_generation == row.authority_generation
        }) {
            Some(slot) => self.acks[slot] = row,
            None => self.acks.push(row),
        }
    }

    /// `SqliteSeedState.authority_state`: the current authority generation and the latest rotation.
    pub fn authority_state(&self) -> Value {
        let latest = self.rotations.iter().max_by_key(|r| r.authority_generation);
        let rotation = latest.map_or(Value::Null, |r| {
            let t = &r.transition;
            let real = |name: &str| t[name].as_f64().map_or(Value::Null, float_value);
            json!({
                "authority_generation": r.authority_generation,
                "previous_generation": t["previous_generation"],
                "old_seed_key_digest": t["old_seed_key_digest"],
                "new_seed_key_digest": t["new_seed_key_digest"],
                "initiated_at": real("initiated_at"),
                "effective_at": real("effective_at"),
                "overlap_expires_at": real("overlap_expires_at"),
                "reason": t["reason"],
                "transition": t,
                "status": if r.pending { "PENDING" } else { "COMPLETED" },
            })
        });
        json!({ "authority_generation": self.authority_generation, "rotation": rotation })
    }

    fn purge_expired(&mut self, now: f64) {
        self.replay.retain(|row| row.expires_at >= now);
    }

    fn seen(&self, node_id: &str, generation: u64, message_id: &str) -> bool {
        self.replay.iter().any(|row| {
            row.node_id == node_id && row.generation == generation && row.message_id == message_id
        })
    }

    fn replay_count(&self, node_id: &str, generation: u64) -> usize {
        self.replay
            .iter()
            .filter(|row| row.node_id == node_id && row.generation == generation)
            .count()
    }

    fn insert_replay(&mut self, node_id: &str, generation: u64, message_id: &str, expires_at: f64) {
        self.replay.push(ReplayRow {
            node_id: node_id.to_owned(),
            generation,
            message_id: message_id.to_owned(),
            expires_at,
        });
    }

    /// `_remember`: purge expired rows, enforce the window, record the id.
    fn remember(
        &mut self,
        node_id: &str,
        generation: u64,
        message: &Value,
        now: f64,
    ) -> Result<(), Refusal> {
        let id = message["message_id"].as_str().unwrap_or_default();
        let expires_at = message["expires_at"].as_f64().unwrap_or_default();
        self.remember_id(node_id, generation, id, expires_at, now)
    }

    /// The replay record every member message shares (`seed_replay`): purge expired rows, enforce the
    /// per-generation window, then insert; a repeated id is a replay.
    pub(crate) fn remember_id(
        &mut self,
        node_id: &str,
        generation: u64,
        message_id: &str,
        expires_at: f64,
        now: f64,
    ) -> Result<(), Refusal> {
        // `remember_member_message` is one transaction: the purge of expired rows and the insert commit together, and
        // a refusal rolls the purge back. The checks therefore look at the rows that would survive the purge and
        // change nothing when they refuse.
        let live = |row: &&ReplayRow| row.expires_at >= now;
        let same_member = |row: &&ReplayRow| row.node_id == node_id && row.generation == generation;
        let rows = self.replay.iter().filter(same_member).filter(live);
        if rows.clone().count() >= MAX_REPLAY_IDS {
            return refuse("seed_replay_window_full");
        }
        if rows.into_iter().any(|row| row.message_id == message_id) {
            return refuse("seed_message_replayed");
        }
        self.purge_expired(now);
        self.insert_replay(node_id, generation, message_id, expires_at);
        Ok(())
    }

    /// The member row of one node, if it joined.
    pub(crate) fn member_row(&self, node_id: &str) -> Option<&Tracked> {
        self.index(node_id).map(|i| &self.members[i])
    }

    fn renewal_row(
        &self,
        node_id: &str,
        generation: u64,
        heartbeat_message_id: &str,
    ) -> Option<&RenewalRow> {
        self.renewals.iter().find(|r| {
            r.node_id == node_id
                && r.generation == generation
                && r.heartbeat_message_id == heartbeat_message_id
        })
    }

    /// `lease_renewal`: the renewal the seed committed for one heartbeat (unsigned message fields).
    pub fn renewal(&self, node_id: &str, heartbeat_message_id: &str) -> Result<Value, Refusal> {
        if !is_segment(node_id) || !is_segment(heartbeat_message_id) {
            return refuse("seed_argument_invalid");
        }
        let Some(index) = self.index(node_id) else {
            return refuse("seed_member_unknown");
        };
        match self.renewal_row(
            node_id,
            self.members[index].member.generation,
            heartbeat_message_id,
        ) {
            Some(row) => Ok(row.renewal.clone()),
            None => refuse("seed_lease_renewal_unknown"),
        }
    }

    /// `_apply_committed_heartbeat`.
    fn apply_heartbeat(
        &mut self,
        index: usize,
        heartbeat: &Value,
        renewal: &Value,
        timing: &Timing,
    ) {
        let sequence = heartbeat["heartbeat_sequence"].as_u64().unwrap_or_default();
        let t = &mut self.members[index];
        if sequence >= t.last_heartbeat_sequence {
            let committed_at = renewal["issued_at"].as_f64().unwrap_or_default();
            let receipt = heartbeat["liveness_source"] == "activation_receipt";
            let intervals = if receipt { 2.0 } else { 1.0 };
            t.last_heartbeat_sequence = sequence;
            t.member.lease_expires_at = t
                .member
                .lease_expires_at
                .max(renewal["lease_expires_at"].as_f64().unwrap_or_default());
            t.last_liveness_at = committed_at;
            t.next_heartbeat_due_at = committed_at + intervals * timing.keepalive_interval_seconds;
            t.active_requests = heartbeat["active_requests"].as_u64().unwrap_or_default();
            t.member.lifecycle_state = heartbeat["lifecycle_state"]
                .as_str()
                .unwrap_or_default()
                .to_owned();
            if receipt {
                t.last_activity_receipt_at = Some(committed_at);
            }
        }
    }
}

pub(crate) fn activation_eligible(m: &Member) -> bool {
    membership::peer_runtime_is_activation_eligible(
        &Value::String(m.peer_class.clone()),
        &m.runtime_capability,
    )
    .unwrap_or(false)
}

/// `receive_member_message`. `expected_protocol` is one of the six member protocols; the message the seed
/// verified is returned. A heartbeat commits a lease renewal (one id asked of the id source), readable
/// through [`SeedTable::renewal`].
pub fn receive_member_message(
    table: &mut SeedTable,
    coord: &Coordinator<'_>,
    envelope: &Value,
    expected_protocol: &str,
    now: f64,
) -> Result<Value, Refusal> {
    let (frame, timing) = (&coord.frame, &coord.timing);
    if !MEMBER_PROTOCOLS.contains(&expected_protocol) {
        return refuse("expected_protocol_invalid");
    }
    let Some(node_id) = envelope["message"]["sender_node_id"].as_str() else {
        return refuse("seed_message_malformed");
    };
    let Some(index) = table.index(node_id) else {
        return refuse("seed_member_unknown");
    };
    if !now.is_finite() {
        return refuse("seed_clock_invalid");
    }
    let heartbeat = expected_protocol == HEARTBEAT_PROTOCOL;
    let generation = table.members[index].member.generation;
    let digest = table.members[index].member.verification_key_digest.clone();
    if !heartbeat && now >= table.members[index].member.lease_expires_at {
        return refuse("seed_member_lease_expired");
    }
    let mut verification_now = now;
    if heartbeat
        && let Some(expiry) = envelope["message"]["expires_at"].as_f64()
        && expiry.is_finite()
    {
        verification_now = now.min(expiry);
    }
    let message = membership::verify(
        envelope,
        membership::Expected {
            protocol: expected_protocol,
            key_digest: &digest,
            swarm_id: None,
            sender_node_id: None,
            sender_endpoint_id: None,
            recipient_node_id: None,
        },
        verification_now,
        frame.limits,
    )
    .map_err(|e| SeedCoordinatorError(e.0))?;
    let message_id = message["message_id"]
        .as_str()
        .unwrap_or_default()
        .to_owned();

    let mut request_digest = String::new();
    if heartbeat {
        table.ensure_current(index)?;
        request_digest =
            digest_of(envelope).ok_or(SeedCoordinatorError("seed_state_unavailable"))?;
        let sequence = message["heartbeat_sequence"].as_u64().unwrap_or_default();
        if let Some(row) = table.renewal_row(node_id, generation, &message_id) {
            if row.request_digest != request_digest || row.sequence != sequence {
                return refuse("seed_heartbeat_retry_mismatch");
            }
            let renewal = row.renewal.clone();
            table.apply_heartbeat(index, &message, &renewal, timing);
            return Ok(message);
        }
    }
    {
        let m = &table.members[index].member;
        if message["swarm_id"] != frame.swarm_id
            || message["sender_node_id"] != m.node_id.as_str()
            || message["sender_endpoint_id"] != m.endpoint_id.as_str()
            || message["incarnation"] != m.incarnation.as_str()
            || message["generation"] != json!(m.generation)
            || message["recipient_node_id"] != frame.seed_node_id
        {
            return refuse("seed_message_mismatch");
        }
    }
    if !heartbeat {
        table.ensure_current(index)?;
    }

    let mut renewal: Option<Value> = None;
    let mut result_update: Option<(usize, bool)> = None;
    let mut save_ack = false;
    if heartbeat {
        if now > message["expires_at"].as_f64().unwrap_or_default() {
            return refuse("membership_message_expired");
        }
        let t = &table.members[index];
        if now >= t.member.lease_expires_at {
            return refuse("seed_member_lease_expired");
        }
        let next = message["lifecycle_state"].as_str().unwrap_or_default();
        match lifecycle_rank(next) {
            Some(rank)
                if lifecycle_rank(&t.member.lifecycle_state).is_none_or(|have| rank >= have) => {}
            _ => return refuse("seed_member_lifecycle_regression"),
        }
        let sequence = message["heartbeat_sequence"].as_u64().unwrap_or_default();
        if sequence <= t.last_heartbeat_sequence {
            return refuse("seed_heartbeat_sequence_stale");
        }
        let renewed_until = bounded_deadline(
            now,
            frame.lease_seconds,
            Some(t.member.lease_expires_at),
            false,
            frame.limits.max_ttl_seconds,
        )?;
        let renewal_id = table.draw_emitted()?;
        // commit_heartbeat_renewal: purge, replay and window checks before the row is written.
        table.purge_expired(now);
        if table.seen(node_id, generation, &message_id) {
            return refuse("seed_message_replayed");
        }
        if table.replay_count(node_id, generation) >= MAX_REPLAY_IDS {
            return refuse("seed_replay_window_full");
        }
        let m = &table.members[index].member;
        let renewal_message = json!({
            "protocol": LEASE_RENEWAL_PROTOCOL,
            "message_id": renewal_id,
            "swarm_id": frame.swarm_id,
            "sender_node_id": frame.seed_node_id,
            "sender_endpoint_id": coord.seed_endpoint_id,
            "recipient_node_id": m.node_id,
            "incarnation": coord.seed_incarnation,
            "generation": m.generation,
            "issued_at": float_value(now),
            "expires_at": float_value(renewed_until),
            "heartbeat_message_id": message_id,
            "member_incarnation": m.incarnation,
            "membership_generation": m.generation,
            "lease_expires_at": float_value(renewed_until),
        });
        let expires_at = message["expires_at"].as_f64().unwrap_or_default();
        table.insert_replay(node_id, generation, &message_id, expires_at);
        table.emitted.insert(renewal_id);
        table.renewals.push(RenewalRow {
            node_id: node_id.to_owned(),
            generation,
            heartbeat_message_id: message_id.clone(),
            request_digest: request_digest.clone(),
            sequence,
            renewal: renewal_message.clone(),
        });
        renewal = Some(renewal_message);
    } else if expected_protocol == ASSIGNMENT_RESULT_PROTOCOL {
        if table.seen(node_id, generation, &message_id) {
            return refuse("seed_message_replayed");
        }
        let assignment_id = message["assignment_id"].as_str().unwrap_or_default();
        let found = table
            .assignments
            .iter()
            .position(|a| a.assignment_id == assignment_id);
        let Some(slot) = found.filter(|&i| {
            let a = &table.assignments[i];
            a.node_id == node_id
                && message["deployment_id"] == a.deployment_id.as_str()
                && message["deployment_epoch"] == json!(a.deployment_epoch)
                && a.membership_generation == generation
        }) else {
            return refuse("seed_assignment_result_mismatch");
        };
        if table.assignments[slot].accepted.is_some() {
            return refuse("seed_assignment_result_already_recorded");
        }
        result_update = Some((slot, message["accepted"] == true));
    } else if expected_protocol == SEED_ROTATION_ACK_PROTOCOL {
        if table.seen(node_id, generation, &message_id) {
            return refuse("seed_message_replayed");
        }
        let rotation = table
            .rotations
            .iter()
            .find(|r| json!(r.authority_generation) == message["authority_generation"]);
        match rotation {
            Some(r) if r.pending => {
                if message["transition_digest"] != r.transition_digest.as_str() {
                    return refuse("seed_authority_rotation_ack_mismatch");
                }
            }
            _ => return refuse("seed_authority_rotation_unknown"),
        }
        // `seed_rotation_acknowledgements.message_id` is UNIQUE: another row already holding this id (another
        // node, or another authority generation) makes the INSERT raise `sqlite3.IntegrityError`, which the
        // source reports as `seed_state_unavailable` before anything is remembered.
        let generation_of_message = message["authority_generation"].as_u64().unwrap_or_default();
        if table.acks.iter().any(|a| {
            a.message_id == message_id
                && !(a.node_id == node_id && a.authority_generation == generation_of_message)
        }) {
            return refuse("seed_state_unavailable");
        }
        save_ack = true;
    } else if table.seen(node_id, generation, &message_id) {
        return refuse("seed_message_replayed");
    }
    if save_ack {
        table.save_acknowledgement(&message);
    }
    if !heartbeat && let Err(refusal) = table.remember(node_id, generation, &message, now) {
        // `save_seed_rotation_acknowledgement` committed before `_remember` refused (a full replay window): the
        // acknowledgement row stays and the call still refuses (RU-035).
        table.row_kept_by_refusal = save_ack;
        return Err(refusal);
    }
    if let Some(renewal) = renewal {
        table.apply_heartbeat(index, &message, &renewal, timing);
    }
    if let Some((slot, accepted)) = result_update {
        table.assignments[slot].accepted = Some(accepted);
    }
    Ok(message)
}

/// `advance_member_generation`: durably fence a member into STOPPING or STOPPED under the next generation.
pub fn advance_member_generation(
    table: &mut SeedTable,
    node_id: &str,
    expected_generation: u64,
    lifecycle_state: &str,
    now: f64,
    timing: &Timing,
) -> Result<Value, Refusal> {
    if !is_segment(node_id)
        || expected_generation < 1
        || !matches!(lifecycle_state, "STOPPING" | "STOPPED")
    {
        return refuse("seed_argument_invalid");
    }
    let Some(index) = table.index(node_id) else {
        return refuse("seed_member_unknown");
    };
    table.ensure_current(index)?;
    if table.members[index].member.generation != expected_generation {
        return refuse("seed_member_generation_stale");
    }
    let t = &mut table.members[index];
    t.member.generation += 1;
    t.last_liveness_at = now;
    t.next_heartbeat_due_at = now;
    t.active_requests = 0;
    t.member.lifecycle_state = lifecycle_state.to_owned();
    Ok(table
        .projection(node_id, now, timing)
        .unwrap_or(Value::Null))
}

/// `member_authority_guard` up to the point where it yields the projection to its caller.
pub fn member_authority_guard(
    table: &SeedTable,
    node_id: &str,
    expected_generation: u64,
    expected_peer_class: &str,
    eligible_lifecycle_states: &[&str],
    now: f64,
    timing: &Timing,
) -> Result<Value, Refusal> {
    if !is_segment(node_id)
        || !is_segment(expected_peer_class)
        || expected_generation < 1
        || eligible_lifecycle_states.is_empty()
        || !eligible_lifecycle_states.iter().all(|s| is_segment(s))
    {
        return refuse("seed_argument_invalid");
    }
    let Some(index) = table.index(node_id) else {
        return refuse("seed_member_unknown");
    };
    let m = &table.members[index].member;
    if m.generation != expected_generation {
        return refuse("seed_member_generation_stale");
    }
    if m.peer_class != expected_peer_class {
        return refuse("seed_member_peer_class_mismatch");
    }
    if now >= m.lease_expires_at {
        return refuse("seed_member_lease_expired");
    }
    if !eligible_lifecycle_states.contains(&m.lifecycle_state.as_str()) {
        return refuse("seed_member_lifecycle_ineligible");
    }
    Ok(table
        .projection(node_id, now, timing)
        .unwrap_or(Value::Null))
}

/// The lease / liveness / activation gate an offer applies to its member and, with the `seed_peer_*` codes,
/// to every peer.
fn liveness_gate(
    t: &Tracked,
    now: f64,
    timing: &Timing,
    codes: [&'static str; 4],
) -> Result<(), Refusal> {
    if now >= t.member.lease_expires_at {
        return refuse(codes[0]);
    }
    let liveness = SeedTable::liveness(t, now, timing);
    if liveness["liveness_dead"] == true {
        return refuse(codes[1]);
    }
    if !matches!(
        liveness["liveness_status"].as_str(),
        Some("idle_fresh" | "active_fresh")
    ) {
        return refuse(codes[2]);
    }
    if !activation_eligible(&t.member) {
        return refuse(codes[3]);
    }
    Ok(())
}

/// `assignment_offer`: the unsigned offer message; the offer is recorded as an open assignment.
pub fn assignment_offer(
    table: &mut SeedTable,
    coord: &Coordinator<'_>,
    offer: &Offer<'_>,
    now: f64,
) -> Result<Value, Refusal> {
    let (frame, timing) = (&coord.frame, &coord.timing);
    if !is_segment(offer.node_id) || !is_segment(offer.assignment_id) {
        return refuse("seed_argument_invalid");
    }
    let Some(index) = table.index(offer.node_id) else {
        return refuse("seed_member_unknown");
    };
    if !now.is_finite() {
        return refuse("seed_clock_invalid");
    }
    liveness_gate(
        &table.members[index],
        now,
        timing,
        [
            "seed_member_lease_expired",
            "seed_member_liveness_dead",
            "seed_member_liveness_stale",
            "seed_member_activation_ineligible",
        ],
    )?;
    let ids = offer.peer_node_ids;
    let distinct: BTreeSet<&String> = ids.iter().collect();
    if ids.len() > MAX_OFFER_PEERS
        || !ids.iter().all(|p| is_segment(p))
        || distinct.len() != ids.len()
        || ids.iter().any(|p| p == offer.node_id)
    {
        return refuse("seed_argument_invalid");
    }
    let mut sorted: Vec<&String> = ids.iter().collect();
    sorted.sort();
    let mut peers: Vec<usize> = Vec::new();
    for peer_id in sorted {
        let Some(p) = table.index(peer_id) else {
            return refuse("seed_peer_unknown");
        };
        liveness_gate(
            &table.members[p],
            now,
            timing,
            [
                "seed_peer_lease_expired",
                "seed_peer_liveness_dead",
                "seed_peer_liveness_stale",
                "seed_peer_activation_ineligible",
            ],
        )?;
        peers.push(p);
    }
    if table
        .assignments
        .iter()
        .any(|a| a.assignment_id == offer.assignment_id)
    {
        return refuse("seed_assignment_exists");
    }
    let member = &table.members[index].member;
    let mut expires_at = bounded_deadline(
        now,
        frame.message_ttl_seconds,
        None,
        false,
        frame.limits.max_ttl_seconds,
    )?
    .min(member.lease_expires_at);
    for &p in &peers {
        expires_at = expires_at.min(table.members[p].member.lease_expires_at);
    }
    let records: Vec<Value> = peers
        .iter()
        .map(|&p| {
            let peer = &table.members[p].member;
            json!({
                "node_id": peer.node_id,
                "endpoint_id": peer.endpoint_id,
                "deployment_epoch": offer.deployment_epoch,
                "membership_generation": peer.generation,
                "valid_from": float_value(now),
                "valid_until": float_value(expires_at),
            })
        })
        .collect();
    let (generation, recipient) = (member.generation, member.node_id.clone());
    let message_id = table.draw_emitted()?;
    let message = json!({
        "protocol": ASSIGNMENT_OFFER_PROTOCOL,
        "message_id": message_id,
        "swarm_id": frame.swarm_id,
        "sender_node_id": frame.seed_node_id,
        "sender_endpoint_id": coord.seed_endpoint_id,
        "recipient_node_id": recipient,
        "incarnation": coord.seed_incarnation,
        "generation": generation,
        "issued_at": float_value(now),
        "expires_at": float_value(expires_at),
        "deployment_id": offer.deployment_id,
        "deployment_epoch": offer.deployment_epoch,
        "assignment_id": offer.assignment_id,
        "assignment_digest": offer.assignment_digest,
        "stage_pack_digest": offer.stage_pack_digest,
        "graph_digest": offer.graph_digest,
        "load_generation": offer.load_generation,
        "placement_provenance": offer.placement_provenance,
        "peer_endpoint_records": records,
    });
    table.emitted.insert(message_id);
    membership::validate(&message, frame.limits).map_err(|e| SeedCoordinatorError(e.0))?;
    table.assignments.push(Assignment {
        node_id: offer.node_id.to_owned(),
        deployment_id: offer.deployment_id.to_owned(),
        deployment_epoch: offer.deployment_epoch,
        membership_generation: generation,
        assignment_id: offer.assignment_id.to_owned(),
        accepted: None,
    });
    Ok(message)
}
