// SPDX-License-Identifier: AGPL-3.0-only
//! Local signed-mailbox carrier. Transport identity is NOT membership authority.
//! Dispatch must authenticate the original envelope against current authority;
//! neither this bridge nor closing its endpoint supplies a native Stop receipt.
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant, Span};
use iroh::{Endpoint, EndpointAddr, RelayMode, endpoint::presets};
use runtime::{
    iroh_control::{ControlRequest, NodeControlClient},
    iroh_host::LoopbackHost,
    managed_owner::{OwnerError, session::CallFence},
};
use std::{
    net::{Ipv4Addr, SocketAddrV4},
    sync::{Arc, Condvar, Mutex},
    thread::JoinHandle,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::sync::watch;
use transport::{Channel, Clock, ConnectionFence};

type Result<T> = std::result::Result<T, OwnerError>;
#[derive(Clone)]
pub struct HostClock(Arc<Instant>);
impl Default for HostClock {
    fn default() -> Self {
        Self(Arc::new(Instant::now()))
    }
}
impl Clock for HostClock {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(
            ClockDomain(0),
            self.0.elapsed().as_millis().try_into().unwrap_or(u64::MAX),
        )
    }
    fn wall_seconds(&self) -> f64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(f64::NAN, |d| d.as_secs_f64())
    }
}
fn span(id: B) -> Result<Span> {
    id.span(Entries::ZERO)
        .map_err(|_| OwnerError("budget_unavailable"))
}
fn runtime() -> Result<tokio::runtime::Runtime> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|_| OwnerError("control_carrier_unavailable"))
}
fn deadline(clock: &impl Clock, id: B) -> Result<MonoInstant> {
    clock
        .monotonic()
        .checked_add(span(id)?)
        .map_err(|_| OwnerError("control_timeout_invalid"))
}
/// Cancellation for an API-owned target worker, independent of owner sessions.
#[derive(Clone)]
pub struct TargetFence(Arc<TargetSignal>);
struct TargetSignal {
    state: watch::Sender<bool>,
    lock: Mutex<()>,
    wake: Condvar,
}
impl Default for TargetFence {
    fn default() -> Self {
        let (state, _) = watch::channel(false);
        Self(Arc::new(TargetSignal {
            state,
            lock: Mutex::new(()),
            wake: Condvar::new(),
        }))
    }
}
impl TargetFence {
    pub fn check(&self) -> Result<()> {
        if *self.0.state.borrow() {
            Err(OwnerError("managed_control_command_interrupted"))
        } else {
            Ok(())
        }
    }
    pub fn cancel(&self) -> Result<()> {
        let guard = self.0.lock.lock();
        self.0.state.send_replace(true);
        self.0.wake.notify_all();
        guard
            .map(drop)
            .map_err(|_| OwnerError("control_carrier_state_unavailable"))
    }
    pub fn wait_for(&self, interval: Span) -> Result<()> {
        let guard = self
            .0
            .lock
            .lock()
            .map_err(|_| OwnerError("control_carrier_state_unavailable"))?;
        self.check()?;
        let guard = self
            .0
            .wake
            .wait_timeout_while(guard, Duration::from_millis(interval.millis()), |_| {
                !*self.0.state.borrow()
            })
            .map_err(|_| OwnerError("control_carrier_state_unavailable"))?;
        drop(guard);
        self.check()
    }
}

// Translate only transport failures corresponding to the source client's
// socket/timeout classes. Authority, destination, budget and custody failures
// remain fatal with their original identity. Never classify all carrier errors.
fn target_exchange_error(error: OwnerError) -> OwnerError {
    OwnerError(match error.0 {
        "control_connect_timeout"
        | "control_open_timeout"
        | "control_write_timeout"
        | "control_read_timeout" => "managed_control_response_timeout",
        "control_connect_failed"
        | "control_open_failed"
        | "control_write_failed"
        | "control_read_failed" => "managed_control_unreachable",
        "empty_control_call_result" => "managed_control_response_invalid",
        code => code,
    })
}

