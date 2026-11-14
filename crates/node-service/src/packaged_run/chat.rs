// SPDX-License-Identifier: AGPL-3.0-only
//! The packaged app's chat over the owner run's route (lane CHAT; ledger row 17, owner ruling 14
//! Nov: one chat path, as v0.1.4). v0.1.4 @ f4e366e9: `DemoApp.chat_route` (app.py:1926) ->
//! `chat.handle` (chat.py:553) -> `ChatService`, whose `app.owner_run` is the owner's run:
//! `_serving` (chat.py:228-233) takes the route only while the run's state is `serving` and it has
//! a `registry_origin`; `state()` reads the run's snapshot (:477-492) and the run model's contract
//! (`_model_id`, :506-511 -> `app.run_model_contract()`, app.py:850-853).
//!
//! v0.2 (EP-PLAN U7, lane S5): the run hands the service its route gateway instead of a registry
//! origin. Here that gateway is the resident owner workflow's open epoch (`OwnerWork::gateway`:
//! `{addr, bearer}` while the epoch is open), read through [`RouteSource`] which the run's work
//! puts in the host's custody when it is composed and takes out at its teardown. No lifecycle lock
//! is held while a chat talks to that gateway: the custody lock is taken to clone the source, the
//! workflow's own state lock to clone the gateway document, and the run's view lock to snapshot
//! the status, each released before the service streams.
use super::*;
use crate::desktop_host::DesktopServices;
use api::{
    chat_service::{HttpRouteGateway, RouteGateway, RunRoute},
    preferences::run_model,
};

/// The run's open-epoch gateway reader (`OwnerWork::gateway_reader`), tied to the run it serves.
#[derive(Clone)]
pub(super) struct RouteSource {
    pub(super) run_id: String,
    pub(super) read: Arc<dyn Fn() -> Option<Value> + Send + Sync>,
}

/// `app.owner_run` as the packaged chat service sees it (`api::chat_service::RunRoute`), over the
/// run the packaged host installed into `config` (`RunConfig::installed`).
#[derive(Clone)]
pub struct RunChatRoute {
    config: RunConfig,
}
impl RunChatRoute {
    pub fn new(config: RunConfig) -> Self {
        Self { config }
    }
}
/// The open epoch's `{addr, bearer}` as the chat's HTTP gateway (the bearer stays inside the
/// gateway object: no reply, log line or timing record carries it).
fn http_gateway(gateway: &Value) -> Option<HttpRouteGateway> {
    let addr = gateway["addr"].as_str()?.parse().ok()?;
    let bearer = gateway["bearer"].as_str()?.to_owned();
    Some(HttpRouteGateway::new(addr, bearer))
}
impl RunRoute for RunChatRoute {
    /// `run.status_snapshot()` (owner_run.py:188-214): the run's view, never its live probe (the
    /// host's maintenance runs that, chat.py:478-479). `None` before the first Start.
    fn status(&self) -> Option<Value> {
        self.config
            .observed()?
            .status()
            .map(|status| status.to_value())
    }
    /// The open epoch's gateway, only while `run_id` is the run that serves (chat.py:229-233; the
    /// epoch replaces `registry_origin`). A run that does not serve, another run's epoch, or a run
    /// whose work has no open epoch has none.
    fn gateway(&self, run_id: &str) -> Option<Arc<dyn RouteGateway>> {
        let installed = self.config.installed()?;
        let status = installed.view.status()?.to_value();
        if status["state"] != "serving" || status["run_id"].as_str() != Some(run_id) {
            return None;
        }
        let source = lock(&installed.host.custody).route.clone()?;
        if source.run_id != run_id {
            return None;
        }
        let gateway = (source.read)()?;
        http_gateway(&gateway).map(|gateway| Arc::new(gateway) as Arc<dyn RouteGateway>)
    }
    /// `app.run_model_contract().model_id` (chat.py:506-511, app.py:850-853: `app_run_model(root)`,
    /// the picker choice or the environment's override).
    fn model_id(&self) -> Option<String> {
        let installed = self.config.installed()?;
        let root = installed.host.with(|c| c.authority.root.clone());
        run_model::selected_model(&root, std::env::var(run_model::ENV).ok().as_deref())
            .ok()
            .map(|model| model.model_id.to_owned())
    }
}

