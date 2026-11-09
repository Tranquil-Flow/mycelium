// SPDX-License-Identifier: AGPL-3.0-only
//! The product evidence application (`mycelium_product_spine/app.py:41-282` @ f4e366e9) with its
//! owner-only replay store (`state.py:293-383`) and the browser surface the product gateway gives it
//! (`mycelium_ui_gateway/app.py:348-352, 591-670`): `GET /api/v1/product/{snapshot,export,events}`.
//!
//! Every GET publishes one snapshot through [`ProductProjector::project`] and appends one
//! `mycelium.product_event.v1` to a bounded replay window (`replay_limit`, default 128). `events`
//! answers the retained events after `Last-Event-ID` as one finite `text/event-stream` body; a cursor
//! older than the window answers `409 product_cursor_stale`. The window survives a restart in
//! `product-evidence-state.v1.json` (0600, atomic, at most 32 MiB) and the publication sequence resumes
//! from it. Source readers are read-only; a failing reader degrades its source with a bounded reason.
use crate::{
    http::{Reply, Request},
    preferences::private_write,
    product_projector::{ProductProjector, ProjectionError, ProjectionInput},
    schema::PublicError,
    wire::{Text, dumps_sorted},
};
use serde_json::{Map, Value, json};
use std::{
    collections::{BTreeMap, VecDeque},
    fs,
    io::Read,
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};

pub const SNAPSHOT_PATH: &str = "/api/v1/product/snapshot";
pub const EVENTS_PATH: &str = "/api/v1/product/events";
pub const EXPORT_PATH: &str = "/api/v1/product/export";
pub const PATHS: [&str; 3] = [SNAPSHOT_PATH, EVENTS_PATH, EXPORT_PATH];
/// `ProductEvidenceApplication(replay_limit=128)` and its `1 <= replay_limit <= 4_096` (`app.py:57,71-77`).
pub const DEFAULT_REPLAY_LIMIT: usize = 128;
pub const MAX_REPLAY_LIMIT: usize = 4_096;
pub const STATE_FILE: &str = "product-evidence-state.v1.json";
const STATE_PROTOCOL: &str = "mycelium.product_evidence_state.v1";
/// `state.MAX_STATE_BYTES` (`state.py:18`).
const MAX_STATE_BYTES: usize = 32 * 1024 * 1024;
/// `MAX_SAFE_INTEGER` of `validate_last_event_id` (`validation.py:652-667`).
const MAX_SAFE_INTEGER: u64 = (1 << 53) - 1;
const ERROR_PROTOCOL: &str = "mycelium.product_ui.error.v1";

