// SPDX-License-Identifier: AGPL-3.0-only
use crate::{Fault, FaultKind, Host, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;

fn banned(key: &str) -> bool {
    let k = key.to_ascii_lowercase();
    [
        "secret",
        "token",
        "cookie",
        "join",
        "grant",
        "key",
        "nonce",
        "signature",
        "credential",
    ]
    .iter()
    .any(|s| k.contains(s))
        || matches!(
            k.as_str(),
            "authorization"
                | "short_code"
                | "code"
                | "handoff"
                | "request_digest"
                | "disclosure_digest"
                | "text"
        )
}
fn collect(value: &Value, secrets: &mut Vec<String>) {
    match value {
        Value::String(s) if !s.is_empty() => secrets.push(s.clone()),
        Value::Array(a) => {
            for v in a {
                collect(v, secrets);
            }
        }
        Value::Object(o) => {
            for v in o.values() {
                collect(v, secrets);
            }
        }
        _ => {}
    }
}
fn discover(value: &Value, secrets: &mut Vec<String>) {
    match value {
        Value::Object(o) => {
            for (k, v) in o {
                if banned(k) {
                    collect(v, secrets);
                } else {
                    discover(v, secrets);
                }
            }
        }
        Value::Array(a) => {
            for v in a {
                discover(v, secrets);
            }
        }
        _ => {}
    }
}
fn scrub(text: &str, secrets: &[String]) -> String {
    let mut text = text.to_owned();
    for secret in secrets {
        if !secret.is_empty() {
            text = text.replace(secret, "[redacted]");
        }
    }
    // Strip URL fragments as well as paths. Preserve whitespace and ordinary prose.
    while let Some(start) = ["http://", "https://", "mycelium:"]
        .iter()
        .filter_map(|p| text.find(p))
        .min()
    {
        let tail = &text[start..];
        let length = tail
            .find(|c: char| c.is_whitespace() || matches!(c, '"' | '<' | '>'))
            .unwrap_or(tail.len());
        text.replace_range(start..start + length, "[redacted-url]");
    }
    text
}
pub fn redact(value: &Value, known: &[String]) -> Value {
    let mut secrets = known.to_vec();
    discover(value, &mut secrets);
    secrets.sort_by_key(|s| std::cmp::Reverse(s.len()));
    secrets.dedup();
    fn visit(v: &Value, secrets: &[String]) -> Value {
        match v {
            Value::Object(o) => Value::Object(
                o.iter()
                    .filter(|(k, _)| !banned(k))
                    .map(|(k, v)| (scrub(k, secrets), visit(v, secrets)))
                    .collect(),
            ),
            Value::Array(a) => Value::Array(a.iter().map(|v| visit(v, secrets)).collect()),
            Value::String(s) => Value::String(scrub(s, secrets)),
            _ => v.clone(),
        }
    }
    visit(value, &secrets)
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Cpu {
    pub pid: u32,
    pub cpu_percent: f64,
    pub command: String,
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Metrics {
    pub load: [f64; 3],
    pub top_cpu: Vec<Cpu>,
}
impl Metrics {
    pub fn valid(&self) -> bool {
        let mut pids = std::collections::BTreeSet::new();
        self.load.iter().all(|n| n.is_finite() && *n >= 0.)
            && self.top_cpu.len() == 5
            && self.top_cpu.iter().all(|p| {
                p.pid > 0
                    && pids.insert(p.pid)
                    && p.cpu_percent.is_finite()
                    && p.cpu_percent >= 0.
                    && !p.command.is_empty()
            })
    }
}
pub fn metrics(load: [f64; 3], ps_comm: &str) -> Result<Metrics> {
    let invalid = || Fault::new("host_metrics_unobserved", FaultKind::Observation);
    if load.iter().any(|v| !v.is_finite() || *v < 0.) {
        return Err(invalid());
    }
    let mut rows = Vec::new();
    for line in ps_comm.lines().filter(|s| !s.trim().is_empty()) {
        let mut words = line.split_whitespace();
        let pid = words
            .next()
            .ok_or_else(invalid)?
            .parse::<u32>()
            .map_err(|_| invalid())?;
        let cpu_percent = words
            .next()
            .ok_or_else(invalid)?
            .parse::<f64>()
            .map_err(|_| invalid())?;
        let comm = words.collect::<Vec<_>>().join(" ");
        if pid == 0 || !cpu_percent.is_finite() || cpu_percent < 0. || comm.is_empty() {
            return Err(invalid());
        }
        let command = comm.rsplit('/').next().ok_or_else(invalid)?.to_owned();
        rows.push(Cpu {
            pid,
            cpu_percent,
            command,
        });
    }
    rows.sort_by(|a, b| {
        b.cpu_percent
            .total_cmp(&a.cpu_percent)
            .then_with(|| a.pid.cmp(&b.pid))
    });
    rows.truncate(5);
    if rows.len() != 5 {
        return Err(invalid());
    }
    Ok(Metrics {
        load,
        top_cpu: rows,
    })
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Mailbox {
    pub database_bytes: Option<u64>,
    pub storage_bytes: u64,
    pub local_files: usize,
    pub state: String,
}
pub fn mailbox(
    target_role: bool,
    target_dir: bool,
    dbs: &[Option<u64>; 2],
    sidecars: &[Option<u64>; 4],
) -> Result<Mailbox> {
    let sum = |values: Vec<u64>| {
        values.into_iter().try_fold(0u64, |sum, n| {
            sum.checked_add(n)
                .ok_or(Fault::new("mailbox_size_overflow", FaultKind::Observation))
        })
    };
    let local_files = dbs.iter().flatten().count();
    let storage_bytes = sum(dbs
        .iter()
        .chain(sidecars.iter())
        .flatten()
        .copied()
        .collect())?;
    let database_bytes = if local_files > 0 {
        Some(sum(dbs.iter().flatten().copied().collect())?)
    } else if target_role && target_dir {
        Some(0)
    } else {
        None
    };
    let state = if local_files > 0 {
        "local_db_observed"
    } else if target_role && target_dir {
        "target_remote_mailbox_no_local_db"
    } else {
        "owner_mailbox_unobserved"
    }
    .into();
    Ok(Mailbox {
        database_bytes,
        storage_bytes,
        local_files,
        state,
    })
}

pub trait Observer {
    fn status(&mut self, host: Host) -> Result<(u16, Value)>;
    fn metrics(&mut self, host: Host) -> Result<Metrics>;
    fn mailbox(&mut self, host: Host) -> Result<Mailbox>;
}
/// A failed owner's observation must never suppress the member read.
fn unavailable(e: Fault) -> Value {
    serde_json::json!({"error":e.code,"kind":e.kind,"unavailable":true})
}
pub fn sample<O: Observer>(io: &mut O, known: &[String]) -> Value {
    let mut out = serde_json::Map::new();
    for (host, name) in [(Host::Owner, "owner"), (Host::Member, "member")] {
        let status = match io.status(host) {
            Ok((http, v)) => serde_json::json!({"http_status":http,"status":v}),
            Err(e) => unavailable(e),
        };
        let metrics = match io.metrics(host) {
            Ok(v) => serde_json::json!(v),
            Err(e) => unavailable(e),
        };
        let mailbox = match io.mailbox(host) {
            Ok(v) => serde_json::json!(v),
            Err(e) => unavailable(e),
        };
        out.insert(
            name.into(),
            serde_json::json!({"observation":status,"metrics":metrics,"mailbox":mailbox}),
        );
    }
    redact(&Value::Object(out), known)
}
pub trait SnapshotStore {
    fn exists(&mut self) -> Result<bool>;
    /// Exclusive create, mode 0600, no symlinks. A race must not overwrite.
    fn create_first(&mut self, snapshot: &Value) -> Result<()>;
}
pub fn failure_snapshot<O: Observer, S: SnapshotStore>(
    io: &mut O,
    store: &mut S,
    cause: Fault,
    known: &[String],
) -> Result<()> {
    if store.exists()? {
        return Ok(());
    }
    let mut out = serde_json::Map::new();
    for (host, name) in [(Host::Owner, "owner"), (Host::Member, "member")] {
        let status = match io.status(host) {
            Ok((http, v)) => serde_json::json!({"http_status":http,"status":v}),
            Err(e) => unavailable(e),
        };
        out.insert(name.into(), status);
    }
    let mut safe = redact(&Value::Object(out), known);
    safe["first_failure"] = serde_json::json!({"code":cause.code,"kind":cause.kind});
    safe["scope"] = Value::String("full_status_denylist_redacted".into());
    store.create_first(&safe)
}
