// SPDX-License-Identifier: AGPL-3.0-only
//! The owner run's chain-port composition both hosts need (lane PH3b S3; moved unchanged from the
//! headless launcher, `examples/headless.rs`): the owner's own local contributor
//! (`local_contributor.py`, app.py:882-909 @ f4e366e9), the run exchange
//! (`demo_app.ensure_exchange`, app.py:1245-1250), what `demo_app.action` asks the app
//! (demo_app.py:109-133), the in-process local agent's collaborators (`chain.agent`) and the
//! contributor and descriptors ports (`AppDemoChain._step_contributor/_step_descriptors`,
//! demo_chain.py:509-725). A host implements [`RunHost`] over its own state; no step logic lives here.
use crate::{
    acquisition::{self, Acquisition},
    app_host::{
        Authority, IDENTITY_FILE, ManagedSlot, SEED_IDENTITY_FILE, build_pair_request, hex,
        identity, identity_in, now, random,
    },
    handoff::{self, Coordinator, Entropy},
    installation_descriptor::{self, Failure, Member as DescriptorMember, Paths, Source},
    jobj,
    joined_member::planning_caps,
    local_agent::{AgentHost, LocalAgent},
    owner_driver::DemoView,
    preferences::{resource_policy, run_model},
    run_exchange::{Announce, RunExchange},
    schema::PublicError,
    slice_fetch::{SliceTransport, SnapshotRangeTransport},
    wire::Json,
};
use owner_run::{
    action::DemoFacts,
    chain::StepError,
    machine::OwnerRunError,
    steps::{
        contributor::ContributorPort,
        exchange::{Announcement, ClockSkew, MemberRow, ReportsPort},
    },
};
use protocol::identity::Identity;
use serde_json::{Map, Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};

fn failure() -> PublicError {
    PublicError::new(409, "action_not_completed")
}

/// The live side of the owner's local contributor: its node and membership session.
pub struct ContributorState {
    pub node: String,
    pub signer: Arc<Identity>,
    pub session: Option<crate::member::Session>,
    /// The session's lease while the renewal loop has the session out on the wire.
    pub lease: Option<f64>,
    /// The renewal holding `session` out on the wire, if any (lane FLK, [`crate::renewal`]).
    pub flight: Option<crate::renewal::Flight>,
    /// The seed refused this membership's renewal ([`crate::renewal::membership_refused`]): its
    /// Session was not put back; readers refuse with this code until the contributor is replaced.
    pub refused: Option<&'static str>,
}

/// What a host keeps for the owner run beside its authority: `DemoApp.local_contributor`,
/// `DemoApp.run_exchange` and `DemoApp.owner_run` as the status and the gates read it.
#[derive(Default)]
pub struct RunFields {
    pub contributor: Option<ContributorState>,
    pub exchange: Option<Arc<RunExchange>>,
    pub demo: DemoView,
}

/// One acquisition of the host's state: the authority, the run fields and what the host keeps
/// beside them (`DemoApp.closed`, the member's `_left`, whether a public relay object exists).
pub struct RunView<'a, M: ManagedSlot> {
    pub authority: &'a mut Authority<M>,
    pub run: &'a mut RunFields,
    pub closed: bool,
    pub left: bool,
    pub relay_absent: bool,
}

