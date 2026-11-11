// SPDX-License-Identifier: AGPL-3.0-only
//! Member side of the signed owner<->member control channel
//! (`api::member_control`): an owned thread with its own runtime and NodeControl
//! endpoint, bound HERE (loopback, or direct LAN on the operator's interface),
//! running the library loop `api::joined_member::work::control_loop`, which
//! accepts ONLY the operator-pinned owner control endpoint and feeds the same
//! serial operation queue as stdin.
use api::{
    joined_member::work::{self, Dispatcher, Status},
    managed_carrier::HostClock,
};
use budgets::BudgetId as B;
use protocol::identity::Identity;
use runtime::iroh_host::{LanPolicy, LoopbackHost, NetworkMode};
use std::{
    net::{IpAddr, SocketAddr},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
};
use transport::Channel;
type Result<T> = std::result::Result<T, String>;
pub struct Remote {
    pub endpoint: iroh::EndpointId,
    pub addrs: Vec<SocketAddr>,
    stop: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}
impl Remote {
    pub fn stop(mut self) -> Result<()> {
        self.stop.store(true, Ordering::SeqCst);
        self.thread
            .take()
            .ok_or("member_control_missing")?
            .join()
            .map_err(|_| "member_control_panicked".into())
    }
}
pub fn start(
    owner: iroh::EndpointId,
    lan: Option<(IpAddr, LanPolicy)>,
    dispatcher: Arc<Dispatcher>,
    signer: Identity,
    status: Status,
) -> Result<Remote> {
    let stop = Arc::new(AtomicBool::new(false));
    let (ready_tx, ready_rx) = mpsc::sync_channel(1);
    let flag = stop.clone();
    let thread = std::thread::spawn(move || {
        if let Err(code) = serve(owner, lan, dispatcher, signer, status, flag, ready_tx) {
            eprintln!("member_control_failed:{code}");
        }
    });
    let wait = std::time::Duration::from_millis(
        B::WorkerStartup
            .span(budgets::Entries::ZERO)
            .map_err(|_| "budget_unavailable")?
            .millis(),
    );
    let (endpoint, addrs) = ready_rx
        .recv_timeout(wait)
        .map_err(|_| "member_control_bind_failed")??;
    Ok(Remote {
        endpoint,
        addrs,
        stop,
        thread: Some(thread),
    })
}
type Ready = mpsc::SyncSender<Result<(iroh::EndpointId, Vec<SocketAddr>)>>;
fn serve(
    owner: iroh::EndpointId,
    lan: Option<(IpAddr, LanPolicy)>,
    dispatcher: Arc<Dispatcher>,
    signer: Identity,
    status: Status,
    stop: Arc<AtomicBool>,
    ready: Ready,
) -> Result<()> {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .map_err(|_| "runtime_failed")?;
    let clock = HostClock::default();
    let secret = iroh::SecretKey::generate();
    let bound = match lan {
        Some((ip, policy)) => LoopbackHost::bind_lan(
            &rt,
            &clock,
            work::until(&clock, B::RouterClose)?,
            secret,
            &[Channel::NodeControl],
            ip,
            policy,
        ),
        None => LoopbackHost::bind_protocols(
            &rt,
            &clock,
            work::until(&clock, B::RouterClose)?,
            secret,
            &[Channel::NodeControl],
        ),
    };
    let mut host = match bound {
        Ok(host) => host,
        Err(e) => {
            let code = format!("member_control_bind_failed:{e:?}");
            ready
                .send(Err(code.clone()))
                .map_err(|_| "member_control_ready")?;
            return Err(code);
        }
    };
    // Direct LAN: only the operator-configured owner key is accepted; the
    // host refuses (and unpins at once) any other key (REVIEW-laneB9 F2).
    if host.mode() == NetworkMode::DirectLan
        && let Err(e) = host.admit_peer_ids(&[owner])
    {
        let code = format!("member_control_admission_failed:{e:?}");
        ready
            .send(Err(code.clone()))
            .map_err(|_| "member_control_ready")?;
        return Err(code);
    }
    let addr = host.address().map_err(|e| format!("{e:?}"))?;
    let me = addr.id;
    ready
        .send(Ok((me, addr.ip_addrs().copied().collect())))
        .map_err(|_| "member_control_ready")?;
    work::control_loop(
        &rt,
        &clock,
        &mut host,
        owner,
        me,
        &dispatcher,
        &signer,
        &status,
        &stop,
    )?;
    host.close(&clock, work::until(&clock, B::RouterClose)?)
        .map_err(|e| format!("{e:?}"))?;
    // `host` drops before `rt` (reverse declaration order): sockets are reaped.
    Ok(())
}
