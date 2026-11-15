// SPDX-License-Identifier: AGPL-3.0-only
//! Work endpoint custody belongs to the network lifecycle, not to the observer's HelloSession.
use super::*;
use api::{
    member_agent::MemberHost,
    run_exchange::owner_pin::{OwnerPin, VerifiedAnnouncement},
};
impl Network {
    /// The work transport for a work service or an owner run starting NOW: the configured choice,
    /// or this Mac's private LAN address read at this moment. Logs the transport it names
    /// (`l11_work_network:<transport> role=<role>`, stderr only).
    pub fn resolve_work_network(&self, role: &str) -> crate::packaged_work::NetworkChoice {
        self.resolve_run_network(role).choice
    }
    /// [`Network::resolve_work_network`] with the reason when this Mac's reading found no single
    /// private network (an owner run refuses a joined Mac by that name).
    pub fn resolve_run_network(&self, role: &str) -> crate::packaged_work::Resolved {
        let resolved = self.work_network.resolution();
        eprintln!("l11_work_network:{} role={role}", resolved.transport);
        resolved
    }
    /// The configured work transport (not resolved).
    pub fn work_network(&self) -> &crate::packaged_work::WorkNetwork {
        &self.work_network
    }
    /// The stage-worker program every work service of this host launches
    /// ([`super::NetworkConfig::native_program`]).
    pub(crate) fn native_program(&self) -> Option<api::native_compiler::NativeProgram> {
        self.native_program.clone()
    }
    pub fn close_work(&self) {
        let mut slot = lock(&self.work);
        let Some(mut work) = slot.take() else {
            return;
        };
        if let Err(code) = work.close() {
            // The service still owns member custody: keep the exact owner in its slot so the
            // next close retries it, instead of dropping it into a false success.
            eprintln!("packaged_work_cleanup_unproven:{code}");
            *slot = Some(work);
        }
    }
    pub fn sync_work_session(&self, authority: &Mutex<Authority>) {
        let a = lock(authority);
        // A renewal temporarily moves the Session out. Retain the preceding snapshot only
        // during that flight; its lease still bounds the resident dispatch admission.
        if a.session.is_some() || a.session_view.is_none() {
            *lock(&self.work_session) = a.session.clone();
        }
    }
}
impl PackagedMember {
    pub(super) fn announce_work(
        &self,
        announcement: VerifiedAnnouncement,
    ) -> Result<Option<Value>, String> {
        if (self.seams.closed)()
            || self.network.work_closed.load(Ordering::SeqCst)
            || self.network.fenced()
        {
            self.network.close_work();
            return Ok(None);
        }
        self.network.sync_work_session(&self.authority);
        if announcement.announcement()["owner_control"].is_null() {
            self.network.close_work();
            return Ok(None);
        }
        OwnerPin::from_announcement(&announcement).map_err(|e| e.0.to_owned())?;
        if let Some(code) = self.resource_check() {
            self.network.close_work();
            return Err(code);
        }
        let mut slot = lock(&self.network.work);
        if let Some(work) = slot.as_mut() {
            if let Err(code) = work.update(announcement.clone()) {
                // update has closed the old generation. A service whose close proved its
                // custody is dropped; one that still owes cleanup stays in the slot (fenced
                // from replacement and stale publication) for a later close to retry.
                if !work.cleanup_pending() {
                    *slot = None;
                }
                return Err(code);
            }
        } else {
            let root = lock(&self.authority).root.clone();
            let (identity, _, _) = app_host::identity(&root).map_err(|e| e.code.to_owned())?;
            *slot = Some(crate::packaged_work::Service::on_network(
                root,
                self.network.work_session.clone(),
                Arc::new(identity),
                announcement,
                self.network.resolve_work_network("member_work"),
                self.network.native_program(),
            )?);
        }
        if self.network.work_closed.load(Ordering::SeqCst) || self.network.fenced() {
            drop(slot);
            self.network.close_work();
            return Err("member_work_closed".into());
        }
        slot.as_ref()
            .ok_or("member_work_unavailable")?
            .registration()
            .map(Some)
    }
}
