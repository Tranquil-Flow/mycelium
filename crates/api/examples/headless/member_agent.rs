// SPDX-License-Identifier: AGPL-3.0-only
//! A JOINED Mac's own demo agent (`demo_app.target_agent` + `auto_step`, `demo_app.py:155-173 @
//! f4e366e9`) is `api::member_agent`, shared with the packaged host. This launcher is its host: the
//! process core's flags, lease, resource gate and acquisition, the loopback test seed path, the
//! sidecar seam, and the core's one bootstrap slot driven by `observation::target`'s worker.
use super::*;
use api::{
    bootstrap_process::TrustedBootstrap,
    managed_carrier::TargetFence,
    member_agent::{self, HostState, MemberHost, OResult, RemoteTarget, Slot},
    target_pump::TargetPump,
};
use observation::target::{self, Wake};
use runtime::managed_owner::OwnerError;

fn locked<T>(m: &Mutex<T>) -> OResult<std::sync::MutexGuard<'_, T>> {
    m.lock().map_err(|_| OwnerError("member_state_unavailable"))
}

/// The process core as the agent's host; weak, so a member pump never keeps the core alive.
pub(super) struct HeadlessMember(std::sync::Weak<Mutex<Core>>);
impl HeadlessMember {
    fn core(&self) -> OResult<Shared> {
        self.0.upgrade().ok_or(OwnerError("managed_owner_closed"))
    }
}
fn host_state(c: &Core) -> HostState {
    HostState {
        root: c.root.clone(),
        target: c.target,
        running: c.running,
        left: c.left,
        closed: c.closed,
    }
}
impl MemberHost for HeadlessMember {
    type Worker = target::Worker;
    fn state(&self) -> OResult<HostState> {
        let core = self.core()?;
        let c = locked(&core)?;
        Ok(host_state(&c))
    }
    fn lease(&self) -> Option<f64> {
        let core = self.core().ok()?;
        let c = core.lock().ok()?;
        c.session_view.and_then(|(_, lease)| lease).or_else(|| {
            c.session
                .as_ref()
                .and_then(api::member::Session::lease_expires_at)
        })
    }
    fn resource_check(&self) -> Option<String> {
        resource_check(&self.core().ok()?).map(str::to_owned)
    }
    fn acquisition(&self) -> Option<Acquisition> {
        self.core().ok()?.lock().ok()?.acquisition.clone()
    }
    fn member_seed_options(&self) -> SeedOptions {
        member_seed_options()
    }
    fn installed_sidecar(&self) -> Result<PathBuf, Failure> {
        installed_sidecar()
    }
    fn lifecycle(&self) -> OResult<Arc<Mutex<()>>> {
        let core = self.core()?;
        let c = locked(&core)?;
        Ok(c.bootstrap_lifecycle.clone())
    }
    fn slot<R>(&self, f: impl FnOnce(Slot<'_, target::Worker>) -> R) -> OResult<R> {
        let core = self.core()?;
        let mut c = locked(&core)?;
        let state = host_state(&c);
        let Core {
            bootstrap,
            bootstrap_failure,
            bootstrap_incarnation,
            ..
        } = &mut *c;
        Ok(f(Slot {
            worker: bootstrap,
            failure: bootstrap_failure,
            incarnation: bootstrap_incarnation,
            state,
        }))
    }
    fn program(&self, root: &Path) -> OResult<TrustedBootstrap> {
        target::program(root)
    }
    fn spawn(
        &self,
        pump: TargetPump<RemoteTarget<Self>>,
        fence: TargetFence,
        scope: Value,
    ) -> target::Worker {
        target::spawn(pump, fence, Arc::new(Wake::default()), scope)
    }
}

/// The member's standing-loop state over this core.
#[derive(Default)]
pub(super) struct MemberAgent {
    host: Option<Arc<HeadlessMember>>,
    agent: member_agent::MemberAgent<HeadlessMember>,
}
impl MemberAgent {
    fn host(&mut self, core: &Shared) -> Arc<HeadlessMember> {
        self.host
            .get_or_insert_with(|| Arc::new(HeadlessMember(Arc::downgrade(core))))
            .clone()
    }
    /// One standing-loop pass (`run_auto_step` -> `auto_step`).
    pub(super) fn tick(&mut self, core: &Shared) {
        let host = self.host(core);
        self.agent.tick(&host);
    }
    /// Leave / quit: no worker survives this Mac's membership.
    pub(super) fn close(&mut self, core: &Shared) {
        let host = self.host(core);
        self.agent.close(&host);
    }
}