/// The packaged chat: `services`' `/chat/*` go through the shipped `ChatService` over the run
/// `config` installs (`DesktopServices::with_chat_run`), with `timing` as the owner's per-chat
/// timing file (admitted under the services' state root, else `chat_timing_path_refused`); the
/// run's ports get the service's `RunChats` handle ([`attach_chats`]). A host whose state was
/// refused at bind has no run: its chat answers `chat_not_ready` and nothing can cancel.
pub fn chat_run(
    services: DesktopServices,
    config: &RunConfig,
    timing: PathBuf,
) -> Result<DesktopServices, PublicError> {
    let services =
        services.with_chat_run(Arc::new(RunChatRoute::new(config.clone())), Some(timing))?;
    if let (Some(chats), Some(installed)) = (services.run_chats(), config.installed()) {
        attach_chats(&installed.host, chats);
    }
    Ok(services)
}
/// `chats` as the handle `host`'s run ports cancel the page's chats through.
pub(super) fn attach_chats(host: &PackagedRun, chats: api::chat_service::RunChats) {
    lock(&host.custody).chats = Some(chats);
}
/// The page's route chats `host`'s run ports reach (none before `chat_run`); the custody lock is
/// held only to clone the handle.
fn chats(host: &PackagedRun) -> Option<api::chat_service::RunChats> {
    lock(&host.custody).chats.clone()
}
/// v0.2's dead-member code: a participant whose seed lease ran out (the open epoch's death
/// evidence or the seed row, `OwnerWork::monitor`). v0.1.4's `_dead_member` (demo_chain.py:1188-
/// 1203) names a member that reports `error`; both are what `monitor` stores as
/// `ctx.state['monitor_error_code']` (:1179-1181). No other monitor code is (the workflow's own
/// failure, `managed_workflow_missing`, the ceiling), as in v0.1.4.
const DEAD_MEMBER: &str = "joined_route_member_stale";
/// `ctx.state['rollover_requested']` (owner_run.py:252-257), read from the run's own machine: the
/// serving poll sets it together with `rollover.state = 'switching'` while the serving step is
/// done (:254-258). A fresh run of a rollover is also `switching` until it serves (:382, 402), but
/// its steps start pending, so its serving step is never done before it serves (:429-433), and a
/// Stop that raced the rollover's due check leaves the state `idle` (:251-252, machine.rs
/// `serving_poll`). The run's view is read with no lock of the host or the run held.
fn rollover_requested(host: &PackagedRun) -> bool {
    let Some(status) = host.with(|c| c.run.demo.clone()).status() else {
        return false;
    };
    let status = status.to_value();
    status["rollover"]["state"] == "switching"
        && status["steps"].as_array().is_some_and(|steps| {
            steps
                .iter()
                .any(|step| step["name"] == "serving" && step["state"] == "done")
        })
}
/// A fresh `RunContext` (owner_run.py:392-399): no chat fact survives into the next run.
pub(super) fn new_run(host: &PackagedRun) {
    if let Some(chats) = chats(host) {
        chats.new_run();
    }
}
/// `chain.teardown` FIRST (demo_chain.py:1326-1338): every chat not done is cancelled while the
/// route still takes `DELETE`, with `'chat_switching_runs' if rollover_requested else
/// (monitor_error_code or 'owner_run_failed') if failed else 'chat_cancelled'`.
pub(super) fn teardown(host: &PackagedRun, failed: bool) {
    if let Some(chats) = chats(host) {
        if rollover_requested(host) {
            chats.rollover_requested();
        }
        chats.teardown(failed);
    }
}
/// `ceiling_reached` returned its code: the run's chats are cancelled with it, once per run, off
/// the caller's thread (demo_chain.py:1155-1163; `RunChats::ceiling_reached` keeps the once).
pub(super) fn ceiling(host: &PackagedRun, probe: &owner_run::machine::CeilingProbe) {
    if let owner_run::machine::CeilingProbe::Code(Some(code)) = probe
        && code == owner_run::machine::EXECUTION_CEILING_CODE
        && let Some(chats) = chats(host)
    {
        chats.ceiling_reached();
    }
}
/// `monitor` (demo_chain.py:1166-1186): its ceiling check cancels as `ceiling_reached` does
/// (:1167, the same once), its dead-member code becomes the teardown's code (:1179-1181).
pub(super) fn monitored(host: &PackagedRun, probe: &owner_run::machine::MonitorProbe) {
    let owner_run::machine::MonitorProbe::Code(Some(code)) = probe else {
        return;
    };
    let Some(chats) = chats(host) else {
        return;
    };
    if code == owner_run::machine::EXECUTION_CEILING_CODE {
        chats.ceiling_reached();
    } else if code == DEAD_MEMBER {
        chats.dead_member(code);
    }
}
