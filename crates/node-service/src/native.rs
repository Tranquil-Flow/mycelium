// SPDX-License-Identifier: AGPL-3.0-only
//! Bounded development slice over real Iroh. NOT a production network admission host.
//! Control, provisioning, renewal and artifact traffic ride L9's signed NodeControl
//! and artifact carriers; activation rides the Router ALPN. The only TCP left is the
//! public HTTP/SSE gateway used by the qualification client (`verify`).
mod agent_worker;
mod child;
mod lifecycle_owner;
mod safety;
mod stage;
mod verify;
mod worker_main;
use crate::{
    activation,
    control::{Peer, Session, Wire},
    iroh_frames,
    liveness::{Death, Watch, WorkerExited},
    loopback::Result,
    phase,
    receipts::{self, Receipts},
    service::{self, Driver, Job},
    spawn,
};
pub use agent_worker::{AgentWorker, Fault, WorkerExit};
use backend_mlx::process::ProcessLimits;
use backend_mlx::resources::ResourcePool;
use budgets::{BudgetId as B, Bytes, ClockDomain, Entries, MonoInstant, Span, WallExpiry};
pub use child::{member, qualification_member};
pub use worker_main::native_worker;

use protocol::{canonical, identity::Identity};
use serde_json::{Value, json};
pub use stage::NativeStage;
use stage::{argmax, header};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
    sync::{Arc, Mutex},
    time::Instant,
};
fn span(id: B) -> Span {
    id.span(Entries::ZERO).expect("registered")
}
fn text<'a>(v: &'a Value, k: &str) -> Result<&'a str> {
    v[k].as_str().ok_or_else(|| format!("missing {k}").into())
}
fn number(v: &Value, k: &str) -> Result<u64> {
    v[k].as_u64().ok_or_else(|| format!("missing {k}").into())
}
fn string_command(program: &str, args: &[&str]) -> Result<String> {
    let out = spawn::confined_output(program, args)?;
    if !out.status.success() {
        return Err(format!("host probe failed: {program}").into());
    }
    Ok(String::from_utf8(out.stdout)?.trim().to_owned())
}
pub fn pressure() -> Result<()> {
    if string_command(
        "/usr/sbin/sysctl",
        &["-n", "kern.memorystatus_vm_pressure_level"],
    )? != "1"
    {
        return Err("memory_pressure_hold".into());
    }
    Ok(())
}
fn host() -> Result<Value> {
    pressure()?;
    let memory: u64 = string_command("/usr/sbin/sysctl", &["-n", "hw.memsize"])?.parse()?;
    let disk = string_command("/bin/df", &["-k", "/System/Volumes/Data"])?;
    let available: u64 = disk
        .lines()
        .last()
        .ok_or("disk_probe")?
        .split_whitespace()
        .nth(3)
        .ok_or("disk_probe")?
        .parse()?;
    Ok(json!({"memory":memory,"storage":available.checked_mul(1024).ok_or("disk_overflow")?}))
}
struct OwnerDriver {
    checkpoint: PathBuf,
    artifact: crate::artifact::Source,
    tokenizer: PathBuf,
    evidence: PathBuf,
    identity: Mutex<Identity>,
    owner_state: PathBuf,
    member_state: PathBuf,
    owner_leaf_state: PathBuf,
    membership: crate::enrollment::LocalMembership,
    lifecycle: lifecycle_owner::LifecycleOwner,
}
impl Driver for OwnerDriver {
    fn run(&self, job: &mut Job) -> std::result::Result<(), api::schema::PublicError> {
        let ledger = crate::reap::Ledger::default();
        let result = self.run_native(job, &ledger);
        if let Err(e) = &result {
            eprintln!("slice driver failed: {e}");
            let discarded = self.discard_staged(job);
            if let Some(exited) = e.downcast_ref::<WorkerExited>()
                && job.terminal().is_none()
            {
                // An agent process itself died: no signed quorum exists, only the local reap.
                self.settle_after_death(job, &ledger, &exited.0, discarded);
            }
        }
        result.map_err(|e| {
            match e.downcast_ref::<WorkerExited>() {
                // v0.1.4's terminal code, not the generic slice failure.
                Some(exited) => api::schema::PublicError::new(503, exited.0.code),
                None => api::schema::PublicError::new(503, "slice_native_failed"),
            }
        })
    }
}
impl OwnerDriver {
    /// v0.1.4's `ManagedWorker._watch` around the whole job: an exited native worker (the
    /// remote member or the owner leaf) fails the job promptly with `worker_native_exited`
    /// instead of leaving the owner blocked until its wall deadline.
    fn run_native(&self, job: &mut Job, ledger: &crate::reap::Ledger) -> Result<()> {
        let watch = Watch::start(job.deadline)?;
        let result = self.run_watched(job, &watch, ledger);
        watch.finish();
        match (result, watch.death()) {
            (Err(_), Some(death)) => {
                phase::mark(
                    "worker_exit_detected",
                    &format!("role={},pid={}", death.role, death.pid),
                );
                self.record_native_exit(job, &death);
                Err(Box::new(WorkerExited(death)))
            }
            (result, _) => result,
        }
    }
    /// A failed run's workers were killed, so the member's staged model copy was not removed by it:
    /// discard it here (never the read-only source) and keep size + digest as evidence.
    /// True when no staged copy remains (removed now, or there was none).
    fn discard_staged(&self, job: &Job) -> bool {
        let record = self
            .evidence
            .join(format!("generation-{}", job.generation))
            .join("staged-discard.json");
        match crate::artifact::discard_staged(
            &self.member_state,
            &self.artifact.pack_digest,
            &record,
        ) {
            Ok(Some(outcome)) => {
                eprintln!("staged model copy discarded after the failed run: {outcome}");
                true
            }
            Ok(None) => true,
            Err(error) => {
                eprintln!("staged model copy NOT discarded: {error}");
                false
            }
        }
    }
    /// v0.1.4 ends a run whose worker died `failed` and allows the next one once the worker's
    /// cleanup is proven (`cleanup_proven() and not process_group_present()`). Here that is: every
    /// child this run spawned was reaped by its supervisor, none of the pids is still in the
    /// process table, and the staged copy is gone. It releases the host generation only; it is not
    /// a signed cleanup quorum (a killed member cannot sign one), and the evidence says so.
    fn settle_after_death(
        &self,
        job: &Job,
        ledger: &crate::reap::Ledger,
        death: &Death,
        staged_discarded: bool,
    ) {
        use std::io::Write as _;
        use std::os::unix::fs::OpenOptionsExt as _;
        let proof = match ledger.prove(death.pid, staged_discarded) {
            Ok(proof) => proof,
            Err(refusal) => {
                eprintln!(
                    "local reap not proven: {}; the generation stays fenced",
                    refusal.code()
                );
                return;
            }
        };
        let document = json!({
            "protocol":"mycelium.slice.local_reap_proof.v1",
            "run_id":job.id,"generation":job.generation,
            "died":{"role":death.role,"pid":death.pid,"code":death.code},
            "children":proof.children().iter().map(|c| json!({"role":c.role,"pid":c.pid,"reaped":c.proven})).collect::<Vec<_>>(),
            "staged_copy_discarded":staged_discarded,
            "signed_cleanup_quorum":false,
            "releases":"host generation only; no L6 Terminal (a killed member cannot sign cleanup)",
            "oracle":"worker.py:1590-1620 cleanup_proven() and not process_group_present() @ f4e366e9; FX-SLICE1-post-worker-death",
        });
        let path = self
            .evidence
            .join(format!("generation-{}", job.generation))
            .join("local-reap.json");
        if let Err(error) = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)
            .and_then(|mut file| file.write_all(document.to_string().as_bytes()))
        {
            eprintln!("local reap evidence not written: {error}");
        }
        phase::mark("local_reap_proven", &format!("pid={}", death.pid));
        if let Err(error) = job.record_reap(proof) {
            eprintln!("local reap proof not recorded: {error}");
        }
    }
    /// Bounded private cause, written like v0.1.4's `native-exit.private.json`; a failure to
    /// write it never replaces the job's real outcome.
    fn record_native_exit(&self, job: &Job, death: &Death) {
        use std::io::Write as _;
        use std::os::unix::fs::OpenOptionsExt as _;
        let node = if death.role == "member" {
            "b-member"
        } else {
            "a-owner"
        };
        let document = json!({
            "protocol":"mycelium.slice.native_exit_diagnostic.v1",
            "run_id":job.id,"node_id":node,"role":death.role,"error_code":death.code,
            "pid":death.pid,"exit_status":death.exit_status,"signal":death.signal,
        });
        let path = self
            .evidence
            .join(format!("generation-{}", job.generation))
            .join("native-exit.private.json");
        if let Err(error) = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)
            .and_then(|mut file| file.write_all(document.to_string().as_bytes()))
        {
            eprintln!("native exit diagnostic not written: {error}");
        }
    }
    fn run_watched(
        &self,
        job: &mut Job,
        watch: &Watch,
        ledger: &crate::reap::Ledger,
    ) -> Result<()> {
        pressure()?;
        let root = self.evidence.join(format!("generation-{}", job.generation));
        fs::create_dir(&root)?;
        let start = Instant::now();
        let generation = job.generation;
        phase::mark("job_begin", &format!("generation={generation}"));
        let mono = || {
            MonoInstant::new(
                ClockDomain(generation),
                u64::try_from(start.elapsed().as_millis()).unwrap_or(u64::MAX),
            )
        };
        let current = || -> Result<node::Now> {
            if self.lifecycle.timed_qualification {
                self.lifecycle.now()
            } else {
                Ok(node::Now {
                    mono: mono(),
                    wall: WallExpiry::from_unix_millis(receipts::wall_ms()?),
                })
            }
        };
        let membership_generation = if self.lifecycle.timed_qualification {
            1
        } else {
            generation
        };
        let incarnation = if self.lifecycle.timed_qualification {
            &self.lifecycle.incarnation
        } else {
            &job.id
        };
        let planning: Value = serde_json::from_str(include_str!("../data/qwen2-planning.json"))?;
        let owner = self.identity.lock().map_err(|_| "identity_poisoned")?;
        let tokenizer = tokenizers::Tokenizer::from_file(&self.tokenizer)?;
        // Explicit raw-prompt gateway: the caller provides ChatML, identical to
        // the existing request_gateway contract (not an invented messages API).
        let prompt = text(&job.document, "prompt")?;
        let tokens = tokenizer.encode(prompt, false)?.get_ids().to_vec();
        let max = number(&job.document, "max_new_tokens")?;
        let context = u32::try_from(tokens.len() as u64 + max)?;
        if u64::from(context) > number(&planning["resources"], "context_tokens")? {
            return Err("context_exceeds_plan".into());
        }
        let host = host()?;
        let cap_memory =
            number(&host, "memory")? / number(&planning["policy"], "memory_fraction_denominator")?;
        let cap_storage = number(&host, "storage")?;
        let caps = node::Caps {
            storage_bytes: cap_storage,
            memory_bytes: cap_memory,
            wall_ms: span(B::MemberChosenWall).millis(),
            commands: 1,
        };
        // Small process bookkeeping reservation; stage admission separately checks
        // real native requirements against the planner's exact assigned share.
        let process_reservation = budgets::Reservation {
            resident: Bytes::new(number(&planning["resources"], "control_archive_bytes")?),
            staged: Bytes::ZERO,
            staging_overhead: Bytes::ZERO,
            scratch: Bytes::ZERO,
            kv: Bytes::ZERO,
        };
        let pool = ResourcePool::new(
            process_reservation
                .total()
                .map_err(|e| format!("reservation: {e:?}"))?,
        );
        let wire_runtime = iroh_frames::runtime()?;
        let wire_clock = iroh_frames::Clock::new(job.generation);
        let mut wire_host = iroh_frames::bind_persisted(
            &wire_runtime,
            &wire_clock,
            &self.owner_state,
            &iroh_frames::OWNER_CHANNELS,
        )?;
        let wire_address = iroh_frames::address(&wire_host)?;
        // An owner blocked in `accept` is released by a worker's death, and a driver blocked on
        // the page's acknowledgement by cancellation (v0.1.4's `_watch` closes the worker).
        watch
            .on_death(crate::control::dial_waker(
                iroh_frames::peer(&wire_address)?,
                job.generation,
            ))
            .keep();
        watch.on_death(job.canceller()).keep();
        let launch = serde_json::to_string(&json!({"owner":wire_address}))?;
        let mut command = Command::new(std::env::current_exe()?);
        command
            .arg(if self.lifecycle.timed_qualification {
                "qualification-member"
            } else {
                "member"
            })
            .arg(&launch)
            .arg(&self.member_state);
        let mut child = spawn::Tracked::new(
            spawn::spawn_worker(
                command,
                pool.clone(),
                process_reservation,
                ProcessLimits {
                    deadline: job.deadline,
                    close: span(B::RouterClose),
                    reap: span(B::WorkerCleanupReserve),
                    poll: span(B::RegistryPoll),
                },
            )?,
            "member",
            ledger,
        );
        let child_pid = child.pid();
        watch.add("member", child_pid);
        // Declared after `child`, so it is dropped first: the supervisor's own cancellation
        // of a still-running member is not a worker death.
        let _member_quiesce = watch.quiesce_on_drop();
        let wire = Wire {
            host: &wire_host,
            clock: &wire_clock,
            runtime: &wire_runtime,
            signer: &owner,
            watch: Some(watch),
        };
        let swarm = self.membership.invitation.swarm.clone();
        let ids = crate::control::Ids {
            request_id: job.id.clone(),
            generation: job.generation,
        };
        // Pull model: the child dials this owner's NodeControl ALPN (address passed
        // on the owner-spawned argv), the owner answers each signed pull and then
        // drives signed commands back to the child's registered endpoint.
        let mut member_session =
            Session::owner(ids.clone(), self.membership.invitation.member_key.clone());
        let member_request = json!({"node":"b-member","owner_key":owner.public_key_record(),"swarm_id":swarm,"request_id":job.id,"generation":job.generation,"membership_generation":membership_generation,"member_incarnation":incarnation,"qualification_clock":self.lifecycle.timed_qualification,"qualification_now":current()?.wall.unix_millis()});
        let (registration, member_endpoint) = wire.serve(
            &mut member_session,
            "register",
            None,
            B::WorkerStartup,
            |body, caller| {
                if number(body, "pid")? != u64::from(child_pid)
                    || body["key"] != self.membership.invitation.member_key
                    || body["iroh"]["id"] != caller.to_string()
                {
                    return Err("member_process_binding".into());
                }
                Ok(member_request.clone())
            },
        )?;
        let member_addr = iroh_frames::peer(&registration["iroh"])?;
        phase::mark("member_registered", &format!("pid={child_pid}"));
        phase::mark("staging_begin", "artifact");
        // Stage the member's artifact right after registration: staging is neither worker
        // startup nor part of the membership/capability reports, which age while it runs.
        let source_identity =
            Identity::from_secret(text(&wire_address, "id")?, &owner.secret_bytes())?;
        let offer = crate::artifact::offer(
            &owner,
            &swarm,
            job.generation,
            "b-member",
            &member_endpoint.to_string(),
            text(&registration["key"], "verification_key_digest")?,
            source_identity.endpoint_id(),
            &self.artifact,
            receipts::wall_ms()?,
        )?;
        let offer_body = json!({"grant":offer.grant,"manifest":offer.manifest,"size":offer.size,"wire":offer.wire.clone()});
        wire.serve(
            &mut member_session,
            "offer",
            Some(member_endpoint),
            B::WorkerStartup,
            |_, _| Ok(offer_body.clone()),
        )?;
        // Serve the granted pack to exactly this member over the artifact ALPN.
        let objects = Arc::new(crate::artifact::CheckpointObjects::open(
            &self.artifact,
            &root.join("artifact-journal"),
        )?);
        let service = runtime::iroh_artifact_source::ArtifactService::new(
            source_identity,
            owner.public_key_record(),
            crate::artifact::grant_limits()?,
            objects.clone(),
            crate::artifact::serve_limits(crate::artifact::wire_limits(&offer.wire)?)?,
            Arc::new(|| receipts::wall_ms().unwrap_or(0)),
        )
        .map_err(|e| format!("artifact service: {e}"))?;
        match wire.next(B::WorkerStartup)? {
            crate::control::Incoming::Artifact(connection)
                if connection.remote_id() == member_endpoint =>
            {
                let _release = crate::control::close_on_death(watch, &connection);
                wire_runtime
                    .block_on(service.serve_connection(connection))
                    .map_err(|e| format!("artifact serve: {e}"))?;
            }
            _ => return Err("artifact_carrier_expected".into()),
        }
        let artifact_chunks = objects.reads.load(std::sync::atomic::Ordering::Relaxed);
        if artifact_chunks != self.artifact.segments.len() as u64 {
            return Err("artifact_chunks_incomplete".into());
        }
        phase::mark("staging_done", &format!("segments={artifact_chunks}"));
        let (hello, _) = wire.serve(
            &mut member_session,
            "hello",
            Some(member_endpoint),
            B::WorkerStartup,
            |body, _| {
                if number(body, "pid")? != u64::from(child_pid)
                    || body["key"] != self.membership.invitation.member_key
                    || body["iroh"] != registration["iroh"]
                {
                    return Err("member_process_binding".into());
                }
                Ok(json!({"accepted":true}))
            },
        )?;
        if member_addr.id.to_string() != text(&hello, "endpoint")?
            || hello["join"]["message"]["endpoint_addr"]["id"] != hello["iroh"]["id"]
            || hello["join"]["message"]["endpoint_addr"]["addrs"] != hello["iroh"]["addresses"]
        {
            return Err("iroh_membership_endpoint_binding".into());
        }
        // Verify signed join before connecting the Router carrier. The final
        // acceptance below is the only one persisted/sent to the member.
        crate::enrollment::accept(
            &owner,
            &self.membership.invitation,
            &hello["join"],
            membership_generation,
            current()?.wall,
        )?;
        use planner::discovery::{MemberIdentity, MembershipVerifier, joined_member, local_owner};
        let owner_id = MemberIdentity {
            node_id: "a-owner".into(),
            generation: membership_generation,
            incarnation: incarnation.clone(),
            endpoint_id: owner.endpoint_id().into(),
            verification_key_digest: owner.verification_key_digest(),
        };
        let member_id = MemberIdentity {
            node_id: "b-member".into(),
            generation: membership_generation,
            incarnation: incarnation.clone(),
            endpoint_id: text(&hello, "endpoint")?.into(),
            verification_key_digest: text(&hello["key"], "verification_key_digest")?.into(),
        };
        let wall = span(B::MemberChosenWall);
        let now = current()?.mono;
        let owner_member = local_owner(
            owner_id,
            planner::placement::Member {
                node_id: "a-owner".into(),
                memory_bytes: number(&host, "memory")?,
                available_storage_bytes: Some(cap_storage),
                decode_tokens_per_second: None,
                caps: Some(planner::placement::Caps {
                    storage_bytes: Some(cap_storage),
                    max_memory_bytes: Some(cap_memory),
                    max_wall_seconds: Some(wall.millis() / 1000),
                }),
            },
            wall,
            now.checked_add(wall).map_err(|e| format!("wall: {e:?}"))?,
            now,
        )?;
        let verifier = MembershipVerifier {
            swarm_id: &swarm,
            recipient_node_id: "a-owner",
            caps: planner::placement::Caps {
                storage_bytes: Some(self.membership.consent.caps.storage_bytes),
                max_memory_bytes: Some(self.membership.consent.caps.memory_bytes),
                max_wall_seconds: Some(self.membership.consent.caps.wall_ms / 1000),
            },
            backend: "mlx",
            precision: "int8-weight-only",
            maximum_bytes: api::requests::MAX_REQUEST_BODY,
        };
        let member = joined_member(
            &verifier,
            &serde_json::to_vec(&hello["capability"])?,
            &member_id,
            wall,
            current()?.wall,
            now,
        )?;
        let acceptance = crate::enrollment::accept(
            &owner,
            &self.membership.invitation,
            &hello["join"],
            membership_generation,
            current()?.wall,
        )?;
        let mut retained = self
            .lifecycle
            .retained
            .lock()
            .map_err(|_| "lifecycle_poisoned")?;
        let mut core = if let Some(core) = retained.take() {
            core
        } else {
            crate::lifecycle::open(
                &if self.lifecycle.timed_qualification {
                    self.evidence.join("owner-lifecycle.sqlite")
                } else {
                    root.join("node.sqlite")
                },
                crate::lifecycle::Admission {
                    owner: node::Binding {
                        swarm_id: swarm.clone(),
                        node_id: "a-owner".into(),
                        seed_key_digest: owner.verification_key_digest(),
                    },
                    policy: caps.clone(),
                    member: control::MemberBinding {
                        target_node_id: member_id.node_id.clone(),
                        target_endpoint_id: member_id.endpoint_id.clone(),
                        target_key_digest: member_id.verification_key_digest.clone(),
                        membership_generation: membership_generation.to_string(),
                        member_incarnation: member_id.incarnation.clone(),
                        run_id: format!("slice-{}", job.generation),
                        deployment_id: "slice-development".into(),
                    },
                    owner_key: owner.public_key_record(),
                    acceptance: acceptance.clone(),
                    consent: Some(self.membership.consent.clone()),
                },
                current()?,
            )?
        };
        fs::write(
            root.join("owner-acceptance.json"),
            serde_json::to_vec_pretty(&acceptance)?,
        )?;
        let request = json!({"owner_node_id":"a-owner","model_id":planning["model"]["model_id"],"quantization":"int8-weight-only","slice_mode":false,"saved":true,"members":[owner_member.member(),member.member()]});
        let members = vec![owner_member, member];
        let input = planner::admission::PlanInputs {
            model: &planning["model"],
            policy: &planning["policy"],
            resources: &planning["resources"],
            request: &request,
        };
        let phases = planner::admission::PhaseBudget {
            compile: span(B::Descriptors),
            startup: span(B::WorkerStartup),
            service: span(B::UnknownMemberWall),
            cleanup: span(B::WorkerCleanupReserve),
        };
        let restart = crate::planning::RestartBudget {
            stop: span(B::RouterClose),
            start: span(B::WorkerStartup),
            safety: span(B::WorkerCleanupReserve),
        };
        let continuation = core.state().rollover_members.is_some();
        let lease = lifecycle_owner::lease_expiry(&acceptance)?;
        let prepared = if continuation {
            let report = node::RolloverReport {
                binding: control::MemberBinding {
                    target_node_id: member_id.node_id.clone(),
                    target_endpoint_id: member_id.endpoint_id.clone(),
                    target_key_digest: member_id.verification_key_digest.clone(),
                    membership_generation: membership_generation.to_string(),
                    member_incarnation: member_id.incarnation.clone(),
                    run_id: format!("slice-{generation}"),
                    deployment_id: "slice-development".into(),
                },
                expiry: lease,
            };
            crate::planning::continue_rollover(
                &mut core,
                input,
                &members,
                &[report],
                current()?,
                phases,
                &restart,
            )
        } else {
            core.report(
                "b-member",
                &membership_generation.to_string(),
                core.state().epoch,
                lease,
                current()?,
            )
            .map_err(|e| format!("fresh report: {e:?}"))?;
            crate::planning::prepare(&mut core, input, &members, current()?, phases, restart)
        }
        .map_err(|e| format!("core prepare: {e:?}"))?;
        let plan = core.state().plan.clone().ok_or("core_plan_missing")?;
        let starts = prepared.starts();
        let execution_origin = current()?.mono;
        let ticket = |id: &str| -> Result<node::Ticket> {
            starts
                .iter()
                .find_map(|effect| match effect {
                    node::Effect::Start { ticket, .. } if ticket.node == id => Some(ticket.clone()),
                    _ => None,
                })
                .ok_or_else(|| "core_start_missing".into())
        };
        let owner_ticket = ticket("a-owner")?;
        let member_ticket = ticket("b-member")?;
        job.record_lifecycle(core.state())?;
        fs::write(
            root.join("core-starting.json"),
            serde_json::to_vec_pretty(core.state())?,
        )?;
        let placement = prepared.placement().clone();
        fs::write(
            root.join("placement.json"),
            serde_json::to_vec_pretty(&placement)?,
        )?;
        let rows = placement["members"].as_array().ok_or("placement_members")?;
        let row = |id: &str| -> Result<&Value> {
            rows.iter()
                .find(|r| r["node_id"] == id)
                .ok_or_else(|| "assignment_missing".into())
        };
        let a = row("a-owner")?;
        let b = row("b-member")?;
        let first = u32::try_from(number(a, "start")?)?;
        let split = u32::try_from(number(a, "end")?)?;
        let last = u32::try_from(number(b, "end")?)?;
        if first != 0
            || number(b, "start")? != u64::from(split)
            || u64::from(last) != number(&planning["model"]["shape"], "num_layers")?
        {
            return Err("noncontiguous_placement".into());
        }
        let member_plan = json!({"acceptance":acceptance,"start":split,"end":last,"total":last,"tokens":context,"memory":plan.assignments["b-member"].memory_bytes});
        wire.serve(
            &mut member_session,
            "plan",
            Some(member_endpoint),
            B::WorkerStartup,
            |_, _| Ok(member_plan.clone()),
        )?;
        eprintln!("waiting member native ready");
        wire.serve(
            &mut member_session,
            "ready",
            Some(member_endpoint),
            B::MemberChosenWall,
            |body, _| {
                if body["ready"] != true || body["membership_accepted"] != true {
                    return Err("member_not_ready".into());
                }
                Ok(json!({"acknowledged":true}))
            },
        )?;
        eprintln!("member native ready after {:?}", start.elapsed());
        phase::mark("member_ready", "");
        let member_connection = wire_host
            .connect(
                &member_addr,
                &wire_clock,
                wire_clock.deadline(B::WorkerStartup)?,
            )
            .map_err(|e| format!("iroh connect: {e:?}"))?;
        crate::control::close_on_death(watch, &member_connection).keep();
        let mut frames = iroh_frames::Frames::new(
            member_connection,
            &wire_runtime,
            &wire_clock,
            &member_addr,
            job.generation,
        )?;
        let mut member_peer = Peer::new(member_addr.clone(), job.generation, member_session);
        watch.on_death(member_peer.canceller()).keep();
        let owner_pool = ResourcePool::new(
            process_reservation
                .total()
                .map_err(|e| format!("reservation: {e:?}"))?,
        );
        let leaf_key = crate::enrollment::identity(&self.owner_leaf_state)?.public_key_record();
        let mut leaf_session = Session::owner(ids, leaf_key.clone());
        let leaf_launch = serde_json::to_string(
            &json!({"owner":wire_address,"owner_key":owner.public_key_record()}),
        )?;
        let mut owner_command = Command::new(std::env::current_exe()?);
        owner_command
            .arg("owner-leaf")
            .arg(&leaf_launch)
            .arg(&self.owner_leaf_state);
        let mut owner_child = spawn::Tracked::new(
            spawn::spawn_worker(
                owner_command,
                owner_pool.clone(),
                process_reservation,
                ProcessLimits {
                    deadline: job.deadline,
                    close: span(B::RouterClose),
                    reap: span(B::WorkerCleanupReserve),
                    poll: span(B::RegistryPoll),
                },
            )?,
            "owner_leaf",
            ledger,
        );
        let owner_pid = owner_child.pid();
        watch.add("owner_leaf", owner_pid);
        let _leaf_quiesce = watch.quiesce_on_drop();
        phase::mark("owner_leaf_spawned", &format!("pid={owner_pid}"));
        let leaf_request = json!({"node":"a-owner","swarm_id":swarm,"request_id":job.id,"generation":job.generation,"caps":plan.assignments["a-owner"]});
        let (leaf_registration, leaf_endpoint) = wire.serve(
            &mut leaf_session,
            "register",
            None,
            B::WorkerStartup,
            |body, caller| {
                if number(body, "pid")? != u64::from(owner_pid)
                    || body["key"] != leaf_key
                    || body["iroh"]["id"] != caller.to_string()
                {
                    return Err("owner_leaf_binding".into());
                }
                Ok(leaf_request.clone())
            },
        )?;
        let leaf_addr = iroh_frames::peer(&leaf_registration["iroh"])?;
        let (owner_hello, _) = wire.serve(
            &mut leaf_session,
            "hello",
            Some(leaf_endpoint),
            B::WorkerStartup,
            |body, _| {
                if number(body, "pid")? != u64::from(owner_pid)
                    || body["key"] != leaf_key
                    || body["iroh"] != leaf_registration["iroh"]
                {
                    return Err("owner_leaf_binding".into());
                }
                Ok(json!({"accepted":true}))
            },
        )?;
        let leaf_plan = json!({"checkpoint":self.checkpoint,"start":0,"end":split,"total":last,"tokens":context,"memory":plan.assignments["a-owner"].memory_bytes});
        wire.serve(
            &mut leaf_session,
            "plan",
            Some(leaf_endpoint),
            B::WorkerStartup,
            |_, _| Ok(leaf_plan.clone()),
        )?;
        wire.serve(
            &mut leaf_session,
            "ready",
            Some(leaf_endpoint),
            B::MemberChosenWall,
            |body, _| {
                if body["ready"] != true {
                    return Err("owner_not_ready".into());
                }
                Ok(json!({"acknowledged":true}))
            },
        )?;
        let leaf_connection = wire_host
            .connect(
                &leaf_addr,
                &wire_clock,
                wire_clock.deadline(B::WorkerStartup)?,
            )
            .map_err(|e| format!("iroh connect: {e:?}"))?;
        crate::control::close_on_death(watch, &leaf_connection).keep();
        let mut leaf_frames = iroh_frames::Frames::new(
            leaf_connection,
            &wire_runtime,
            &wire_clock,
            &leaf_addr,
            job.generation,
        )?;
        let mut leaf_peer = Peer::new(leaf_addr.clone(), job.generation, leaf_session);
        watch.on_death(leaf_peer.canceller()).keep();
        eprintln!("owner native ready after {:?}", start.elapsed());
        phase::mark("owner_leaf_ready", "");
        let child_tcp = [tcp_socket_count(child_pid)?, tcp_socket_count(owner_pid)?];
        if child_tcp != [0, 0] {
            return Err("child_tcp_socket_present".into());
        }
        core.started(&member_ticket, execution_origin, current()?)
            .map_err(|e| format!("core member ready: {e:?}"))?;
        core.started(&owner_ticket, execution_origin, current()?)
            .map_err(|e| format!("core owner ready: {e:?}"))?;
        job.record_lifecycle(core.state())?;
        fs::write(
            root.join("core-serving.json"),
            serde_json::to_vec_pretty(core.state())?,
        )?;
        let (mut receipts, session) = Receipts::open(
            &owner,
            (&hello["key"], text(&hello, "endpoint")?),
            &job.id,
            job.generation,
            &job.document,
            start.elapsed(),
            job.deadline.saturating_duration_since(Instant::now()),
        )?;
        job.install(session).map_err(|e| e.code)?;
        phase::mark("token_grant", "receipts_open");
        let mut input = tokens;
        let mut token_ids = Vec::new();
        let mut rendered = String::new();
        let mut detok = crate::detok::Detokenizer::new(&[151643, 151645]);
        let mut position = 0u32;
        let mut cancelled = false;
        // RU-020: a failure of the token loop no longer ends the job here. The member agents
        // survive their native workers, so the owner still closes both peers and settles the
        // session as Failed from their signed receipts.
        let looped = (|| -> Result<()> {
            for index in 0..max {
                watch.check()?;
                let step_now = current()?;
                let effects = core
                    .tick(step_now)
                    .map_err(|e| format!("core tick: {e:?}"))?;
                if effects
                    .iter()
                    .any(|e| matches!(e, node::Effect::Stop { .. }))
                    || members.iter().any(|m| m.current(step_now.mono).is_err())
                {
                    job.request_cancel()?;
                }
                if job.cancelled() {
                    cancelled = true;
                    break;
                }
                let phase = if index == 0 { "PREFILL" } else { "DECODE" };
                let h = header(&job.id, job.generation, position, phase);
                leaf_peer.call(&wire, "step", json!({"tokens":input}), B::WorkerStartup)?;
                let owner_frame = leaf_frames.receive(&h)?;
                let hidden = activation::unframe(&owner_frame, &h)?;
                position = position
                    .checked_add(hidden.shape[1])
                    .ok_or("position_overflow")?;
                member_peer.call(&wire, "step", json!({}), B::WorkerStartup)?;
                frames.send(&owner_frame)?;
                let raw = frames.receive(&h)?;
                let logits = activation::unframe(&raw, &h)?;
                let next = argmax(&logits)?;
                if job.cancelled() {
                    cancelled = true;
                    break;
                }
                let stop = matches!(next, 151643 | 151645);
                let final_token = stop || index + 1 == max;
                let envelope = receipts.sign(
                    &owner,
                    &[next],
                    final_token,
                    &canonical::sha256_prefixed(&raw),
                )?;
                let ids = job
                    .receipt(envelope.clone(), start.elapsed().as_secs_f64(), |raw| {
                        fs::write(root.join(format!("receipt-{index}.json")), raw)
                            .map_err(|_| protocol::ContractError("receipt_seal_failed"))
                    })
                    .map_err(|e| e.code)?;
                if ids != [next] {
                    return Err("receipt_token_binding".into());
                }
                phase::mark("token_signed", &format!("index={index}"));
                token_ids.extend(ids);
                // v0.1.4's incremental UTF-8 decoder: a token boundary inside a character emits
                // nothing until the character is whole (the old whole-list decode failed the request).
                let token_text = tokenizer.id_to_token(next).ok_or("invalid_token_id")?;
                let mut suffix = detok.push(next, &crate::detok::byte_level_bytes(&token_text));
                if final_token {
                    suffix.push_str(&detok.finish());
                }
                rendered.push_str(&suffix);
                if let Err(e) = job.token(index as usize, suffix) {
                    if job.cancelled() {
                        cancelled = true;
                        break;
                    }
                    return Err(e.code.into());
                }
                if final_token {
                    break;
                }
                input = vec![next];
            }
            Ok(())
        })();
        let failed = looped.err();
        // The job ends here as far as the workers are concerned (v0.1.4 closes the worker, which
        // ends its watch); a worker that died before this point must not be settled as cancelled.
        watch.check()?;
        watch.quiesce();
        let automatic =
            self.lifecycle.timed_qualification && generation == 1 && !cancelled && failed.is_none();
        let stop_request = if automatic {
            let trigger = lifecycle_owner::timed_stop(
                &self.lifecycle,
                &mut core,
                &owner,
                &self.membership,
                &wire,
                &mut member_peer,
                &hello["key"],
            )?;
            fs::write(
                root.join("rollover-trigger.json"),
                serde_json::to_vec(&trigger)?,
            )?;
            None
        } else {
            Some(
                core.request_stop(current()?)
                    .map_err(|e| format!("core stop: {e:?}"))?,
            )
        };
        job.record_lifecycle(core.state())?;
        let mut local = leaf_peer.call(
            &wire,
            "close",
            json!({"subject":receipts.subject}),
            B::WorkerStartup,
        )?;
        let mut remote = member_peer.call(
            &wire,
            "close",
            json!({"subject":receipts.subject}),
            B::WorkerStartup,
        )?;
        if remote["stats"]["iroh_router_closed"] != true
            || local["stats"]["iroh_router_closed"] != true
        {
            return Err("iroh_router_cleanup_unproven".into());
        }
        for (peer, closed) in [
            ("member", frames.close()),
            ("owner_leaf", leaf_frames.close()),
        ] {
            // A peer that already closed its side after its worker died leaves nothing to close.
            if let Err(error) = closed {
                if failed.is_none() {
                    return Err(error);
                }
                eprintln!("{peer} router carrier close after the failed run: {error}");
            }
        }
        iroh_frames::close(&mut wire_host, &wire_clock)?;
        drop(wire_host);
        drop(wire_runtime);
        let owner_exit = owner_child.wait()?;
        if !owner_exit.cleanup_proven
            || !owner_exit.errors.is_empty()
            || !owner_exit.status.is_some_and(|s| s.success())
        {
            return Err("owner_reap_unproven".into());
        }
        local["stats"]["iroh_endpoint_closed_by_exit"] = json!(true);
        let exit = child.wait()?;
        if !exit.cleanup_proven
            || !exit.errors.is_empty()
            || !exit.status.is_some_and(|s| s.success())
        {
            return Err("member_reap_unproven".into());
        }
        // The NodeControl endpoint closed after its final reply; a successful
        // exit status is the only proof (child exits non-zero if close failed).
        remote["stats"]["iroh_endpoint_closed_by_exit"] = json!(true);
        if string_command("/bin/ps", &["-p", &child_pid.to_string(), "-o", "pid="]).is_ok() {
            return Err("member_process_residue".into());
        }
        let deadline = mono()
            .checked_add(span(B::StageCleanup))
            .map_err(|e| format!("cleanup deadline: {e:?}"))?;
        let mut round = router::cleanup::CleanupRound::new(
            receipts.subject.clone(),
            vec![
                router::cleanup::CleanupPeer {
                    node: "b-member".into(),
                    host: "loopback-mac".into(),
                    process: json!(child_pid),
                    run: format!("slice-{}", job.generation),
                    deployment: "slice-development".into(),
                    endpoint: text(&hello, "endpoint")?.into(),
                    key: hello["key"].clone(),
                    generation: json!(job.generation),
                },
                router::cleanup::CleanupPeer {
                    node: "a-owner".into(),
                    host: "loopback-mac".into(),
                    process: json!(owner_pid),
                    run: format!("slice-{}", job.generation),
                    deployment: "slice-development".into(),
                    endpoint: text(&owner_hello, "endpoint")?.into(),
                    key: owner_hello["key"].clone(),
                    generation: json!(job.generation),
                },
            ],
            deadline,
        )?;
        round.accept("b-member", &remote["cleanup"], || (mono(), true))?;
        round.accept("a-owner", &local["cleanup"], || (mono(), true))?;
        if owner_pool.used()?.memory.get() != 0 {
            return Err("owner_process_reservation_residue".into());
        }
        let proof = round.finish(
            || {
                if pool
                    .used()
                    .map_err(|_| protocol::ContractError("process_pool"))?
                    .memory
                    .get()
                    == 0
                {
                    Ok(())
                } else {
                    Err(protocol::ContractError("process_reservation_residue"))
                }
            },
            || (mono(), true),
        )?;
        let mut local_closed = node::worker::Supervisor::new(
            owner_ticket,
            crate::worker::LeafAdapter::new(owner_child.into_inner()),
        );
        let mut member_closed = node::worker::Supervisor::new(
            member_ticket,
            crate::worker::LeafAdapter::new(child.into_inner()),
        );
        local_closed
            .request_stop()
            .map_err(|e| format!("owner stop: {e:?}"))?;
        member_closed
            .request_stop()
            .map_err(|e| format!("member stop: {e:?}"))?;
        let observations = [
            local_closed.observe_cleanup(),
            member_closed.observe_cleanup(),
        ];
        let observations = observations
            .into_iter()
            .collect::<std::result::Result<Vec<_>, _>>()
            .map_err(|e| format!("cleanup: {e:?}"))?;
        for observation in &observations {
            core.cleaned(observation, current()?)
                .map_err(|e| format!("core cleaned: {e:?}"))?;
        }
        if !core.state().workers.is_empty() || core.state().phase != node::RunPhase::Idle {
            return Err("core_cleanup_incomplete".into());
        }
        if let Some(stop_request) = stop_request {
            let stop_proof = core
                .finish_stop(&stop_request.fence)
                .map_err(|e| format!("stop proof: {e:?}"))?;
            fs::write(
                root.join("stop-proof.json"),
                serde_json::to_vec(
                    &json!({"epoch":stop_proof.epoch(),"run":stop_proof.run(),"cleanup_proven":true}),
                )?,
            )?;
        } else {
            if core.state().rollover_members.is_none() {
                return Err("rollover_continuation_lost".into());
            }
            fs::write(
                root.join("rollover-cleanup.json"),
                serde_json::to_vec(
                    &json!({"epoch":core.state().epoch,"run":core.state().run,"cleanup_proven":true,"pending_continuation":true,"clock":"injected-L1-wall-deadline"}),
                )?,
            )?;
        }
        job.record_lifecycle(core.state())?;
        fs::write(
            root.join("core-cleaned.json"),
            serde_json::to_vec_pretty(core.state())?,
        )?;
        if let Some(failure) = failed {
            // RU-020: both agents are alive and signed (a dead native worker was reaped by its
            // own agent), so the session settles as Failed from a real quorum, as v0.1.4 does.
            let exited = [("member", &remote), ("owner_leaf", &local)]
                .into_iter()
                .find_map(|(role, peer)| {
                    let worker = &peer["stats"]["worker_exit"];
                    Some(Death {
                        code: crate::liveness::NATIVE_EXITED,
                        role,
                        pid: u32::try_from(worker["pid"].as_u64()?).ok()?,
                        exit_status: worker["exit_status"]
                            .as_i64()
                            .and_then(|v| i32::try_from(v).ok()),
                        signal: worker["signal"]
                            .as_i64()
                            .and_then(|v| i32::try_from(v).ok()),
                    })
                });
            let code = exited.as_ref().map_or("slice_native_failed", |d| d.code);
            fs::write(
                root.join("failed-settle.json"),
                serde_json::to_vec_pretty(&json!({
                    "protocol":"mycelium.slice.failed_settle.v1",
                    "run_id":job.id,"generation":job.generation,
                    "terminal":"failed","error_code":code,"driver_error":failure.to_string(),
                    "signed_cleanup_quorum":true,"receipts":["b-member","a-owner"],
                    "agents":{"member_pid":child_pid,"owner_leaf_pid":owner_pid,"survived_to_close":true},
                    "died":exited.as_ref().map(|d| json!({"role":d.role,"pid":d.pid,"code":d.code,"exit_status":d.exit_status,"signal":d.signal})),
                    "member_stats":remote["stats"],"owner_leaf_stats":local["stats"],
                    "oracle":"member agent survives its worker and signs: r9-oracle process-topology-observations.json (FX-SLICE1-member-process-topology); RU-020",
                }))?,
            )?;
            job.settle_failed(proof, code, mono()).map_err(|e| e.code)?;
            phase::mark("failed_settled", &format!("code={code}"));
            // The Core is Idle and cleaned: the next Start continues on the same durable store
            // (its owner policy is unchanged), like the run after an explicit Stop.
            if self.lifecycle.timed_qualification {
                *retained = Some(core);
            }
            return match exited {
                Some(death) => {
                    self.record_native_exit(job, &death);
                    Err(Box::new(WorkerExited(death)))
                }
                None => Err(failure),
            };
        }

        fs::write(
            root.join("result.json"),
            serde_json::to_vec_pretty(
                &json!({"iroh_endpoints":{"owner":wire_address["id"],"member":hello["iroh"]["id"]},"lifecycle_boundary":if automatic {"wall_timed_rollover"} else {"explicit_stop"},"started_by":if continuation {"l6_continue_rollover"} else {"l7_prepare"},"lifecycle_run":core.state().run,"lifecycle_epoch":core.state().epoch,"owner_pid":std::process::id(),"owner_leaf_pid":owner_pid,"member_pid":child_pid,"member_reaped":true,"owner_native":local,"member_native":remote["stats"],"token_ids":token_ids,"text":rendered,"cancelled":cancelled,"planner_active":core.state().workers.len(),"reservation_authority":"L6-durable-core","pressure":1,"development_transport":false,"iroh_activation_transport":true,"iroh_control_transport":"signed-node-control","loopback_tcp_control":false,"artifact":{"served_chunks":artifact_chunks,"bytes":self.artifact.size,"pack_digest":self.artifact.pack_digest,"member":remote["stats"]["artifact"]},"child_tcp_sockets":{"member":child_tcp[0],"owner_leaf":child_tcp[1]}}),
            )?,
        )?;
        if self.lifecycle.timed_qualification {
            *retained = Some(core);
        }
        job.settle(
            proof,
            if cancelled {
                router::cleanup::Terminal::Cancelled
            } else {
                router::cleanup::Terminal::Completed
            },
            mono(),
        )
        .map_err(|e| e.code)?;
        Ok(())
    }
}
pub fn qualify(checkpoint: &Path, tokenizer: &Path, evidence: &Path) -> Result<()> {
    verify::run(checkpoint, tokenizer, evidence)
}
/// RU-020 qualification: a run whose native worker is killed from outside ends `failed` from a
/// real signed quorum, the agents survive, and the NEXT Start completes a normal run.
pub fn qualify_restart(checkpoint: &Path, tokenizer: &Path, evidence: &Path) -> Result<()> {
    verify::run_failed_then_normal(checkpoint, tokenizer, evidence)
}
pub fn development_qualification() -> Result<Value> {
    Ok(
        json!({"qualification_id":"slice-development-NOT-CERTIFIED","qualification_digest":canonical::digest(&json!(["development_only",backend_mlx::admission::CHECKPOINT]))?,
        "deployment_id":"slice-development","deployment_epoch":1,"topology_version":1,"model_id":"Qwen/Qwen2.5-0.5B-Instruct","resolved_commit":backend_mlx::admission::REVISION,
        "manifest_digest":format!("sha256:{}",backend_mlx::admission::MANIFEST),"path_manifest_digest":canonical::digest(&json!(["loopback","a-owner","b-member"]))?,"stage_load_proof_digests":[]}),
    )
}
pub fn configured_driver(
    checkpoint: &Path,
    tokenizer: &Path,
    evidence: &Path,
    private: &Path,
) -> Result<Arc<dyn service::Driver>> {
    configured_driver_mode(checkpoint, tokenizer, evidence, private, false)
}
fn configured_driver_mode(
    checkpoint: &Path,
    tokenizer: &Path,
    evidence: &Path,
    private: &Path,
    timed_qualification: bool,
) -> Result<Arc<dyn service::Driver>> {
    use crate::enrollment::{Invitation, LocalMembership, identity};
    let owner = identity(&private.join("owner"))?;
    let member_state = private.join("member");
    let member = identity(&member_state)?;
    let owner_leaf_state = private.join("owner-leaf");
    let _owner_leaf_identity = identity(&owner_leaf_state)?;
    let invitation = Invitation {
        swarm: canonical::digest(&json!([owner.verification_key_digest(), "swarm"]))?[7..].into(),
        owner_node: "a-owner".into(),
        owner_endpoint: owner.endpoint_id().into(),
        owner_key: owner.public_key_record(),
        member_node: "b-member".into(),
        member_key: member.public_key_record(),
        nonce: service::random_id().map_err(|e| e.code)?,
    };
    let caps = node::Caps {
        storage_bytes: B::SavedStorageConsent
            .bytes()
            .map_err(|e| format!("storage: {e:?}"))?
            .get(),
        memory_bytes: number(&host()?, "memory")?
            / number(
                &serde_json::from_str::<Value>(include_str!("../data/qwen2-planning.json"))?["policy"],
                "memory_fraction_denominator",
            )?,
        wall_ms: span(B::MemberChosenWall).millis(),
        commands: B::PyFinalMyceliumA13MacosModelResourcesMaxCommandsL562C82
            .count()
            .map_err(|e| format!("commands: {e:?}"))?
            .get(),
    };
    // Qualification acts as the local member operator, not an owner RPC consent
    // bypass. This does not claim the unported pairing/consent GUI was exercised.
    let membership = match LocalMembership::load_optional(&member_state)? {
        Some(local) => {
            if local.invitation.owner_key != invitation.owner_key
                || local.invitation.member_key != invitation.member_key
                || local.invitation.swarm != invitation.swarm
                || local.consent.withdrawn_at.is_some()
            {
                return Err("persisted_membership_pin_mismatch".into());
            }
            local
        }
        None => LocalMembership::approve(
            &member_state,
            invitation,
            caps,
            true,
            WallExpiry::from_unix_millis(receipts::wall_ms()?),
        )?,
    };
    Ok(Arc::new(OwnerDriver {
        checkpoint: checkpoint.into(),
        artifact: crate::artifact::Source::measure(checkpoint)?,
        tokenizer: tokenizer.into(),
        evidence: evidence.into(),
        identity: Mutex::new(owner),
        owner_state: private.join("owner"),
        member_state,
        owner_leaf_state,
        membership,
        lifecycle: lifecycle_owner::LifecycleOwner::new(timed_qualification)?,
    }))
}
/// Read-only exact-pid probe of an owned child: its TCP socket count, via lsof.
/// A child with a TCP socket would contradict "control rides Iroh QUIC (UDP)".
fn tcp_socket_count(pid: u32) -> Result<usize> {
    let out = spawn::confined_output(
        "/usr/sbin/lsof",
        &["-nP", "-a", "-p", &pid.to_string(), "-i", "TCP", "-Fn"],
    )?;
    match out.status.code() {
        Some(0) => Ok(String::from_utf8(out.stdout)?
            .lines()
            .filter(|l| l.starts_with('n'))
            .count()),
        Some(1) if out.stdout.is_empty() => Ok(0),
        _ => Err("socket_probe_failed".into()),
    }
}
