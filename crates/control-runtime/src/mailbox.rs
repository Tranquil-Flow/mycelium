// SPDX-License-Identifier: AGPL-3.0-only
//! SQLite dispatch boundary for signed managed commands. All membership checks,
//! claim transitions and signed cached replies share the same IMMEDIATE transaction.
//! Native/process/network effects NEVER run here. Claims are delivery, not execution
//! authority. Source: managed_control.py:1132-1800 @ f4e366e9.
use crate::{AuthenticatedStatement, Error, Result, Store, VerifiedRequest};
pub use protocol::identity::Identity;
use rusqlite::{Connection, OptionalExtension, Transaction, params};
use serde_json::{Value, json};
use std::sync::Arc;
mod delivery;
mod effects;
mod legacy;
mod staging;
pub use delivery::DeliveryWake;
pub use effects::{DispatchEffects, DurableEffect};
pub use staging::{StagePolicy, StagePublication};
const RESULT: &str = "mycelium.managed_control.owner_result.v1";
const OWNER: &str = "mycelium.managed_control.owner_command.v1";
const POLL: &str = "mycelium.managed_control.target_poll.v1";
const POLL2: &str = "mycelium.managed_control.target_poll.v2";
const ACK: &str = "mycelium.managed_control.target_ack.v1";
const BINDING: [&str; 7] = [
    "target_node_id",
    "target_endpoint_id",
    "target_key_digest",
    "membership_generation",
    "member_incarnation",
    "run_id",
    "deployment_id",
];
#[derive(Clone, Copy)]
pub struct Limits {
    pub pending: budgets::Entries,
    pub active_work: budgets::Entries,
    pub active_control: budgets::Entries,
}
pub struct Mailbox {
    stage_policy: Option<StagePolicy>,
    wake: std::sync::Arc<DeliveryWake>,
    store: Store,
    owner: Arc<Identity>,
    swarm: String,
    owner_node: String,
    limits: Limits,
}
fn require(ok: bool, code: &'static str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(Error::Refused(code))
    }
}
fn contract(e: protocol::ContractError) -> Error {
    Error::Refused(e.0)
}
fn bytes(v: &Value) -> Result<Vec<u8>> {
    protocol::canonical::to_bytes(v).map_err(|_| Error::Refused("managed_control_json_invalid"))
}
fn digest(v: &Value) -> Result<String> {
    protocol::canonical::digest(v).map_err(|_| Error::Refused("managed_control_json_invalid"))
}
fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    v[key]
        .as_str()
        .ok_or(Error::Refused("managed_control_fields_invalid"))
}
fn segment(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 128
        && s.as_bytes()[0].is_ascii_alphanumeric()
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
}
fn binding(v: &Value) -> Value {
    Value::Object(
        BINDING
            .iter()
            .map(|k| ((*k).into(), v[*k].clone()))
            .collect(),
    )
}
fn work(v: &Value) -> bool {
    matches!(
        v["statement"]["native_command"]["command"].as_str(),
        Some(
            "configure"
                | "start"
                | "infer_start"
                | "infer_decode"
                | "rotate"
                | "install_token_path"
        )
    )
}
fn priority(v: &Value) -> u8 {
    let n = &v["statement"]["native_command"];
    if matches!(
        n["command"].as_str(),
        Some("infer_cancel_wait" | "infer_cancel" | "cancel" | "stop")
    ) || (n["command"] == "snapshot" && n["payload"].get("cleanup_subject").is_some())
    {
        0
    } else if work(v) {
        2
    } else {
        1
    }
}
impl Mailbox {
    /// Limits are injected from the L1 registry by native composition, not defaults.
    pub fn new(
        store: Store,
        owner: Identity,
        swarm: &str,
        owner_node: &str,
        limits: Limits,
    ) -> Result<Self> {
        Self::new_shared(store, Arc::new(owner), swarm, owner_node, limits)
    }
    /// Retain the original immutable signer, using the same identity binding and
    /// validation as [`Self::new`]. A retained signer is not current authority;
    /// the caller must still enforce seed currentness and revocation.
    pub fn new_shared(
        mut store: Store,
        owner: Arc<Identity>,
        swarm: &str,
        owner_node: &str,
        limits: Limits,
    ) -> Result<Self> {
        require(
            segment(swarm)
                && segment(owner_node)
                && limits.pending.get() > 0
                && limits.active_work.get() > 0
                && limits.active_control.get() > 0,
            "managed_control_configuration_invalid",
        )?;
        let tx = store
            .connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        tx.execute_batch("CREATE TABLE IF NOT EXISTS control_mailbox_identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1),document BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS control_mailbox_members (node TEXT PRIMARY KEY,document BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS control_dispatch_receipts (command_id TEXT PRIMARY KEY REFERENCES managed_commands(command_id) ON DELETE CASCADE, receipt BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS control_dispatch (command_id TEXT PRIMARY KEY REFERENCES managed_commands(command_id) ON DELETE CASCADE, binding BLOB NOT NULL,envelope BLOB NOT NULL,sequence INTEGER NOT NULL,expires REAL NOT NULL,claim_token TEXT,terminal INTEGER NOT NULL DEFAULT 0,native BLOB);")?;
        let identity = bytes(
            &json!({"swarm_id":swarm,"owner_node_id":owner_node,"endpoint":owner.endpoint_id(),"key":owner.public_key_record()}),
        )?;
        let saved: Option<Vec<u8>> = tx
            .query_row(
                "SELECT document FROM control_mailbox_identity WHERE singleton=1",
                [],
                |r| r.get(0),
            )
            .optional()?;
        if let Some(saved) = saved {
            require(
                saved == identity,
                "managed_control_identity_binding_mismatch",
            )?;
        } else {
            // Existing Python metadata is independent of this extension and must agree.
            let legacy: bool = tx.query_row(
                "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE name='managed_metadata')",
                [],
                |r| r.get(0),
            )?;
            if legacy {
                for (k, v) in [
                    ("swarm_id", swarm),
                    ("owner_node_id", owner_node),
                    ("owner_endpoint_id", owner.endpoint_id()),
                    ("owner_key_digest", owner.verification_key_digest().as_str()),
                ] {
                    let value: Option<String> = tx
                        .query_row(
                            "SELECT value FROM managed_metadata WHERE key=?1",
                            [k],
                            |r| r.get(0),
                        )
                        .optional()?;
                    require(
                        value.as_deref() == Some(v),
                        "managed_control_identity_binding_mismatch",
                    )?;
                }
            }
            tx.execute(
                "INSERT INTO control_mailbox_identity VALUES (1,?1)",
                [identity],
            )?;
        }
        legacy::restore(&tx, &owner)?;
        effects::initialize(&tx)?;
        staging::initialize(&tx)?;
        tx.commit()?;
        Ok(Self {
            wake: Default::default(),
            stage_policy: None,
            store,
            owner,
            swarm: swarm.into(),
            owner_node: owner_node.into(),
            limits,
        })
    }
    /// Trusted membership adapter ONLY: caller verifies the L5 membership receipt.
    /// Publishing this snapshot and dispatch are serialized by the database lock.
    /// A signed request never supplies its own current binding/key/lease.
    pub fn bind_member(&mut self, b: &Value, key: &Value, expires: f64) -> Result<()> {
        require(
            b.as_object().is_some_and(|o| o.len() == BINDING.len())
                && BINDING.iter().all(|k| b.get(*k).is_some())
                && expires.is_finite(),
            "managed_control_member_binding_invalid",
        )?;
        require(
            key["verification_key_digest"] == b["target_key_digest"],
            "managed_control_key_pin_mismatch",
        )?;
        let doc = bytes(&json!({"binding":b,"key":key,"expires":expires}))?;
        self.store.connection.execute("INSERT INTO control_mailbox_members VALUES (?1,?2) ON CONFLICT(node) DO UPDATE SET document=excluded.document",params![text(b,"target_node_id")?,doc])?;
        Ok(())
    }
    pub fn revoke_member(&mut self, node: &str) -> Result<()> {
        self.store
            .connection
            .execute("DELETE FROM control_mailbox_members WHERE node=?1", [node])?;
        Ok(())
    }
    /// Authentication is performed anew under the write lock, even on exact retries.
    /// Claim tokens come from a cryptographic host source; tests inject deterministic tokens.
    pub fn dispatch(
        &mut self,
        raw: &[u8],
        clock: impl FnOnce() -> f64,
        token: impl FnOnce() -> Result<String>,
    ) -> Result<Vec<u8>> {
        self.dispatch_until(raw, clock, token, None)
    }
    fn dispatch_until(
        &mut self,
        raw: &[u8],
        clock: impl FnOnce() -> f64,
        token: impl FnOnce() -> Result<String>,
        deadline: Option<std::time::Instant>,
    ) -> Result<Vec<u8>> {
        self.dispatch_transaction(raw, clock, token, deadline, |_| Ok(()))
    }
    /// Atomically commit authenticated mailbox/replay and database-only state and
    /// outbox intents. No native IO in the callback; errors (including COMMIT)
    /// roll back every write. Cached replies do not invoke the callback. Empty
    /// replaceable polls do; consumers must inspect `response` and must never
    /// treat a poll, claim or staging acknowledgement as execution authority.
    /// Callbacks are trusted host code, not peer-supplied functions.
    pub fn dispatch_with_effects(
        &mut self,
        raw: &[u8],
        clock: impl FnOnce() -> f64,
        token: impl FnOnce() -> Result<String>,
        effect: impl FnOnce(&DispatchEffects<'_, '_>) -> Result<()>,
    ) -> Result<Vec<u8>> {
        self.dispatch_transaction(raw, clock, token, None, effect)
    }
    fn dispatch_transaction(
        &mut self,
        raw: &[u8],
        clock: impl FnOnce() -> f64,
        token: impl FnOnce() -> Result<String>,
        deadline: Option<std::time::Instant>,
        effect: impl FnOnce(&DispatchEffects<'_, '_>) -> Result<()>,
    ) -> Result<Vec<u8>> {
        let env = protocol::strict::object(raw, protocol::managed::MAX_FRAME_BYTES)
            .map_err(|_| Error::Refused("managed_control_json_invalid"))?;
        let tx = self
            .store
            .connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        require(
            deadline.is_none_or(|end| std::time::Instant::now() < end),
            "managed_control_deadline_elapsed",
        )?;
        let now = clock();
        require(now.is_finite(), "managed_control_clock_invalid")?;
        let s = &env["statement"];
        let kind = text(s, "protocol")?;
        require(
            [OWNER, RESULT, POLL, POLL2, ACK].contains(&kind),
            "managed_control_protocol_invalid",
        )?;
        let saved: Option<Vec<u8>> = tx
            .query_row(
                "SELECT document FROM control_mailbox_members WHERE node=?1",
                [text(s, "target_node_id")?],
                |r| r.get(0),
            )
            .optional()?;
        let member: Value =
            serde_json::from_slice(&saved.ok_or(Error::Refused("managed_control_member_stale"))?)
                .map_err(|_| Error::Refused("managed_control_state_corrupt"))?;
        require(
            member["expires"].as_f64().is_some_and(|end| now < end),
            "managed_control_member_stale",
        )?;
        for field in BINDING {
            require(
                s[field] == member["binding"][field],
                "managed_control_member_stale",
            )?;
        }
        require(
            s["swarm_id"] == self.swarm,
            "managed_control_authority_binding_invalid",
        )?;
        let owner_key = self.owner.public_key_record();
        let (key, endpoint) = if kind == OWNER || kind == RESULT {
            (&owner_key, self.owner.endpoint_id())
        } else {
            (
                &member["key"],
                text(&member["binding"], "target_endpoint_id")?,
            )
        };
        let statement = protocol::managed::verify(
            &env,
            kind,
            key,
            endpoint,
            now,
            control::managed_time_limits()?,
        )
        .map_err(contract)?;
        validate(&statement, kind, self.limits)?;
        if kind == OWNER || kind == RESULT {
            require(
                statement["owner_node_id"] == self.owner_node,
                "managed_control_authority_binding_invalid",
            )?;
        }
        let verified = VerifiedRequest {
            request: AuthenticatedStatement {
                statement,
                signer: text(key, "verification_key_digest")?.into(),
            },
            digest: digest(&env)?,
        };
        let context = Context {
            owner: &self.owner,
            swarm: &self.swarm,
            owner_node: &self.owner_node,
            now,
            limits: self.limits,
        };
        let response = crate::replay::execute_mode(
            &tx,
            &verified,
            now,
            kind == POLL || kind == POLL2,
            |tx| {
                let s = verified.request.statement();
                let response = match kind {
                    OWNER => enqueue(tx, &context, s, &env),
                    RESULT => result(tx, &context, s),
                    POLL | POLL2 => poll(tx, &context, s, token),
                    ACK => ack(tx, &context, s),
                    _ => Err(Error::Refused("managed_control_protocol_invalid")),
                }?;
                let outcome: Value = serde_json::from_slice(&response)
                    .map_err(|_| Error::Refused("managed_control_json_invalid"))?;
                effect(&DispatchEffects {
                    tx,
                    statement: s,
                    response: &outcome["statement"],
                    now,
                })?;
                Ok(response)
            },
        )?;
        tx.commit()?;
        if kind == OWNER {
            self.wake.notify()?;
        }
        Ok(response)
    }
}
fn validate(s: &Value, kind: &str, limits: Limits) -> Result<()> {
    // L5 owns signed verification and common grammar. Validate the extended
    // request's common projection without modifying the authenticated statement.
    let mut common = s.clone();
    let obj = common
        .as_object_mut()
        .ok_or(Error::Refused("managed_control_fields_invalid"))?;
    if kind == POLL2 {
        require(
            obj.remove("active_claims").is_some(),
            "managed_control_fields_invalid",
        )?;
        obj.insert("protocol".into(), POLL.into());
    }
    if kind == ACK {
        for key in [
            "command_id",
            "claim_token",
            "disposition",
            "native_response",
        ] {
            require(obj.remove(key).is_some(), "managed_control_fields_invalid")?;
        }
        obj.insert("protocol".into(), POLL.into());
    }
    if kind == RESULT {
        obj.insert("protocol".into(), OWNER.into());
    }
    protocol::managed::validate_request_common(&common).map_err(contract)?;
    if kind == OWNER || kind == RESULT {
        protocol::native::validate_command(&s["native_command"]).map_err(contract)?;
        require(
            s["native_command"]["run_id"] == s["run_id"]
                && s["native_command"]["deployment_id"] == s["deployment_id"],
            "managed_control_run_binding_mismatch",
        )?;
    }
    if kind == POLL2 {
        let a = s["active_claims"]
            .as_object()
            .ok_or(Error::Refused("managed_control_active_claim_invalid"))?;
        require(
            (a.len() as u64)
                <= limits
                    .active_work
                    .get()
                    .checked_add(limits.active_control.get())
                    .ok_or(Error::Refused("managed_control_configuration_invalid"))?
                && a.iter()
                    .all(|(k, v)| segment(k) && v.as_str().is_some_and(segment)),
            "managed_control_active_claim_invalid",
        )?;
    }
    if kind == ACK {
        require(
            segment(text(s, "command_id")?) && segment(text(s, "claim_token")?),
            "managed_control_identifier_invalid",
        )?;
        match text(s, "disposition")? {
            "reconciliation_required" => require(
                s["native_response"].is_null(),
                "managed_control_native_response_invalid",
            )?,
            disposition @ ("completed" | "failed") => protocol::native::validate_response(
                &s["native_response"],
                text(s, "command_id")?,
                text(s, "target_node_id")?,
                disposition,
            )
            .map_err(contract)?,
            _ => return Err(Error::Refused("managed_control_disposition_invalid")),
        }
    }
    Ok(())
}
struct Context<'a> {
    owner: &'a Identity,
    swarm: &'a str,
    owner_node: &'a str,
    now: f64,
    limits: Limits,
}
impl Context<'_> {
    fn response(&self, kind: &str, s: &Value, extra: Value) -> Result<Vec<u8>> {
        let mut body = binding(s);
        let ttl = control::managed_time_limits()?.max_ttl_seconds;
        for(k,v)in json!({"protocol":format!("mycelium.managed_control.{kind}.v1"),"request_id":s["request_id"],"swarm_id":self.swarm,"owner_node_id":self.owner_node,"owner_endpoint_id":self.owner.endpoint_id(),"issued_at":self.now,"expires_at":self.now+ttl}).as_object().ok_or(Error::Refused("managed_control_json_invalid"))? {body[k]=v.clone();}
        for (k, v) in extra
            .as_object()
            .ok_or(Error::Refused("managed_control_json_invalid"))?
        {
            body[k] = v.clone();
        }
        let signature = self
            .owner
            .sign(&body)
            .map_err(|_| Error::Refused("managed_control_sign_failed"))?;
        bytes(
            &json!({"protocol":protocol::managed::SIGNED_ENVELOPE_PROTOCOL,"statement":body,"signature":signature,"verification_key":self.owner.public_key_record()}),
        )
    }
}
fn enqueue(tx: &Transaction<'_>, c: &Context<'_>, s: &Value, env: &Value) -> Result<Vec<u8>> {
    let id = text(&s["native_command"], "command_id")?;
    let prior: bool = tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM managed_commands WHERE command_id=?1)",
        [id],
        |r| r.get(0),
    )?;
    require(!prior, "managed_control_conflicting_retry")?;
    let bound = bytes(&binding(s))?;
    let count:u64=tx.query_row("SELECT count(*) FROM control_dispatch d JOIN managed_commands m USING(command_id) WHERE d.binding=?1 AND (m.state IN ('pending','claimed') OR (m.state='reconciliation_required' AND d.terminal=0))",[&bound],|r|r.get(0))?;
    require(
        count < c.limits.pending.get(),
        "managed_control_capacity_exhausted",
    )?;
    if legacy::present(tx)? {
        legacy::enqueue(tx, c, s, env)?;
    } else {
        tx.execute("INSERT INTO managed_commands(command_id,target_key_digest,state,run_id,deployment_id,updated_at) VALUES (?1,?2,'pending',?3,?4,?5)",params![id,text(s,"target_key_digest")?,text(s,"run_id")?,text(s,"deployment_id")?,c.now])?;
    }
    tx.execute("INSERT INTO control_dispatch(command_id,binding,envelope,sequence,expires) VALUES (?1,?2,?3,?4,?5)",params![id,bound,bytes(env)?,s["sequence"].as_i64().ok_or(Error::Refused("managed_control_sequence_unrepresentable"))?,s["expires_at"].as_f64()])?;
    c.response(
        "enqueue_receipt",
        s,
        json!({"command_id":id,"command_digest":digest(&s["native_command"])?,"state":"pending"}),
    )
}
struct Row {
    id: String,
    state: String,
    env: Value,
    token: Option<String>,
    sequence: i64,
}
fn rows(db: &Connection, bound: &[u8]) -> Result<Vec<Row>> {
    let mut q=db.prepare("SELECT d.command_id,m.state,d.envelope,d.claim_token,d.sequence FROM control_dispatch d JOIN managed_commands m USING(command_id) WHERE d.binding=?1 AND d.terminal=0 AND m.state IN ('pending','claimed','reconciliation_required') ORDER BY d.sequence")?;
    let raw = q
        .query_map([bound], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, Vec<u8>>(2)?,
                r.get::<_, Option<String>>(3)?,
                r.get::<_, i64>(4)?,
            ))
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    raw.into_iter()
        .map(|(id, state, bytes, token, sequence)| {
            Ok(Row {
                id,
                state,
                env: serde_json::from_slice(&bytes)
                    .map_err(|_| Error::Refused("managed_control_state_corrupt"))?,
                token,
                sequence,
            })
        })
        .collect()
}
fn poll(
    tx: &Transaction<'_>,
    c: &Context<'_>,
    s: &Value,
    token: impl FnOnce() -> Result<String>,
) -> Result<Vec<u8>> {
    let bound = bytes(&binding(s))?;
    tx.execute("UPDATE managed_commands SET state='expired',updated_at=?1 WHERE state='pending' AND command_id IN (SELECT command_id FROM control_dispatch WHERE binding=?2 AND expires<?1)",params![c.now,bound])?;
    let mut rows = rows(tx, &bound)?;
    require(
        rows.len() as u64 <= c.limits.pending.get(),
        "managed_control_capacity_exhausted",
    )?;
    let active = s.get("active_claims").and_then(Value::as_object);
    if let Some(active) = active {
        for (id, token) in active {
            require(
                rows.iter().any(|r| {
                    r.id == *id && r.state != "pending" && r.token.as_deref() == token.as_str()
                }),
                "managed_control_active_claim_invalid",
            )?;
        }
    }
    if let Some(row) = rows.iter().find(|r| {
        r.state == "reconciliation_required"
            || (r.state == "claimed" && !active.is_some_and(|a| a.contains_key(&r.id)))
    }) {
        tx.execute("UPDATE managed_commands SET state='reconciliation_required',updated_at=?1 WHERE command_id=?2",params![c.now,row.id])?;
        return c.response("poll_receipt",s,json!({"delivery_kind":"reconciliation_required","command_id":row.id,"claim_token":row.token,"command_envelope":null}));
    }
    let work_count = rows
        .iter()
        .filter(|r| r.state == "claimed" && work(&r.env))
        .count() as u64;
    let control_count = rows
        .iter()
        .filter(|r| r.state == "claimed" && !work(&r.env))
        .count() as u64;
    require(
        work_count <= c.limits.active_work.get() && control_count <= c.limits.active_control.get(),
        "managed_control_active_claim_invalid",
    )?;
    let concurrent = s["protocol"] == POLL2;
    if concurrent {
        rows.sort_by_key(|r| (priority(&r.env), r.sequence));
    }
    let next = rows.iter().find(|r| {
        r.state == "pending"
            && (!concurrent
                || if work(&r.env) {
                    work_count < c.limits.active_work.get()
                } else {
                    control_count < c.limits.active_control.get()
                })
    });
    if let Some(row) = next {
        let token = token()?;
        require(segment(&token), "managed_control_claim_token_invalid")?;
        tx.execute("UPDATE managed_commands SET state='claimed',claimed_request_id=?1,updated_at=?2 WHERE command_id=?3 AND state='pending'",params![text(s,"request_id")?,c.now,row.id])?;
        tx.execute(
            "UPDATE control_dispatch SET claim_token=?1 WHERE command_id=?2",
            params![token, row.id],
        )?;
        if legacy::present(tx)? {
            tx.execute(
                "UPDATE managed_commands SET claim_token=?1,claimed_at=?2 WHERE command_id=?3",
                params![token, c.now, row.id],
            )?;
        }
        c.response("poll_receipt",s,json!({"delivery_kind":"command","command_id":row.id,"claim_token":token,"command_envelope":row.env}))
    } else {
        c.response("poll_receipt",s,json!({"delivery_kind":"empty","command_id":null,"claim_token":null,"command_envelope":null}))
    }
}
type ResultRow = (Vec<u8>, Vec<u8>, String, Option<Vec<u8>>);
fn result(tx: &Transaction<'_>, c: &Context<'_>, s: &Value) -> Result<Vec<u8>> {
    let id = text(&s["native_command"], "command_id")?;
    let bound = bytes(&binding(s))?;
    tx.execute("UPDATE managed_commands SET state='expired',updated_at=?1 WHERE state='pending' AND command_id IN (SELECT command_id FROM control_dispatch WHERE binding=?2 AND expires<=?1)",params![c.now,bound])?;
    let row:Option<ResultRow>=tx.query_row("SELECT d.binding,d.envelope,m.state,d.native FROM control_dispatch d JOIN managed_commands m USING(command_id) WHERE command_id=?1",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
    let (stored, envelope, state, native) =
        row.ok_or(Error::Refused("managed_control_command_unknown"))?;
    let envelope: Value = serde_json::from_slice(&envelope)
        .map_err(|_| Error::Refused("managed_control_state_corrupt"))?;
    require(
        bound == stored && envelope["statement"]["native_command"] == s["native_command"],
        "managed_control_command_binding_mismatch",
    )?;
    let native: Value = match native {
        Some(b) => serde_json::from_slice(&b)
            .map_err(|_| Error::Refused("managed_control_state_corrupt"))?,
        None => Value::Null,
    };
    let native_digest = if ["completed", "failed"].contains(&state.as_str()) {
        protocol::native::validate_response(&native, id, text(s, "target_node_id")?, &state)
            .map_err(contract)?;
        let receipt: Vec<u8> = tx.query_row(
            "SELECT receipt FROM control_dispatch_receipts WHERE command_id=?1",
            [id],
            |r| r.get(0),
        )?;
        let receipt: Value = serde_json::from_slice(&receipt)
            .map_err(|_| Error::Refused("managed_control_state_corrupt"))?;
        protocol::identity::verify(
            &receipt["statement"],
            &receipt["signature"],
            &c.owner.public_key_record(),
        )
        .map_err(|_| Error::Refused("managed_control_state_corrupt"))?;
        let digest = digest(&native)?;
        require(
            receipt["statement"]["native_response_digest"] == digest
                && receipt["statement"]["command_id"] == id
                && binding(&receipt["statement"]) == binding(s),
            "managed_control_state_corrupt",
        )?;
        Some(digest)
    } else {
        require(native.is_null(), "managed_control_state_corrupt")?;
        None
    };
    c.response("result_receipt",s,json!({"command_id":id,"command_digest":digest(&s["native_command"])? ,"state":state,"native_response":native,"native_response_digest":native_digest}))
}

type AckRow = (Vec<u8>, Option<String>, bool, Option<Vec<u8>>, String);
fn ack(tx: &Transaction<'_>, c: &Context<'_>, s: &Value) -> Result<Vec<u8>> {
    let id = text(s, "command_id")?;
    let row:Option<AckRow>=tx.query_row("SELECT d.binding,d.claim_token,d.terminal,d.native,m.state FROM control_dispatch d JOIN managed_commands m USING(command_id) WHERE command_id=?1",[id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?;
    let (bound, token, terminal, native, state) =
        row.ok_or(Error::Refused("managed_control_command_unknown"))?;
    require(
        bound == bytes(&binding(s))?,
        "managed_control_command_binding_mismatch",
    )?;
    require(
        token.as_deref() == Some(text(s, "claim_token")?),
        "managed_control_claim_token_mismatch",
    )?;
    let disposition = text(s, "disposition")?;
    let next = if s["native_response"].is_null() {
        None
    } else {
        Some(bytes(&s["native_response"])?)
    };
    if terminal {
        require(
            state == disposition && native == next,
            "managed_control_conflicting_retry",
        )?;
    } else {
        require(
            state == "claimed" || state == "reconciliation_required",
            "managed_control_ack_state_invalid",
        )?;
        tx.execute(
            "UPDATE managed_commands SET state=?1,updated_at=?2 WHERE command_id=?3",
            params![disposition, c.now, id],
        )?;
        tx.execute(
            "UPDATE control_dispatch SET terminal=1,native=?1 WHERE command_id=?2",
            params![next, id],
        )?;
    }
    let response=c.response("ack_receipt",s,json!({"command_id":id,"state":disposition,"native_response_digest":if s["native_response"].is_null(){None}else{Some(digest(&s["native_response"])? )}}))?;
    if legacy::present(tx)? {
        tx.execute("UPDATE managed_commands SET native_response_digest=?1,native_response_json=?2,terminal_receipt_json=?3 WHERE command_id=?4",params![if s["native_response"].is_null(){None}else{Some(digest(&s["native_response"])? )},next.as_ref().map(|v|String::from_utf8_lossy(v).into_owned()),String::from_utf8_lossy(&response),id])?;
    }
    tx.execute(
        "INSERT INTO control_dispatch_receipts VALUES (?1,?2) ON CONFLICT(command_id) DO NOTHING",
        params![id, response],
    )?;
    Ok(response)
}
