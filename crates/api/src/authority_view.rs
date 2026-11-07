// SPDX-License-Identifier: AGPL-3.0-only
//! The owner `authority` block of `GET /api/status` for a swarm that already exists on disk, read
//! from the files v0.1.3/v0.1.4 persist: `authority/authority.json` (`DemoAuthority._metadata`,
//! `authority.py:638-705`) and the `seed_members` table of `authority/seed/state.sqlite3`
//! (`coordinator.members()`), plus the idle `owner_run` block and the `/api/create` answer
//! (`app.py:1250-1284`).
//!
//! Read-only. This host has no seed coordinator, so nothing RESUMES the member: v0.1.4 advances the
//! generation and renews the lease when it restarts on a state root (executed oracle:
//! `oracle_authority.json`, variant `baseline`, generation 1 -> 2, `lease_state` fresh); this view
//! reports the stored generation and a lease judged against the clock. `membership_state` is the
//! source's rule: `revoked` for a `STOPPING`/`STOPPED` member, `current` otherwise.
use crate::{jobj, preferences::private_state, schema::PublicError, wire::Json};
use rusqlite::{Connection, OpenFlags};
use serde_json::Value;
use std::path::Path;

const STATE_PROTOCOL: &str = "mycelium.demo_authority_state.v1";
const STATUS_PROTOCOL: &str = "mycelium.demo_authority_status.v1";
const RESULT_PROTOCOL: &str = "mycelium.demo_membership_result.v1";
/// `managed_control.MAX_CLOCK_SKEW_SECONDS`, shown as `clock.tolerance_seconds` (`clock_offset.py:93`).
const CLOCK_TOLERANCE_SECONDS: f64 = 30.0;

