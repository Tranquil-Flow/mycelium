// SPDX-License-Identifier: AGPL-3.0-only
//! Per-endpoint, test-only clock/transport injection. No global time advance and no sleeps.
#![allow(dead_code)]
use serde_json::Value;
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex, OnceLock},
    time::{Duration, Instant},
};

type Call = Box<dyn FnMut(&Value) -> Result<Value, String> + Send>;
pub(crate) struct Hook {
    pub call: Call,
    pub age: Arc<Mutex<Option<Duration>>>,
}
fn hooks() -> &'static Mutex<BTreeMap<iroh::EndpointId, Hook>> {
    static HOOKS: OnceLock<Mutex<BTreeMap<iroh::EndpointId, Hook>>> = OnceLock::new();
    HOOKS.get_or_init(Mutex::default)
}
pub(crate) fn install(id: iroh::EndpointId, hook: Hook) {
    assert!(hooks().lock().unwrap().insert(id, hook).is_none());
}
pub(crate) fn remove(id: iroh::EndpointId) {
    assert!(hooks().lock().unwrap().remove(&id).is_some());
}
pub(crate) fn call(id: iroh::EndpointId, request: &Value) -> Option<Result<Value, String>> {
    hooks()
        .lock()
        .unwrap()
        .get_mut(&id)
        .map(|h| (h.call)(request))
}
pub(crate) fn age(
    id: iroh::EndpointId,
    pending: &mut std::collections::VecDeque<(u64, String, Instant)>,
) {
    let hooks = hooks().lock().unwrap();
    if let Some(hook) = hooks.get(&id) {
        if let Some(age) = *hook.age.lock().unwrap() {
            if let Some((_, name, began)) = pending.front_mut() {
                if name == "serve" {
                    *began = Instant::now().checked_sub(age).unwrap();
                }
            }
        }
    }
}
