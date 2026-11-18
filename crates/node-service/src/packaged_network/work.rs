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
        // Lane XB patch 2 (D1): an owner run binds in the mode its owner chose when the run
        // started ([`Network::begin_owner_run`]); nothing re-reads a file mid-run.
        let network = self
            .owner_run_network()
            .unwrap_or_else(|_| self.work_network.clone());
        let resolved = network.resolution();
        eprintln!("l11_work_network:{} role={role}", resolved.transport);
        resolved
    }
    /// Lane XB patch 2 (D1): the owner reads its choice for the run starting now, once; the
    /// run keeps it, and its signed form is what the run announcement carries.
    pub fn begin_owner_run(
        &self,
    ) -> Result<api::run_exchange::work_network::RunReach, &'static str> {
        self.reach.begin_run(&self.root)
    }
    /// Lane XB patch 2 (XB-05): the window's `/api/network/reach` action on this Mac's
    /// preferences (`crate::packaged_work::reach::action`).
    pub(crate) fn reach_action(
        &self,
        body: &serde_json::Map<String, Value>,
        owner: bool,
    ) -> Result<Value, &'static str> {
        crate::packaged_work::reach::action(&self.root, &self.reach, body, owner)
    }
    /// The owner's run ended: no active choice, no active endpoints.
    pub fn end_owner_run(&self) {
        self.reach.end_run();
    }
    /// The owner run's network: the configured one for the same network (or without a run);
    /// across networks this Mac's reading under its relay and portmapping preference. A choice
    /// this Mac cannot bind is a named refusal (`network_reach_invalid`, `work_network_*`).
    pub fn owner_run_network(&self) -> Result<crate::packaged_work::WorkNetwork, &'static str> {
        use crate::packaged_work::reach;
        match self.reach.run_binding() {
            None => Ok(self.work_network.clone()),
            Some(binding) => {
                // Lane XB patch 3 (REVIEW XB2-05): the portmapping frozen at the run's start.
                let (signed, portmapping) = binding?;
                let portmapping = match signed {
                    api::run_exchange::work_network::RunReach::SameNetwork => false,
                    _ => portmapping,
                };
                reach::follow(&self.work_network, self.reach.relay(), &signed, portmapping)
            }
        }
    }
    /// Lane XB patch 2 (D1): a joined Mac's work transport for the run `announcement` signs:
    /// exactly the owner's signed mode, under this Mac's relay (which the signed relay must
    /// equal, D4) and this Mac's portmapping preference (D2). Named refusals, never a fallback.
    pub(crate) fn member_network(
        &self,
        announcement: &VerifiedAnnouncement,
    ) -> Result<crate::packaged_work::NetworkChoice, &'static str> {
        use crate::packaged_work::reach;
        use api::run_exchange::work_network::RunReach;
        // Lane XB patch 4 (REVIEW XB3-03): this run's path window starts with its first work
        // announcement (status lists this run's connections only).
        self.reach.member_paths(
            announcement.announcement()["run_id"]
                .as_str()
                .unwrap_or_default(),
        );
        let signed = RunReach::from_announcement(announcement)?;
        let portmapping = match signed {
            RunReach::SameNetwork => false,
            RunReach::Relay(_) => self.reach.member_portmapping(
                announcement.announcement()["run_id"]
                    .as_str()
                    .unwrap_or_default(),
                &self.root,
            )?,
        };
        let network = reach::follow(&self.work_network, self.reach.relay(), &signed, portmapping)?;
        let resolved = network.resolution();
        eprintln!(
            "l11_work_network:{} role=member_work signed={}",
            resolved.transport,
            signed.name()
        );
        // Lane XB patch 3 (REVIEW XB2-01): the bound mode is the signed mode, or a named refusal.
        reach::bound_as_signed(&signed, &resolved.choice)?;
        Ok(resolved.choice)
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
        self.close_work_proven().unwrap_or(());
    }
    /// [`Self::close_work`] with its verdict: `Err` while the service still owns member custody.
    pub(super) fn close_work_proven(&self) -> Result<(), String> {
        self.reach.deactivate();
        let mut slot = lock(&self.work);
        let Some(mut work) = slot.take() else {
            return Ok(());
        };
        if let Err(code) = work.close() {
            // The service still owns member custody: keep the exact owner in its slot so the
            // next close retries it, instead of dropping it into a false success.
            eprintln!("packaged_work_cleanup_unproven:{code}");
            *slot = Some(work);
            return Err(code);
        }
        Ok(())
    }
    pub fn sync_work_session(&self, authority: &Mutex<Authority>) {
        let a = lock_authority(authority);
        // A renewal temporarily moves the Session out. Retain the preceding snapshot only
        // during that flight, and only while it stands in (lane FLK, `api::renewal`: not after
        // it unwound, ran past its deadline or was refused); its lease still bounds the resident
        // dispatch admission.
        let in_flight = a
            .member_flight
            .as_ref()
            .is_some_and(api::renewal::Flight::stands_in);
        if a.session.is_some() || !in_flight {
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
            self.network.reach.clear_refusal();
            return Ok(None);
        }
        OwnerPin::from_announcement(&announcement).map_err(|e| e.0.to_owned())?;
        if let Some(code) = self.resource_check() {
            self.network.close_work();
            return Err(code);
        }
        // Lane XB patch 3 (REVIEW XB2-02): EVERY announcement's signed reach is validated in
        // full (closed shape, version, this Mac's relay) before a service may be reused; a
        // malformed or foreign field is a named refusal and closes the service (no stale
        // publication), a valid change of mode closes it and binds anew in the signed mode.
        let signed = announcement.announcement().clone();
        let bound = lock(&self.network.work)
            .as_ref()
            .map(|work| work.run_reach().clone());
        match crate::packaged_work::reach::next_service(
            bound.as_ref(),
            api::run_exchange::work_network::RunReach::from_announcement(&announcement),
            self.network.reach.relay(),
        ) {
            Err(code) => {
                self.network.close_work();
                self.network.reach.refuse(&signed, code);
                return Err(code.to_owned());
            }
            Ok(crate::packaged_work::reach::Next::Rebind) if bound.is_some() => {
                self.network.close_work();
                if lock(&self.network.work).is_some() {
                    // The old service still owes its cleanup: no new one beside it.
                    return Err("packaged_work_cleanup_unproven".into());
                }
            }
            Ok(_) => {}
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
            let root = lock_authority(&self.authority).root.clone();
            let (identity, _, _) = app_host::identity(&root).map_err(|e| e.code.to_owned())?;
            // D1 / D3: the signed mode, or a named refusal on this Mac's card and in its report
            // to the owner (never a silent fallback, never the owner's timeout).
            let refuse = |code: &str| {
                self.network.reach.refuse(&signed, code);
                code.to_owned()
            };
            let choice = self.network.member_network(&announcement).map_err(refuse)?;
            *slot = Some(
                crate::packaged_work::Service::on_network(
                    root,
                    self.network.work_session.clone(),
                    Arc::new(identity),
                    announcement,
                    choice.clone(),
                    self.network.native_program(),
                )
                .map_err(|code| match code.as_str() {
                    runtime::iroh_host::RELAY_UNREACHABLE => refuse(&code),
                    _ => code,
                })?,
            );
            self.network.reach.activate(&choice);
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
