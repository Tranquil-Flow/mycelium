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

/// What one failed `accept` means for the listener. v0.1.4 serves through
/// `socketserver.serve_forever`, which swallows accept errors and keeps serving; v0.2 keeps that
/// for the errors a healthy listener can meet and ends only when the listening socket itself is
/// unusable.
#[derive(Debug, PartialEq, Eq)]
pub(super) enum AcceptFailure {
    /// Exhausted descriptors or buffers, a client that reset before the accept, a signal, a
    /// network error on the pending connection: back off one poll and accept again.
    Transient(String),
    /// Anything else (`EBADF`, `EINVAL`, `ENOTSOCK`, ...): the listener has ended.
    Fatal(String),
}

/// `<what>:<ErrorKind>:errno_<n|none>`: the kind alone (`Uncategorized` for EMFILE) cannot tell
/// the causes apart in a field log (REVIEW-laneB15f FU2).
pub(super) fn failure_code(what: &str, error: &std::io::Error) -> String {
    format!(
        "{what}:{:?}:errno_{}",
        error.kind(),
        error
            .raw_os_error()
            .map_or_else(|| "none".to_owned(), |n| n.to_string())
    )
}

pub(super) fn accept_failure(error: &std::io::Error) -> AcceptFailure {
    let errno = error.raw_os_error();
    let code = failure_code("listener_accept_failed", error);
    let transient = matches!(
        errno,
        Some(
            libc::EMFILE
                | libc::ENFILE
                | libc::ENOBUFS
                | libc::ENOMEM
                | libc::ECONNABORTED
                | libc::ECONNRESET
                | libc::EINTR
                | libc::EPROTO
                | libc::ETIMEDOUT
                | libc::ENETDOWN
                | libc::ENETUNREACH
                | libc::EHOSTDOWN
                | libc::EHOSTUNREACH
        )
    );
    if transient {
        AcceptFailure::Transient(code)
    } else {
        AcceptFailure::Fatal(code)
    }
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
    #[test]
    fn exhausted_or_reset_accepts_are_retried_and_a_broken_listener_ends_with_its_errno() {
        for errno in [
            libc::EMFILE,
            libc::ENFILE,
            libc::ENOBUFS,
            libc::ECONNABORTED,
            libc::EINTR,
        ] {
            let error = std::io::Error::from_raw_os_error(errno);
            assert!(
                matches!(accept_failure(&error), AcceptFailure::Transient(_)),
                "errno {errno} ends the listener"
            );
        }
        for errno in [libc::EBADF, libc::EINVAL, libc::ENOTSOCK, libc::EOPNOTSUPP] {
            let error = std::io::Error::from_raw_os_error(errno);
            let AcceptFailure::Fatal(code) = accept_failure(&error) else {
                panic!("errno {errno} is retried forever");
            };
            assert!(code.ends_with(&format!(":errno_{errno}")), "{code}");
        }
        let AcceptFailure::Fatal(code) = accept_failure(&std::io::Error::other("x")) else {
            panic!("a non-OS accept error is retried");
        };
        assert!(code.ends_with(":errno_none"), "{code}");
    }
}
