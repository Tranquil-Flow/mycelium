// SPDX-License-Identifier: AGPL-3.0-only
//! Lazy API ownership of the admitted owner-run Runner. The host owns installation and ordered JSON;
//! the Runner alone owns the command loop, stop event, rebuild watcher and teardown joins.
use crate::{actions::Action, schema::PublicError, wire::Json};
use owner_run::{
    action::{self, DemoFacts, DemoPath, Refusal},
    machine::Identity,
    runner::{Runner, RunnerProbe, RunnerView, SystemClocks},
};
pub use owner_run::{
    chain::{StepEnv, StepError},
    runner::{Chain, Timing},
};
use serde_json::{Map, Value};
use std::sync::{Arc, Mutex, MutexGuard};

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// `time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(epoch))` (`new_run_identity`, `owner_run.py:65-68`).
pub fn utc_stamp(epoch: f64) -> String {
    let whole = epoch.floor() as i64;
    let days = whole.div_euclid(86_400);
    let rest = whole.rem_euclid(86_400);
    // civil from days (Howard Hinnant)
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}{month:02}{day:02}T{:02}{:02}{:02}Z",
        rest / 3_600,
        rest % 3_600 / 60,
        rest % 60
    )
}

/// `new_run_identity()`: a fresh run id and deployment id from the operating system's randomness.
pub fn fresh_identity(epoch: f64) -> std::io::Result<Identity> {
    use std::io::Read;
    let mut random = [0u8; 19];
    std::fs::File::open("/dev/urandom")?.read_exact(&mut random)?;
    Ok(owner_run::machine::new_run_identity(
        random,
        &utc_stamp(epoch),
    ))
}

/// Optional lazy installation is host state; the contained view is non-owning.
#[derive(Clone, Default)]
pub struct DemoView(Arc<Mutex<Option<RunnerView>>>);
impl DemoView {
    fn set(&self, view: RunnerView) {
        *lock(&self.0) = Some(view);
    }
    fn installed(&self) -> Option<RunnerView> {
        lock(&self.0).clone()
    }
    /// Clone without retaining the installation lock across a run read.
    pub fn status(&self) -> Option<Json> {
        self.installed().map(|view| status_json(view.snapshot()))
    }
    pub fn active(&self) -> bool {
        self.installed().is_some_and(|view| view.active())
    }
    /// Prepare external work first. Callback is short, nonblocking, non-reentrant and must not
    /// close/drop the owner. Lock ordering is run -> host, never host -> run.
    pub fn while_inactive<R>(&self, f: impl FnOnce() -> R) -> Option<R> {
        let installed = lock(&self.0);
        match installed.clone() {
            None => Some(f()),
            Some(view) => {
                drop(installed);
                view.while_inactive(f)
            }
        }
    }
}

/// The host maintenance's live status of the lazily installed run (`run.status()` in
/// `PublishedStatus.refresh`, published_status.py:32-48): the ceiling, rollover and monitor checks
/// on the run's own thread. Never called by a status reader.
#[derive(Clone, Default)]
pub struct DemoProbe(Arc<Mutex<Option<RunnerProbe>>>);
impl DemoProbe {
    fn set(&self, probe: RunnerProbe) {
        *lock(&self.0) = Some(probe);
    }
    /// `if run is not None: run.status()` (published_status.py:41-43); the snapshot is not the
    /// maintenance's to publish. Clones the probe without retaining the installation lock.
    pub fn refresh(&self) {
        let installed = lock(&self.0).clone();
        if let Some(probe) = installed {
            probe.status();
        }
    }
}

/// Preserve the public source key order (`owner_run.py:188-214`), independent of Value map features.
/// Nested values keep the existing host conversion, not a new serializer.
fn status_json(snapshot: Value) -> Json {
    const FIELDS: [&str; 20] = [
        "protocol",
        "state",
        "reason",
        "step",
        "failed_step",
        "error_code",
        "error_message",
        "error_fix",
        "run_id",
        "deployment_id",
        "steps",
        "registry_origin",
        "participants",
        "started_at",
        "route_ready",
        "cleanup_unproven",
        "cleanup_errors",
        "rollover",
        "compilation_cache_hit",
        "build_seconds",
    ];
    Json::Obj(
        FIELDS
            .into_iter()
            .map(|key| (key.to_owned(), Json::from(&snapshot[key])))
            .collect(),
    )
}

