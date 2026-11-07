// SPDX-License-Identifier: AGPL-3.0-only
//! Shared error alias and the loopback-only guard for the *public HTTP/SSE gateway*
//! sockets of the qualification client. Node control, provisioning, renewal,
//! artifact and activation traffic no longer touch TCP: see `control`/`iroh_frames`.
use std::net::TcpStream;
pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error + Send + Sync>>;
pub fn configure(stream: &TcpStream) -> Result<()> {
    if !stream.peer_addr()?.ip().is_loopback() || !stream.local_addr()?.ip().is_loopback() {
        return Err("loopback_required".into());
    }
    stream.set_nonblocking(false)?;
    stream.set_nodelay(true)?;
    let budget = crate::service::duration(budgets::BudgetId::MemberChosenWall);
    stream.set_read_timeout(Some(budget))?;
    stream.set_write_timeout(Some(budget))?;
    Ok(())
}
