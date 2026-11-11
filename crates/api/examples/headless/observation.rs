// SPDX-License-Identifier: AGPL-3.0-only
//! Production ownership: one CLIENT journal per seed, fresh factory per run.
//!
//! The observe port itself is `api::owner_observation` (lane PH3b S3m, moved unchanged from this
//! file); this launcher implements its `ChainHost` over its Core and keeps its test seam.
use super::*;
use api::owner_observation::{self as moved, ChainHost, ChainView};
pub(super) use api::owner_observation::{bootstrap, target};
#[cfg(test)]
use api::owner_observation::{limits, policy};
#[cfg(test)]
use control_runtime::mailbox::{Mailbox, StagePublication};
use runtime::managed_owner::OwnerError;
#[cfg(test)]
#[path = "observation_tests.rs"]
mod tests;

type OResult<T> = Result<T, OwnerError>;

pub(super) type Adapter = moved::Adapter<HeadlessRun>;
/// The seed-owned managed owner of this launcher's Core.
pub(super) type Managed = moved::Managed<HeadlessRun>;
#[cfg(test)]
type Backend = moved::Backend<HeadlessRun>;
pub(super) fn adapter(core: std::sync::Weak<Mutex<Core>>) -> Adapter {
    moved::adapter(core)
}

/// The observation and compile ports' host over this launcher's Core (one lock, as before).
impl ChainHost for HeadlessRun {
    type Weak = std::sync::Weak<Mutex<Core>>;
    fn downgrade(&self) -> Self::Weak {
        Arc::downgrade(&self.0)
    }
    fn upgrade(weak: &Self::Weak) -> Option<Self> {
        weak.upgrade().map(HeadlessRun)
    }
    fn try_with<R>(&self, f: impl FnOnce(&mut ChainView<'_, Self>) -> R) -> Option<R> {
        let mut guard = self.0.lock().ok()?;
        let Core {
            authority,
            run,
            closed,
            acquisition,
            bootstrap_lifecycle,
            bootstrap_incarnation,
            bootstrap,
            bootstrap_failure,
            ..
        } = &mut *guard;
        Some(f(&mut ChainView {
            authority,
            run,
            closed: *closed,
            acquisition,
            bootstrap_lifecycle,
            bootstrap_incarnation,
            bootstrap,
            bootstrap_failure,
        }))
    }
    fn exchange_resource_check(&self) -> Option<&'static str> {
        exchange_resource_check(&self.0)
    }
    fn startup_seam(&self) -> OResult<Option<std::net::TcpStream>> {
        startup_test_seam()
    }
}

fn startup_test_seam() -> OResult<Option<std::net::TcpStream>> {
    use std::io::{Read, Write};
    let Some(address) = std::env::var_os("MYCELIUM_L10_TEST_BOOTSTRAP_BEFORE_STAGE") else {
        return Ok(None);
    };
    let invalid = |_| OwnerError("bootstrap_test_seam_failed");
    let address: std::net::SocketAddr = address
        .to_str()
        .ok_or(OwnerError("bootstrap_test_seam_invalid"))?
        .parse()
        .map_err(|_| OwnerError("bootstrap_test_seam_invalid"))?;
    if !address.ip().is_loopback() {
        return Err(OwnerError("bootstrap_test_seam_invalid"));
    }
    let limit = std::time::Duration::from_millis(
        BudgetId::WorkerStartup
            .span(Entries::ZERO)
            .map_err(|_| OwnerError("budget_unavailable"))?
            .millis(),
    );
    let mut stream = std::net::TcpStream::connect_timeout(&address, limit).map_err(invalid)?;
    stream.set_read_timeout(Some(limit)).map_err(invalid)?;
    stream.set_write_timeout(Some(limit)).map_err(invalid)?;
    stream.write_all(b"held").map_err(invalid)?;
    let mut release = [0];
    stream.read_exact(&mut release).map_err(invalid)?;
    if release != [1] {
        return Err(OwnerError("bootstrap_test_seam_invalid"));
    }
    Ok(Some(stream))
}