/// The host side of the owner run's composition.
pub trait RunHost: Clone + Send + Sync + 'static {
    type Managed: ManagedSlot;
    /// Run `f` on the state as one snapshot; a poisoned lock still yields it.
    fn with<R>(&self, f: impl FnOnce(&mut RunView<'_, Self::Managed>) -> R) -> R;
    /// Serializes bootstrap side effects and contributor cleanup; never held with the state on entry.
    fn bootstrap_lifecycle(&self) -> Arc<Mutex<()>>;
    /// `authority.status().get('membership_state') == 'current'`.
    fn membership_current(&self) -> bool;
    /// Durable admission of a contributor Start (no unsettled native bootstrap of an earlier one).
    fn admit_contributor(&self, root: &Path) -> Result<(), PublicError>;
    /// Close the contributor's native bootstrap (the caller holds the bootstrap lifecycle).
    fn close_contributor(&self) -> Result<(), PublicError>;
    /// `app._resource_check()`.
    fn resource_check(&self) -> Option<&'static str>;
    /// `app.acquisition`.
    fn acquisition(&self) -> Option<Acquisition>;
    /// The member's staged hello for the owner's bootstrap offer (`demo_target` bootstrap).
    fn bootstrap(
        &self,
        node: &str,
        run: &str,
        deployment: &str,
        offer: &str,
    ) -> Result<(), Failure>;
    /// `installed_sidecar()`: the native sidecar this host names in its descriptor.
    fn sidecar(&self) -> Result<PathBuf, Failure>;
    /// `actor.pending_cleanup()` of the owner's own contributor `node`: the offer whose stage
    /// cleanup the owner requested, if any. Without a producer nothing is pending.
    fn pending_cleanup(&self, _node: &str) -> Result<Option<Value>, Failure> {
        Ok(None)
    }
    /// `_cleanup_if_requested`'s stop and cleanup accept for the contributor `node`. Without a
    /// producer it refuses (the run's cleanup wait then names the Mac).
    fn cleanup(&self, _node: &str, _request: &Value) -> Result<(), Failure> {
        Err(Failure::new("RuntimeError", "member_cleanup_unavailable"))
    }
    /// The wall clock the contributor's membership renewal reads ([`crate::app_host::now`]). A
    /// test host may step it (lane FLK patch 3: a local lease-expiry check is no seed refusal).
    fn renewal_clock(&self) -> f64 {
        crate::app_host::now()
    }
}

/// `<root>/local-contributor`: the contributor's own state directory (identity, membership, consent).
pub fn contributor_dir(root: &Path) -> PathBuf {
    root.join("local-contributor")
}
fn session_lease(session: &Option<crate::member::Session>) -> Option<f64> {
    session
        .as_ref()
        .and_then(crate::member::Session::lease_expires_at)
}

/// `create_local_contributor(owner_app)` behind `_start_local_contributor`: the consent gate, then the member
/// joins its own owner's seed over the pinned local TLS - a retained membership resumes
/// (`LocalOwnerMember.start`), a first one is paired (`pair_request` -> `approve_pairing(ttl 120)` ->
/// `accept_pairing`). v0.1.4 does not renew at once, so the member stays `NEW` until the renewal loop's first
/// pass (executed: `status_after_start` lifecycle `NEW`).
pub fn start_local_contributor<H: RunHost>(host: &H) -> Result<Json, PublicError> {
    let lifecycle = host.bootstrap_lifecycle();
    let _lifecycle = lifecycle.lock().map_err(|_| failure())?;
    let (root, runtime, record, already, target, running, has_policy) = host.with(|c| {
        (
            c.authority.root.clone(),
            c.authority.seed.clone(),
            c.authority.swarm.clone(),
            c.run.contributor.is_some(),
            c.authority.target,
            c.authority.running,
            resource_policy::read(&c.authority.root).ok().flatten(),
        )
    });
    if already {
        return Err(PublicError::new(400, "local_contributor_already_running"));
    }
    host.admit_contributor(&root)?;
    let current = host.membership_current();
    // `local_contributor_owner_consent_required` / `local_owner_unavailable` are ValueErrors (409).
    let (Some(policy), Some(runtime), Some(record)) = (has_policy, runtime, record) else {
        return Err(failure());
    };
    if target || !running || !current {
        return Err(failure());
    }
    let dir = contributor_dir(&root);
    fs::create_dir_all(&dir).map_err(|_| failure())?;
    fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700))
        .map_err(|_| failure())?;
    let swarm_id = record["swarm_id"].as_str().ok_or_else(failure)?.to_owned();
    // A retained contributor membership is pinned to the seed URL it joined, and every quick-tunnel
    // start mints a new origin. `open_owner_seed` repins the owner's own member; the local
    // contributor needs the same, or it can never renew against the live seed and "This Mac joins
    // the run" fails with `RemoteMacMemberError` on every run after the first relay restart.
    if crate::peer::load_membership_state(&dir)?.is_some() {
        crate::peer::repin_owner_seed_url(&dir, &runtime.seed_url)?;
    }
    let (signer, _, node) = identity(&dir)?;
    let options = runtime.options();
    let random_hex = |n: usize| random(n).map(|b| hex(&b)).unwrap_or_default();
    let env = crate::peer::JoinEnv {
        signer: &signer,
        clock: &now,
        random_hex: &random_hex,
        seed: &options,
    };
    let session = if crate::peer::load_membership_state(&dir)?.is_some() {
        match crate::peer::start(&dir, &signer, true, &env) {
            Ok(started) => started.session,
            Err(failed) => return Err(failed.error),
        }
    } else {
        let (seed_signer, _, _) = identity_in(&root, SEED_IDENTITY_FILE)?;
        let coordinator = Coordinator {
            signer: seed_signer,
            swarm_id,
            seed_url: runtime.seed_url.clone(),
        };
        let storage = policy.get("storage_bytes").cloned().unwrap_or(Value::Null);
        let quota = json!({"storage_bytes": storage, "network_bytes_per_day": storage, "background_allowed": false});
        let request = build_pair_request(&dir, &dir)?;
        let handoff = handoff::approve_pairing(
            &root,
            true,
            Some(&coordinator),
            &request.to_value(),
            &json!(120),
            &quota,
            now(),
            &Entropy::os,
        )?;
        match crate::peer::accept_handoff(&dir, true, &handoff.to_value(), &env) {
            Ok(accepted) => accepted.session,
            Err(error) => return Err(error),
        }
    };
    if session.is_none() {
        return Err(failure());
    }
    // The owner's own worker is a member like every other one: a shard receive loads
    // `standing-consent.json` from the worker's root (`NativeShard::try_new`). Record the grant the
    // user acknowledged (`peer_visibility` in the resource policy) and bind it to the membership this
    // worker just accepted, so its own stage is served like a peer's.
    // Its shard receive reserves disk under ITS root's resource policy (`compiler_inputs`
    // `reserve_output`), as a joined Mac's does under the policy its join wrote
    // (`joined_member::work`): the owner's own worker is this Mac, so that policy is the owner's
    // current one. Without it every real run refused the owner's own stage at the slices step
    // (`compiler_storage_consent_required`).
    resource_policy::write(&dir, &policy)?;
    grant_self_consent(&dir, &node, &policy)?;
    let lease = session_lease(&session);
    host.with(|c| {
        c.run.contributor = Some(ContributorState {
            node: node.clone(),
            signer: Arc::new(signer),
            session,
            lease,
            flight: None,
            refused: None,
        });
    });
    Ok(jobj! {
        "created" => true, "physical_hosts" => 1u64, "node_id" => node,
        "membership_state" => "current", "route_ready" => false,
    })
}

