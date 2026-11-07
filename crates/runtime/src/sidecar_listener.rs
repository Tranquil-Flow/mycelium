// SPDX-License-Identifier: AGPL-3.0-only
//! Local listener loops over [`PooledRouter`] sessions: the accept half of `run_sidecar`
//! (sidecar.rs:657-750). A session slot is taken BEFORE accepting, so at most
//! `max_sessions` (shipped: 16, `MAX_LOCAL_SESSIONS`) are served and later connections wait in
//! the backlog; a Unix-socket peer with a different uid, or a loopback peer that is not the
//! one allowed process, is dropped before any handshake byte and reported as
//! `local_peer_rejected`. Differences from the oracle: the loops end on a caller-supplied
//! future instead of Ctrl-C, and live session tasks are aborted when the loop ends.
use crate::{
    iroh_pool::PooledRouter,
    sidecar_process::{
        BootstrapSecret, ConnectionOwnerTable, LoopbackListener, SidecarError,
        authorize_loopback_client, os_random_nonce, same_effective_uid,
    },
    sidecar_session::{SessionLimits, serve_session},
};
use std::{
    future::Future,
    net::SocketAddr,
    pin::pin,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
};
use tokio::{
    io::{AsyncRead, AsyncWrite},
    net::UnixListener,
    sync::{OwnedSemaphorePermit, Semaphore},
    task::JoinSet,
};
use transport::Clock;

/// Observer for the sidecar's stderr events (`local_peer_rejected`); render with
/// [`crate::sidecar_process::event_line`].
pub type EventObserver = Arc<dyn Fn(&'static str) + Send + Sync>;

/// What a listener loop serves with.
pub struct LocalServer<C: Clock + Send + Sync + 'static> {
    pub router: Arc<PooledRouter<C>>,
    pub secret: BootstrapSecret,
    pub endpoint_id: String,
    pub limits: SessionLimits,
    /// Concurrent sessions (shipped: 16).
    pub max_sessions: usize,
    pub events: EventObserver,
}

fn spawn_session<C, S>(
    tasks: &mut JoinSet<()>,
    server: &LocalServer<C>,
    stream: S,
    permit: OwnedSemaphorePermit,
    session_id: u64,
) where
    C: Clock + Send + Sync + 'static,
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let router = server.router.clone();
    let secret = *server.secret.as_bytes();
    let endpoint_id = server.endpoint_id.clone();
    let limits = server.limits;
    tasks.spawn(async move {
        let _permit = permit;
        let Ok(nonce) = os_random_nonce() else {
            return;
        };
        let outcome = serve_session(
            stream,
            session_id,
            &secret,
            &endpoint_id,
            nonce,
            &router,
            &limits,
        )
        .await;
        if outcome.is_err() {
            // A failed session simply closes; its undelivered items were already redelivered.
        }
    });
}

/// Serves the Unix-domain socket until `stop` resolves. A fatal accept error is
/// `runtime_error`.
pub async fn serve_unix_listener<C: Clock + Send + Sync + 'static>(
    listener: &UnixListener,
    server: &LocalServer<C>,
    stop: impl Future<Output = ()>,
) -> Result<(), SidecarError> {
    let sessions = Arc::new(Semaphore::new(server.max_sessions.max(1)));
    let next_session = AtomicU64::new(1);
    let mut tasks = JoinSet::new();
    let mut stop = pin!(stop);
    let result = loop {
        let permit = tokio::select! {
            permit = sessions.clone().acquire_owned() => match permit {
                Ok(permit) => permit,
                Err(_) => break Ok(()),
            },
            () = &mut stop => break Ok(()),
        };
        let accepted = tokio::select! {
            accepted = listener.accept() => accepted,
            () = &mut stop => break Ok(()),
        };
        let Ok((stream, _)) = accepted else {
            break Err(SidecarError::Runtime);
        };
        if !same_effective_uid(&stream) {
            drop(permit);
            (server.events)("local_peer_rejected");
            continue;
        }
        let id = next_session.fetch_add(1, Ordering::Relaxed);
        spawn_session(&mut tasks, server, stream, permit, id);
    };
    tasks.abort_all();
    while tasks.join_next().await.is_some() {}
    result
}

/// The loopback-channel loop: each connection must pass [`authorize_loopback_client`] for
/// the one `allowed_client_pid`, off the reactor and before any handshake byte.
pub async fn serve_loopback_listener<C: Clock + Send + Sync + 'static>(
    listener: &LoopbackListener,
    owners: Arc<dyn ConnectionOwnerTable>,
    allowed_client_pid: u32,
    server: &LocalServer<C>,
    stop: impl Future<Output = ()>,
) -> Result<(), SidecarError> {
    let sessions = Arc::new(Semaphore::new(server.max_sessions.max(1)));
    let next_session = AtomicU64::new(1);
    let mut tasks = JoinSet::new();
    let mut stop = pin!(stop);
    let result = loop {
        let permit = tokio::select! {
            permit = sessions.clone().acquire_owned() => match permit {
                Ok(permit) => permit,
                Err(_) => break Ok(()),
            },
            () = &mut stop => break Ok(()),
        };
        let accepted = tokio::select! {
            accepted = listener.accept() => accepted,
            () = &mut stop => break Ok(()),
        };
        let Ok((stream, client)) = accepted else {
            break Err(SidecarError::Runtime);
        };
        let table = owners.clone();
        let local = SocketAddr::V4(listener.local_addr());
        let authorized = tokio::task::spawn_blocking(move || {
            authorize_loopback_client(table.as_ref(), client, local, allowed_client_pid)
        })
        .await;
        if !matches!(authorized, Ok(Ok(()))) {
            drop(permit);
            (server.events)("local_peer_rejected");
            continue;
        }
        let id = next_session.fetch_add(1, Ordering::Relaxed);
        spawn_session(&mut tasks, server, stream, permit, id);
    };
    tasks.abort_all();
    while tasks.join_next().await.is_some() {}
    result
}
