// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's native-hello observation (`observe` step of the owner run chain, B-RUN-25):
//! `collect_managed_hello_observations` (`managed_observations.py @ f4e366e9`) and the
//! `ObservationContext` that publishes the metadata-only bootstrap offers, collects, and retires
//! them (`chain_observation.py @ f4e366e9`).
//!
//! Sans-IO: the signed managed sessions, the offer mailbox, the descriptor verifier and the clocks
//! are traits a host implements ([`HelloSessions`], [`ObservationHost`]); nothing here opens a
//! socket or starts a thread. The step that waits on this (progress budgets, stop, the member
//! reports) is `owner-run`'s (`steps::observe`, SLICE-1) and reaches it through its `ObservePort`.
use budgets::{BudgetId as B, Entries};
use serde_json::{Map, Value};
use std::collections::BTreeSet;
use std::fmt;

/// `OBSERVATIONS_PROTOCOL` (`managed_observations.py:36`).
pub const OBSERVATIONS_PROTOCOL: &str = "mycelium.managed_hello_observations.v1";
/// `CONTROL_COMMAND_PROTOCOL` (`managed_control.py:83`): the protocol a native hello answers in.
pub const CONTROL_COMMAND_PROTOCOL: &str = "mycelium.physical_node_control.v1";
/// The bootstrap payload protocol (`chain_observation.py:prepare_observation`).
const HELLO_BOOTSTRAP_PROTOCOL: &str = "mycelium.a13.hello_bootstrap.v1";
/// Prefix of the command id of one hello (`managed_observations.py:collect...`).
const COMMAND_ID_PREFIX: &str = "managed-observation-hello-";
/// `RUN_ID_RE`: `[a-z0-9][a-z0-9-]{0,80}-qwen-[0-9]{8}T[0-9]{6}Z\Z`
/// (`managed_observations.py:37`); the prefix is 1 to `RUN_ID_PREFIX_MAX` bytes.
const RUN_ID_PREFIX_MAX: usize = 81;
const RUN_ID_MARKER: &str = "-qwen-";
/// The number of descriptors one bootstrap accepts (`chain_observation.py`: `1<=len<=8`).
const DESCRIPTORS_MIN: usize = 1;
const DESCRIPTORS_MAX: usize = 8;
/// `_SEGMENT_RE` of `managed_control.py:237`: `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`.
const SEGMENT_MAX: usize = 128;

/// A closed failure code, exactly the string v0.1.4 raises (`ManagedObservationsError.code` or the
/// `ValueError` text of `prepare_observation`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ObservationError(pub String);
impl ObservationError {
    pub fn new(code: &str) -> Self {
        Self(code.to_owned())
    }
    pub fn code(&self) -> &str {
        &self.0
    }
}
impl fmt::Display for ObservationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for ObservationError {}

// ---- the session boundary --------------------------------------------------------------------

/// `session.close()` raised.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CloseFailed;

/// One signed managed session to one member (the oracle's `ManagedSessionFactory` product).
pub trait HelloSession {
    /// `session.send(command_id=, command="hello", payload={})`. `Err(code)`: the exception's
    /// `code` attribute when it is a non-empty string.
    fn send_hello(&mut self, command_id: &str) -> Result<Value, Option<String>>;
    /// `session.close()`; `Err(CloseFailed)` when it raised.
    fn close(&mut self) -> Result<(), CloseFailed>;
    /// `getattr(session, "cleanup_error", None) is not None`.
    fn cleanup_error(&self) -> bool;
    /// `session.stop_response`: `None` when absent, else whether its `ok` `is True`.
    fn stop_response_ok(&self) -> Option<bool>;
}

/// The factory of sessions for one run/deployment (`ManagedSessionFactory`).
pub trait HelloSessions: Send {
    fn run_id(&self) -> &str;
    fn deployment_id(&self) -> &str;
    /// The nodes the factory is bound to (`factory._bindings`).
    fn bound_nodes(&self) -> BTreeSet<String>;
    /// `factory(argv=(), node_id=, run_id=, deployment_id=, timeout_seconds=)`; `Err(code)` as for
    /// [`HelloSession::send_hello`].
    fn open(
        &mut self,
        node_id: &str,
        timeout_seconds: f64,
    ) -> Result<Box<dyn HelloSession + '_>, Option<String>>;
    /// `_current_binding`: the member is still enrolled with the binding the factory captured.
    fn binding_current(&mut self, node_id: &str) -> bool;
}