/// `grant_standing_consent` + `bind_standing_consent` (`standing_consent.py:139,161`) for the
/// owner's own worker.
///
/// Caps come from the resource policy the user chose (the storage limit is theirs; the wall and
/// command budgets are this app's own grant defaults) and the grant is bound to the membership the
/// worker just accepted, so a different owner never inherits it.
fn grant_self_consent(
    dir: &std::path::Path,
    node: &str,
    policy: &Value,
) -> Result<(), PublicError> {
    use crate::preferences::standing_consent;
    let Some(state) = crate::peer::load_membership_state(dir)? else {
        return Ok(());
    };
    // An existing grant is the user's: never re-grant over it (different caps are a refusal, not an
    // update). Only a fresh worker - or one whose grant was withdrawn - needs one recorded.
    if standing_consent::load(dir)
        .map_err(|e| PublicError::new(409, e.0))?
        .is_some_and(|record| record["state"] != "withdrawn")
    {
        return Ok(());
    }
    let storage = policy
        .get("storage_bytes")
        .and_then(Value::as_u64)
        .unwrap_or(4_000_000_000);
    // The bounds the app's own grant uses: the user's storage limit, the standing consent's own
    // maximum wall, and the same memory/command ceilings the join path records. The wall is the
    // maximum because the compile gives the local node the proposal's wall up to this grant (lane
    // A, B8: `owner_compiler::local_grant` reads this contributor's stored grant); a lower grant
    // (it was `MemberChosenWall`, a member's choice in a v0.1.4 test fixture) made the contributor
    // refuse its own share, `member_wall_cap_below_share`.
    // Caps are integers (`_int_in`): a float wall is refused as `standing_consent_caps_invalid`.
    // Commands: owner ruling 16 Nov 2026, 5,000 (it was 1,000, which ended one run's serving after
    // about 995 answer tokens: REPORT-laneCAP measurement run). The figure a joined Mac's own page
    // grants (`app.js` `max_commands:5000`) and the compile offers each stage
    // (`DemoChainMaxCommands`), inside the standing consent's maximum (10,000). A stored grant
    // keeps its own figure (above: never re-granted).
    let wall = budgets::BudgetId::PyMyceliumA13MacosStandingConsentMaxWallSecondsMaximumL102
        .span(budgets::Entries::ZERO)
        .map_or(3600u64, |s| s.millis() / 1000);
    let body = json!({
        "acknowledged": true,
        "disclosure_digest": standing_consent::DISCLOSURE_DIGEST,
        "caps": {
            "storage_bytes": storage,
            "max_memory_bytes": 4_000_000_000u64,
            "max_wall_seconds": wall,
            "max_commands": 5000u64,
        },
    });
    standing_consent::grant(dir, &body, policy, now(), None)
        .map_err(|e| PublicError::new(409, e.0))?;
    let binding = json!({
        "swarm_id": state.get("swarm_id").cloned().unwrap_or(Value::Null),
        "node_id": node,
        "seed_key_digest": state.get("seed_key_digest").cloned().unwrap_or(Value::Null),
    });
    standing_consent::bind(dir, &binding, now()).map_err(|e| PublicError::new(409, e.0))?;
    Ok(())
}

