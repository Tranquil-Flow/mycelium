// SPDX-License-Identifier: AGPL-3.0-only
//! Exact process incarnations for liveness answers.
//!
//! Signal 0 on a bare pid cannot tell the original process from a later process
//! that reused its pid (reads "alive"), and it reports another user's live
//! process as EPERM (read as "gone" by a naive `is_ok`). An [`Incarnation`] is a
//! pid plus the kernel's start time and owner of that one process. Capture it
//! while the caller still owns the unreaped child (the pid cannot be reused
//! before `wait`), then compare: a different start time or owner at that pid is
//! a different process, so the captured one is gone. macOS refuses the query
//! for another user's process (EPERM): that is reported as such, never as "no
//! process", and for a captured incarnation of OUR user it means the pid now
//! belongs to someone else, so ours is gone.
use std::io;

/// One process: pid, kernel start time (µs since the epoch) and real uid.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Incarnation {
    pub pid: u32,
    pub started_us: u64,
    pub uid: u32,
}
/// A liveness answer for one captured incarnation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Liveness {
    /// Exactly this incarnation still runs (not a zombie).
    Alive,
    /// No process holds the pid, it is a zombie, or a different incarnation
    /// (another start time or owner) holds it now.
    Gone,
    /// The kernel refused the query; no claim either way.
    Unknown,
}
impl Incarnation {
    /// The incarnation currently holding `pid`; `Ok(None)` when no live
    /// (non-zombie) process holds it; `PermissionDenied` when another user's
    /// process holds it.
    pub fn of(pid: u32) -> io::Result<Option<Self>> {
        sys::incarnation(pid)
    }
    /// Whether exactly this incarnation is still running.
    pub fn liveness(&self) -> Liveness {
        match Self::of(self.pid) {
            Ok(Some(now)) if now == *self => Liveness::Alive,
            Ok(_) => Liveness::Gone,
            // Our own processes are always readable to us: a refusal means
            // another user's process holds the pid now.
            Err(e)
                if e.kind() == io::ErrorKind::PermissionDenied
                    && self.uid == rustix::process::getuid().as_raw() =>
            {
                Liveness::Gone
            }
            Err(_) => Liveness::Unknown,
        }
    }
}
#[cfg(target_os = "macos")]
mod sys {
    use super::Incarnation;
    use std::io;
    const SZOMB: u32 = 5;
    /// `proc_pidinfo(PROC_PIDTBSDINFO)`: this user's processes only (EPERM for
    /// another user's).
    #[allow(unsafe_code)] // proc_pidinfo(3) into a zeroed struct of exactly the passed size
    pub fn incarnation(pid: u32) -> io::Result<Option<Incarnation>> {
        let raw = i32::try_from(pid)
            .ok()
            .filter(|p| *p > 0)
            .ok_or_else(|| io::Error::from(io::ErrorKind::InvalidInput))?;
        let size = std::mem::size_of::<libc::proc_bsdinfo>();
        let wanted =
            i32::try_from(size).map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))?;
        // SAFETY: `proc_bsdinfo` is plain old data (all-zero is valid), the buffer
        // is exactly `wanted` bytes and proc_pidinfo writes at most that many.
        let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
        let filled = unsafe {
            libc::proc_pidinfo(
                raw,
                libc::PROC_PIDTBSDINFO,
                0,
                (&raw mut info).cast(),
                wanted,
            )
        };
        if filled != wanted {
            let error = io::Error::last_os_error();
            return match error.raw_os_error() {
                Some(libc::ESRCH) => Ok(None),
                _ if filled > 0 => Err(io::Error::from(io::ErrorKind::InvalidData)),
                _ => Err(error),
            };
        }
        if info.pbi_pid != pid || info.pbi_status == SZOMB {
            return Ok(None);
        }
        Ok(Some(Incarnation {
            pid,
            started_us: info
                .pbi_start_tvsec
                .saturating_mul(1_000_000)
                .saturating_add(info.pbi_start_tvusec),
            uid: info.pbi_ruid,
        }))
    }
}
#[cfg(not(target_os = "macos"))]
mod sys {
    use super::Incarnation;
    use std::io;
    pub fn incarnation(_pid: u32) -> io::Result<Option<Incarnation>> {
        Err(io::Error::from(io::ErrorKind::Unsupported))
    }
}
#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::*;
    use std::process::{Command, Stdio};

    #[test]
    fn a_captured_child_is_alive_until_reaped_and_a_reused_pid_reads_gone() {
        let mut child = Command::new("/bin/cat")
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .spawn()
            .unwrap();
        let captured = Incarnation::of(child.id()).unwrap().unwrap();
        assert_eq!(captured.pid, child.id());
        assert_eq!(captured.liveness(), Liveness::Alive);
        // The same pid with another start time is a different process (what a
        // reused pid looks like): the captured incarnation is gone, not alive.
        let earlier = Incarnation {
            started_us: captured.started_us - 1,
            ..captured
        };
        assert_eq!(earlier.liveness(), Liveness::Gone);
        let foreign = Incarnation {
            uid: captured.uid.wrapping_add(1),
            ..captured
        };
        assert_eq!(foreign.liveness(), Liveness::Gone);
        child.kill().unwrap();
        child.wait().unwrap();
        assert_eq!(captured.liveness(), Liveness::Gone);
    }
    #[test]
    fn another_users_live_process_is_never_reported_as_no_process() {
        // launchd (pid 1, root): signal 0 from this user is EPERM.
        assert_eq!(
            rustix::process::test_kill_process(rustix::process::Pid::INIT),
            Err(rustix::io::Errno::PERM)
        );
        let held = Incarnation::of(1).map(|i| i.is_some());
        assert_eq!(
            held.map_err(|e| e.kind()),
            Err(std::io::ErrorKind::PermissionDenied),
            "another user's live process is neither ours nor absent"
        );
    }
    #[test]
    fn our_recorded_pid_now_held_by_another_user_reads_gone() {
        let ours_once = Incarnation {
            pid: 1,
            started_us: 0,
            uid: rustix::process::getuid().as_raw(),
        };
        assert_eq!(ours_once.liveness(), Liveness::Gone);
        // A pid recorded for another user cannot be decided from here.
        let theirs = Incarnation {
            uid: ours_once.uid.wrapping_add(1),
            ..ours_once
        };
        assert_eq!(theirs.liveness(), Liveness::Unknown);
    }
    #[test]
    fn a_pid_nobody_holds_reads_none() {
        let mut gone = Command::new("/usr/bin/true").spawn().unwrap();
        let pid = gone.id();
        gone.wait().unwrap();
        // Reaped: no incarnation, unless the pid was already reused (then a
        // different process with its own start time holds it).
        if let Some(now) = Incarnation::of(pid).unwrap() {
            assert_eq!(now.pid, pid);
        }
        assert!(Incarnation::of(0).is_err());
    }
}