/// One member's host and boot identity from its native hello.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Observation {
    pub node_id: String,
    pub host_id: String,
    pub boot_id: String,
}

/// The `mycelium.managed_hello_observations.v1` document.
#[derive(Clone, Debug, PartialEq)]
pub struct ObservationsDocument {
    pub deployment_id: String,
    pub run_id: String,
    pub captured_at_unix_ms: i64,
    pub observations: Vec<Observation>,
}
impl ObservationsDocument {
    pub fn to_value(&self) -> Value {
        let mut rows = Vec::new();
        for o in &self.observations {
            let mut row = Map::new();
            row.insert("node_id".into(), Value::from(o.node_id.as_str()));
            row.insert("host_id".into(), Value::from(o.host_id.as_str()));
            row.insert("boot_id".into(), Value::from(o.boot_id.as_str()));
            rows.push(Value::Object(row));
        }
        let mut doc = Map::new();
        doc.insert("protocol".into(), Value::from(OBSERVATIONS_PROTOCOL));
        doc.insert(
            "deployment_id".into(),
            Value::from(self.deployment_id.as_str()),
        );
        doc.insert("run_id".into(), Value::from(self.run_id.as_str()));
        doc.insert(
            "captured_at_unix_ms".into(),
            Value::from(self.captured_at_unix_ms),
        );
        doc.insert("observations".into(), Value::Array(rows));
        Value::Object(doc)
    }
}

fn err(code: &str) -> ObservationError {
    ObservationError::new(code)
}

// ---- run id, freshness ---------------------------------------------------------------------

/// `_valid_run_id`: the exact `RUN_ID_RE` (no trailing newline, lower-case only).
fn valid_run_id(run_id: &str) -> Result<&str, ObservationError> {
    let bytes = run_id.as_bytes();
    let stamp_len = 8 + 1 + 6 + 1;
    let suffix = RUN_ID_MARKER.len() + stamp_len;
    let ok = bytes.len() > suffix && (1..=RUN_ID_PREFIX_MAX).contains(&(bytes.len() - suffix)) && {
        let (prefix, tail) = bytes.split_at(bytes.len() - suffix);
        let (marker, stamp) = tail.split_at(RUN_ID_MARKER.len());
        marker == RUN_ID_MARKER.as_bytes()
            && prefix
                .iter()
                .enumerate()
                .all(|(i, b)| b.is_ascii_lowercase() || b.is_ascii_digit() || (i > 0 && *b == b'-'))
            && stamp[..8].iter().all(u8::is_ascii_digit)
            && stamp[8] == b'T'
            && stamp[9..15].iter().all(u8::is_ascii_digit)
            && stamp[15] == b'Z'
    };
    if ok {
        Ok(run_id)
    } else {
        Err(err("managed_observation_run_id_invalid"))
    }
}