/// One pass of the contributor's half of `DemoApp._renew_loop` (`contributor.renew()`).
///
/// The Session leaves the contributor for the seed round trip as a recorded renewal flight
/// ([`crate::renewal`], lane FLK FLK-02): the pass lands it through [`ContributorFlight`], which
/// puts the Session back only into the same flight (never over a replaced contributor or a later
/// flight), keeps a Session the seed refused out ([`crate::renewal::membership_refused`]), and on
/// unwind ends the flight without a Session, so no reader keeps standing on its snapshot.
pub fn renew_contributor<H: RunHost>(host: &H) {
    let taken = host.with(|c| {
        if c.closed || !c.authority.running || c.left || c.authority.target {
            return None;
        }
        let (Some(runtime), Some(state)) = (c.authority.seed.clone(), c.run.contributor.as_mut())
        else {
            return None;
        };
        let session = state.session.take()?;
        let flight = crate::renewal::Flight::begin();
        let id = flight.id;
        state.flight = Some(flight);
        Some((c.authority.root.clone(), runtime, session, id))
    });
    let Some((root, runtime, mut session, id)) = taken else {
        return;
    };
    let flight = ContributorFlight {
        host,
        id,
        landed: false,
    };
    let dir = contributor_dir(&root);
    let mut refused = None;
    if let Ok((signer, _, _)) = identity(&dir) {
        let options = runtime.options();
        let random_hex = |n: usize| random(n).map(|b| hex(&b)).unwrap_or_default();
        let clock = || host.renewal_clock();
        let env = crate::peer::JoinEnv {
            signer: &signer,
            clock: &clock,
            random_hex: &random_hex,
            seed: &options,
        };
        if let Err(failure) = crate::peer::renew_with_origin(&dir, true, Some(&mut session), &env) {
            eprintln!("contributor renew: {}", failure.error.code);
            // Only the seed's own refusal keeps the Session out (FLK2-02); a code raised on this
            // Mac (a lease the local wall clock reads as past) puts it back, as at the base.
            refused = crate::renewal::membership_refused(&failure).then_some(failure.error.code);
        }
    }
    flight.land(session, refused);
}
/// A contributor renewal's flight, owned by the renewal pass from its take to its put-back.
struct ContributorFlight<'a, H: RunHost> {
    host: &'a H,
    id: u64,
    landed: bool,
}
impl<H: RunHost> ContributorFlight<'_, H> {
    /// The put-back: only into this same flight. A transport or local failure puts the Session
    /// back as before; a seed refusal of the membership does not, and is recorded.
    fn land(mut self, session: crate::member::Session, refused: Option<&'static str>) {
        let id = self.id;
        self.host.with(|c| {
            let Some(state) = c.run.contributor.as_mut() else {
                return;
            };
            if state.flight.as_ref().map(|f| f.id) != Some(id) {
                return;
            }
            state.flight = None;
            if let Some(code) = refused {
                state.refused = Some(code);
                return;
            }
            if state.session.is_none() {
                state.lease = session.lease_expires_at();
                state.session = Some(session);
            }
        });
        // Only once the put-back ran: an unwind inside it still ends the flight in `drop`.
        self.landed = true;
    }
}
impl<H: RunHost> Drop for ContributorFlight<'_, H> {
    /// A pass that never reached its put-back (its thread unwinds): the flight ends without a
    /// Session, as a missing Session was before the snapshot fallback existed.
    fn drop(&mut self) {
        if self.landed {
            return;
        }
        let id = self.id;
        self.host.with(|c| {
            if let Some(state) = c.run.contributor.as_mut()
                && state.flight.as_ref().map(|f| f.id) == Some(id)
            {
                state.flight = None;
            }
        });
    }
}

/// `app._stop_local_contributor()`: the membership stays retained, the live session ends.
pub fn stop_local_contributor<H: RunHost>(host: &H) -> Result<(), PublicError> {
    // target::close puts refused cleanup owners back. Retain their contributor
    // too: reporting stopped or permitting a replacement would erase the debt.
    let lifecycle = host.bootstrap_lifecycle();
    let _lifecycle = lifecycle.lock().map_err(|_| failure())?;
    host.close_contributor()?;
    host.with(|c| c.run.contributor = None);
    Ok(())
}

