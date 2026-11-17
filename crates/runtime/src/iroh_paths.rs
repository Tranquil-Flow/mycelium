// SPDX-License-Identifier: AGPL-3.0-only
//! Lane XB patch 3 (REVIEW XB2-04): LIVE path evidence of this process's peer connections.
//!
//! Every connection a [`crate::iroh_host::LoopbackHost`] on another Mac's network (direct LAN,
//! or across networks) dials or accepts is registered here for its lifetime, by a WEAK handle:
//! the registry never keeps a connection open. [`snapshot`] samples each one when it is read
//! (status reads it), so the answer is what iroh selected for that peer at that moment: `relay`
//! (the Mycelium relay carries the bytes), `direct` (an IP path: same LAN, hole-punched or
//! portmapped; iroh does not say which) or `none` (no path selected yet). A closed connection
//! leaves at its next sample. The host also logs one `l11_path:` line per connection (each route
//! epoch is one) when its path is first seen, on every change and when it closes, so a run has
//! independent path evidence in its log.
//!
//! Portmapping is NOT evidence here: iroh 1.0.2 exposes a granted mapping only through its
//! `metrics` feature, which this build does not enable; status says "requested, outcome not
//! observable in this build" instead of claiming a mapping.
use std::sync::{
    Mutex,
    atomic::{AtomicU64, Ordering},
};

/// A connection's path now: `Some(kind)` while it is open (`relay` / `direct` / `none`), `None`
/// once it is gone (its entry is dropped).
pub type Sampler = Box<dyn Fn() -> Option<&'static str> + Send + Sync>;

struct Entry {
    id: u64,
    remote: String,
    direction: &'static str,
    reach: &'static str,
    sample: Sampler,
}

static ENTRIES: Mutex<Vec<Entry>> = Mutex::new(Vec::new());
static NEXT: AtomicU64 = AtomicU64::new(1);

/// One live connection as sampled now.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PathSample {
    /// The peer's endpoint id (short form).
    pub remote: String,
    /// `out` (this Mac dialled) or `in` (this Mac accepted).
    pub direction: &'static str,
    /// The run's reach the endpoint was bound in: `same_network` or `relay`.
    pub reach: &'static str,
    /// `relay`, `direct` or `none`.
    pub path: &'static str,
}

/// Keeps one connection registered; dropping it removes the entry.
#[derive(Debug)]
pub struct Registration(u64);
impl Drop for Registration {
    fn drop(&mut self) {
        held().retain(|entry| entry.id != self.0);
    }
}

fn held() -> std::sync::MutexGuard<'static, Vec<Entry>> {
    ENTRIES
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// Register a connection to `remote` (its path read by `sample` whenever status is read).
pub fn register(
    remote: String,
    direction: &'static str,
    reach: &'static str,
    sample: Sampler,
) -> Registration {
    let id = NEXT.fetch_add(1, Ordering::Relaxed);
    held().push(Entry {
        id,
        remote,
        direction,
        reach,
        sample,
    });
    Registration(id)
}

/// Every registered connection, sampled now; connections that are gone are dropped.
pub fn snapshot() -> Vec<PathSample> {
    let mut entries = held();
    let mut live = Vec::new();
    entries.retain(|entry| match (entry.sample)() {
        Some(path) => {
            live.push(PathSample {
                remote: entry.remote.clone(),
                direction: entry.direction,
                reach: entry.reach,
                path,
            });
            true
        }
        None => false,
    });
    live
}

/// The log of one registered connection: a line when its path is first seen and on every
/// change (`l11_path:`), and one when it ends (`l11_path_closed:`, also when its host closes and
/// drops this). Holds the connection's [`Registration`].
#[derive(Debug)]
pub struct PathLog {
    remote: String,
    direction: &'static str,
    reach: &'static str,
    last: Option<&'static str>,
    _registration: Registration,
}
impl PathLog {
    pub fn new(
        remote: String,
        direction: &'static str,
        reach: &'static str,
        registration: Registration,
    ) -> Self {
        Self {
            remote,
            direction,
            reach,
            last: None,
            _registration: registration,
        }
    }
    /// The path seen now; logged when it differs from the last one.
    pub fn saw(&mut self, path: &'static str) {
        if self.last != Some(path) {
            eprintln!(
                "l11_path:{path} remote={} dir={} reach={}",
                self.remote, self.direction, self.reach
            );
            self.last = Some(path);
        }
    }
}
impl Drop for PathLog {
    fn drop(&mut self) {
        eprintln!(
            "l11_path_closed:{} remote={} dir={} reach={}",
            self.last.unwrap_or("none"),
            self.remote,
            self.direction,
            self.reach
        );
    }
}

/// The kind of path `connection` runs on now (`relay`, `direct` or `none`).
pub fn path_kind(connection: &iroh::endpoint::Connection) -> &'static str {
    crate::iroh_host::selected_path(connection).unwrap_or("none")
}