/// Days since 1970-01-01 of a proleptic Gregorian date (Howard Hinnant's `days_from_civil`).
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (month + 9) % 12;
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}
fn leap(year: i64) -> bool {
    (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
}

/// `_run_started_unix`: the stamp of the run id as UTC seconds; a stamp `datetime.strptime` or the
/// `datetime` constructor refuses (month 13, 31 April, 29 February of a common year, hour 24,
/// second 60, year 0) is `run_id_invalid`.
fn run_started_unix(run_id: &str) -> Result<f64, ObservationError> {
    let invalid = || err("managed_observation_run_id_invalid");
    let stamp = &run_id[run_id.len() - 16..run_id.len() - 1];
    let number = |range: std::ops::Range<usize>| -> Result<i64, ObservationError> {
        stamp[range].parse::<i64>().map_err(|_| invalid())
    };
    let (year, month, day) = (number(0..4)?, number(4..6)?, number(6..8)?);
    let (hour, minute, second) = (number(9..11)?, number(11..13)?, number(13..15)?);
    let month_days = [
        31,
        if leap(year) { 29 } else { 28 },
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ];
    let day_ok = (1..=12).contains(&month) && day >= 1 && {
        let index = usize::try_from(month - 1).map_err(|_| invalid())?;
        day <= month_days[index]
    };
    if year < 1 || !day_ok || hour > 23 || minute > 59 || second > 59 {
        return Err(invalid());
    }
    let seconds = days_from_civil(year, month, day) * 86_400 + hour * 3_600 + minute * 60 + second;
    #[allow(clippy::cast_precision_loss)]
    Ok(seconds as f64)
}

/// `_fresh_enough(now, then)`: `-skew <= now - then <= stale` (`managed_observations.py:40-41`).
fn fresh_enough(now: f64, then: f64) -> Result<bool, ObservationError> {
    let stale = registry_seconds(B::PyMyceliumA13MacosManagedObservationsMaxStaleSecondsL40)?;
    let skew = registry_seconds(B::PyMyceliumA13MacosManagedObservationsMaxSkewSecondsL41)?;
    Ok(-skew <= now - then && now - then <= stale)
}
fn registry_seconds(id: B) -> Result<f64, ObservationError> {
    let span = id
        .span(Entries::ZERO)
        .map_err(|_| err("managed_observation_budget_unavailable"))?;
    #[allow(clippy::cast_precision_loss)]
    Ok(span.millis() as f64 / 1000.0)
}
/// `_clock_value`: a finite reading.
fn clock_value(clock: &mut dyn FnMut() -> f64) -> Result<f64, ObservationError> {
    let value = clock();
    if value.is_finite() {
        Ok(value)
    } else {
        Err(err("managed_observation_clock_invalid"))
    }
}

// ---- one native hello ------------------------------------------------------------------------

fn identity_shape(value: &str) -> bool {
    let bytes = value.as_bytes();
    let hex = |tail: &[u8]| {
        tail.len() == 32 && tail.iter().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
    };
    (bytes.starts_with(b"host-") && hex(&bytes[5..]))
        || (bytes.starts_with(b"boot-") && hex(&bytes[5..]))
}

/// `_native_hello_identity`: validates one hello response; returns `(host, boot)`.
fn native_hello_identity(
    response: &Value,
    node_id: &str,
    run_id: &str,
    deployment_id: &str,
    command_id: &str,
) -> Result<(String, String), ObservationError> {
    let malformed = || err("managed_observation_response_malformed");
    let response = response.as_object().ok_or_else(malformed)?;
    if response.get("ok") == Some(&Value::Bool(false)) {
        return Err(err("managed_observation_response_failed"));
    }
    let fields: BTreeSet<&str> = response.keys().map(String::as_str).collect();
    let expected: BTreeSet<&str> = [
        "protocol",
        "command_id",
        "node_id",
        "ok",
        "route_ready",
        "result",
    ]
    .into_iter()
    .collect();
    let is = |key: &str, text: &str| response.get(key).and_then(Value::as_str) == Some(text);
    if fields != expected
        || !is("protocol", CONTROL_COMMAND_PROTOCOL)
        || !is("command_id", command_id)
        || !is("node_id", node_id)
        || response.get("route_ready") != Some(&Value::Bool(false))
        || response.get("ok") != Some(&Value::Bool(true))
    {
        return Err(malformed());
    }
    let result = response
        .get("result")
        .and_then(Value::as_object)
        .ok_or_else(malformed)?;
    let r = |key: &str, text: &str| result.get(key).and_then(Value::as_str) == Some(text);
    if !r("protocol", CONTROL_COMMAND_PROTOCOL)
        || !r("node_id", node_id)
        || !r("run_id", run_id)
        || !r("deployment_id", deployment_id)
        || result.get("route_ready") != Some(&Value::Bool(false))
        || !r("state", "NEW")
    {
        return Err(err("managed_observation_identity_mismatch"));
    }
    let mut identities = Vec::new();
    for (kind, other_prefix) in [("host", "boot-"), ("boot", "host-")] {
        let value = result.get(&format!("{kind}_id")).unwrap_or(&Value::Null);
        if value.is_null() {
            return Err(err(&format!("managed_observation_{kind}_identity_missing")));
        }
        match value.as_str() {
            Some(text) if identity_shape(text) && !text.starts_with(other_prefix) => {
                identities.push(text.to_owned());
            }
            _ => return Err(err("managed_observation_identity_synthetic")),
        }
    }
    let boot = identities.pop().unwrap_or_default();
    let host = identities.pop().unwrap_or_default();
    Ok((host, boot))
}

/// `collect_managed_hello_observations`: hello through a fresh signed session to exactly the listed
/// members, validated for identity, freshness, distinctness and current membership. `clock` is the
/// wall clock in seconds (read twice); `token` yields the 16 hex digits of one command id.
pub fn collect_hello_observations(
    factory: &mut dyn HelloSessions,
    run_id: &str,
    deployment_id: &str,
    node_ids: &[String],
    clock: &mut dyn FnMut() -> f64,
    token: &mut dyn FnMut() -> String,
) -> Result<ObservationsDocument, ObservationError> {
    let run_id = valid_run_id(run_id)?;
    if deployment_id.is_empty() {
        return Err(err("managed_observation_deployment_invalid"));
    }
    if run_id != factory.run_id() || deployment_id != factory.deployment_id() {
        return Err(err("managed_observation_factory_scope_mismatch"));
    }
    let started_at = run_started_unix(run_id)?;
    let now = clock_value(clock)?;
    if !fresh_enough(now, started_at)? {
        return Err(err("managed_observation_run_id_stale"));
    }
    let distinct: BTreeSet<&str> = node_ids.iter().map(String::as_str).collect();
    if node_ids.is_empty()
        || node_ids.iter().any(String::is_empty)
        || distinct.len() != node_ids.len()
    {
        return Err(err("managed_observation_nodes_invalid"));
    }
    let bound = factory.bound_nodes();
    if bound.iter().map(String::as_str).collect::<BTreeSet<_>>() != distinct {
        return Err(err("managed_observation_member_not_current"));
    }
    let timeout =
        registry_seconds(B::PyMyceliumA13MacosManagedObservationsDefaultTimeoutSecondsL43)?;
    let mut observations: Vec<Observation> = Vec::new();
    let mut seen_hosts = BTreeSet::new();
    let mut seen_boots = BTreeSet::new();
    for node_id in node_ids {
        let command_id = format!("{COMMAND_ID_PREFIX}{}", token());
        let (response, close_failed) = {
            let mut session = factory.open(node_id, timeout).map_err(session_failed)?;
            let response = match session.send_hello(&command_id) {
                Ok(response) => response,
                Err(code) => {
                    // The primary send failure stays the reported outcome.
                    let secondary = session.close();
                    drop(secondary);
                    return Err(session_failed(code));
                }
            };
            // The observation succeeded, so a failed stop/close fails closed.
            let closed = session.close();
            let failed = closed.is_err()
                || session.cleanup_error()
                || session.stop_response_ok() == Some(false);
            (response, failed)
        };
        if close_failed {
            return Err(err("managed_observation_session_close_failed"));
        }
        let (host, boot) =
            native_hello_identity(&response, node_id, run_id, deployment_id, &command_id)?;
        if !seen_hosts.insert(host.clone()) {
            return Err(err("managed_observation_hosts_not_distinct"));
        }
        if !seen_boots.insert(boot.clone()) {
            return Err(err("managed_observation_boots_not_distinct"));
        }
        observations.push(Observation {
            node_id: node_id.clone(),
            host_id: host,
            boot_id: boot,
        });
    }
    // Membership must still be current after collection, not only before.
    for node_id in node_ids {
        if !factory.binding_current(node_id) {
            return Err(err("managed_observation_member_not_current"));
        }
    }
    let captured = clock_value(clock)?;
    if !fresh_enough(now, captured)? {
        return Err(err("managed_observation_captured_stale"));
    }
    #[allow(clippy::cast_possible_truncation)]
    let captured_at_unix_ms = (captured * 1000.0) as i64;
    Ok(ObservationsDocument {
        deployment_id: deployment_id.to_owned(),
        run_id: run_id.to_owned(),
        captured_at_unix_ms,
        observations,
    })
}
fn session_failed(code: Option<String>) -> ObservationError {
    match code {
        Some(code) if !code.is_empty() => ObservationError(code),
        _ => err("managed_observation_session_failed"),
    }
}

// ---- the observation context --------------------------------------------------------------

/// A published bootstrap offer (`control_mailbox.publish_offer` result); opaque to the context.
#[derive(Clone, Debug, PartialEq)]
pub struct Offer {
    pub offer_id: String,
    pub raw: Value,
}

/// Everything `prepare_observation` / `ObservationContext` reads from the app and its mailbox.
pub trait ObservationHost {
    /// `app.installation_role == 'owner'`.
    fn is_owner(&self) -> bool;
    /// `app.running`.
    fn running(&self) -> bool;
    /// `app.acquisition.prepared_source().manifest_digest`; a failure propagates unchanged.
    fn prepared_manifest_digest(&mut self) -> Result<String, ObservationError>;
    /// `coordinator.swarm_id`.
    fn swarm_id(&self) -> String;
    /// `coordinator.member(node)`; `None` when it raises.
    fn member(&self, node_id: &str) -> Option<Value>;
    /// `verify_installation_descriptor(card, member=, swarm_id=, run_id=, deployment_id=)`: the
    /// complete verified facts; a refusal propagates unchanged.
    fn verify_descriptor(
        &mut self,
        card: &Value,
        member: &Value,
        swarm_id: &str,
        run_id: &str,
        deployment_id: &str,
    ) -> Result<Value, ObservationError>;
    /// `ManagedSessionFactory(owner=owner_channel(app.authority), run_id=, deployment_id=,
    /// members=)`.
    fn session_factory(
        &mut self,
        run_id: &str,
        deployment_id: &str,
        members: &[Value],
    ) -> Result<Box<dyn HelloSessions>, ObservationError>;
    /// `control_mailbox.publish_offer(target=member, run_id=, deployment_id=, archive=)`.
    fn publish_offer(
        &mut self,
        member: &Value,
        run_id: &str,
        deployment_id: &str,
        archive: &[u8],
    ) -> Result<Offer, ObservationError>;
    /// `control_mailbox.retire_offer(offer)`.
    fn retire_offer(&mut self, offer: &Offer) -> Result<Value, ObservationError>;
    /// `control_mailbox.request_stage_cleanup(offer)`.
    fn request_stage_cleanup(&mut self, offer: &Offer) -> Result<(), ObservationError>;
}

/// `_segment` of `managed_control.py`: `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`, no trailing newline.
fn valid_segment(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=SEGMENT_MAX).contains(&bytes.len())
        && bytes
            .iter()
            .enumerate()
            .all(|(i, b)| b.is_ascii_alphanumeric() || (i > 0 && matches!(b, b'.' | b'_' | b'-')))
}