/// `ensure_exchange(app)`: an owner with a running seed has one exchange per seed runtime (v0.1.4 keys it on the
/// control mailbox), announcing the run model while idle. The idle refresh is prepared outside any lock and
/// committed under the run's own lock so it can never overwrite a run that started in between.
pub fn ensure_exchange<H: RunHost>(host: &H) -> Option<Arc<RunExchange>> {
    let (exchange, wanted, demo) = host.with(|c| {
        if c.authority.target || c.authority.swarm.is_none() || !c.authority.running {
            return None;
        }
        // The seed runtime owns the exact exchange mounted on its server. Never key this on an
        // allocation address: a closed runtime's address can be reused by its replacement.
        let exchange = c.authority.seed.as_ref()?.exchange.clone();
        c.run.exchange = Some(exchange.clone());
        let document = run_model::document_json(
            &c.authority.root,
            std::env::var(run_model::ENV).ok().as_deref(),
            None,
        );
        Some((exchange, document.ok(), c.run.demo.clone()))
    })?;
    let phase = exchange.announcement(None).get("phase").cloned();
    let idle = matches!(&phase, Some(Json::Str(p)) if p == "idle" || p == "stopped");
    if idle
        && !demo.active()
        && let Some(document) = wanted
    {
        let model = json!({
            "choice": document.get("selected").map_or(Value::Null, Json::to_value),
            "model_id": document.get("model_id").map_or(Value::Null, Json::to_value),
            "resolved_commit": document.get("resolved_commit").map_or(Value::Null, Json::to_value),
        });
        if let Ok(prepared) = exchange.prepare_announcement(Announce {
            phase: "idle".into(),
            model: Some(model),
            ..Announce::default()
        }) {
            demo.while_inactive(|| exchange.commit_announcement(&prepared, true));
        }
    }
    Some(exchange)
}

/// `installed_sidecar()` (`native_runtime.py:8-22`): the native sidecar at `candidate` (else beside the
/// executable, `native/mycelium-iroh-sidecar`), a regular file this user owns, executable and not writable by
/// others.
pub fn installed_sidecar(candidate: Option<PathBuf>) -> Result<PathBuf, Failure> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    let invalid = || Failure::new("ValueError", "native_sidecar_installation_invalid");
    let candidate = match candidate {
        Some(path) => path,
        None => std::env::current_exe()
            .map_err(|_| invalid())?
            .parent()
            .ok_or_else(invalid)?
            .join("native")
            .join("mycelium-iroh-sidecar"),
    };
    let path = fs::canonicalize(candidate).map_err(|_| invalid())?;
    let meta = fs::symlink_metadata(&path).map_err(|_| invalid())?;
    let mode = meta.permissions().mode();
    if !meta.is_file()
        || meta.uid() != rustix::process::geteuid().as_raw()
        || mode & 0o100 == 0
        || mode & 0o022 != 0
    {
        return Err(invalid());
    }
    Ok(path)
}

