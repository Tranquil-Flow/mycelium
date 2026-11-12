// SPDX-License-Identifier: AGPL-3.0-only
//! Joined member process: pairing, B3 custody, native stage compute and route
//! serving. Operator commands arrive over private stdin and, when started with
//! `--owner-control <owner control endpoint id>`, over the signed remote control
//! channel (`remote.rs`). The carrier is LocalOnly loopback unless `--lan <ip>`
//! explicitly selects the direct-LAN mode (allow-list: private ranges plus any
//! `--lan-allow <ip>`). No route_ready claim.
use api::{
    app_host,
    joined_member::work::{self, Work},
    joined_shard, peer,
    seed::SeedOptions,
};
use budgets::{BudgetId as B, Entries};
use protocol::identity::Identity;
use runtime::iroh_host::{LanPolicy, LoopbackHost};
use serde_json::{Value, json};
use std::path::PathBuf;
use transport::{Channel, Clock};
#[path = "shard_member/membership.rs"]
mod membership;
use membership::Membership;
#[path = "shard_member/control.rs"]
mod control;
#[path = "shard_member/remote.rs"]
mod remote;
type Result<T> = std::result::Result<T, String>;
fn main() {
    if let Err(code) = run() {
        eprintln!("shard_member_failed:{code}");
        std::process::exit(1);
    }
}
struct Args {
    root: PathBuf,
    lan: Option<std::net::IpAddr>,
    allow: Vec<std::net::IpAddr>,
    owner_control: Option<iroh::EndpointId>,
    /// The member operator's own consent decision (member-side, C1).
    consent: Option<Value>,
}
fn args() -> Result<Args> {
    let mut args = std::env::args().skip(1);
    let usage = "usage: --state-root PATH [--lan IP] [--lan-allow IP]... [--owner-control ENDPOINT_ID] [--consent-file PATH]";
    if args.next().as_deref() != Some("--state-root") {
        return Err(usage.into());
    }
    let mut parsed = Args {
        root: PathBuf::from(args.next().ok_or("state_root_required")?),
        lan: None,
        allow: Vec::new(),
        owner_control: None,
        consent: None,
    };
    while let Some(flag) = args.next() {
        let value = args.next().ok_or(usage)?;
        match flag.as_str() {
            "--lan" => parsed.lan = Some(value.parse().map_err(|_| "lan_ip_invalid")?),
            "--lan-allow" => parsed
                .allow
                .push(value.parse().map_err(|_| "lan_allow_invalid")?),
            "--owner-control" => {
                parsed.owner_control = Some(value.parse().map_err(|_| "owner_control_invalid")?)
            }
            "--consent-file" => parsed.consent = Some(operator_consent(&value)?),
            _ => return Err(usage.into()),
        }
    }
    if parsed.lan.is_none() && !parsed.allow.is_empty() {
        return Err("lan_allow_requires_lan".into());
    }
    Ok(parsed)
}
/// Read the member operator's consent decision: `{device_memory_bytes,
/// policy, consent}` (the same body the local operator's `consent` takes).
fn operator_consent(path: &str) -> Result<Value> {
    let limit =
        api::preferences::private_state::private_json_limit().map_err(|e| e.0.to_owned())?;
    let text = std::fs::read(path).map_err(|_| "consent_file_unreadable")?;
    if text.len() as u64 > limit {
        return Err("consent_file_invalid".into());
    }
    let body: Value = serde_json::from_slice(&text).map_err(|_| "consent_file_invalid")?;
    if !body["policy"].is_object() || !body["consent"].is_object() {
        return Err("consent_file_invalid".into());
    }
    // v0.1.4 order (`app.py:1742-1757`): the policy and the standing grant's
    // limits are checked before this member pairs or joins anything; a
    // decision it cannot honour ends it here, not after a seed join.
    work::usable_decision(&body)?;
    Ok(body)
}
/// The library work service reaches this process's membership (its renewal loop is this
/// example's own `membership.rs`).
impl work::Member for Membership {
    fn session(&self) -> &std::sync::Arc<std::sync::Mutex<Option<api::member::Session>>> {
        &self.session
    }
    fn current(
        &self,
        signer: &Identity,
    ) -> std::result::Result<Value, api::compiler_inputs::Error> {
        Membership::current(self, signer)
    }
}
fn run() -> Result<()> {
    let Args {
        root,
        lan,
        allow,
        owner_control,
        consent: operator_decision,
    } = args()?;
    let policy = match lan {
        Some(_) => Some(LanPolicy::with_explicit(allow).map_err(|_| "lan_allow_invalid")?),
        None => None,
    };
    // Public host admission normalizes trusted ancestors and refuses root links.
    let authority =
        app_host::Authority::<app_host::Unmanaged>::load(root).map_err(str::to_owned)?;
    let root = authority.root;
    let (signer, _, _) = app_host::identity(&root).map_err(|e| e.code.to_owned())?;
    // Pump QUIC even while stdin, file verification or native compute blocks.
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .map_err(|_| "runtime_failed")?;
    let clock = api::managed_carrier::HostClock::default();
    let deadline = || {
        clock
            .monotonic()
            .checked_add(
                B::RouterClose
                    .span(Entries::ZERO)
                    .map_err(|_| "budget_unavailable")?,
            )
            .map_err(|_| "deadline_invalid".to_owned())
    };
    let mut host = match (lan, &policy) {
        (Some(ip), Some(policy)) => LoopbackHost::bind_lan(
            &rt,
            &clock,
            deadline()?,
            iroh::SecretKey::generate(),
            &[Channel::Router],
            ip,
            policy.clone(),
        ),
        _ => LoopbackHost::bind(&rt, &clock, deadline()?),
    }
    .map_err(|e| format!("{e:?}"))?;
    // Accepted carrier peers for seed/source dials: loopback, or the LAN allow-list.
    let reachable = |ip: std::net::IpAddr| match &policy {
        Some(policy) => policy.allows(ip),
        None => ip.is_loopback(),
    };
    let addr = host.address().map_err(|e| format!("{e:?}"))?;
    let carrier = Identity::from_secret(&addr.id.to_string(), &signer.secret_bytes())
        .map_err(|_| "identity_failed")?;
    let mut session: Option<Membership> = None;
    let mut owned = Work::operator(root.clone());
    let input = control::Input::start(owner_control.is_some())?;
    let status: work::Status =
        work::Live::new(json!({"joined":false,"left":false,"worker_pid":null,"route_ready":false}));
    let control_channel = match owner_control {
        Some(owner) => {
            let channel = remote::start(
                owner,
                lan.zip(policy.clone()),
                input.dispatcher.clone(),
                Identity::from_secret(signer.endpoint_id(), &signer.secret_bytes())
                    .map_err(|_| "identity_failed")?,
                status.clone(),
            )?;
            // Public pins only (no secret): what the owner operator needs to dial.
            let notice = json!({"member_control":{"endpoint_id":channel.endpoint.to_string(),
                "addrs":channel.addrs,"carrier_endpoint_id":addr.id.to_string(),
                "carrier_addrs":addr.ip_addrs().collect::<Vec<_>>(),"pid":std::process::id()}});
            api::preferences::private_state::store(
                &root.join("member-control.json"),
                &notice,
                api::preferences::private_state::private_json_limit()
                    .map_err(|e| e.0.to_owned())?,
            )
            .map_err(|e| e.0.to_owned())?;
            eprintln!("{notice}");
            Some(channel)
        }
        None => None,
    };
    while let Ok(operation) = input.requests.recv() {
        let operation = operation?;
        let request = &operation.request;
        let command = request["command"]
            .as_str()
            .ok_or("operator_command_invalid")?;
        let reply: Result<Value> = (|| match command {
            _ if owned.left
                && matches!(
                    command,
                    "receive" | "prepare_native" | "serve" | "accept" | "resume"
                ) =>
            {
                Err("member_left".into())
            }
            "carrier_address" => {
                let sockets: Vec<std::net::SocketAddr> = addr.ip_addrs().copied().collect();
                // Once joined, the enrolled key signs where this carrier listens.
                let advert = match session.as_ref() {
                    Some(member) => {
                        let held = member.session.lock().map_err(|_| "session_poisoned")?;
                        Some(
                            joined_shard::carrier::advertise(
                                held.as_ref().ok_or("not_joined")?,
                                &signer,
                                &carrier,
                                &sockets,
                                app_host::now(),
                            )
                            .map_err(|e| e.0.to_owned())?,
                        )
                    }
                    None => None,
                };
                Ok(json!({"endpoint_id":addr.id.to_string(),
                    "socket":sockets.first().ok_or("carrier_address_missing")?,
                    "sockets":sockets,"advert":advert}))
            }
            "serve" => {
                let served = owned.serve(
                    session.as_ref(),
                    &work::Carrier {
                        host: &host,
                        runtime: &rt,
                        clock: &clock,
                        endpoint: addr.id,
                        lan: policy.as_ref(),
                    },
                    &signer,
                    &operation,
                );
                // B11 (gap row 19): the epoch is over. A direct-LAN carrier is
                // recycled on the same socket and key before the reply, so its
                // signed address stays valid while iroh's never-evicting
                // address map of this epoch goes with the old endpoint.
                if lan.is_none() {
                    return served;
                }
                let recycled = deadline()
                    .and_then(|d| host.recycle_lan(&clock, d).map_err(|e| format!("{e:?}")));
                match (served, recycled) {
                    (Ok(mut report), Ok(())) => {
                        report["carrier_recycled"] = json!(true);
                        // B14: the recycled endpoint KEEPS the hosts it
                        // learned from key-verified owner handshakes.
                        let learned: Vec<String> = host
                            .lan_learned_hosts()
                            .unwrap_or_default()
                            .iter()
                            .map(|a| a.ip().to_string())
                            .collect();
                        report["carrier_learned_hosts"] = json!(learned);
                        Ok(report)
                    }
                    (Ok(_), Err(e)) => Err(format!("carrier_recycle_failed:{e}")),
                    (Err(code), Ok(())) => Err(code),
                    (Err(code), Err(e)) => Err(format!("{code};carrier_recycle_failed:{e}")),
                }
            }
            "leave" => owned.leave(&mut session, &signer, &operation),
            "pair_request" => app_host::build_pair_request(&root, &root)
                .map(|v| v.to_value())
                .map_err(|e| e.code.to_owned()),
            "accept" => {
                if session.is_some() {
                    return Err("already_joined".into());
                }
                let socket: std::net::SocketAddr =
                    serde_json::from_value(request["seed_address"].clone())
                        .map_err(|_| "seed_address_invalid")?;
                if !reachable(socket.ip()) {
                    return Err(if policy.is_some() {
                        "seed_address_refused"
                    } else {
                        "seed_address_not_loopback"
                    }
                    .into());
                }
                let options = SeedOptions {
                    ca_pem: Some(
                        request["ca_pem"]
                            .as_str()
                            .ok_or("ca_required")?
                            .as_bytes()
                            .to_vec(),
                    ),
                    connect_to: Some(socket),
                    server_name: Some("127.0.0.1".into()),
                };
                let random = |n| {
                    app_host::random(n)
                        .map(|v| app_host::hex(&v))
                        .unwrap_or_default()
                };
                let env = peer::JoinEnv {
                    signer: &signer,
                    clock: &app_host::now,
                    random_hex: &random,
                    seed: &options,
                };
                // The operator's `--consent-file` decision is granted before
                // the join (unbound) and bound to the swarm joined below.
                if let Some(body) = &operator_decision {
                    work::grant_decision(&root, body)?;
                }
                let mut accepted = peer::accept_handoff(&root, true, &request["handoff"], &env)
                    .map_err(|e| e.code.to_owned())?;
                peer::renew(&root, true, accepted.session.as_mut(), &env)
                    .map_err(|e| e.code.to_owned())?;
                session = Some(Membership::new(
                    accepted.session.ok_or("session_missing")?,
                    root.clone(),
                    &signer,
                    options,
                )?);
                // The operator's `--consent-file` decision binds to the swarm
                // this member just joined (member-side, before any offer).
                if operator_decision.is_some() {
                    work::bind_consent(&root, session.as_ref().ok_or("session_missing")?)?;
                }
                let held = session
                    .as_ref()
                    .ok_or("session_missing")?
                    .session
                    .lock()
                    .map_err(|_| "session_poisoned")?;
                joined_shard::register(
                    held.as_ref().ok_or("session_missing")?,
                    &signer,
                    &carrier,
                    app_host::now(),
                )
                .map_err(|e| e.0.to_owned())
            }
            "resume" => {
                let member = session.as_ref().ok_or("not_joined")?;
                member.resume(&signer)?;
                member.current(&signer).map_err(|e| e.0.to_owned())
            }
            // C1: consent stays member-side. Only this member's own operator
            // (private stdin) writes it; the pinned owner may only request it.
            "consent" if operation.origin == work::Origin::Remote => {
                Err("member_consent_member_side".into())
            }
            "consent" => work::grant_consent(&root, session.as_ref().ok_or("not_joined")?, request),
            // The member's own standing consent and resource policy, for the
            // owner to check before any offer (signed on the remote channel).
            "consent_status" => work::consent_status(&root),
            "receive" => owned.receive(
                session.as_ref(),
                &work::Carrier {
                    host: &host,
                    runtime: &rt,
                    clock: &clock,
                    endpoint: addr.id,
                    lan: policy.as_ref(),
                },
                &signer,
                &carrier,
                &operation,
            ),
            "prepare_native" => owned.prepare_native(session.as_ref(), &signer, &operation),
            "native_step" => owned.native_step(session.as_ref(), &signer, &operation),
            "native_close" => owned.native_close(),
            "discard" => owned.discard(),
            "close" => Ok(json!({"closing":true,"route_ready":false})),
            _ => Err("operator_command_unknown".into()),
        })();
        let out = match reply {
            Ok(value) => json!({"ok":true,"result":value}),
            Err(code) => json!({"ok":false,"code":code}),
        };
        owned.record(&status, session.is_some(), command, &operation, &out);
        input.complete(&operation, out)?;
        if command == "close" {
            break;
        }
    }
    if let Some(channel) = control_channel {
        channel.stop()?;
    }
    input.join()?;
    host.close(&clock, deadline()?)
        .map_err(|e| format!("{e:?}"))?;
    drop(host);
    drop(rt);
    // Retained shard debt deliberately survives EOF; no native cleanup inference.
    Ok(())
}