/// The bootstrap archive: a tar (Python `tarfile`, PAX format: a plain ustar header, since nothing
/// needs an extended one) holding `bootstrap.json`, mode 0600, mtime 0, whose payload is
/// `json.dumps({protocol, run_id, deployment_id, model_load_authorized: False}, sort_keys=True)`.
/// The ids are validated segments / run ids, so no JSON escaping is needed.
pub fn bootstrap_archive(run_id: &str, deployment_id: &str) -> Vec<u8> {
    let payload = format!(
        "{{\"deployment_id\": \"{deployment_id}\", \"model_load_authorized\": false, \"protocol\": \"{HELLO_BOOTSTRAP_PROTOCOL}\", \"run_id\": \"{run_id}\"}}"
    );
    let block = 512;
    let record = 10_240;
    let mut header = vec![0_u8; block];
    let mut put = |offset: usize, text: &str| {
        header[offset..offset + text.len()].copy_from_slice(text.as_bytes());
    };
    put(0, "bootstrap.json");
    put(100, "0000600\0");
    put(108, "0000000\0");
    put(116, "0000000\0");
    put(124, &format!("{:011o}\0", payload.len()));
    put(136, "00000000000\0");
    put(148, "        ");
    put(156, "0");
    put(257, "ustar\0");
    put(263, "00");
    let checksum: u32 = header.iter().map(|b| u32::from(*b)).sum();
    header[148..156].copy_from_slice(format!("{checksum:06o}\0 ").as_bytes());
    let mut archive = header;
    archive.extend_from_slice(payload.as_bytes());
    archive.resize(archive.len().div_ceil(block) * block, 0);
    archive.extend(std::iter::repeat_n(0, 2 * block));
    archive.resize(archive.len().div_ceil(record) * record, 0);
    archive
}