/// The local agent's collaborators over the host.
pub struct AgentView<'a, H: RunHost> {
    pub host: &'a H,
    pub exchange: Arc<RunExchange>,
    pub node: String,
    /// The snapshot `prepared_model` just verified, for the `describe` of the same descriptor.
    pub prepared: Option<(PathBuf, acquisition::PreparedFacts)>,
}
impl<H: RunHost> AgentView<'_, H> {
    fn acquisition(&self) -> Result<Acquisition, Failure> {
        self.host
            .acquisition()
            .ok_or_else(|| Failure::new("AttributeError", "demo_step_failed"))
    }
    /// `_prepare_slice_root(run_id)`: this Mac's empty per-run slice root inside its artifact cache.
    fn prepare_slice_root(&self, run_id: &str) -> Result<String, Failure> {
        use std::os::unix::fs::DirBuilderExt;
        let base = self.acquisition()?.state_root().join("slices");
        let invalid = || Failure::new("ValueError", "slice_destination_invalid");
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&base)
            .map_err(|_| invalid())?;
        for entry in fs::read_dir(&base).map_err(|_| invalid())?.flatten() {
            let kind = entry.file_type();
            if entry.file_name() != run_id && kind.is_ok_and(|t| t.is_dir() && !t.is_symlink()) {
                fs::remove_dir_all(entry.path()).unwrap_or_default();
            }
        }
        let root = base.join(run_id);
        if !root.exists() {
            fs::DirBuilder::new()
                .mode(0o700)
                .create(&root)
                .map_err(|_| invalid())?;
        }
        let meta = fs::symlink_metadata(&root).map_err(|_| invalid())?;
        if meta.file_type().is_symlink()
            || fs::canonicalize(&root).ok().as_deref() != Some(root.as_path())
        {
            return Err(invalid());
        }
        Ok(root.to_string_lossy().into_owned())
    }
}
impl<H: RunHost> AgentHost for AgentView<'_, H> {
    fn bootstrap(&mut self, run: &str, deployment: &str, offer: &str) -> Result<(), Failure> {
        self.host.bootstrap(&self.node, run, deployment, offer)
    }
    fn pending_cleanup(&mut self) -> Result<Option<Value>, Failure> {
        self.host.pending_cleanup(&self.node)
    }
    fn cleanup(&mut self, request: &Value) -> Result<(), Failure> {
        self.host.cleanup(&self.node, request)
    }
    fn announcement(&mut self) -> Result<Option<Value>, crate::local_agent::ExchangeUnavailable> {
        Ok(Some(
            self.exchange.announcement(Some(&self.node)).to_value(),
        ))
    }
    fn member(&mut self) -> Option<String> {
        self.host
            .with(|c| c.run.contributor.as_ref().map(|s| s.node.clone()))
    }
    fn run_active(&mut self) -> bool {
        self.host.with(|c| c.run.demo.clone()).active()
    }
    fn resource_check(&mut self) -> Option<String> {
        self.host.resource_check().map(str::to_owned)
    }
    fn prepared_model(&mut self) -> Result<(String, String), Failure> {
        let acquisition = self.acquisition()?;
        let prepared = acquisition
            .prepared_source()
            .map_err(|e| Failure::new("AcquisitionError", e.code()))?;
        self.prepared = Some(prepared);
        let model = acquisition.model();
        Ok((model.model_id.to_owned(), model.resolved_commit.to_owned()))
    }
    fn describe(
        &mut self,
        run_id: &str,
        deployment_id: &str,
        slice_manifest_digest: Option<&str>,
    ) -> Result<Value, Failure> {
        let (root, swarm, lease) = self.host.with(|c| {
            let swarm = c
                .authority
                .swarm
                .as_ref()
                .and_then(|s| s["swarm_id"].as_str().map(str::to_owned));
            (
                c.authority.root.clone(),
                swarm,
                c.run.contributor.as_ref().and_then(|s| s.lease),
            )
        });
        let dir = contributor_dir(&root);
        let state = crate::peer::load_membership_state(&dir)
            .ok()
            .flatten()
            .ok_or_else(|| Failure::new("RemoteMacMemberError", "peer_member_not_current"))?;
        // `staging_control_snapshot`: a stopped or expired member cannot describe itself.
        // A *missing* lease is not expired: `_start_local_contributor` returns a NEW member whose
        // lease appears only on the renewal loop's first pass (see `create_local_contributor`
        // above). Rejecting that made the "This Mac joins the run" step fail on every fresh start,
        // because the run attaches before the loop has ever renewed.
        if lease.is_some_and(|lease| now() >= lease) {
            return Err(Failure::new(
                "RemoteMacMemberError",
                "peer_member_not_current",
            ));
        }
        let (signer, endpoint, node) =
            identity(&dir).map_err(|_| Failure::new("ValueError", "endpoint_identity_invalid"))?;
        let member = DescriptorMember {
            node_id: node,
            endpoint_id: endpoint,
            verification_key_digest: signer.verification_key_digest(),
            generation: state
                .get("membership_generation")
                .and_then(Value::as_u64)
                .unwrap_or(0),
            incarnation: state
                .get("incarnation")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned(),
        };
        let source = match slice_manifest_digest {
            Some(digest) => Source::Slice {
                source_root: self.prepare_slice_root(run_id)?,
                manifest_digest: digest.to_owned(),
            },
            None => {
                let (root, facts) = match self.prepared.take() {
                    Some(prepared) => prepared,
                    None => self
                        .acquisition()?
                        .prepared_source()
                        .map_err(|e| Failure::new("AcquisitionError", e.code()))?,
                };
                Source::Whole {
                    source_root: root.to_string_lossy().into_owned(),
                    manifest_digest: facts.manifest_digest,
                }
            }
        };
        let paths = Paths {
            state_root: dir.to_string_lossy().into_owned(),
            endpoint_secret_file: dir.join(IDENTITY_FILE).to_string_lossy().into_owned(),
            python_executable: std::env::current_exe()
                .ok()
                .and_then(|p| fs::canonicalize(p).ok())
                .map_or_else(String::new, |p| p.to_string_lossy().into_owned()),
            sidecar_binary: self.host.sidecar()?.to_string_lossy().into_owned(),
        };
        installation_descriptor::installation_descriptor(
            &signer,
            &member,
            swarm.as_deref().unwrap_or_default(),
            run_id,
            deployment_id,
            true,
            &source,
            &paths,
            now(),
        )
    }
    /// `OwnerRunConsent.member_caps`: `planning_caps(root)` (standing caps intersected with the policy).
    fn member_caps(&mut self) -> Result<Value, Failure> {
        let root = self.host.with(|c| c.authority.root.clone());
        Ok(planning_caps(&root).unwrap_or_else(|| json!({})))
    }
    /// `OwnerRunConsent.storage_cap`: `int(read_resource_policy(root)['storage_bytes'])`.
    fn storage_cap(&mut self) -> Result<u64, Failure> {
        let root = self.host.with(|c| c.authority.root.clone());
        resource_policy::read(&root)
            .ok()
            .flatten()
            .and_then(|p| p.get("storage_bytes").and_then(Value::as_u64))
            .ok_or_else(|| Failure::new("KeyError", "demo_step_failed"))
    }
    /// `slice_root(run_id)`: the per-run root `describe` prepared in this Mac's artifact cache.
    fn slice_root(&mut self, run_id: &str) -> Result<PathBuf, Failure> {
        Ok(self.acquisition()?.state_root().join("slices").join(run_id))
    }
    /// `SnapshotRangeTransport(source.source_root, ...)` over this Mac's own verified snapshot. The
    /// compiled-bundle clone shortcut is not wired (the Rust compiler keeps no transfer bundle there),
    /// so the owner's slice is read by range from the snapshot and verified like any other.
    fn slice_transport(&mut self, _announcement: &Value) -> Option<Box<dyn SliceTransport>> {
        let acquisition = self.acquisition().ok()?;
        let (root, _) = acquisition.prepared_source().ok()?;
        let model = acquisition.model();
        Some(Box::new(SnapshotRangeTransport::new(
            root,
            model.model_id,
            model.resolved_commit,
            None,
        )))
    }
    fn sink(&mut self, report: &Value) -> Result<(), crate::local_agent::ExchangeUnavailable> {
        self.exchange
            .submit_local(&self.node, report)
            .map_err(|_| crate::local_agent::ExchangeUnavailable)
    }
    fn clock(&mut self) -> f64 {
        now()
    }
}