/// One `seed_members` row, the columns `status()` reads.
#[derive(Clone, Debug, PartialEq)]
pub struct Member {
    pub node_id: String,
    pub generation: u64,
    pub lifecycle_state: String,
    pub lease_expires_at: f64,
}
/// A persisted owner authority.
#[derive(Clone, Debug, PartialEq)]
pub struct PersistedAuthority {
    pub swarm_id: String,
    pub target_node_id: String,
    pub contribution_enabled: bool,
    pub members: Vec<Member>,
}
fn refuse(code: &'static str) -> PublicError {
    PublicError::new(409, code).raised_by(crate::schema::Raised::Authority)
}
impl PersistedAuthority {
    /// `None`: no swarm was ever created here (`metadata is None`).
    pub fn load(root: &Path) -> Result<Option<Self>, PublicError> {
        let dir = root.join("authority");
        let limit = private_state::private_json_limit().unwrap_or(0);
        let Some(meta) = private_state::load(&dir.join("authority.json"), limit)
            .map_err(|e| PublicError::new(409, e.0).raised_by(crate::schema::Raised::Authority))?
        else {
            return Ok(None);
        };
        let text = |k: &str| meta.get(k).and_then(Value::as_str).map(str::to_owned);
        let (Some(swarm_id), Some(target)) = (text("swarm_id"), text("target_node_id")) else {
            return Err(refuse("authority_state_invalid"));
        };
        if text("protocol").as_deref() != Some(STATE_PROTOCOL) {
            return Err(refuse("authority_state_invalid"));
        }
        let contribution_enabled = meta
            .get("consent")
            .and_then(|c| c.get("contribution_enabled"))
            .and_then(Value::as_bool)
            .ok_or_else(|| refuse("authority_state_invalid"))?;
        let members = read_members(&dir.join("seed").join("state.sqlite3"))?;
        // the source refuses to start on a state whose member is gone (resume: `seed_member_unknown`)
        // or whose target is not among the rows (`authority_membership_state_invalid`)
        if members.is_empty() {
            return Err(refuse("seed_member_unknown"));
        }
        if !members.iter().any(|m| m.node_id == target) {
            return Err(refuse("authority_membership_state_invalid"));
        }
        Ok(Some(Self {
            swarm_id,
            target_node_id: target,
            contribution_enabled,
            members,
        }))
    }
    fn target(&self) -> Option<&Member> {
        self.members
            .iter()
            .find(|m| m.node_id == self.target_node_id)
    }
    fn membership_state(&self) -> &'static str {
        match self.target() {
            Some(m) if matches!(m.lifecycle_state.as_str(), "STOPPING" | "STOPPED") => "revoked",
            Some(_) => "current",
            None => "unenrolled",
        }
    }
    /// `DemoAuthority.status()` (no `members`): what `POST /api/create` answers for a swarm that is
    /// already `current` (`app.py:1608`).
    pub fn authority_status(&self, started: bool, now: f64) -> Json {
        let mut status = self.status(started, now);
        if let Json::Obj(fields) = &mut status {
            fields.retain(|(k, _)| k != "members");
        }
        status
    }
    /// `DemoAuthority.status()` + the `members` list `DemoApp.status()` adds for an owner with a
    /// coordinator, in the source's key order.
    pub fn status(&self, started: bool, now: f64) -> Json {
        let target = self.target();
        let lease = target.map_or("none", |m| {
            if m.lease_expires_at > now {
                "fresh"
            } else {
                "expired"
            }
        });
        let members: Vec<Json> = self
            .members
            .iter()
            .map(|m| {
                jobj! {
                    "node_id" => m.node_id.as_str(), "generation" => m.generation,
                    "lifecycle_state" => m.lifecycle_state.as_str(),
                    "clock" => jobj! {
                        "offset_seconds" => Json::Null, "exceeds_tolerance" => false,
                        "tolerance_seconds" => CLOCK_TOLERANCE_SECONDS, "samples" => 0u64,
                    },
                }
            })
            .collect();
        jobj! {
            "protocol" => STATUS_PROTOCOL,
            "started" => started,
            "swarm_created" => true,
            "local_tls" => "pinned_loopback",
            "membership_state" => self.membership_state(),
            "member_count" => self.members.len(),
            "capability_state" => "not_reported",
            "capability" => Json::Null,
            "membership_generation" => target.map_or(Json::Null, |m| Json::from(m.generation)),
            "lease_state" => lease,
            "contribution_enabled" => self.contribution_enabled,
            "route_ready" => false,
            "members" => Json::Arr(members),
        }
    }
    /// `_membership_result` (the `POST /api/create` answer).
    pub fn membership_result(&self) -> Json {
        let target = self.target();
        jobj! {
            "protocol" => RESULT_PROTOCOL,
            "membership_state" => self.membership_state(),
            "membership_generation" => target.map_or(Json::Null, |m| Json::from(m.generation)),
            "lifecycle_state" => target.map_or("", |m| m.lifecycle_state.as_str()),
            "route_ready" => false,
        }
    }
}
/// `DemoAuthority.status()` of an owner whose authority was stopped (no coordinator): `members` is not
/// listed and the retained membership is all that is left (`authority.py:638-690` @ f4e366e9). With no
/// retained membership the swarm is `unenrolled`.
pub fn retained_status(generation: Option<u64>) -> Json {
    let (state, lease) = match generation {
        Some(_) => ("retained", "unknown"),
        None => ("unenrolled", "none"),
    };
    jobj! {
        "protocol" => STATUS_PROTOCOL, "started" => false, "swarm_created" => true,
        "local_tls" => "inactive", "membership_state" => state, "member_count" => 0u64,
        "capability_state" => "not_reported", "capability" => Json::Null,
        "membership_generation" => generation.map_or(Json::Null, Json::from),
        "lease_state" => lease, "contribution_enabled" => false, "route_ready" => false,
    }
}
/// The authority block of an owner with no swarm (`metadata is None`).
pub fn no_swarm_status(started: bool) -> Json {
    jobj! {
        "protocol" => STATUS_PROTOCOL, "started" => started, "swarm_created" => false,
        "local_tls" => "inactive", "membership_state" => "absent", "member_count" => 0u64,
        "capability_state" => "not_reported", "capability" => Json::Null,
        "membership_generation" => Json::Null, "lease_state" => "none",
        "contribution_enabled" => false, "route_ready" => false,
    }
}
fn read_members(path: &Path) -> Result<Vec<Member>, PublicError> {
    let invalid = || refuse("authority_state_invalid");
    let connection = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|_| invalid())?;
    let mut statement = connection
        .prepare(
            "select node_id, generation, lifecycle_state, lease_expires_at from seed_members order by node_id",
        )
        .map_err(|_| invalid())?;
    let rows = statement
        .query_map([], |row| {
            Ok(Member {
                node_id: row.get(0)?,
                generation: row.get::<_, i64>(1)?.try_into().unwrap_or(0),
                lifecycle_state: row.get(2)?,
                lease_expires_at: row.get(3)?,
            })
        })
        .map_err(|_| invalid())?;
    rows.collect::<Result<Vec<_>, _>>().map_err(|_| invalid())
}
/// `_demo_status(self, 'owner')` of an owner that never started a run (`demo_app`): the ten pending
/// steps of the run checklist.
pub fn idle_owner_run() -> Json {
    const STEPS: [(&str, &str); 10] = [
        ("model", "Model ready on this Mac"),
        ("contributor", "This Mac joins the run"),
        ("descriptors", "Joined Macs report in"),
        ("observe", "Native hello on every Mac"),
        ("compile", "Compile the route over the current members"),
        ("slices", "Every Mac fetches only its own slice"),
        ("publish", "Publish work to every Mac"),
        ("targets", "Every Mac accepts and starts its slice"),
        ("activate", "Activate and qualify the route"),
        ("serving", "Serving prompts"),
    ];
    let steps: Vec<Json> = STEPS
        .iter()
        .map(|(name, label)| {
            jobj! {"name" => *name, "label" => *label, "state" => "pending", "detail" => Json::Null}
        })
        .collect();
    jobj! {
        "state" => "idle", "step" => Json::Null, "error_code" => Json::Null,
        "error_message" => Json::Null, "failed_step" => Json::Null, "steps" => Json::Arr(steps),
        "participants" => Json::Null, "registry_origin" => Json::Null, "route_ready" => false,
        "run_id" => Json::Null,
    }
}