/// Why a source reader returned nothing (`ProductSourceUnavailable`, `app.py:21-38`).
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SourceError {
    /// A transient, bounded public reason (`[a-z][a-z0-9_]{0,63}`).
    Unavailable(&'static str),
    /// Any other failure: the source's generic `<source>_failed` code is published.
    Failed,
}

/// The read-only authority readers of one application (`app.py:44-60`).
pub trait Sources: Send + Sync {
    /// `membership_source`: the seed's current member rows.
    fn members(&self) -> Result<Vec<Map<String, Value>>, SourceError>;
    /// `assignment_source`; `None` when the composition has none (`assignment-source` unsupported).
    fn assignments(&self) -> Option<Result<Vec<Map<String, Value>>, SourceError>> {
        None
    }
    /// `route_source`: the live route's public `mycelium.live_route_status.v1`.
    fn route_status(&self) -> Result<Option<Value>, SourceError> {
        Ok(None)
    }
    /// `qualification_source`: the current `mycelium.route_qualification.v1` record.
    fn qualification(&self) -> Result<Option<Value>, SourceError> {
        Ok(None)
    }
    /// `recovery_source`: the request gateway's `mycelium.full_context_recovery_runtime.v1`.
    fn recovery(&self) -> Result<Option<Value>, SourceError> {
        Ok(None)
    }
}

/// A refusal of the application or its store: a closed public code.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct AppError(pub &'static str);

impl From<ProjectionError> for AppError {
    fn from(_: ProjectionError) -> Self {
        Self("product_snapshot_unavailable")
    }
}

/// What `events` found after publishing.
#[derive(Debug)]
pub enum Events {
    /// The retained events after the cursor, oldest first.
    After(Vec<Value>),
    /// The cursor is older than the replay window (`cursor < floor - 1`, `app.py:153-156`).
    Stale,
}

fn reason(error: &SourceError, fallback: &str) -> String {
    match error {
        SourceError::Unavailable(code) => (*code).to_owned(),
        SourceError::Failed => fallback.to_owned(),
    }
}

/// The owner-only replay store under an already-created private root (`ProductEvidenceStateStore`).
struct Store {
    root: PathBuf,
    identity: (u64, u64),
    limit: usize,
}

fn root_identity(root: &Path) -> Result<(u64, u64), AppError> {
    let invalid = AppError("product_evidence_state_root_invalid");
    let meta = fs::symlink_metadata(root).map_err(|_| invalid)?;
    if !meta.file_type().is_dir()
        || meta.uid() != rustix::process::getuid().as_raw()
        || meta.mode() & 0o7777 != 0o700
    {
        return Err(invalid);
    }
    Ok((meta.dev(), meta.ino()))
}

impl Store {
    fn open(root: &Path, limit: usize) -> Result<Self, AppError> {
        Ok(Self {
            identity: root_identity(root)?,
            root: root.to_path_buf(),
            limit,
        })
    }
    fn revalidate(&self) -> Result<(), AppError> {
        if root_identity(&self.root)? != self.identity {
            return Err(AppError("product_evidence_state_root_changed"));
        }
        Ok(())
    }
    fn load(&self) -> Result<Vec<Value>, AppError> {
        self.revalidate()?;
        let corrupt = AppError("product_evidence_state_corrupt");
        let unavailable = AppError("product_evidence_state_unavailable");
        let path = self.root.join(STATE_FILE);
        let file = match fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(&path)
        {
            Ok(file) => file,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(_) => return Err(unavailable),
        };
        let meta = file.metadata().map_err(|_| unavailable)?;
        if !meta.file_type().is_file()
            || meta.uid() != rustix::process::getuid().as_raw()
            || meta.nlink() != 1
            || meta.mode() & 0o7777 != 0o600
        {
            return Err(unavailable);
        }
        let mut raw = Vec::new();
        file.take(MAX_STATE_BYTES as u64 + 1)
            .read_to_end(&mut raw)
            .map_err(|_| unavailable)?;
        if raw.len() > MAX_STATE_BYTES {
            return Err(AppError("product_evidence_state_too_large"));
        }
        let document: Value = serde_json::from_slice(&raw).map_err(|_| corrupt)?;
        let fields = document.as_object().ok_or(corrupt)?;
        let events = fields
            .get("events")
            .and_then(Value::as_array)
            .filter(|events| events.len() <= self.limit)
            .ok_or(corrupt)?;
        if fields.len() != 3
            || fields.get("protocol") != Some(&json!(STATE_PROTOCOL))
            || fields.get("replay_limit") != Some(&json!(self.limit))
            || events
                .iter()
                .any(|event| protocol::product::event(event).is_err())
            || events
                .windows(2)
                .any(|pair| pair[1]["cursor"].as_u64() != pair[0]["cursor"].as_u64().map(|c| c + 1))
        {
            return Err(corrupt);
        }
        self.revalidate()?;
        Ok(events.clone())
    }
    /// `write` (`state.py:362-383`): the newest events whose encoding fits `MAX_STATE_BYTES`.
    fn write(&self, events: &VecDeque<Value>) -> Result<usize, AppError> {
        let mut retained: Vec<&Value> = events.iter().rev().take(self.limit).collect();
        retained.reverse();
        let payload = loop {
            let document = json!({
                "protocol": STATE_PROTOCOL,
                "replay_limit": self.limit,
                "events": retained,
            });
            let payload = dumps_sorted(&document, Text::Ascii);
            if payload.len() <= MAX_STATE_BYTES {
                break payload;
            }
            if retained.len() <= 1 {
                return Err(AppError("product_evidence_state_too_large"));
            }
            retained.remove(0);
        };
        self.revalidate()?;
        private_write(&self.root, STATE_FILE, &payload)
            .map_err(|_| AppError("product_evidence_state_write_failed"))?;
        self.revalidate()?;
        Ok(retained.len())
    }
}

/// Create (or admit) the private product state directory `<authority>/serving/product`.
pub fn private_root(parent: &Path) -> Result<PathBuf, AppError> {
    let mut root = parent.to_path_buf();
    for part in ["serving", "product"] {
        root.push(part);
        match fs::DirBuilder::new().mode(0o700).create(&root) {
            Ok(()) => fs::set_permissions(&root, fs::Permissions::from_mode(0o700))
                .map_err(|_| AppError("product_evidence_state_root_invalid"))?,
            // An existing directory is admitted only as a real directory (never a symlink).
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                if !fs::symlink_metadata(&root).is_ok_and(|meta| meta.file_type().is_dir()) {
                    return Err(AppError("product_evidence_state_root_invalid"));
                }
            }
            Err(_) => return Err(AppError("product_evidence_state_root_invalid")),
        }
    }
    root_identity(&root)?;
    Ok(root)
}