/// What `demo_app.action` asks the app (`demo_app.py:109-133`), read once under the host's lock.
pub struct HostFacts {
    owner: bool,
    exchange: bool,
    joined_without_relay: u64,
}
impl DemoFacts for HostFacts {
    fn owner(&self) -> bool {
        self.owner
    }
    fn ensure_exchange(&mut self) -> bool {
        self.exchange
    }
    fn members_joined_without_relay(&self) -> Option<u64> {
        Some(self.joined_without_relay)
    }
}
/// `app.installation_role`, `ensure_exchange(app) is not None` (an owner authority with its control mailbox:
/// the swarm exists and its seed coordinator runs) and `members_joined_without_relay()` (members other than
/// the owner while no relay object exists; `app.py:836-842`).
pub fn demo_facts<H: RunHost>(host: &H) -> HostFacts {
    ensure_exchange(host);
    host.with(|c| {
        let owner = !c.authority.target;
        let exchange = owner
            && c.authority.swarm.is_some()
            && c.authority.running
            && c.authority.seed.is_some();
        let joined_without_relay = match &c.authority.seed {
            Some(seed) if c.relay_absent && owner && c.authority.swarm.is_some() => seed
                .coordinator
                .members(None)
                .map_or(0, |m| m.len().saturating_sub(1) as u64),
            _ => 0,
        };
        HostFacts {
            owner,
            exchange,
            joined_without_relay,
        }
    })
}

/// A failure inside a step keeps only its class (`owner_run.py:420-422`): a 400 is a `DemoError`.
pub fn step_exception(error: PublicError) -> StepError {
    let class = if error.status == 400 {
        "DemoError"
    } else {
        error.raised.identity().0
    };
    StepError::Exception {
        class: class.to_owned(),
    }
}