/// One host-owned, local-only control service. No public relay, lookup or model.
/// The callback receives the exact signed bytes and must do current membership
/// authentication again at dispatch. One request is admitted at a time.
pub struct LocalMailboxCarrier {
    peer: EndpointAddr,
    authority: watch::Sender<Option<ConnectionFence>>,
    stop: watch::Sender<bool>,
    worker: Mutex<Option<JoinHandle<Result<()>>>>,
}
impl LocalMailboxCarrier {
    pub fn start(dispatch: impl Fn(&[u8]) -> Result<Vec<u8>> + Send + 'static) -> Result<Self> {
        let (ready_tx, ready_rx) = std::sync::mpsc::sync_channel(1);
        let (stop, mut stopped) = watch::channel(false);
        let worker = std::thread::spawn(move || {
            let rt = runtime()?;
            rt.block_on(async move {
                let bind = Endpoint::builder(presets::Minimal)
                    .relay_mode(RelayMode::Disabled).clear_address_lookup().clear_ip_transports()
                    .portmapper_config(iroh::endpoint::PortmapperConfig::Disabled)
                    .bind_addr(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))
                    .map_err(|_| OwnerError("control_carrier_bind_failed"))?
                    .alpns(vec![Channel::NodeControl.alpn().to_vec()]).bind();
                // Local socket construction: no relay/discovery and no invented
                // bootstrap deadline aliased from an unrelated client timeout.
                let endpoint = bind.await.map_err(|_| OwnerError("control_carrier_bind_failed"))?;
                let peer = endpoint.bound_sockets().into_iter().fold(EndpointAddr::new(endpoint.id()), |a,s| a.with_ip_addr(s));
                let result = async {
                    ready_tx.send(peer).map_err(|_| OwnerError("control_carrier_start_cancelled"))?;
                    loop {
                        let incoming = tokio::select! {
                            biased;
                            _ = stopped.changed() => break,
                            next = endpoint.accept() => next.ok_or(OwnerError("control_carrier_closed"))?,
                        };
                        let exchange = async {
                            let connection = incoming.await.map_err(|_| OwnerError("control_accept_failed"))?;
                            let result = async {
                                let (mut send, mut recv) = connection.accept_bi().await.map_err(|_| OwnerError("control_read_failed"))?;
                                let maximum = B::PyMyceliumIrohSidecarClientControlCallMaxBytesL79.bytes()
                                    .map_err(|_| OwnerError("budget_unavailable"))?.get().try_into()
                                    .map_err(|_| OwnerError("budget_unavailable"))?;
                                let bytes = recv.read_to_end(maximum).await.map_err(|_| OwnerError("invalid_control_call"))?;
                                if bytes.is_empty() { return Err(OwnerError("invalid_control_call")); }
                                let answer = dispatch(&bytes)?;
                                if answer.is_empty() || answer.len() > maximum { return Err(OwnerError("invalid_control_result")); }
                                send.write_all(&answer).await.map_err(|_| OwnerError("control_write_failed"))?;
                                send.finish().map_err(|_| OwnerError("control_write_failed"))?;
                                // Keep the executor alive until the caller's normal close. This
                                // flushes the receipt and reciprocal Close; no native proof.
                                connection.closed().await;
                                Ok(())
                            }.await;
                            connection.close(7u32.into(), b"control call ended");
                            result
                        };
                        let wait = Duration::from_millis(span(B::NodeControlCallTimeoutCeiling)?.millis());
                        let result = tokio::select! {
                            biased;
                            _ = stopped.changed() => break,
                            result = tokio::time::timeout(wait, exchange) => result.unwrap_or(Err(OwnerError("control_read_timeout"))),
                        };
                        if let Err(error) = result { eprintln!("managed mailbox carrier: {}", error.0); }
                    }
                    Ok(())
                }.await;
                let close = tokio::time::timeout(Duration::from_millis(span(B::RouterClose)?.millis()), endpoint.close()).await
                    .map_err(|_| OwnerError("control_carrier_cleanup_unproven"));
                result.and(close)
            })
        });
        let peer = match ready_rx.recv() {
            Ok(peer) => peer,
            Err(_) => {
                return match worker.join() {
                    Ok(Err(error)) => Err(error),
                    _ => Err(OwnerError("control_carrier_start_failed")),
                };
            }
        };
        let fence = ConnectionFence {
            endpoint_id: *peer.id.as_bytes(),
            local_generation: 0,
            remote_generation: 0,
        };
        let (authority, _) = watch::channel(Some(fence));
        Ok(Self {
            peer,
            authority,
            stop,
            worker: Mutex::new(Some(worker)),
        })
    }
    /// Revoke all outstanding transport capabilities immediately. Reopening a
    /// different service never reactivates this watch or an old session factory.
    pub fn revoke(&self) {
        self.authority.send_replace(None);
    }
    pub fn call(
        &self,
        clock: &impl Clock,
        bytes: &[u8],
        end: MonoInstant,
        local: Option<&CallFence>,
    ) -> Result<Vec<u8>> {
        self.call_inner(clock, bytes, end, local, None)
    }
    pub fn call_target(
        &self,
        clock: &impl Clock,
        bytes: &[u8],
        end: MonoInstant,
        target: &TargetFence,
    ) -> Result<Vec<u8>> {
        self.call_inner(clock, bytes, end, None, Some(target))
            .map_err(target_exchange_error)
    }
    fn call_inner(
        &self,
        clock: &impl Clock,
        bytes: &[u8],
        end: MonoInstant,
        local: Option<&CallFence>,
        target: Option<&TargetFence>,
    ) -> Result<Vec<u8>> {
        let mut authority = self.authority.subscribe();
        let fence = (*authority.borrow()).ok_or(OwnerError("managed_owner_closed"))?;
        if let Some(local) = local {
            local.check()?;
        }
        if let Some(target) = target {
            target.check()?;
        }
        let rt = runtime()?;
        let forward = target.map(|target| {
            let mut global = authority.clone();
            let mut cancelled = target.0.state.subscribe();
            let (merged, receiver) = watch::channel(Some(fence));
            authority = receiver;
            rt.spawn(async move {
                if global.borrow().is_none() || *cancelled.borrow() {
                    merged.send_replace(None);
                    return;
                }
                tokio::select! { biased; _=global.changed()=>{}, _=cancelled.changed()=>{} }
                merged.send_replace(None);
            })
        });
        let mut host = LoopbackHost::bind(&rt, clock, end)
            .map_err(|_| OwnerError("control_carrier_bind_failed"))?;
        let result = (|| {
            let client = NodeControlClient::new(
                &host,
                clock,
                B::NodeControlCallConcurrency
                    .count()
                    .map_err(|_| OwnerError("budget_unavailable"))?,
            )
            .map_err(|e| OwnerError(e.0))?;
            client
                .call_fenced(
                    ControlRequest {
                        peer: &self.peer,
                        fence,
                        command: bytes,
                        deadline: end,
                    },
                    &mut authority,
                    local,
                )
                .map_err(|e| OwnerError(e.0))
        })();
        if let Some(forward) = forward {
            forward.abort();
            if let Err(e) = rt.block_on(forward) {
                // Cancellation is the expected result of our explicit abort.
                if !e.is_cancelled() {
                    return Err(OwnerError("control_carrier_worker_panicked"));
                }
            }
        }
        let closed = host
            .close(clock, deadline(clock, B::RouterClose)?)
            .map_err(|_| OwnerError("control_carrier_cleanup_unproven"));
        drop(host);
        drop(rt);
        closed?;
        if let Some(target) = target {
            target.check()?;
        }
        result
    }
    pub fn close(&self) -> Result<()> {
        self.revoke();
        self.stop.send_replace(true);
        if let Some(worker) = self
            .worker
            .lock()
            .map_err(|_| OwnerError("control_carrier_state_unavailable"))?
            .take()
        {
            worker
                .join()
                .map_err(|_| OwnerError("control_carrier_worker_panicked"))??;
        }
        Ok(())
    }
}
impl Drop for LocalMailboxCarrier {
    fn drop(&mut self) {
        if let Err(error) = self.close() {
            eprintln!("managed mailbox carrier close: {}", error.0);
        }
    }
}

#[cfg(test)]
mod target_error_tests {
    use super::*;
    #[test]
    fn only_transport_uncertainty_maps_to_source_retry_classes() {
        for code in [
            "control_connect_timeout",
            "control_open_timeout",
            "control_write_timeout",
            "control_read_timeout",
        ] {
            assert_eq!(
                target_exchange_error(OwnerError(code)).0,
                "managed_control_response_timeout"
            );
        }
        for code in [
            "control_connect_failed",
            "control_open_failed",
            "control_write_failed",
            "control_read_failed",
        ] {
            assert_eq!(
                target_exchange_error(OwnerError(code)).0,
                "managed_control_unreachable"
            );
        }
        for code in [
            "cancelled",
            "peer_rotated",
            "control_peer_mismatch",
            "invalid_control_destination",
            "control_timeout_invalid",
            "control_timeout_exceeded",
            "control_carrier_cleanup_unproven",
            "managed_owner_closed",
            "control_response_too_large",
            "unknown_error",
        ] {
            assert_eq!(target_exchange_error(OwnerError(code)).0, code);
        }
    }
}