struct Inner {
    projector: ProductProjector,
    events: VecDeque<Value>,
}

/// One product evidence application (`ProductEvidenceApplication`).
pub struct ProductApp {
    sources: Arc<dyn Sources>,
    clock: Box<dyn Fn() -> u64 + Send + Sync>,
    inner: Mutex<Inner>,
    store: Option<Store>,
    limit: usize,
}

impl ProductApp {
    /// `__init__` (`app.py:44-118`): a stored window is restored and the sequence resumes after it.
    pub fn new(
        projector: ProductProjector,
        sources: Arc<dyn Sources>,
        clock: Box<dyn Fn() -> u64 + Send + Sync>,
        replay_limit: usize,
        state_root: Option<&Path>,
    ) -> Result<Self, AppError> {
        if !(1..=MAX_REPLAY_LIMIT).contains(&replay_limit) {
            return Err(AppError("product_replay_limit_invalid"));
        }
        let store = state_root
            .map(|root| Store::open(root, replay_limit))
            .transpose()?;
        let restored = match &store {
            Some(store) => store.load()?,
            None => Vec::new(),
        };
        if let Some(last) = restored.last() {
            let publication = &last["snapshot"]["publication"];
            let (Some(generation), Some(cursor)) = (
                publication["generation"].as_u64(),
                publication["cursor"].as_u64(),
            ) else {
                return Err(AppError("product_evidence_state_corrupt"));
            };
            projector
                .restore_publication(generation, cursor)
                .map_err(|_| AppError("product_publication_state_invalid"))?;
        }
        Ok(Self {
            sources,
            clock,
            inner: Mutex::new(Inner {
                projector,
                events: restored.into(),
            }),
            store,
            limit: replay_limit,
        })
    }

    fn lock(&self) -> Result<std::sync::MutexGuard<'_, Inner>, AppError> {
        self.inner
            .lock()
            .map_err(|_| AppError("product_snapshot_unavailable"))
    }

    /// `_publish` (`app.py:206-281`) under the held window lock.
    fn publish_locked(&self, inner: &mut Inner) -> Result<Value, AppError> {
        let mut errors: BTreeMap<String, String> = BTreeMap::new();
        let members = self.sources.members().unwrap_or_else(|e| {
            errors.insert(
                "membership-source".into(),
                reason(&e, "membership_source_failed"),
            );
            Vec::new()
        });
        let assignments = self.sources.assignments().map(|read| {
            read.unwrap_or_else(|e| {
                errors.insert(
                    "assignment-source".into(),
                    reason(&e, "assignment_source_failed"),
                );
                Vec::new()
            })
        });
        let route = self.sources.route_status().unwrap_or_else(|e| {
            errors.insert("route-source".into(), reason(&e, "route_source_failed"));
            None
        });
        let qualification = self.sources.qualification().unwrap_or_else(|e| {
            errors.insert(
                "qualification-source".into(),
                reason(&e, "qualification_source_failed"),
            );
            None
        });
        let recovery = self.sources.recovery().unwrap_or_else(|e| {
            errors.insert(
                "recovery-source".into(),
                reason(&e, "recovery_source_failed"),
            );
            None
        });
        let snapshot = inner.projector.project(&ProjectionInput {
            members: &members,
            assignments: assignments.as_deref(),
            route_status: route.as_ref(),
            qualification: qualification.as_ref(),
            recovery_status: recovery.as_ref(),
            now_unix_ms: (self.clock)(),
            source_errors: &errors,
        })?;
        let cursor = snapshot["publication"]["cursor"]
            .as_u64()
            .ok_or(AppError("product_snapshot_unavailable"))?;
        let event = json!({
            "protocol": "mycelium.product_event.v1",
            "cursor": cursor,
            "previous_cursor": cursor - 1,
            "event_kind": "snapshot_published",
            "snapshot": snapshot,
        });
        protocol::product::event(&event).map_err(|_| AppError("product_snapshot_unavailable"))?;
        inner.events.push_back(event);
        while inner.events.len() > self.limit {
            inner.events.pop_front();
        }
        if let Some(store) = &self.store {
            let retained = store.write(&inner.events)?;
            while inner.events.len() > retained {
                inner.events.pop_front();
            }
        }
        Ok(snapshot)
    }

    /// `GET /v1/product/snapshot` (and `/export`): publish and answer the new snapshot.
    pub fn snapshot(&self) -> Result<Value, AppError> {
        let mut inner = self.lock()?;
        self.publish_locked(&mut inner)
    }

    /// `GET /v1/product/events` (`app.py:144-191`): publish, then the retained events after `cursor`.
    pub fn events(&self, cursor: Option<u64>) -> Result<Events, AppError> {
        let mut inner = self.lock()?;
        let snapshot = self.publish_locked(&mut inner)?;
        let floor = inner.events.front().map_or_else(
            || snapshot["publication"]["cursor"].as_u64().unwrap_or(0),
            |first| first["cursor"].as_u64().unwrap_or(0),
        );
        if cursor.is_some_and(|cursor| cursor.saturating_add(1) < floor) {
            return Ok(Events::Stale);
        }
        Ok(Events::After(
            inner
                .events
                .iter()
                .filter(|event| cursor.is_none_or(|c| event["cursor"].as_u64() > Some(c)))
                .cloned()
                .collect(),
        ))
    }

    /// How many events the window holds now.
    pub fn retained(&self) -> usize {
        self.lock().map_or(0, |inner| inner.events.len())
    }
}