/// The contributor and descriptors steps' ports over the host (`AppDemoChain` reads these from `app`),
/// with the in-process agent that answers the owner's announcements as this Mac's member (`chain.agent`).
#[derive(Clone)]
pub struct OwnerPorts<H: RunHost> {
    pub host: H,
    /// The in-process agent, shared by every clone (the contributor and the descriptors steps).
    pub agent: Arc<Mutex<Option<LocalAgent>>>,
}
impl<H: RunHost> OwnerPorts<H> {
    pub fn new(host: H) -> Self {
        Self {
            host,
            agent: Arc::new(Mutex::new(None)),
        }
    }
    fn exchange(&self) -> Result<Arc<RunExchange>, StepError> {
        ensure_exchange(&self.host)
            .ok_or_else(|| OwnerRunError::new("owner_exchange_unavailable").into())
    }
}
impl<H: RunHost> ContributorPort for OwnerPorts<H> {
    /// `with app.guard: if app.local_contributor is None: app._start_local_contributor()`.
    fn ensure_local_contributor(&mut self) -> Result<String, StepError> {
        if let Some(node) = self
            .host
            .with(|c| c.run.contributor.as_ref().map(|s| s.node.clone()))
        {
            return Ok(node);
        }
        start_local_contributor(&self.host).map_err(step_exception)?;
        self.host
            .with(|c| c.run.contributor.as_ref().map(|s| s.node.clone()))
            .ok_or_else(|| StepError::Exception {
                class: "AttributeError".to_owned(),
            })
    }
    /// `getattr(app, 'run_exchange', None) is not None`.
    fn exchange_available(&self) -> bool {
        self.host.with(|c| c.run.exchange.is_some())
    }
    fn attach_agent(&mut self, _local: &str) -> Result<(), StepError> {
        *self
            .agent
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(LocalAgent::new());
        Ok(())
    }
}
impl<H: RunHost> ReportsPort for OwnerPorts<H> {
    fn forget_reports(&mut self) {
        if let Ok(exchange) = self.exchange() {
            exchange.forget_reports();
        }
    }
    fn announce(&mut self, announcement: &Announcement) -> Result<(), StepError> {
        let exchange = self.exchange()?;
        let per_node: Map<String, Value> =
            announcement.per_node.iter().flatten().cloned().collect();
        exchange
            .announce(Announce {
                phase: announcement.phase.clone(),
                run_id: Some(announcement.run_id.clone()),
                deployment_id: Some(announcement.deployment_id.clone()),
                model: Some(json!({
                    "choice": crate::model_catalog::find(&announcement.model_id, &announcement.resolved_commit)
                        .and_then(crate::model_catalog::choice_for_model),
                    "model_id": announcement.model_id,
                    "resolved_commit": announcement.resolved_commit,
                })),
                slice_mode: announcement.slice_mode,
                manifest_digest: Some(announcement.manifest_digest.clone()),
                work_limits: None,
                per_node,
            })
            .map(|_| ())
            .map_err(|_| StepError::Exception {
                class: "ValueError".to_owned(),
            })
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        let reports = self.exchange()?.reports();
        Ok(reports
            .as_object()
            .map(|m| m.iter().map(|(k, v)| (k.clone(), v.clone())).collect())
            .unwrap_or_default())
    }
    fn members(&mut self) -> Vec<MemberRow> {
        let Some(seed) = self.host.with(|c| c.authority.seed.clone()) else {
            return Vec::new();
        };
        seed.coordinator
            .members(None)
            .unwrap_or_default()
            .iter()
            .map(|m| MemberRow {
                node_id: m["node_id"].as_str().unwrap_or_default().to_owned(),
                lifecycle_state: m["lifecycle_state"].as_str().map(str::to_owned),
                lease_expires_at: m["lease_expires_at"].as_f64(),
            })
            .collect()
    }
    /// The owner's authority node, from where it is stable: `owner_node` is recorded with the owner's
    /// membership, not read from `owner_session`, which `app_host::renew_owner` takes out for every
    /// heartbeat (lane FLK, FLK-03: the owner's row then counted as a joined Mac).
    fn own_node_id(&mut self) -> Option<String> {
        self.host.with(|c| c.authority.owner_node.clone())
    }
    fn member_identity(&mut self, node: &str) -> Value {
        let member = self
            .host
            .with(|c| c.authority.seed.clone())
            .and_then(|seed| seed.coordinator.member(node).ok())
            .unwrap_or(Value::Null);
        json!([
            member["generation"],
            member["incarnation"],
            member["verification_key_digest"],
            member["endpoint_id"]
        ])
    }
    /// Only authenticated signed exchange observations feed these source-median diagnostics.
    fn clock_skew(&mut self, nodes: &[String]) -> Result<Option<ClockSkew>, StepError> {
        let offsets = self.host.with(|c| {
            c.authority
                .seed
                .as_ref()
                .map(|seed| seed.clock_offsets.clone())
        });
        Ok(offsets.and_then(|offsets| {
            let peers = offsets.exceeded(Some(nodes));
            (!peers.is_empty()).then(|| ClockSkew {
                exceeded: peers,
                tolerance: offsets.tolerance(),
            })
        }))
    }
    fn tick_local(&mut self) {
        let agent = self
            .agent
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        let (Some(mut agent), Ok(exchange)) = (agent, self.exchange()) else {
            return;
        };
        if let Some(node) = self
            .host
            .with(|c| c.run.contributor.as_ref().map(|s| s.node.clone()))
        {
            let mut view = AgentView {
                host: &self.host,
                exchange,
                node,
                prepared: None,
            };
            agent.tick(&mut view);
        }
        *self
            .agent
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(agent);
    }
}
