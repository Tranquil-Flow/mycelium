// SPDX-License-Identifier: AGPL-3.0-only
//! CLIENT private owner journal and signed request producer, not the server mailbox.
//! Port of control_session.py and durable_requests.py at f4e366e9. One owner per
//! signing identity must be shared by every target. Dropping it only releases
//! local ownership; it never claims a remote stop or process exit.
use budgets::{BudgetId as B, Entries, Span};
use protocol::{canonical, identity::Identity};
use rustix::fs::{FlockOperation, Mode, OFlags};
use serde_json::{Value, json};
use std::{
    fs::{DirBuilder, File},
    io::{Read, Write},
    os::unix::fs::{DirBuilderExt, MetadataExt},
    path::Path,
    sync::atomic::{AtomicU64, Ordering},
};

pub mod session;
#[cfg(test)]
mod tests;
const PROTOCOL: &str = "mycelium.a13.owner_control_journal.v3";
const COMMAND: &str = "mycelium.managed_control.owner_command.v1";
const RESULT: &str = "mycelium.managed_control.owner_result.v1";
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct OwnerError(pub &'static str);
type Result<T> = std::result::Result<T, OwnerError>;
impl From<protocol::ContractError> for OwnerError {
    fn from(e: protocol::ContractError) -> Self {
        Self(e.0)
    }
}
#[derive(Clone, Copy)]
pub struct Limits {
    max_bytes: u64,
    pub max_commands: u64,
    pub max_requests: u64,
    sequence_block: u64,
    ledger_tail: u64,
}
fn count(id: B) -> Result<u64> {
    match id.entry().value {
        budgets::table::Value::Count(n) => Ok(n.get()),
        _ => Err(OwnerError("budget_unavailable")),
    }
}
impl Limits {
    pub fn registered() -> Result<Self> {
        let max_bytes = match B::PyMyceliumA13MacosControlSessionMaxOwnerJournalBytesL125
            .entry()
            .value
        {
            budgets::table::Value::Storage(v) => v.get(),
            _ => return Err(OwnerError("budget_unavailable")),
        };
        Ok(Self {
            max_bytes,
            max_commands: count(B::PyMyceliumA13MacosControlSessionMaxOwnerCommandsL126)?,
            max_requests: count(B::PyFinalMyceliumA13MacosControlSessionMaxRequestsL138C77)?,
            sequence_block: count(B::ManagedOwnerSequenceReservationBlock)?,
            ledger_tail: count(B::ManagedOwnerConfirmedLedgerTail)?,
        })
    }
    fn validate(self) -> Result<Self> {
        if self.max_commands < 2
            || self.max_commands > count(B::PyMyceliumA13MacosControlSessionMaxOwnerCommandsL126)?
            || self.max_requests < self.max_commands
            || self.max_requests > count(B::PyUpperGuardMyceliumA13MacosControlSessionL141C23)?
        {
            return Err(OwnerError("managed_owner_configuration_invalid"));
        }
        Ok(self)
    }
}
#[derive(Clone)]
pub struct RequestMeta {
    pub request_id: String,
    pub nonce: String,
    pub issued_at: f64,
    pub ttl: Span,
}
#[derive(Clone)]
struct Intent {
    binding: Value,
    digest: String,
    prepared: Option<Value>,
    confirmed: bool,
}
impl Intent {
    fn document(&self) -> Value {
        json!({"binding":self.binding,"digest":self.digest,"enqueue_request":self.prepared,"enqueue_confirmed":self.confirmed})
    }
}
struct State {
    authority: Value,
    sequence: u64,
    ceiling: u64,
    writes: u64,
    commands: Vec<(String, Intent)>,
}
impl State {
    fn document(&self) -> Value {
        let commands: serde_json::Map<String, Value> = self
            .commands
            .iter()
            .map(|(k, v)| (k.clone(), v.document()))
            .collect();
        json!({"protocol":PROTOCOL,"authority":self.authority,"sequence":self.sequence,"sequence_ceiling":self.ceiling,"write_intents":self.writes,"commands":commands})
    }
    fn load(mut v: Value, authority: &Value, limits: Limits) -> Result<(Self, bool)> {
        let bad = OwnerError("managed_owner_state_invalid");
        let legacy = v["protocol"] == "mycelium.a13.owner_control_journal.v1";
        if legacy {
            for row in v["commands"].as_object_mut().ok_or(bad)?.values_mut() {
                if !shape(row, &["binding", "digest"]) {
                    return Err(bad);
                }
                row["enqueue_request"] = Value::Null;
                row["enqueue_confirmed"] = json!(true);
            }
            v["protocol"] = json!("mycelium.a13.owner_control_journal.v2");
        }
        let migrated = v["protocol"] == "mycelium.a13.owner_control_journal.v2";
        if migrated {
            let seq = v["sequence"].as_u64().ok_or(bad)?;
            v["write_intents"] = json!(seq);
            v["sequence_ceiling"] = json!(seq.checked_add(limits.sequence_block).ok_or(bad)?);
            v["protocol"] = json!(PROTOCOL);
        }
        if !shape(
            &v,
            &[
                "protocol",
                "authority",
                "sequence",
                "sequence_ceiling",
                "write_intents",
                "commands",
            ],
        ) || v["protocol"] != PROTOCOL
            || &v["authority"] != authority
        {
            return Err(bad);
        }
        let sequence = v["sequence"].as_u64().ok_or(bad)?;
        let ceiling = v["sequence_ceiling"].as_u64().ok_or(bad)?;
        let writes = v["write_intents"].as_u64().ok_or(bad)?;
        if sequence >= ceiling {
            return Err(bad);
        }
        let rows = v["commands"].as_object().ok_or(bad)?;
        if rows.len() as u64 > count(B::PyMyceliumA13MacosControlSessionMaxOwnerCommandsL126)? {
            return Err(bad);
        }
        let mut commands = Vec::new();
        for (key, row) in rows {
            if !shape(
                row,
                &["binding", "digest", "enqueue_request", "enqueue_confirmed"],
            ) || !row["binding"].is_object()
                || !(row["enqueue_request"].is_null() || row["enqueue_request"].is_object())
            {
                return Err(bad);
            }
            commands.push((
                key.clone(),
                Intent {
                    binding: row["binding"].clone(),
                    digest: row["digest"].as_str().ok_or(bad)?.into(),
                    prepared: (!row["enqueue_request"].is_null())
                        .then(|| row["enqueue_request"].clone()),
                    confirmed: row["enqueue_confirmed"].as_bool().ok_or(bad)?,
                },
            ));
        }
        Ok((
            Self {
                authority: authority.clone(),
                sequence,
                ceiling,
                writes,
                commands,
            },
            migrated,
        ))
    }
}
fn shape(v: &Value, fields: &[&str]) -> bool {
    v.as_object()
        .is_some_and(|m| m.len() == fields.len() && fields.iter().all(|f| m.contains_key(*f)))
}
fn private_file(file: &File) -> Result<()> {
    let m = file.metadata().map_err(|_| {
        eprintln!("l11_auth_chk:meta");
        OwnerError("authority_state_invalid")
    })?;
    if !m.is_file()
        || m.nlink() != 1
        || m.uid() != rustix::process::getuid().as_raw()
        || m.mode() & 0o7777 != 0o600
    {
        eprintln!(
            "l11_auth_chk:mode file={} nlink={} uid={} mode={:o}",
            m.is_file(),
            m.nlink(),
            m.uid(),
            m.mode() & 0o7777
        );
        return Err(OwnerError("authority_state_invalid"));
    }
    Ok(())
}
static TEMP: AtomicU64 = AtomicU64::new(0);
struct PrivateJournal {
    root: File,
    _lock: File,
}
impl PrivateJournal {
    fn open(path: &Path) -> Result<Self> {
        if !path.exists() {
            DirBuilder::new()
                .recursive(true)
                .mode(0o700)
                .create(path)
                .map_err(|_| OwnerError("authority_state_path_invalid"))?;
        }
        let root: File = rustix::fs::open(
            path,
            OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .map_err(|_| OwnerError("authority_state_path_invalid"))?
        .into();
        let m = root
            .metadata()
            .map_err(|_| OwnerError("authority_state_path_invalid"))?;
        if m.uid() != rustix::process::getuid().as_raw() || m.mode() & 0o7777 != 0o700 {
            return Err(OwnerError("authority_state_path_invalid"));
        }
        let lock: File = rustix::fs::openat(
            &root,
            "owner.lock",
            OFlags::RDWR | OFlags::CREATE | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::from_raw_mode(0o600),
        )
        .map_err(|_| OwnerError("managed_owner_state_invalid"))?
        .into();
        private_file(&lock).map_err(|_| OwnerError("managed_owner_state_invalid"))?;
        rustix::fs::flock(&lock, FlockOperation::NonBlockingLockExclusive).map_err(|e| {
            if e == rustix::io::Errno::WOULDBLOCK {
                OwnerError("managed_owner_already_running")
            } else {
                OwnerError("managed_owner_state_invalid")
            }
        })?;
        Ok(Self { root, _lock: lock })
    }
    fn load(&self, limit: u64) -> Result<Option<Value>> {
        let fd = match rustix::fs::openat(
            &self.root,
            "owner.json",
            OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC | OFlags::NONBLOCK,
            Mode::empty(),
        ) {
            Ok(fd) => fd,
            Err(rustix::io::Errno::NOENT) => return Ok(None),
            Err(_) => {
                eprintln!("l11_auth_chk:open");
                return Err(OwnerError("authority_state_invalid"));
            }
        };
        let file: File = fd.into();
        private_file(&file)?;
        let size = file
            .metadata()
            .map_err(|_| OwnerError("authority_state_invalid"))?
            .len();
        if size == 0 || size > limit {
            eprintln!("l11_auth_chk:size {size} limit {limit}");
            return Err(OwnerError("authority_state_invalid"));
        }
        let mut raw = Vec::new();
        file.take(limit + 1)
            .read_to_end(&mut raw)
            .map_err(|_| OwnerError("authority_state_invalid"))?;
        let value = protocol::strict::object(&raw, limit as usize).map_err(|e| {
            eprintln!("l11_auth_chk:strict {e:?}");
            OwnerError("authority_state_invalid")
        })?;
        let reencoded = canonical::to_bytes(&value).map_err(|_| {
            eprintln!("l11_auth_chk:reencode");
            OwnerError("authority_state_invalid")
        })?;
        if reencoded != raw {
            eprintln!(
                "l11_auth_chk:canon raw={} reencoded={}",
                raw.len(),
                reencoded.len()
            );
            return Err(OwnerError("authority_state_invalid"));
        }
        Ok(Some(value))
    }
    fn save(&self, value: &Value, limit: u64) -> Result<()> {
        self.save_named("owner.json", value, limit)
    }
    fn save_named(&self, name: &str, value: &Value, limit: u64) -> Result<()> {
        let bad = OwnerError("authority_state_write_failed");
        let bytes = canonical::to_bytes(value).map_err(|_| bad)?;
        if bytes.is_empty() || bytes.len() as u64 > limit {
            return Err(bad);
        }
        let temporary = format!(
            ".owner.json.{}.{}.tmp",
            std::process::id(),
            TEMP.fetch_add(1, Ordering::Relaxed)
        );
        let mut file: File = rustix::fs::openat(
            &self.root,
            &temporary,
            OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::from_raw_mode(0o600),
        )
        .map_err(|_| bad)?
        .into();
        let result = (|| {
            rustix::fs::fchmod(&file, Mode::from_raw_mode(0o600)).map_err(|_| bad)?;
            file.write_all(&bytes).map_err(|_| bad)?;
            file.sync_all().map_err(|_| bad)?;
            drop(file);
            rustix::fs::renameat(&self.root, &temporary, &self.root, name).map_err(|_| bad)?;
            self.root.sync_all().map_err(|_| bad)
        })();
        finish_atomic_save(
            result,
            rustix::fs::unlinkat(&self.root, &temporary, rustix::fs::AtFlags::empty()),
        )
    }
}
// Cleanup must not mask a failed write/rename/fsync, even if the temporary is gone.
fn finish_atomic_save(write_result: Result<()>, removal: rustix::io::Result<()>) -> Result<()> {
    match removal {
        Ok(()) => write_result,
        // Rename normally consumed the temporary; preserve any earlier failure.
        Err(rustix::io::Errno::NOENT) => write_result,
        Err(_) => Err(OwnerError("authority_state_write_failed")),
    }
}
/// Serialized client producer. The caller owns the existing signer and transport;
/// this object owns the actual durable intent and signer-global sequence state.
pub struct ManagedControlOwner {
    journal: PrivateJournal,
    state: State,
    volatile: u64,
    limits: Limits,
    broken: bool,
    reconciling: bool,
    diagnostic_error: Option<OwnerError>,
    reconciliation_error: Option<OwnerError>,
}
impl ManagedControlOwner {
    pub fn open(
        path: &Path,
        signer: &Identity,
        swarm: &str,
        owner: &str,
        limits: Limits,
    ) -> Result<Self> {
        let limits = limits.validate()?;
        let authority = json!({"swarm_id":swarm,"owner_node_id":owner,"key_digest":signer.verification_key_digest()});
        let journal = PrivateJournal::open(path)?;
        let (state, migrated) = match journal.load(limits.max_bytes)? {
            Some(value) => State::load(value, &authority, limits)?,
            None => {
                let state = State {
                    authority,
                    sequence: 0,
                    ceiling: limits.sequence_block,
                    writes: 0,
                    commands: Vec::new(),
                };
                journal.save(&state.document(), limits.max_bytes)?;
                (state, false)
            }
        };
        let mut this = Self {
            volatile: state.ceiling - 1,
            state,
            journal,
            limits,
            broken: false,
            reconciling: false,
            diagnostic_error: None,
            reconciliation_error: None,
        };
        if migrated {
            this.persist()?
        }
        Ok(this)
    }
    fn guard(&self) -> Result<()> {
        if self.broken {
            Err(OwnerError("managed_owner_closed"))
        } else {
            Ok(())
        }
    }
    fn persist(&mut self) -> Result<()> {
        if self
            .journal
            .save(&self.state.document(), self.limits.max_bytes)
            .is_err()
        {
            self.broken = true;
            return Err(OwnerError("managed_owner_state_unavailable"));
        }
        Ok(())
    }
    fn next_sequence(&mut self) -> Result<u64> {
        self.guard()?;
        self.volatile = self
            .volatile
            .checked_add(1)
            .ok_or(OwnerError("managed_owner_state_unavailable"))?;
        if self.volatile >= self.state.ceiling {
            self.state.sequence = self.volatile;
            self.state.ceiling = self
                .volatile
                .checked_add(self.limits.sequence_block)
                .ok_or(OwnerError("managed_owner_state_unavailable"))?;
            self.persist()?;
        }
        Ok(self.volatile)
    }
    fn signed(
        &self,
        signer: &Identity,
        binding: &Value,
        command: &Value,
        meta: RequestMeta,
        sequence: u64,
        result: bool,
    ) -> Result<Value> {
        if self.state.authority["key_digest"] != signer.verification_key_digest() {
            return Err(OwnerError("managed_owner_signer_invalid"));
        }
        protocol::native::validate_command(command)?;
        let mut statement = binding.clone();
        let m = statement
            .as_object_mut()
            .ok_or(OwnerError("managed_control_member_binding_invalid"))?;
        m.extend(json!({"protocol":COMMAND,"swarm_id":self.state.authority["swarm_id"],"owner_node_id":self.state.authority["owner_node_id"],"native_command":command,"sequence":sequence,"nonce":meta.nonce,"request_id":meta.request_id,"issued_at":meta.issued_at,"expires_at":meta.issued_at+meta.ttl.millis() as f64/1000.0}).as_object().expect("fixed object").clone());
        protocol::managed::validate_request_common(&statement)?;
        if statement["run_id"] != command["run_id"]
            || statement["deployment_id"] != command["deployment_id"]
        {
            return Err(OwnerError("managed_control_member_binding_invalid"));
        }
        if result {
            statement["protocol"] = json!(RESULT)
        }
        let signature = signer
            .sign(&statement)
            .map_err(|_| OwnerError("managed_control_signature_invalid"))?;
        let envelope = json!({"protocol":protocol::managed::SIGNED_ENVELOPE_PROTOCOL,"statement":statement,"signature":signature,"verification_key":signer.public_key_record()});
        protocol::managed::verify(
            &envelope,
            if result { RESULT } else { COMMAND },
            &signer.public_key_record(),
            signer.endpoint_id(),
            meta.issued_at,
            time_limits()?,
        )?;
        Ok(json!({"operation":if result {"result"}else{"enqueue"},"envelope":envelope}))
    }
    /// Saves the exact prepared envelope before returning it to any IO caller.
    /// An existing intent returns the original envelope, never refreshed bytes.
    pub fn prepare_enqueue(
        &mut self,
        signer: &Identity,
        binding: &Value,
        command: &Value,
        meta: RequestMeta,
    ) -> Result<Value> {
        self.guard()?;
        protocol::native::validate_command(command)?;
        let key = command["command_id"]
            .as_str()
            .ok_or(OwnerError("managed_control_native_command_invalid"))?;
        let digest = canonical::sha256_prefixed(
            &canonical::to_bytes(command)
                .map_err(|_| OwnerError("managed_control_json_invalid"))?,
        );
        if let Some((_, row)) = self.state.commands.iter().find(|(k, _)| k == key) {
            if row.binding != *binding || row.digest != digest {
                return Err(OwnerError("managed_control_conflicting_retry"));
            }
            return row
                .prepared
                .clone()
                .ok_or(OwnerError("managed_control_legacy_reconciliation_required"));
        }
        if self.state.commands.iter().any(|(_, r)| !r.confirmed) {
            return Err(OwnerError("managed_control_reconciliation_required"));
        }
        if self.state.commands.len() as u64 > self.limits.ledger_tail + 1 {
            let drop_count = self.state.commands.len() - (self.limits.ledger_tail as usize + 1);
            self.state.commands.drain(..drop_count);
            self.state.writes = self.state.writes.saturating_sub(drop_count as u64);
        }
        let maximum = if command["command"] == "stop" {
            self.limits.max_commands
        } else {
            self.limits.max_commands - 1
        };
        if self.state.commands.len() as u64 >= maximum {
            return Err(OwnerError("managed_owner_command_limit_exceeded"));
        }
        if self.state.writes >= self.limits.max_requests {
            return Err(OwnerError("managed_owner_request_limit_exceeded"));
        }
        let sequence = self.next_sequence()?;
        let prepared = self.signed(signer, binding, command, meta, sequence, false)?;
        self.state.commands.push((
            key.to_owned(),
            Intent {
                binding: binding.clone(),
                digest,
                prepared: Some(prepared.clone()),
                confirmed: false,
            },
        ));
        self.state.writes += 1;
        self.persist()?;
        Ok(prepared)
    }
    /// Read-only request: reserves sequence blocks, but charges no write intent.
    pub fn prepare_result(
        &mut self,
        signer: &Identity,
        binding: &Value,
        command: &Value,
        meta: RequestMeta,
    ) -> Result<Value> {
        let sequence = self.next_sequence()?;
        self.signed(signer, binding, command, meta, sequence, true)
    }
}
/// Borrow existing identity, membership and IO; no host authority lock may span IO.
pub trait OwnerHost: transport::Clock {
    /// Optional local cancellation fence; shared sessions install their real waiter.
    fn check_local(&self) -> Result<()> {
        Ok(())
    }
    fn signer(&self) -> &Identity;
    fn current_member(&mut self, node: &str) -> Result<Value>;
    fn nonce(&mut self) -> Result<String>;
    /// Raw signed receipt envelope; the owner verifies it with independent pins.
    fn exchange(&mut self, prepared: &Value, deadline: budgets::MonoInstant) -> Result<Value>;
    fn wait(&mut self, allowance: Span, deadline: budgets::MonoInstant) -> Result<()>;
}
impl ManagedControlOwner {
    /// Bounded enqueue/reconcile/result core. `binding` is the independently
    /// admitted session binding, updated only for a newer live same member.
    /// This does not implement ManagedNodeSession stop/interrupt/factory ownership.
    pub fn exchange(
        &mut self,
        host: &mut impl OwnerHost,
        binding: &mut Value,
        command: &Value,
        deadline: budgets::MonoInstant,
    ) -> Result<Value> {
        self.begin_exchange(host, binding, command, deadline)?;
        loop {
            if let Some(response) = self.poll_exchange(host, binding, command, deadline)? {
                return Ok(response);
            }
            host.wait(
                span(B::PyUpperMyceliumA13MacosControlSessionL545C12)?
                    .min(remaining(host, deadline)?),
                deadline,
            )?;
        }
    }
    fn begin_exchange(
        &mut self,
        host: &mut impl OwnerHost,
        binding: &mut Value,
        command: &Value,
        deadline: budgets::MonoInstant,
    ) -> Result<()> {
        self.guard()?;
        remaining(host, deadline)?;
        resolve(host, binding)?;
        protocol::native::validate_command(command)?;
        let key = command["command_id"]
            .as_str()
            .ok_or(OwnerError("managed_control_native_command_invalid"))?
            .to_owned();
        if let Some((_, row)) = self.state.commands.iter().find(|(k, _)| k == &key) {
            let digest = canonical::sha256_prefixed(
                &canonical::to_bytes(command)
                    .map_err(|_| OwnerError("managed_control_json_invalid"))?,
            );
            if row.binding != *binding || row.digest != digest {
                return Err(OwnerError("managed_control_conflicting_retry"));
            }
        }
        self.reconcile_others(host, binding, &key, deadline)?;
        let prepared = if let Some((_, row)) = self.state.commands.iter().find(|(k, _)| k == &key) {
            let digest = canonical::sha256_prefixed(
                &canonical::to_bytes(command)
                    .map_err(|_| OwnerError("managed_control_json_invalid"))?,
            );
            if row.binding != *binding || row.digest != digest {
                return Err(OwnerError("managed_control_conflicting_retry"));
            }
            row.prepared.clone()
        } else {
            let meta = request_meta(
                host,
                span(B::PyUpperMyceliumA13MacosControlSessionL473C54)?
                    .min(remaining(host, deadline)?),
            )?;
            Some(self.prepare_enqueue(host.signer(), binding, command, meta)?)
        };
        let confirmed = self
            .state
            .commands
            .iter()
            .find(|(k, _)| k == &key)
            .expect("persisted intent")
            .1
            .confirmed;
        if !confirmed {
            let prepared =
                prepared.ok_or(OwnerError("managed_control_legacy_reconciliation_required"))?;
            remaining(host, deadline)?;
            let replayed = (|| {
                let body = protocol::managed::verify(
                    &prepared["envelope"],
                    COMMAND,
                    &host.signer().public_key_record(),
                    host.signer().endpoint_id(),
                    host.wall_seconds(),
                    time_limits()?,
                )?;
                protocol::managed::validate_request_common(&body)?;
                // durable_requests.py:64: the same signing key can sign another swarm.
                if body["swarm_id"] != self.state.authority["swarm_id"] {
                    return Err(OwnerError("managed_control_prepared_request_invalid"));
                }
                for field in [
                    "target_node_id",
                    "target_endpoint_id",
                    "target_key_digest",
                    "membership_generation",
                    "member_incarnation",
                    "run_id",
                    "deployment_id",
                ] {
                    if body[field] != binding[field] {
                        return Err(OwnerError("managed_control_member_stale"));
                    }
                }
                // durable_requests.py:70: owner scope is independent of the signer.
                if body["owner_node_id"] != self.state.authority["owner_node_id"] {
                    return Err(OwnerError("managed_control_authority_binding_invalid"));
                }
                self.receipt(host, &prepared, binding, command, deadline)
                    .map(|_| ())
            })();
            host.check_local()?;
            match replayed {
                Ok(()) => {}
                Err(OwnerError("managed_control_message_expired")) => {
                    // Absence is NOT non-execution. Only an authenticated receipt
                    // establishing a known row permits confirmation; never reenqueue.
                    let result = self.result(host, binding, command, deadline).map_err(|e| {
                        if matches!(
                            e.0,
                            "managed_owner_closed"
                                | "managed_owner_state_unavailable"
                                | "managed_control_response_timeout"
                        ) {
                            e
                        } else {
                            OwnerError("managed_control_reconciliation_required")
                        }
                    })?;
                    if !matches!(
                        result["state"].as_str(),
                        Some(
                            "pending"
                                | "claimed"
                                | "completed"
                                | "failed"
                                | "expired"
                                | "reconciliation_required"
                        )
                    ) {
                        return Err(OwnerError("managed_control_reconciliation_required"));
                    }
                }
                Err(_) => return Err(OwnerError("managed_control_reconciliation_required")),
            }
            self.state
                .commands
                .iter_mut()
                .find(|(k, _)| k == &key)
                .expect("persisted intent")
                .1
                .confirmed = true;
            self.persist()?;
        }
        Ok(())
    }
    fn poll_exchange(
        &mut self,
        host: &mut impl OwnerHost,
        binding: &mut Value,
        command: &Value,
        deadline: budgets::MonoInstant,
    ) -> Result<Option<Value>> {
        self.guard()?;
        remaining(host, deadline)?;
        resolve(host, binding)?;
        let result = self.result(host, binding, command, deadline)?;
        remaining(host, deadline)?;
        match result["state"].as_str() {
            Some("completed" | "failed") => {
                self.guard()?;
                resolve(host, binding)?;
                remaining(host, deadline)?;
                Ok(Some(result["native_response"].clone()))
            }
            Some("expired") => Err(OwnerError("managed_control_expired")),
            Some("reconciliation_required") => {
                Err(OwnerError("managed_control_reconciliation_required"))
            }
            _ => Ok(None),
        }
    }
    /// Last best-effort diagnostic failure, never silently discarded.
    pub fn diagnostic_error(&self) -> Option<OwnerError> {
        self.diagnostic_error
    }
    /// Most recent failed automatic reconciliation; the unresolved-row guard
    /// still decides whether new work can proceed, exactly as in the source.
    pub fn reconciliation_error(&self) -> Option<OwnerError> {
        self.reconciliation_error
    }
    pub fn reconcile(
        &mut self,
        host: &mut impl OwnerHost,
        key: &str,
        deadline: budgets::MonoInstant,
    ) -> Result<Value> {
        self.guard()?;
        let row = self
            .state
            .commands
            .iter()
            .find(|(k, _)| k == key)
            .ok_or(OwnerError("managed_control_command_unknown"))?
            .1
            .clone();
        let prepared = row
            .prepared
            .ok_or(OwnerError("managed_control_legacy_reconciliation_required"))?;
        let command = prepared["envelope"]["statement"]["native_command"].clone();
        let mut binding = row.binding;
        resolve(host, &mut binding)?;
        self.exchange(host, &mut binding, &command, deadline)
    }
    fn reconcile_others(
        &mut self,
        host: &mut impl OwnerHost,
        binding: &Value,
        key: &str,
        deadline: budgets::MonoInstant,
    ) -> Result<()> {
        let keys: Vec<String> = self
            .state
            .commands
            .iter()
            .filter(|(k, r)| k != key && !r.confirmed)
            .map(|(k, _)| k.clone())
            .collect();
        if !keys.is_empty() && !self.reconciling {
            self.reconciling = true;
            for key in &keys {
                let attempt = (|| {
                    let allowance = span(B::PyUpperMyceliumA13MacosControlSessionL403C48)?
                        .min(remaining(host, deadline)?);
                    let local = host
                        .monotonic()
                        .checked_add(allowance)
                        .map_err(|_| OwnerError("managed_control_response_timeout"))?;
                    self.reconcile(host, key, local)
                })();
                if let Err(error) = attempt {
                    self.reconciliation_error = Some(error);
                }
            }
            self.reconciling = false;
        }
        let now = host.wall_seconds();
        let retired: Vec<String> = self
            .state
            .commands
            .iter()
            .filter(|(k, row)| {
                k != key
                    && !row.confirmed
                    && (row.binding["run_id"] != binding["run_id"]
                        || row.binding["deployment_id"] != binding["deployment_id"]
                        || row.prepared.as_ref().is_some_and(|p| {
                            p["envelope"]["statement"]["expires_at"]
                                .as_f64()
                                .is_some_and(|expiry| expiry <= now)
                        }))
            })
            .map(|(k, _)| k.clone())
            .collect();
        if !retired.is_empty() {
            self.state.commands.retain(|(k, _)| !retired.contains(k));
            let diagnostic = json!({"protocol":"mycelium.a13.retired_intents.v1","current_run_id":binding["run_id"],"current_deployment_id":binding["deployment_id"],"retired":retired.iter().map(|k|k.chars().take(32).collect::<String>()).collect::<Vec<_>>(),"retired_count":retired.len(),"sequence":self.state.sequence,"sequence_ceiling":self.state.ceiling});
            let limit = B::PyMyceliumA13MacosAuthorityMaxPrivateJsonBytesL96
                .bytes()
                .map_err(|_| OwnerError("budget_unavailable"))?
                .get();
            if let Err(error) =
                self.journal
                    .save_named("retired-intents.private.json", &diagnostic, limit)
            {
                self.diagnostic_error = Some(error);
            }
            self.persist()?;
        }
        if self
            .state
            .commands
            .iter()
            .any(|(k, r)| k != key && !r.confirmed)
        {
            return Err(OwnerError("managed_control_reconciliation_required"));
        }
        Ok(())
    }
    fn result(
        &mut self,
        host: &mut impl OwnerHost,
        binding: &Value,
        command: &Value,
        deadline: budgets::MonoInstant,
    ) -> Result<Value> {
        self.guard()?;
        remaining(host, deadline)?;
        let meta = request_meta(
            host,
            span(B::PyFinalMyceliumA13MacosManagedControlTtlSecondsL2386C73)?,
        )?;
        let prepared = self.prepare_result(host.signer(), binding, command, meta)?;
        remaining(host, deadline)?;
        self.receipt(host, &prepared, binding, command, deadline)
    }
    fn receipt(
        &self,
        host: &mut impl OwnerHost,
        prepared: &Value,
        binding: &Value,
        command: &Value,
        deadline: budgets::MonoInstant,
    ) -> Result<Value> {
        self.guard()?;
        remaining(host, deadline)?;
        let envelope = host.exchange(prepared, deadline)?;
        let body = &prepared["envelope"]["statement"];
        let key = host.signer().public_key_record();
        let expected = protocol::managed_receipt::ExpectedReceipt {
            kind: if prepared["operation"] == "enqueue" {
                protocol::managed_receipt::ReceiptKind::Enqueue
            } else {
                protocol::managed_receipt::ReceiptKind::Result
            },
            owner_key: &key,
            request_id: body["request_id"]
                .as_str()
                .ok_or(OwnerError("managed_control_prepared_request_invalid"))?,
            swarm_id: self.state.authority["swarm_id"]
                .as_str()
                .expect("owner swarm"),
            owner_node_id: self.state.authority["owner_node_id"]
                .as_str()
                .expect("owner node"),
            binding,
            native_command: command,
            now: host.wall_seconds(),
        };
        let result = protocol::managed_receipt::verify(&envelope, &expected, time_limits()?)?;
        remaining(host, deadline)?;
        self.guard()?;
        Ok(result)
    }
}
fn span(id: B) -> Result<Span> {
    id.span(Entries::ZERO)
        .map_err(|_| OwnerError("budget_unavailable"))
}
fn remaining(host: &impl OwnerHost, deadline: budgets::MonoInstant) -> Result<Span> {
    host.check_local()?;
    let left = deadline
        .elapsed_since(host.monotonic())
        .map_err(|_| OwnerError("managed_control_response_timeout"))?;
    if left == Span::ZERO {
        Err(OwnerError("managed_control_response_timeout"))
    } else {
        Ok(left)
    }
}
fn request_meta(host: &mut impl OwnerHost, ttl: Span) -> Result<RequestMeta> {
    Ok(RequestMeta {
        nonce: host.nonce()?,
        request_id: host.nonce()?,
        issued_at: host.wall_seconds(),
        ttl,
    })
}
fn target(
    host: &mut impl OwnerHost,
    node: &str,
    run: &Value,
    deployment: &Value,
) -> Result<(Value, Value)> {
    let bad = OwnerError("managed_control_member_stale");
    let member = host.current_member(node).map_err(|_| bad)?;
    if member["node_id"] != node
        || !matches!(
            member["peer_class"].as_str(),
            Some("mac_mlx_iroh" | "windows_native_iroh" | "linux_native_iroh")
        )
        || !member["lease_expires_at"]
            .as_f64()
            .is_some_and(|lease| lease.is_finite() && host.wall_seconds() < lease)
    {
        return Err(bad);
    }
    let current = binding_from_member(&member, run, deployment).map_err(|_| bad)?;
    Ok((member, current))
}
fn binding_from_member(member: &Value, run: &Value, deployment: &Value) -> Result<Value> {
    let current = json!({"target_node_id":member["node_id"],"target_endpoint_id":member["endpoint_id"],"target_key_digest":member["verification_key_digest"],"membership_generation":member["generation"],"member_incarnation":member["incarnation"],"run_id":run,"deployment_id":deployment});
    protocol::managed::validate_binding(&current).map_err(OwnerError::from)?;
    Ok(current)
}
fn resolve(host: &mut impl OwnerHost, binding: &mut Value) -> Result<()> {
    let bad = OwnerError("managed_control_member_stale");
    let node = binding["target_node_id"].as_str().ok_or(bad)?;
    let (member, current) = target(host, node, &binding["run_id"], &binding["deployment_id"])?;
    if current != *binding {
        let same = [
            "target_node_id",
            "target_endpoint_id",
            "target_key_digest",
            "run_id",
            "deployment_id",
        ]
        .iter()
        .all(|f| current[*f] == binding[*f]);
        let advanced = match (
            binding["membership_generation"].as_u64(),
            current["membership_generation"].as_u64(),
        ) {
            (Some(a), Some(b)) => b > a,
            _ => false,
        };
        if !(same
            && advanced
            && matches!(
                member["lifecycle_state"].as_str(),
                Some("NEW" | "CONFIGURED" | "RUNNING")
            ))
        {
            return Err(bad);
        }
        *binding = current;
    }
    Ok(())
}
fn time_limits() -> Result<protocol::TimeLimits> {
    let seconds = |id: B| {
        id.span(Entries::ZERO)
            .map(|v| v.millis() as f64 / 1000.0)
            .map_err(|_| OwnerError("budget_unavailable"))
    };
    Ok(protocol::TimeLimits {
        max_ttl_seconds: seconds(B::ControlTtl)?,
        clock_skew_seconds: seconds(B::ControlClockSkew)?,
    })
}