/// The gateway's error document (`_send_error`, `app.py:1031-1053`).
fn refusal(status: u16, code: &str, retryable: bool) -> Reply {
    let mut reply = Reply::json_bytes(
        status,
        dumps_sorted(
            &json!({"protocol": ERROR_PROTOCOL, "code": code, "retryable": retryable}),
            Text::Utf8,
        ),
    );
    if status == 405 {
        reply.headers.push(("allow".into(), "GET".into()));
    }
    reply
}

/// `validate_last_event_id` (`validation.py:652-667`): at most one ASCII decimal header.
fn last_event_id(request: &Request) -> Result<Option<u64>, ()> {
    let values: Vec<&str> = request.values("last-event-id").collect();
    match values.as_slice() {
        [] => Ok(None),
        [one] if !one.is_empty() && one.bytes().all(|b| b.is_ascii_digit()) => one
            .parse::<u64>()
            .ok()
            .filter(|v| *v <= MAX_SAFE_INTEGER)
            .map(Some)
            .ok_or(()),
        _ => Err(()),
    }
}

/// One SSE frame of the gateway (`app.py:640-659`).
fn frame(event: &Value) -> Vec<u8> {
    let mut out = format!(
        "id: {}\nevent: product_snapshot\ndata: ",
        event["cursor"].as_u64().unwrap_or(0)
    )
    .into_bytes();
    out.extend(dumps_sorted(event, Text::Utf8));
    out.extend(b"\n\n");
    out
}

/// A product route of the browser surface. `app` is `None` while the composition has no product
/// application (`_product_app is None`): every route then answers `503 product_*_unavailable`.
pub fn handle(app: Option<&ProductApp>, request: &Request) -> Result<Reply, PublicError> {
    let path = request.target.as_str();
    let unavailable = match path {
        SNAPSHOT_PATH => "product_snapshot_unavailable",
        EXPORT_PATH => "product_export_unavailable",
        EVENTS_PATH => "product_events_unavailable",
        _ => return Err(PublicError::new(404, "not_found")),
    };
    if request.method != "GET" {
        return Ok(refusal(405, "method_not_allowed", false));
    }
    let Some(app) = app else {
        return Ok(refusal(503, unavailable, true));
    };
    if path != EVENTS_PATH {
        return Ok(match app.snapshot() {
            Ok(snapshot) => Reply::json(200, &snapshot)?,
            Err(_) => refusal(503, "product_snapshot_unavailable", true),
        });
    }
    let Ok(cursor) = last_event_id(request) else {
        return Ok(refusal(400, "invalid_last_event_id", false));
    };
    Ok(match app.events(cursor) {
        Err(_) => refusal(503, "product_snapshot_unavailable", true),
        Ok(Events::Stale) => refusal(409, "product_cursor_stale", false),
        Ok(Events::After(events)) => Reply {
            status: 200,
            headers: vec![
                (
                    "content-type".into(),
                    "text/event-stream; charset=utf-8".into(),
                ),
                ("cache-control".into(), "no-store".into()),
                ("x-content-type-options".into(), "nosniff".into()),
            ],
            body: events.iter().flat_map(frame).collect(),
        },
    })
}