/// `ObservationContext`: the run's bootstrap offers and its session factory.
pub struct ObservationContext {
    pub run_id: String,
    pub deployment_id: String,
    pub members: Vec<Value>,
    /// The authenticated cards used for this observation, retained for the
    /// source compiler. Never reconstruct installation facts from a digest.
    pub descriptors: Vec<Value>,
    /// Full verified installation facts, captured before the first offer.
    pub compiler_facts: std::collections::BTreeMap<String, Value>,
    /// `facts` in insertion (descriptor) order: `(node id, verified manifest digest)`.
    pub facts: Vec<(String, String)>,
    /// `offers` in insertion order: `(node id, offer)`.
    pub offers: Vec<(String, Offer)>,
    factory: Box<dyn HelloSessions>,
    pub observations: Option<ObservationsDocument>,
}

/// `prepare_observation`: publishes metadata-only offers; targets separately consent to the hello.
/// The whole batch is validated before the first offer is published.
pub fn prepare_observation(
    host: &mut dyn ObservationHost,
    run_id: &str,
    deployment_id: &str,
    descriptors: &Value,
    acknowledged: bool,
) -> Result<ObservationContext, ObservationError> {
    if !host.is_owner() || !host.running() || !acknowledged {
        return Err(err("bootstrap_owner_consent_required"));
    }
    let run_id = valid_run_id(run_id)?;
    if !valid_segment(deployment_id) {
        return Err(err("managed_control_identifier_invalid"));
    }
    let cards = match descriptors.as_array() {
        Some(cards) if (DESCRIPTORS_MIN..=DESCRIPTORS_MAX).contains(&cards.len()) => cards,
        _ => return Err(err("bootstrap_descriptors_invalid")),
    };
    let prepared = host.prepared_manifest_digest()?;
    let swarm_id = host.swarm_id();
    let mut facts: Vec<(String, String)> = Vec::new();
    let mut compiler_facts = std::collections::BTreeMap::new();
    let mut members: Vec<Value> = Vec::new();
    for card in cards {
        let member = card
            .get("statement")
            .and_then(|statement| statement.get("target_node_id"))
            .and_then(Value::as_str)
            .and_then(|node| host.member(node).map(|member| (node.to_owned(), member)));
        let Some((node, member)) = member else {
            return Err(err("bootstrap_member_invalid"));
        };
        if facts.iter().any(|(known, _)| *known == node) {
            return Err(err("bootstrap_duplicate_member"));
        }
        let verified = host.verify_descriptor(card, &member, &swarm_id, run_id, deployment_id)?;
        let digest = verified["manifest_digest"]
            .as_str()
            .ok_or_else(|| err("installation_descriptor_facts_invalid"))?
            .to_owned();
        if digest != prepared {
            return Err(err("bootstrap_model_mismatch"));
        }
        compiler_facts.insert(node.clone(), verified);
        facts.push((node, digest));
        members.push(member);
    }
    let archive = bootstrap_archive(run_id, deployment_id);
    let factory = host.session_factory(run_id, deployment_id, &members)?;
    let mut offers = Vec::new();
    for ((node, _), member) in facts.iter().zip(&members) {
        let offer = host.publish_offer(member, run_id, deployment_id, &archive)?;
        offers.push((node.clone(), offer));
    }
    Ok(ObservationContext {
        run_id: run_id.to_owned(),
        deployment_id: deployment_id.to_owned(),
        members,
        descriptors: cards.clone(),
        compiler_facts,
        facts,
        offers,
        factory,
        observations: None,
    })
}

