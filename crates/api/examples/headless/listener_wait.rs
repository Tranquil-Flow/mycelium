// SPDX-License-Identifier: AGPL-3.0-only
//! Listener readiness, retaining the existing bounded shutdown observation.
use std::{net::TcpListener, time::Duration};

#[allow(unsafe_code)] // poll(2) borrows one live listener fd for this bounded call
pub(super) fn wait(listener: &TcpListener, limit: Duration) -> std::io::Result<()> {
    use std::os::fd::AsRawFd;
    let mut descriptor = libc::pollfd {
        fd: listener.as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    let timeout = i32::try_from(limit.as_millis()).map_err(|_| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "listener_wait_budget_invalid",
        )
    })?;
    // SAFETY: descriptor points to exactly one initialized pollfd; listener
    // remains alive and borrowed until poll returns. No descriptor is consumed.
    let result = unsafe { libc::poll(&mut descriptor, 1, timeout) };
    if result < 0 {
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(error);
        }
        // A signal is a wakeup too: the caller checks shutdown before waiting.
    } else if descriptor.revents & (libc::POLLERR | libc::POLLHUP | libc::POLLNVAL) != 0 {
        return Err(std::io::Error::other("listener_wait_descriptor_failed"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{net::TcpStream, time::Instant};
    fn limit() -> Duration {
        Duration::from_millis(
            budgets::BudgetId::OwnerPoll
                .span(budgets::Entries::ZERO)
                .unwrap()
                .millis(),
        )
    }
    #[test]
    fn queued_connection_wakes_listener_without_a_fixed_poll_delay() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let client = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
        let started = Instant::now();
        wait(&listener, limit()).unwrap();
        assert!(
            started.elapsed() < limit(),
            "ready listener slept through its entire poll budget"
        );
        let (accepted, _) = listener.accept().unwrap();
        drop((accepted, client));
    }
    #[test]
    fn idle_listener_wait_is_bounded_for_shutdown_observation() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        wait(&listener, limit()).unwrap();
        assert_eq!(
            listener.accept().unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock
        );
    }
}