/// The closed codes the ladder and the run produce; anything else would be a defect, never a new code.
fn public(refusal: Refusal) -> PublicError {
    const CODES: [&str; 9] = [
        "owner_role_required",
        "invalid_request",
        "execution_ceiling_invalid",
        "owner_swarm_required",
        "relay_required",
        "owner_run_active",
        "owner_run_rebuild_requires_run",
        "owner_run_rebuild_in_progress",
        "owner_run_reason_invalid",
    ];
    let code = CODES
        .iter()
        .find(|c| **c == refusal.code)
        .copied()
        .unwrap_or("action_not_completed");
    let status = if code == "action_not_completed" {
        409
    } else {
        400
    };
    let error = PublicError::new(status, code);
    match (refusal.message, refusal.fix) {
        (Some(message), Some(fix)) => error.with_detail(message, Value::Object(fix)),
        _ => error,
    }
}

type ChainFactory = Box<dyn FnMut() -> Box<dyn Chain> + Send>;
type Identities = Box<dyn FnMut() -> Identity + Send>;

/// `app.owner_run` and its driver: installed on the first admitted demo action.
pub struct DemoRuns {
    factory: ChainFactory,
    identities: Option<Identities>,
    timing: Timing,
    driver: Option<Runner>,
    view: DemoView,
    probe: DemoProbe,
}

impl DemoRuns {
    pub fn new(factory: ChainFactory, identities: Identities, timing: Timing) -> Self {
        Self {
            factory,
            identities: Some(identities),
            timing,
            driver: None,
            view: DemoView::default(),
            probe: DemoProbe::default(),
        }
    }

    /// The status handle hosts keep beside their own state.
    pub fn view(&self) -> DemoView {
        self.view.clone()
    }
    /// The live-status handle a host's maintenance calls (never its status readers).
    pub fn probe(&self) -> DemoProbe {
        self.probe.clone()
    }

    /// `demo_app.action` (`demo_app.py:109-133`): the ladder, then the run.
    pub fn demo_action(
        &mut self,
        action: Action,
        body: &Map<String, Value>,
        facts: &mut dyn DemoFacts,
    ) -> Result<Json, PublicError> {
        let path = match action {
            Action::DemoStart => DemoPath::Start,
            Action::DemoStop => DemoPath::Stop,
            Action::DemoRebuild => DemoPath::Rebuild,
            _ => return Err(PublicError::new(409, "action_not_completed")),
        };
        let plan = action::admit(path, &Value::Object(body.clone()), facts).map_err(public)?;
        if self.driver.is_none() {
            let identities = self
                .identities
                .take()
                .ok_or(PublicError::new(409, "action_not_completed"))?;
            let driver = Runner::spawn(
                (self.factory)(),
                identities,
                Arc::new(SystemClocks::new()),
                self.timing,
            )
            .map_err(|_| PublicError::new(409, "action_not_completed"))?;
            self.view.set(driver.view());
            self.probe.set(driver.prober());
            self.driver = Some(driver);
        }
        let driver = self
            .driver
            .as_ref()
            .ok_or(PublicError::new(409, "action_not_completed"))?;
        let (snapshot, rebuild_requested) = driver.apply(&plan).map_err(public)?;
        let mut answer = status_json(snapshot);
        if rebuild_requested {
            answer.set("rebuild_requested", Json::Bool(true));
        }
        Ok(answer)
    }

    /// `run.status_snapshot()`; `None` until the first demo action (the host shows the idle document).
    pub fn status(&self) -> Option<Json> {
        self.view.status()
    }
    /// `OwnerRun.active()`.
    pub fn active(&self) -> bool {
        self.view.active()
    }
    /// `if run is not None and run.active(): run.stop()` (`demo_app.close`, demo_app.py:201-202),
    /// without the join: the caller joins with [`DemoRuns::close`].
    pub fn stop(&self) {
        if let Some(driver) = self.driver.as_ref().filter(|d| d.active()) {
            driver.stop();
        }
    }
    pub fn quiescent(&self) -> bool {
        self.driver.as_ref().is_none_or(Runner::quiescent)
    }
    /// `DemoApp.close`: stop the run and end its thread.
    pub fn close(&mut self) {
        if let Some(driver) = self.driver.take() {
            driver.close();
        }
    }
}
