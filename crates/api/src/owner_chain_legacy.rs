// SPDX-License-Identifier: AGPL-3.0-only
//! Compatibility for the original one-object API ports. All step/state logic is
//! owned by OwnerChain. These proxies borrow a host collaborator for one call;
//! no temporary borrow or Runner/Host is retained in a callback.
use super::{ChainPorts, HostChain, ModelAdapter, SharedHostChain};
use owner_run::{
    chain::{StepEnv, StepError},
    machine::{CeilingProbe, MonitorProbe, Reason},
    owner_chain::{ChainPorts as OwnerPorts, Ports},
    runner::Chain,
    state::RunState,
    steps::{
        contributor::ContributorPort,
        exchange::{Announcement, ClockSkew, MemberRow, ReportsPort},
        model::{PreparedSource, Reverify},
    },
};
use serde_json::Value;
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex, MutexGuard},
};

struct Shared<P>(Arc<Mutex<P>>);
impl<P> Clone for Shared<P> {
    fn clone(&self) -> Self {
        Self(self.0.clone())
    }
}
impl<P> Shared<P> {
    fn lock(&self) -> MutexGuard<'_, P> {
        self.0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }
}
impl<P: ChainPorts> ChainPorts for Shared<P> {
    fn owner_and_running(&self) -> bool {
        self.lock().owner_and_running()
    }
    fn swarm_created(&self) -> bool {
        self.lock().swarm_created()
    }
    fn acquisition_state(&self) -> String {
        self.lock().acquisition_state()
    }
    fn reverify_available(&self) -> bool {
        self.lock().reverify_available()
    }
    fn reverify(&self) -> Reverify {
        self.lock().reverify()
    }
    fn prepared_source(&self) -> Result<PreparedSource, StepError> {
        self.lock().prepared_source()
    }
    fn storage_consent_cap(&self) -> Result<Option<Value>, StepError> {
        self.lock().storage_consent_cap()
    }
    fn check_rollover_members(&self, binding: &Value) -> Result<(), StepError> {
        self.lock().check_rollover_members(binding)
    }
}
impl<P: ChainPorts> ContributorPort for Shared<P> {
    fn ensure_local_contributor(&mut self) -> Result<String, StepError> {
        self.lock()
            .contributor()
            .ok_or_else(|| {
                super::unwired(
                    "contributor",
                    "NP-L10-contributor",
                    "SE-L10-LOCAL-CONTRIBUTOR",
                )
            })?
            .ensure_local_contributor()
    }
    fn exchange_available(&self) -> bool {
        self.lock()
            .contributor()
            .is_some_and(|p| p.exchange_available())
    }
    fn attach_agent(&mut self, local: &str) -> Result<(), StepError> {
        self.lock()
            .contributor()
            .ok_or_else(|| {
                super::unwired(
                    "contributor",
                    "NP-L10-contributor",
                    "SE-L10-LOCAL-CONTRIBUTOR",
                )
            })?
            .attach_agent(local)
    }
}
impl<P: ChainPorts> Shared<P> {
    fn with_reports<R>(&self, f: impl FnOnce(&mut dyn ReportsPort) -> R) -> R {
        let mut host = self.lock();
        // Capability is sampled by ports(), on this same sole-owner run thread.
        // Implementations must keep a returned capability available for that step.
        f(host
            .reports()
            .expect("host removed a borrowed reports capability"))
    }
}
impl<P: ChainPorts> ReportsPort for Shared<P> {
    fn forget_reports(&mut self) {
        self.with_reports(|p| p.forget_reports());
    }
    fn announce(&mut self, a: &Announcement) -> Result<(), StepError> {
        self.with_reports(|p| p.announce(a))
    }
    fn announce_work(&mut self, a: &Announcement, limits: &Value) -> Result<(), StepError> {
        self.with_reports(|p| p.announce_work(a, limits))
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        self.with_reports(|p| p.reports())
    }
    fn members(&mut self) -> Vec<MemberRow> {
        self.with_reports(|p| p.members())
    }
    fn own_node_id(&mut self) -> Option<String> {
        self.with_reports(|p| p.own_node_id())
    }
    fn member_identity(&mut self, node: &str) -> Value {
        self.with_reports(|p| p.member_identity(node))
    }
    fn clock_skew(&mut self, nodes: &[String]) -> Result<Option<ClockSkew>, StepError> {
        self.with_reports(|p| p.clock_skew(nodes))
    }
    fn tick_local(&mut self) {
        self.with_reports(|p| p.tick_local());
    }
}

pub(super) struct LegacyPorts<P: ChainPorts> {
    model: ModelAdapter<Shared<P>>,
    contributor: Shared<P>,
    reports: Shared<P>,
}
impl<P: ChainPorts> OwnerPorts for LegacyPorts<P> {
    fn ports(&mut self) -> Ports<'_> {
        let (contributor, reports) = {
            let mut p = self.reports.lock();
            (p.contributor().is_some(), p.reports().is_some())
        };
        Ports {
            model: &mut self.model,
            contributor: contributor.then_some(&mut self.contributor as &mut dyn ContributorPort),
            reports: reports.then_some(&mut self.reports as &mut dyn ReportsPort),
            observe: None,
            compile: None,
        }
    }
}

impl<P: ChainPorts> HostChain<P> {
    pub fn new(ports: P) -> Self {
        let shared = Shared(Arc::new(Mutex::new(ports)));
        Self {
            inner: SharedHostChain::new(LegacyPorts {
                model: ModelAdapter::new(shared.clone()),
                contributor: shared.clone(),
                reports: shared.clone(),
            }),
            host: shared.0,
        }
    }
    pub fn state(&self) -> Option<&RunState> {
        self.inner.state()
    }
}
impl<P: ChainPorts> Chain for HostChain<P> {
    fn new_run(
        &mut self,
        run: &str,
        deployment: &str,
        reason: Reason,
        ceiling: Option<&Value>,
        binding: Option<&Value>,
    ) {
        self.inner
            .new_run(run, deployment, reason, ceiling, binding);
    }
    fn run_step(&mut self, step: &str, env: &mut dyn StepEnv) -> Result<(), StepError> {
        // The original constructor has no run state before new_run. Preserve
        // its preflight order without recreating any step implementation.
        if self.inner.state().is_none() && matches!(step, "model" | "descriptors") {
            return Err(StepError::Exception {
                class: "KeyError".into(),
            });
        }
        let result = self.inner.run_step(step, env);
        if self.inner.state().is_none() && step == "contributor" && result.is_ok() {
            return Err(StepError::Exception {
                class: "KeyError".into(),
            });
        }
        if step == "model" && result.is_ok() {
            let choice = self
                .inner
                .state()
                .and_then(|s| s.model.as_ref())
                .and_then(|m| m.choice.as_deref());
            self.host
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .note_model(choice);
        }
        result
    }
    fn rollover_binding(&self) -> Option<Value> {
        self.inner.rollover_binding()
    }
    fn teardown(&mut self, failed: bool, env: &mut dyn StepEnv) -> Result<(), String> {
        self.inner.teardown(failed, env)
    }
    fn rollover_due(&mut self) -> Option<bool> {
        self.inner.rollover_due()
    }
    fn ceiling_reached(&mut self) -> CeilingProbe {
        self.inner.ceiling_reached()
    }
    fn monitor(&mut self) -> MonitorProbe {
        self.inner.monitor()
    }
}
