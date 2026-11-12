// SPDX-License-Identifier: AGPL-3.0-only
//! Owned work service for an already-enrolled packaged contributor. No pairing surface.
use api::{member::Session, run_exchange::owner_pin::VerifiedAnnouncement};
use protocol::identity::Identity;
use serde_json::Value;
use std::{path::PathBuf, sync::{Arc, Mutex}};
pub struct Service;
impl Service {
    pub fn start(_root: PathBuf, _session: Arc<Mutex<Option<Session>>>, _signer: Arc<Identity>, _announcement: VerifiedAnnouncement) -> Result<Self, String> {
        Err("member_work_unavailable".into())
    }
    pub fn registration(&self) -> Result<Value, String> { Err("member_work_unavailable".into()) }
    pub fn close(&mut self) -> Result<(), String> { Ok(()) }
}
