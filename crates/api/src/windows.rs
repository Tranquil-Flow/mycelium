// SPDX-License-Identifier: AGPL-3.0-only
//! Pending join windows (`DemoApp._pending_windows`, app.py:803-822, 1687-1723 @ f4e366e9): a
//! code asked for while no public relay runs is kept as "ready to share once the relay
//! starts". The list is shared with the host, whose status publishes it.
use crate::wire::Json;
use std::sync::{Arc, Mutex};

/// One code asked for while no relay runs.
#[derive(Clone, Debug, PartialEq)]
pub struct PendingWindow {
    pub pending_id: String,
    pub ttl_seconds: u32,
}
/// The shared `DemoApp._pending_windows` list.
#[derive(Clone, Default)]
pub struct PendingWindows(Arc<Mutex<Vec<PendingWindow>>>);
impl PendingWindows {
    fn with<R>(&self, f: impl FnOnce(&mut Vec<PendingWindow>) -> R) -> R {
        match self.0.lock() {
            Ok(mut list) => f(&mut list),
            Err(poisoned) => f(&mut poisoned.into_inner()),
        }
    }
    pub fn push(&self, window: PendingWindow) {
        self.with(|list| list.push(window));
    }
    pub fn len(&self) -> usize {
        self.with(|list| list.len())
    }
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
    /// Takes every pending window out of the list (`pending, self._pending_windows = list(...), []`).
    pub fn drain(&self) -> Vec<PendingWindow> {
        self.with(std::mem::take)
    }
    /// Removes the pending window with this id (`list.remove`), `false` when none matches.
    pub fn remove(&self, pending_id: &str) -> bool {
        self.with(|list| {
            let at = list.iter().position(|w| w.pending_id == pending_id);
            at.map(|at| list.remove(at)).is_some()
        })
    }
    /// `[dict(p) for p in self._pending_windows]` (status `join_flow.pending_windows`), each
    /// entry in the insertion order of `_open_pending_window`.
    pub fn json(&self) -> Json {
        Json::Arr(self.with(|list| {
            list.iter()
                .map(|w| {
                    crate::jobj! {
                        "pending_id" => w.pending_id.as_str(),
                        "ttl_seconds" => u64::from(w.ttl_seconds),
                        "state" => "waiting_for_relay",
                        "note" => "ready to share once the relay starts",
                    }
                })
                .collect()
        }))
    }
}