impl ObservationContext {
    /// `sorted(context.facts)`.
    pub fn participants(&self) -> Vec<String> {
        let mut nodes: Vec<String> = self.facts.iter().map(|(node, _)| node.clone()).collect();
        nodes.sort();
        nodes
    }

    /// `collect()`: one hello attempt covers every Mac and has no partial result. A second call
    /// after success is `observations_already_collected`; once the observations are held, the
    /// stage cleanup of every offer is requested (a failure there leaves the observations set).
    pub fn collect(
        &mut self,
        host: &mut dyn ObservationHost,
        clock: &mut dyn FnMut() -> f64,
        token: &mut dyn FnMut() -> String,
    ) -> Result<Value, ObservationError> {
        if self.observations.is_some() {
            return Err(err("observations_already_collected"));
        }
        let nodes: Vec<String> = self.facts.iter().map(|(node, _)| node.clone()).collect();
        let document = collect_hello_observations(
            &mut *self.factory,
            &self.run_id,
            &self.deployment_id,
            &nodes,
            clock,
            token,
        )?;
        self.observations = Some(document.clone());
        for (_, offer) in &self.offers {
            host.request_stage_cleanup(offer)?;
        }
        let mut value = document.to_value();
        if let Value::Object(map) = &mut value {
            map.insert("state".into(), Value::from("observed"));
            map.insert("route_ready".into(), Value::Bool(false));
        }
        Ok(value)
    }

    /// `retire_bootstrap()`: retires every offer in order; the first failure ends it (earlier
    /// offers stay retired).
    pub fn retire_bootstrap(
        &self,
        host: &mut dyn ObservationHost,
    ) -> Result<Vec<(String, Value)>, ObservationError> {
        let mut retired = Vec::new();
        for (node, offer) in &self.offers {
            retired.push((node.clone(), host.retire_offer(offer)?));
        }
        Ok(retired)
    }

    /// The hosts of the collected observations, in observation order (`context.observations`).
    pub fn observed_hosts(&self) -> Vec<String> {
        self.observations
            .as_ref()
            .map(|doc| doc.observations.iter().map(|o| o.host_id.clone()).collect())
            .unwrap_or_default()
    }
}
