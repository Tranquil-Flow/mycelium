// SPDX-License-Identifier: AGPL-3.0-only
//! Child descriptor confinement: the one audited `unsafe` boundary of this crate.
//!
//! v0.1.4 launched every child through Python `subprocess` (`close_fds=True`), so a
//! worker never kept a parent socket or pipe. `std::process::Command` does not give
//! that. macOS has no atomic `SOCK_CLOEXEC`/`pipe2`: std, mio, iroh, SQLite wrappers and
//! the host toolkit mark a descriptor close-on-exec only *after* the creating call
//! returns. A child forked in that gap (or any descriptor a library never marks) is
//! inherited for the child's whole life: a closed listener then stays bound
//! (`AddrInUse` on rebind after rollover/restart), a closed connection never sends FIN,
//! and a guardian's exclusive lifetime pipe would never reach EOF.
//!
//! Contract: a spawned worker or guardian receives **exactly descriptors 0, 1 and 2**,
//! as std redirected them from the `Stdio` settings; nothing else is inheritable and
//! callers cannot widen the set (a caller `pre_exec` registered before this one that dups a
//! descriptor above stderr is re-marked close-on-exec by this closure). The one deliberate
//! widening is [`confine_descriptors_except`] (the v0.1.4 `pass_fds` equivalent): the child
//! receives exactly stdio plus the listed descriptors, each verified open before the spawn,
//! with close-on-exec cleared in the child only.
//!
//! `POSIX_SPAWN_CLOEXEC_DEFAULT` was weighed and rejected: `Command` exposes no
//! getter for `env_clear` or a registered `pre_exec`, so rebuilding it as a
//! `posix_spawn` call would silently drop caller intent, and the kernel still keeps the
//! child's copy until the exec point of no return, exactly the interval below.
//!
//! The closure runs in the forked child right before `exec` and *marks* every open
//! descriptor above stderr close-on-exec rather than closing it. Closing could break std:
//! its exec-error pipe is already close-on-exec, and closing it would report a failed
//! `exec` as a successful spawn. Descriptors already close-on-exec are not touched.
//!
//! Which descriptors are open is asked of the kernel, not found by probing every number up
//! to `RLIMIT_NOFILE` (round 10; the probe cost ~1 s per spawn at the macOS default limit of
//! 1,048,575). That is what CPython's `close_fds=True` does on macOS: `_posixsubprocess`
//! lists `/dev/fd` and closes only what is listed, falling back to a brute-force loop when
//! the directory cannot be read (executed oracle behaviour:
//! `L6-evidence/round10-oracle-spawn-probe.out`). Here the list is `proc_pidinfo
//! (PROC_PIDLISTFDS)` on our own pid, a single system call into a stack buffer (no
//! `opendir` allocation, no descriptor consumed, async-signal-safe), taken in the child right
//! after `fork`: it is the table the child will exec with, so a descriptor the parent
//! creates or closes afterwards cannot be missing from it. Numbers below `DENSE_SWEEP` are
//! additionally probed one by one (a backstop against a defective list), and if the list is
//! unavailable or may be truncated the old probe runs over the rest of `RLIMIT_NOFILE`:
//! fail closed, never weaker than before.
//!
//! Residual window, not eliminable by any in-process mechanism: a descriptor that another
//! parent thread creates, closes and drops between `fork` and `exec` is still held by the
//! not-yet-exec'd child for that interval. The interval is now a few hundred `fcntl`
//! calls, one system call and `exec` (measured ~1.8 ms instead of ~130 ms to ~1 s at limit
//! 1,048,575). Callers that rebind a just-closed address must still order it after the
//! spawn. Unlike the old probe, a descriptor above a limit lowered after it was opened is
//! covered (it is in the kernel's list).
//!
//! The other direction: [`adopt_inherited_descriptor`] turns a descriptor *this* process
//! inherited (L9 sidecar `--bootstrap-fd N`) into an `OwnedFd`, failing closed. It is the
//! only place a raw inherited descriptor number becomes owned (the `runtime` crate forbids
//! `unsafe`).
#![allow(unsafe_code)]
use rustix::{
    fs::{FileType, fstat},
    io::{Errno, FdFlags, fcntl_getfd, fcntl_setfd},
    process::{Resource, getpid, getrlimit},
};
use std::{
    collections::BTreeSet,
    io,
    os::fd::{BorrowedFd, FromRawFd, OwnedFd, RawFd},
    os::unix::process::CommandExt,
    process::Command,
    sync::Mutex,
};
/// First descriptor that is not stdin/stdout/stderr.
const FIRST_CONFINED: i32 = 3;
/// Words of the child's stack buffer for the open-descriptor list. The platform listing
/// needs two words per descriptor, so it holds `LIST_SLOTS / 2` descriptors.
const LIST_SLOTS: usize = 8192;
/// Descriptor numbers below this are always probed one by one (see `confine_range`): about
/// a quarter-thousand `F_GETFD` calls, microseconds, whatever the limit is.
const DENSE_SWEEP: i32 = 256;
/// The two descriptor operations the child scan needs; a seam so failure is injectable.
trait Descriptors {
    fn flags(&mut self, fd: i32) -> Result<FdFlags, Errno>;
    fn set_flags(&mut self, fd: i32, flags: FdFlags) -> Result<(), Errno>;
    /// Write the number of every descriptor this process has open into `out` and return
    /// how many (any order). `None` is "cannot tell": unsupported, failed, or `out` too
    /// small (a full buffer may be truncated, so it is never trusted).
    fn open_descriptors(&mut self, _out: &mut [i32]) -> Option<usize> {
        None
    }
}
struct Process;
impl Descriptors for Process {
    fn flags(&mut self, fd: i32) -> Result<FdFlags, Errno> {
        // SAFETY: the borrow only spans this `fcntl(F_GETFD)`, defined for any integer
        // descriptor (EBADF when it is not open).
        fcntl_getfd(unsafe { BorrowedFd::borrow_raw(fd) })
    }
    fn set_flags(&mut self, fd: i32, flags: FdFlags) -> Result<(), Errno> {
        // SAFETY: as above, for `fcntl(F_SETFD)`; nothing is closed or owned through it.
        fcntl_setfd(unsafe { BorrowedFd::borrow_raw(fd) }, flags)
    }
    fn open_descriptors(&mut self, out: &mut [i32]) -> Option<usize> {
        list_open_descriptors(out)
    }
}
/// macOS `proc_pidinfo(PROC_PIDLISTFDS)`: the kernel's descriptor table for one process as
/// `struct proc_fdinfo { int32_t proc_fd; uint32_t proc_fdtype; }` records. A plain
/// system call behind a libSystem wrapper (`libproc`): no allocation, no lock, no
/// descriptor consumed, so it is usable between `fork` and `exec`. The same table
/// `/dev/fd` (the directory CPython's `close_fds` walks) is generated from.
#[cfg(target_vendor = "apple")]
mod proc_info {
    use std::ffi::{c_int, c_void};
    unsafe extern "C" {
        pub fn proc_pidinfo(
            pid: c_int,
            flavor: c_int,
            arg: u64,
            buffer: *mut c_void,
            buffersize: c_int,
        ) -> c_int;
    }
    pub const PROC_PIDLISTFDS: c_int = 1;
    #[repr(C)]
    #[derive(Clone, Copy)]
    pub struct ProcFdInfo {
        pub fd: i32,
        pub kind: u32,
    }
    /// Words (`i32`) per record; the compaction below reads and writes `i32` slots.
    pub const WORDS: usize = size_of::<ProcFdInfo>() / size_of::<i32>();
    const _: () = assert!(size_of::<ProcFdInfo>() == 8 && align_of::<ProcFdInfo>() == 4);
}
/// List this process's open descriptors into the front of `out`; `out.len() / WORDS`
/// records fit. `None` unless the list is complete: an error, zero records (a live process
/// always has the descriptors std needs), a size that is not whole records, or a buffer
/// filled to the brim (the kernel truncates silently, so full means "maybe more").
/// Async-signal-safe: one system call into the caller's buffer.
#[cfg(target_vendor = "apple")]
fn list_open_descriptors(out: &mut [i32]) -> Option<usize> {
    use proc_info::{PROC_PIDLISTFDS, WORDS, proc_pidinfo};
    let capacity = out.len() / WORDS;
    let bytes = i32::try_from(capacity * size_of::<i32>() * WORDS).ok()?;
    // SAFETY: `out` is valid for writes of `capacity * 8` bytes (it holds at least
    // `capacity * WORDS` `i32`s) and `ProcFdInfo` has `i32` alignment; `proc_pidinfo`
    // only writes whole records into it and reads no other memory. Our own pid is always
    // inspectable; a refusal is an error return, never a crash.
    let filled = unsafe {
        proc_pidinfo(
            getpid().as_raw_nonzero().get(),
            PROC_PIDLISTFDS,
            0,
            out.as_mut_ptr().cast(),
            bytes,
        )
    };
    let filled = usize::try_from(filled).ok()?;
    let records = filled / (size_of::<i32>() * WORDS);
    if filled % (size_of::<i32>() * WORDS) != 0 || records == 0 || records >= capacity {
        return None;
    }
    // Compact the descriptor numbers to the front, in place. Record `i` starts at word
    // `i * WORDS` >= `i`, so a forward copy never overwrites a number not yet read.
    for index in 0..records {
        out[index] = out[index * WORDS];
    }
    Some(records)
}
/// No kernel listing is wired up on this platform: callers fall back to the bounded scan.
#[cfg(not(target_vendor = "apple"))]
fn list_open_descriptors(_out: &mut [i32]) -> Option<usize> {
    None
}
/// Mark one descriptor close-on-exec if it is open and not already. "Not open" is fine; any
/// other failure is returned so the spawn is refused: a descriptor that could not be
/// marked might still reach the child.
fn confine_one(ops: &mut impl Descriptors, fd: i32) -> io::Result<()> {
    match ops.flags(fd) {
        Ok(flags) if flags.contains(FdFlags::CLOEXEC) => Ok(()),
        Ok(flags) => Ok(ops.set_flags(fd, flags | FdFlags::CLOEXEC)?),
        Err(Errno::BADF) => Ok(()),
        Err(error) => Err(error.into()),
    }
}
/// Mark every open descriptor at or above `FIRST_CONFINED` close-on-exec, except `keep`,
/// then make each `keep` descriptor inheritable. A listed descriptor that is no longer open
/// refuses the spawn.
///
/// Cost is bounded by the open descriptors, not by `limit`, as in CPython's
/// `close_fds`, which walks `/dev/fd`: (1) a sweep of the dense range
/// `FIRST_CONFINED..DENSE_SWEEP` by `F_GETFD`, where nearly every real descriptor lives (the
/// kernel hands out the lowest free number) and which also backstops a defective list;
/// (2) the kernel's own list of open descriptors, taken in the child right after `fork`, so
/// it is exactly the table the child will exec with and nothing opened concurrently in the
/// parent after the fork can be missing from it; the numbers it holds at or above the dense
/// range are marked; (3) if the list is unavailable or possibly truncated, the old
/// `DENSE_SWEEP..limit` scan, so the result is never weaker than before.
fn confine_range(ops: &mut impl Descriptors, limit: i32, keep: &[i32]) -> io::Result<()> {
    let dense_end = limit.min(DENSE_SWEEP);
    for fd in FIRST_CONFINED..dense_end {
        if !keep.contains(&fd) {
            confine_one(ops, fd)?;
        }
    }
    let mut listing = [0_i32; LIST_SLOTS];
    match ops.open_descriptors(&mut listing) {
        Some(count) => {
            for &fd in &listing[..count] {
                if fd >= dense_end && !keep.contains(&fd) {
                    confine_one(ops, fd)?;
                }
            }
        }
        None => {
            for fd in dense_end..limit {
                if !keep.contains(&fd) {
                    confine_one(ops, fd)?;
                }
            }
        }
    }
    for fd in keep {
        release(ops, *fd)?;
    }
    Ok(())
}
/// Child side: clear close-on-exec on one listed descriptor and prove it stuck. It must be
/// open (it was when the list was accepted; closing it since is a failure, never a skip).
/// No allocation: this runs between `fork` and `exec`.
fn release(ops: &mut impl Descriptors, fd: i32) -> io::Result<()> {
    let flags = ops.flags(fd)?;
    if flags.contains(FdFlags::CLOEXEC) {
        ops.set_flags(fd, flags - FdFlags::CLOEXEC)?;
        if ops.flags(fd)?.contains(FdFlags::CLOEXEC) {
            return Err(Errno::IO.into());
        }
    }
    Ok(())
}
/// Parent side: every listed descriptor must be a real, distinct, open descriptor above
/// stderr. Only reads descriptor state; nothing is changed.
fn check_keep(ops: &mut impl Descriptors, keep: &[i32]) -> Result<(), PassRefusal> {
    for (index, fd) in keep.iter().enumerate() {
        if *fd < 0 {
            return Err(PassRefusal::Negative);
        }
        if *fd < FIRST_CONFINED {
            return Err(PassRefusal::Reserved);
        }
        match ops.flags(*fd) {
            Ok(_) => {}
            Err(Errno::BADF) => return Err(PassRefusal::Closed),
            Err(_) => return Err(PassRefusal::StateUnavailable),
        }
        if keep[..index].contains(fd) {
            return Err(PassRefusal::Duplicate);
        }
    }
    Ok(())
}
/// Make `command` leave every inherited descriptor above stderr behind at exec.
///
/// Fails closed: if the descriptor limit is unavailable, infinite or not representable
/// (it bounds the fallback scan used when the kernel's descriptor list cannot be had)
/// the command is left untouched and an error is returned, so a caller never believes an
/// unconfined spawn is confined. Call it before creating any resource the spawn owns, and
/// after registering any caller `pre_exec` (closures run in registration order; this one
/// must run last to re-mark a descriptor such a closure duplicated).
pub fn confine_descriptors(command: &mut Command) -> io::Result<()> {
    confine_descriptors_except(command, &[])
}
/// [`confine_descriptors`] with a keep-list: the v0.1.4 `subprocess.Popen(close_fds=True,
/// pass_fds=...)` equivalent. In the child every descriptor above stderr is made
/// close-on-exec **except exactly `keep`**, whose close-on-exec flag is cleared *in the
/// child only*, between `fork` and `exec` (async-signal-safe `fcntl` calls; the parent's
/// flags are never touched, so a concurrently spawned sibling cannot inherit them).
///
/// Refused before anything is registered or spawned ([`pass_refusal`] gives the reason):
/// 0-2 (stdio comes from the `Stdio` settings), a negative number, a descriptor that is not
/// open, a number listed twice. If a listed descriptor is closed after this call but before
/// the fork, the child fails and `spawn` returns `EBADF`; the program is never exec'd
/// without it. An empty list is exactly [`confine_descriptors`].
///
/// Residual, not eliminable: a kept descriptor is inheritable by the child *and* the same
/// number must be given to it out of band (argv/env) by the caller; the keep-list does not
/// duplicate or renumber. Through the guardian the descriptor is also held by the guardian
/// for its lifetime (see `PersistentChild::spawn_passing`). A listed descriptor that its
/// owner closes between this call and the fork *and* whose number std's own spawn
/// plumbing then reuses cannot be told apart from the original: the owner must keep it
/// open until `spawn` returns.
pub fn confine_descriptors_except(command: &mut Command, keep: &[RawFd]) -> io::Result<()> {
    let limit = getrlimit(Resource::Nofile)
        .current
        .and_then(|limit| i32::try_from(limit).ok())
        .filter(|limit| *limit > FIRST_CONFINED)
        .ok_or_else(|| io::Error::other("descriptor_limit_unavailable"))?;
    check_keep(&mut Process, keep).map_err(pass_refused)?;
    let keep = keep.to_vec();
    // SAFETY: the closure runs in the forked child before exec. It only calls
    // `fcntl(F_GETFD/F_SETFD)`, `getpid` and `proc_pidinfo` (one system call into a stack
    // buffer) through thin libc wrappers, which allocate nothing and take no lock, so it
    // is async-signal-safe. `limit` is a plain integer and
    // `keep` a `Vec` moved into the closure and only read (no allocation, no drop in the
    // child); no Rust-owned descriptor is closed or dropped. A failure makes `exec` not
    // happen: std reports the spawn as failed.
    unsafe { command.pre_exec(move || confine_range(&mut Process, limit, &keep)) };
    Ok(())
}
/// Why `confine_descriptors_except` refused a keep-list. Nothing is registered on the
/// command and nothing is spawned.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PassRefusal {
    /// Negative number.
    Negative,
    /// 0, 1 or 2: stdio is set by the `Stdio` settings, never by a keep-list.
    Reserved,
    /// Not open in this process.
    Closed,
    /// The descriptor state could not be read; unknown means refused.
    StateUnavailable,
    /// The same number is listed twice.
    Duplicate,
}
impl PassRefusal {
    /// Stable machine-readable reason.
    pub const fn code(self) -> &'static str {
        match self {
            Self::Negative => "pass_descriptor_negative",
            Self::Reserved => "pass_descriptor_reserved",
            Self::Closed => "pass_descriptor_closed",
            Self::StateUnavailable => "pass_descriptor_state_unavailable",
            Self::Duplicate => "pass_descriptor_duplicate",
        }
    }
}
impl std::fmt::Display for PassRefusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.code())
    }
}
impl std::error::Error for PassRefusal {}
/// The refusal carried by an error from [`confine_descriptors_except`], if it is one.
pub fn pass_refusal(error: &io::Error) -> Option<PassRefusal> {
    error.get_ref()?.downcast_ref::<PassRefusal>().copied()
}
fn pass_refused(why: PassRefusal) -> io::Error {
    let kind = match why {
        PassRefusal::StateUnavailable => io::ErrorKind::Other,
        _ => io::ErrorKind::InvalidInput,
    };
    io::Error::new(kind, why)
}
/// What an adopted descriptor must be. L9's sidecar bootstrap secret travels in a
/// [`DescriptorKind::Fifo`]; the others exist so a caller never has to guess "anything".
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DescriptorKind {
    Fifo,
    Socket,
    Regular,
}
/// Why `adopt_inherited_descriptor` refused. Every refusal leaves the descriptor open
/// and unowned by this process (the caller exits non-zero).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdoptionRefusal {
    /// Negative number.
    Negative,
    /// 0, 1 or 2: the process's own stdio is never an adoptable inherited descriptor.
    ///
    /// **V03-L6-1 (stricter than v0.1.4).** v0.1.4 adopts such a number blindly, but its
    /// parent only produces one with its own stdin closed: `physical_inference_node.py`
    /// (f4e366e9, 1932-1942) passes `os.pipe()`'s read end via `pass_fds` while forcing the
    /// child's 0/1/2 to DEVNULL/PIPE/PIPE; `os.pipe()` is >= 3 whenever stdio is open, and
    /// with stdin closed it is 0 and the child reads 0 bytes (its fd 0 is DEVNULL). So 0-2
    /// refuses only a state in which v0.1.4 never delivers the secret
    /// (`L6-evidence/round9-V03-L6-1-oracle-proof.txt`).
    Reserved,
    /// Not open in this process (v0.1.4 aborts with exit 134 here).
    Closed,
    /// The descriptor state could not be read; unknown means refused.
    StateUnavailable,
    /// Open, but not of the expected kind.
    WrongKind,
    /// This process already adopted (and so may own) this number.
    AlreadyAdopted,
    /// Close-on-exec could not be set (or did not stick).
    CloexecFailed,
}
impl AdoptionRefusal {
    /// Stable machine-readable reason.
    pub const fn code(self) -> &'static str {
        match self {
            Self::Negative => "descriptor_negative",
            Self::Reserved => "descriptor_reserved",
            Self::Closed => "descriptor_closed",
            Self::StateUnavailable => "descriptor_state_unavailable",
            Self::WrongKind => "descriptor_wrong_kind",
            Self::AlreadyAdopted => "descriptor_already_adopted",
            Self::CloexecFailed => "descriptor_cloexec_failed",
        }
    }
    /// The v0.1.4 sidecar error code (`SidecarError::code`, sidecar.rs:204-220) at the same
    /// point: a wrong kind is `bootstrap_not_pipe`; every other refusal is
    /// `invalid_bootstrap_fd` (v0.1.4 gave that for a negative number and aborted, exit
    /// 134, for a closed one).
    pub const fn sidecar_code(self) -> &'static str {
        match self {
            Self::WrongKind => "bootstrap_not_pipe",
            _ => "invalid_bootstrap_fd",
        }
    }
}
impl std::fmt::Display for AdoptionRefusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.code())
    }
}
impl std::error::Error for AdoptionRefusal {}
/// The refusal carried by an error from [`adopt_inherited_descriptor`], if it is one.
pub fn refusal(error: &io::Error) -> Option<AdoptionRefusal> {
    error.get_ref()?.downcast_ref::<AdoptionRefusal>().copied()
}
fn refused(why: AdoptionRefusal) -> io::Error {
    let kind = match why {
        AdoptionRefusal::AlreadyAdopted => io::ErrorKind::AlreadyExists,
        AdoptionRefusal::StateUnavailable | AdoptionRefusal::CloexecFailed => io::ErrorKind::Other,
        _ => io::ErrorKind::InvalidInput,
    };
    io::Error::new(kind, why)
}
/// The extra operation adoption needs beyond [`Descriptors`]: the file kind (`fstat`).
trait Inherited: Descriptors {
    /// `Ok(None)` is an open descriptor of a kind adoption never accepts.
    fn kind(&mut self, fd: i32) -> Result<Option<DescriptorKind>, Errno>;
}
impl Inherited for Process {
    fn kind(&mut self, fd: i32) -> Result<Option<DescriptorKind>, Errno> {
        // SAFETY: the borrow only spans this `fstat`, defined for any integer descriptor.
        let stat = fstat(unsafe { BorrowedFd::borrow_raw(fd) })?;
        Ok(match FileType::from_raw_mode(stat.st_mode) {
            FileType::Fifo => Some(DescriptorKind::Fifo),
            FileType::Socket => Some(DescriptorKind::Socket),
            FileType::RegularFile => Some(DescriptorKind::Regular),
            _ => None,
        })
    }
}
/// Numbers this process has adopted. Never released: a number that was adopted and closed
/// may since have been reused for something that is not an inherited descriptor.
static ADOPTED: Mutex<BTreeSet<i32>> = Mutex::new(BTreeSet::new());
/// Validate and claim `fd`, leaving it close-on-exec; the descriptor is not wrapped here.
/// Order matters: nothing is changed (no flag, no claim) until the descriptor is known to
/// be open and of the expected kind.
fn adopt_checked(
    ops: &mut impl Inherited,
    claimed: &Mutex<BTreeSet<i32>>,
    fd: i32,
    expected: DescriptorKind,
) -> Result<(), AdoptionRefusal> {
    if fd < 0 {
        return Err(AdoptionRefusal::Negative);
    }
    // V03-L6-1: stricter than v0.1.4 (see `AdoptionRefusal::Reserved`).
    if fd < FIRST_CONFINED {
        return Err(AdoptionRefusal::Reserved);
    }
    let unknown = |error: Errno| match error {
        Errno::BADF => AdoptionRefusal::Closed,
        _ => AdoptionRefusal::StateUnavailable,
    };
    let flags = ops.flags(fd).map_err(unknown)?;
    if ops.kind(fd).map_err(unknown)? != Some(expected) {
        return Err(AdoptionRefusal::WrongKind);
    }
    if !claimed
        .lock()
        .map_err(|_| AdoptionRefusal::StateUnavailable)?
        .insert(fd)
    {
        return Err(AdoptionRefusal::AlreadyAdopted);
    }
    if !flags.contains(FdFlags::CLOEXEC) {
        ops.set_flags(fd, flags | FdFlags::CLOEXEC)
            .map_err(|_| AdoptionRefusal::CloexecFailed)?;
    }
    match ops.flags(fd) {
        Ok(after) if after.contains(FdFlags::CLOEXEC) => Ok(()),
        _ => Err(AdoptionRefusal::CloexecFailed),
    }
}

/// Take ownership of a descriptor this process inherited at exec (L9's sidecar
/// `--bootstrap-fd N`). Fails closed; see [`AdoptionRefusal`]:
///
/// * 0-2 and negative numbers are refused; a closed number is refused (not aborted);
/// * the kind must be `expected` (checked before anything is changed);
/// * the descriptor is made close-on-exec so no later child inherits it;
/// * a number is adopted at most once per process, ever.
///
/// Call it during start-up before other threads open or close descriptors: the number is
/// checked and then wrapped, and a concurrent close in between cannot be detected.
pub fn adopt_inherited_descriptor(fd: RawFd, expected: DescriptorKind) -> io::Result<OwnedFd> {
    adopt_checked(&mut Process, &ADOPTED, fd, expected).map_err(refused)?;
    // SAFETY: `fd` was just proven open (`F_GETFD`) and of the expected kind; the claim
    // above is taken at most once per number, so no earlier call to this function holds
    // it; the caller contract is that nothing else in the process wraps it.
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    #[derive(Clone, Debug, Default, PartialEq)]
    enum Listing {
        /// The kernel list is exactly the table.
        #[default]
        Table,
        /// The enumerator under-reports: these open descriptors are missing from it.
        Omits(Vec<i32>),
        /// Truncated, failed or unsupported: "cannot tell".
        Unavailable,
    }
    #[derive(Default)]
    struct Fake {
        listing: Listing,
        flag_calls: usize,
        list_calls: usize,
        table: BTreeMap<i32, FdFlags>,
        fail_get: Option<(i32, Errno)>,
        fail_set: Option<(i32, Errno)>,
        marked: Vec<i32>,
        kinds: BTreeMap<i32, Option<DescriptorKind>>,
        kind_calls: Vec<i32>,
        fail_kind: Option<(i32, Errno)>,
        ignore_set: bool,
    }
    impl Descriptors for Fake {
        fn flags(&mut self, fd: i32) -> Result<FdFlags, Errno> {
            self.flag_calls += 1;
            match self.fail_get {
                Some((at, e)) if at == fd => Err(e),
                _ => self.table.get(&fd).copied().ok_or(Errno::BADF),
            }
        }
        fn set_flags(&mut self, fd: i32, flags: FdFlags) -> Result<(), Errno> {
            match self.fail_set {
                Some((at, e)) if at == fd => Err(e),
                _ => {
                    if !self.ignore_set {
                        self.table.insert(fd, flags);
                    }
                    self.marked.push(fd);
                    Ok(())
                }
            }
        }
        fn open_descriptors(&mut self, out: &mut [i32]) -> Option<usize> {
            self.list_calls += 1;
            let omitted = match &self.listing {
                Listing::Unavailable => return None,
                Listing::Omits(fds) => fds.clone(),
                Listing::Table => Vec::new(),
            };
            let open: Vec<i32> = self
                .table
                .keys()
                .copied()
                .filter(|fd| !omitted.contains(fd))
                .collect();
            if open.len() >= out.len() / 2 {
                return None; // would fill the buffer: possibly truncated
            }
            out[..open.len()].copy_from_slice(&open);
            Some(open.len())
        }
    }
    fn fake() -> Fake {
        let mut f = Fake::default();
        for fd in 0..=2 {
            f.table.insert(fd, FdFlags::empty());
        }
        f.table.insert(3, FdFlags::empty());
        f.table.insert(5, FdFlags::CLOEXEC);
        f.table.insert(7, FdFlags::empty());
        f
    }
    #[test]
    fn marks_only_open_inheritable_descriptors_above_stderr() {
        let mut f = fake();
        confine_range(&mut f, 16, &[]).unwrap();
        assert_eq!(f.marked, [3, 7]);
        assert!(
            f.table
                .values()
                .skip(3)
                .all(|v| v.contains(FdFlags::CLOEXEC))
        );
        assert_eq!(f.table[&0], FdFlags::empty(), "stdio is never touched");
    }
    #[test]
    fn failure_to_mark_a_descriptor_refuses_the_spawn() {
        let mut f = fake();
        f.fail_set = Some((7, Errno::PERM));
        let error = confine_range(&mut f, 16, &[]).expect_err("unmarked descriptor must refuse");
        assert_eq!(error.raw_os_error(), Some(Errno::PERM.raw_os_error()));
    }
    #[test]
    fn unreadable_descriptor_flags_refuse_the_spawn_but_closed_ones_do_not() {
        let mut f = fake();
        f.fail_get = Some((6, Errno::IO));
        let error = confine_range(&mut f, 16, &[]).expect_err("unknown state must refuse");
        assert_eq!(error.raw_os_error(), Some(Errno::IO.raw_os_error()));
        let mut closed = fake();
        closed.fail_get = Some((6, Errno::BADF));
        confine_range(&mut closed, 16, &[]).unwrap();
    }
    // ---- enumeration (round 10: do not pay the RLIMIT_NOFILE scan) ---------------------
    /// The limit a default macOS shell reports; the scan this round removes costs ~1 s at it.
    const HUGE_LIMIT: i32 = 1_048_575;
    /// An independent bound (not derived from the implementation): a few hundred calls at
    /// most is "cheap", a million is "the scan".
    const CHEAP_CALLS: usize = 1_000;
    fn sparse() -> Fake {
        let mut f = fake(); // 0-2 stdio, 3 inheritable, 5 cloexec, 7 inheritable
        f.table.insert(40, FdFlags::empty());
        // Either side of the end of the dense sweep: one must be probed, one listed.
        f.table.insert(DENSE_SWEEP - 1, FdFlags::empty());
        f.table.insert(DENSE_SWEEP, FdFlags::empty());
        f.table.insert(5000, FdFlags::empty());
        f.table.insert(70_000, FdFlags::CLOEXEC);
        f.table.insert(70_001, FdFlags::empty());
        f
    }
    #[test]
    fn an_enumerable_descriptor_table_is_confined_without_scanning_the_limit() {
        let mut f = sparse();
        confine_range(&mut f, HUGE_LIMIT, &[]).unwrap();
        assert!(
            f.flag_calls < CHEAP_CALLS,
            "{} F_GETFD calls for 10 open descriptors at limit {HUGE_LIMIT}",
            f.flag_calls
        );
        assert_eq!(f.list_calls, 1, "the table is listed exactly once");
        for (fd, flags) in &f.table {
            if *fd >= 3 {
                assert!(flags.contains(FdFlags::CLOEXEC), "{fd} left inheritable");
            }
        }
        let mut marked = f.marked.clone();
        marked.sort_unstable();
        assert_eq!(
            marked,
            [3, 7, 40, DENSE_SWEEP - 1, DENSE_SWEEP, 5000, 70_001],
            "exactly the inheritable ones, once each"
        );
        assert_eq!(f.table[&0], FdFlags::empty(), "stdio untouched");
    }
    #[test]
    fn descriptors_above_a_lowered_limit_are_still_confined_when_listed() {
        // Stricter than the scan: a limit lowered after the descriptor was opened no
        // longer hides it from the child.
        let mut f = sparse();
        confine_range(&mut f, 16, &[]).unwrap();
        for fd in [3, 7, 40, DENSE_SWEEP - 1, DENSE_SWEEP, 5000, 70_001] {
            assert!(f.table[&fd].contains(FdFlags::CLOEXEC), "{fd}");
        }
    }
    #[test]
    fn an_enumerator_that_misses_a_low_descriptor_is_backstopped_by_the_dense_sweep() {
        let mut f = sparse();
        f.listing = Listing::Omits(vec![3, 7]);
        confine_range(&mut f, HUGE_LIMIT, &[]).unwrap();
        assert!(f.table[&3].contains(FdFlags::CLOEXEC));
        assert!(f.table[&7].contains(FdFlags::CLOEXEC));
        assert!(f.table[&5000].contains(FdFlags::CLOEXEC), "listed, high");
    }
    #[test]
    fn an_unavailable_listing_falls_back_to_the_full_bounded_scan_and_still_confines() {
        let mut f = sparse();
        f.listing = Listing::Unavailable;
        confine_range(&mut f, 80_000, &[]).unwrap();
        assert_eq!(f.list_calls, 1);
        assert!(
            f.flag_calls >= 80_000 - 3,
            "fail closed: every number below the limit is probed, got {}",
            f.flag_calls
        );
        for fd in [3, 7, 40, DENSE_SWEEP - 1, DENSE_SWEEP, 5000, 70_001] {
            assert!(f.table[&fd].contains(FdFlags::CLOEXEC), "{fd}");
        }
    }
    #[test]
    fn a_listing_that_fills_the_buffer_is_not_trusted() {
        // Exactly as many open descriptors as the buffer holds: cannot be told apart from
        // a truncated list, so the scan runs.
        let mut f = Fake::default();
        let capacity = LIST_SLOTS / 2;
        for fd in 0..i32::try_from(capacity - 1).unwrap() {
            f.table.insert(fd, FdFlags::empty());
        }
        f.table.insert(5000, FdFlags::empty());
        assert_eq!(f.table.len(), capacity);
        confine_range(&mut f, 6000, &[]).unwrap();
        assert!(f.table[&5000].contains(FdFlags::CLOEXEC));
        assert!(f.flag_calls >= 6000 - 3, "scan ran: {}", f.flag_calls);
    }
    #[test]
    fn the_keep_list_works_with_an_enumerated_table() {
        let mut f = sparse();
        confine_range(&mut f, HUGE_LIMIT, &[5000, 70_000, 7]).unwrap();
        assert_eq!(f.table[&5000], FdFlags::empty(), "listed high, kept");
        assert_eq!(
            f.table[&70_000],
            FdFlags::empty(),
            "listed cloexec, released"
        );
        assert_eq!(f.table[&7], FdFlags::empty(), "listed dense, kept");
        assert!(f.table[&70_001].contains(FdFlags::CLOEXEC), "high decoy");
        assert!(f.table[&40].contains(FdFlags::CLOEXEC), "mid decoy");
        assert!(f.table[&3].contains(FdFlags::CLOEXEC), "low decoy");
        assert!(f.flag_calls < CHEAP_CALLS, "{}", f.flag_calls);
    }
    #[test]
    fn enumerated_marking_failures_still_refuse_the_spawn() {
        let mut f = sparse();
        f.fail_set = Some((70_001, Errno::PERM));
        let error = confine_range(&mut f, HUGE_LIMIT, &[]).expect_err("high mark failed");
        assert_eq!(error.raw_os_error(), Some(Errno::PERM.raw_os_error()));
        let mut g = sparse();
        g.fail_get = Some((5000, Errno::IO));
        let error = confine_range(&mut g, HUGE_LIMIT, &[]).expect_err("high state unknown");
        assert_eq!(error.raw_os_error(), Some(Errno::IO.raw_os_error()));
        let mut closed = sparse();
        closed.fail_get = Some((5000, Errno::BADF)); // closed meanwhile: nothing to confine
        confine_range(&mut closed, HUGE_LIMIT, &[]).unwrap();
    }
    // ---- the real enumerator against independent enumerators (macOS) ----------------------
    /// Fixture side of `the_real_enumeration_equals_the_fcntl_scan_and_dev_fd`. It runs in a
    /// fresh single-threaded process so no other test can open or close a descriptor between
    /// the three enumerations. Prints one verdict line.
    #[cfg(target_vendor = "apple")]
    #[test]
    fn enumeration_fixture() {
        use rustix::{
            io::{fcntl_dupfd_cloexec, fcntl_setfd},
            process::{Resource, Rlimit, getrlimit, setrlimit},
        };
        let Some(wanted) = std::env::var("L6_ENUM_LIMIT")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
        else {
            return;
        };
        let hard = getrlimit(Resource::Nofile).maximum;
        let soft = hard.map_or(wanted, |h| h.min(wanted));
        setrlimit(
            Resource::Nofile,
            Rlimit {
                current: Some(soft),
                maximum: hard,
            },
        )
        .unwrap();
        let limit = i32::try_from(soft).unwrap();
        // Diverse kinds, both flag states, and numbers far above the dense range.
        let mut keep: Vec<std::os::fd::OwnedFd> = Vec::new();
        let (r, w) = std::io::pipe().unwrap();
        keep.push(r.into());
        keep.push(w.into());
        let (a, b) = std::os::unix::net::UnixStream::pair().unwrap();
        keep.push(a.into());
        keep.push(b.into());
        keep.push(std::net::UdpSocket::bind("127.0.0.1:0").unwrap().into());
        keep.push(std::net::TcpListener::bind("127.0.0.1:0").unwrap().into());
        keep.push(std::fs::File::open("/dev/null").unwrap().into());
        keep.push(std::fs::File::open(".").unwrap().into());
        let plain = std::fs::File::open("/dev/zero").unwrap();
        fcntl_setfd(&plain, FdFlags::empty()).unwrap();
        keep.push(plain.into());
        for high in [300, 1000, 3000] {
            if high < limit {
                let moved = fcntl_dupfd_cloexec(&keep[0], high).unwrap();
                keep.push(moved);
            }
        }
        let mut buffer = [0_i32; LIST_SLOTS];
        let count = Process
            .open_descriptors(&mut buffer)
            .expect("the kernel listing must be available and fit");
        let mut listed = buffer[..count].to_vec();
        listed.sort_unstable();
        // A buffer that cannot hold the whole table, or holds it exactly, is never trusted
        // (the kernel truncates silently); one record of headroom is.
        let mut small = [0_i32; 8];
        let mut exact = vec![0_i32; count * 2];
        let mut roomy = vec![0_i32; (count + 1) * 2];
        let truncation_handled = count > small.len() / 2
            && list_open_descriptors(&mut small).is_none()
            && list_open_descriptors(&mut exact).is_none()
            && list_open_descriptors(&mut roomy) == Some(count)
            && {
                let mut again = roomy[..count].to_vec();
                again.sort_unstable();
                again == listed
            };
        let in_dev_fd = {
            let mut names: Vec<i32> = std::fs::read_dir("/dev/fd")
                .unwrap()
                .map(|e| e.unwrap().file_name().to_str().unwrap().parse().unwrap())
                .collect();
            names.sort_unstable();
            names
        };
        let scanned: Vec<i32> = (0..limit).filter(|fd| Process.flags(*fd).is_ok()).collect();
        let only_in_dev_fd: Vec<i32> = in_dev_fd
            .iter()
            .copied()
            .filter(|fd| !scanned.contains(fd))
            .collect();
        let verdict = if truncation_handled
            && listed == scanned
            && scanned.iter().all(|fd| in_dev_fd.contains(fd))
            && only_in_dev_fd.len() == 1
        {
            "ENUM_OK"
        } else {
            "ENUM_MISMATCH"
        };
        println!(
            "{verdict} n={count} limit={limit} highest={:?} listed={listed:?} scanned={scanned:?} dev_fd={in_dev_fd:?}",
            scanned.last()
        );
        drop(keep);
    }
    #[cfg(target_vendor = "apple")]
    #[test]
    fn the_real_enumeration_equals_the_fcntl_scan_and_dev_fd() {
        for limit in [4096, 1_048_575] {
            let output = std::process::Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "fds::tests::enumeration_fixture",
                    "--test-threads=1",
                    "--nocapture",
                ])
                .env("L6_ENUM_LIMIT", limit.to_string())
                .output()
                .unwrap();
            let text = String::from_utf8_lossy(&output.stdout);
            let line = text
                .lines()
                .find(|l| l.contains("ENUM_"))
                .unwrap_or_else(|| panic!("no verdict at limit {limit}: {text} {output:?}"));
            assert!(line.contains("ENUM_OK"), "limit {limit}: {line}");
            assert!(
                line.contains("highest=Some(3000)"),
                "descriptors far above the dense range were part of the table: {line}"
            );
        }
    }
    // ---- keep-list ------------------------------------------------------------------
    #[test]
    fn keep_list_is_released_and_every_other_descriptor_is_still_marked() {
        let mut f = fake();
        f.table.insert(9, FdFlags::CLOEXEC);
        f.table.insert(11, FdFlags::empty());
        confine_range(&mut f, 16, &[5, 7]).unwrap();
        assert_eq!(f.table[&3], FdFlags::CLOEXEC, "decoy marked");
        assert_eq!(
            f.table[&11],
            FdFlags::CLOEXEC,
            "decoy after the listed marked"
        );
        assert_eq!(
            f.table[&9],
            FdFlags::CLOEXEC,
            "closed-on-exec decoy stays so"
        );
        assert_eq!(
            f.table[&5],
            FdFlags::empty(),
            "listed cloexec descriptor released"
        );
        assert_eq!(
            f.table[&7],
            FdFlags::empty(),
            "listed gap descriptor stays inheritable"
        );
        assert_eq!(
            f.marked,
            [3, 11, 5],
            "7 needed no change; 9 was already marked"
        );
        assert_eq!(f.table[&0], FdFlags::empty(), "stdio untouched");
    }
    #[test]
    fn a_listed_descriptor_above_the_scan_limit_is_still_released() {
        let mut f = fake();
        f.table.insert(40, FdFlags::CLOEXEC);
        confine_range(&mut f, 16, &[40]).unwrap();
        assert_eq!(f.table[&40], FdFlags::empty());
    }
    #[test]
    fn a_listed_descriptor_that_is_closed_in_the_child_refuses_the_spawn() {
        let mut f = fake();
        let error = confine_range(&mut f, 16, &[4]).expect_err("listed but not open");
        assert_eq!(error.raw_os_error(), Some(Errno::BADF.raw_os_error()));
        let mut unreadable = fake();
        unreadable.fail_get = Some((5, Errno::IO));
        let error = confine_range(&mut unreadable, 16, &[5]).expect_err("unknown state");
        assert_eq!(error.raw_os_error(), Some(Errno::IO.raw_os_error()));
    }
    #[test]
    fn clearing_close_on_exec_is_checked_and_a_failure_refuses_the_spawn() {
        let mut failing = fake();
        failing.fail_set = Some((5, Errno::PERM));
        let error = confine_range(&mut failing, 16, &[5]).expect_err("clear failed");
        assert_eq!(error.raw_os_error(), Some(Errno::PERM.raw_os_error()));
        let mut silent = fake();
        silent.ignore_set = true; // set_flags reports success but the flag stays
        let error = confine_range(&mut silent, 16, &[5]).expect_err("flag did not clear");
        assert_eq!(error.raw_os_error(), Some(Errno::IO.raw_os_error()));
    }
    #[test]
    fn an_empty_keep_list_is_exactly_the_old_confinement() {
        let mut with = fake();
        let mut old = fake();
        confine_range(&mut with, 16, &[]).unwrap();
        // The pre-round-9 behaviour, spelled out: mark everything open above stderr.
        for fd in FIRST_CONFINED..16 {
            if let Some(flags) = old.table.get_mut(&fd) {
                *flags |= FdFlags::CLOEXEC;
            }
        }
        assert_eq!(with.table, old.table);
        assert_eq!(with.marked, [3, 7]);
    }
    #[test]
    fn keep_list_validation_refuses_stdio_negative_closed_unknown_and_duplicates() {
        let mut f = fake();
        f.table.insert(9, FdFlags::empty());
        for fd in 0..=2 {
            assert_eq!(
                check_keep(&mut f, &[5, fd]),
                Err(PassRefusal::Reserved),
                "{fd}"
            );
            assert_eq!(
                check_keep(&mut f, &[fd]),
                Err(PassRefusal::Reserved),
                "{fd}"
            );
        }
        assert_eq!(check_keep(&mut f, &[-1]), Err(PassRefusal::Negative));
        assert_eq!(
            check_keep(&mut f, &[5, i32::MIN]),
            Err(PassRefusal::Negative)
        );
        assert_eq!(check_keep(&mut f, &[4]), Err(PassRefusal::Closed));
        assert_eq!(check_keep(&mut f, &[5, 4]), Err(PassRefusal::Closed));
        assert_eq!(check_keep(&mut f, &[5, 9, 5]), Err(PassRefusal::Duplicate));
        assert_eq!(check_keep(&mut f, &[9, 9]), Err(PassRefusal::Duplicate));
        f.fail_get = Some((9, Errno::IO));
        assert_eq!(
            check_keep(&mut f, &[5, 9]),
            Err(PassRefusal::StateUnavailable)
        );
        f.fail_get = None;
        assert_eq!(check_keep(&mut f, &[]), Ok(()));
        assert_eq!(check_keep(&mut f, &[3, 5, 7, 9]), Ok(()));
        assert!(f.marked.is_empty(), "validation never changes a descriptor");
    }
    #[test]
    fn pass_refusals_carry_stable_codes_through_io_error() {
        for (why, code) in [
            (PassRefusal::Negative, "pass_descriptor_negative"),
            (PassRefusal::Reserved, "pass_descriptor_reserved"),
            (PassRefusal::Closed, "pass_descriptor_closed"),
            (
                PassRefusal::StateUnavailable,
                "pass_descriptor_state_unavailable",
            ),
            (PassRefusal::Duplicate, "pass_descriptor_duplicate"),
        ] {
            let error = pass_refused(why);
            assert_eq!(pass_refusal(&error), Some(why));
            assert_eq!(why.code(), code);
        }
        assert_eq!(pass_refusal(&io::Error::other("x")), None);
    }
    // ---- adoption -------------------------------------------------------------------
    impl Inherited for Fake {
        fn kind(&mut self, fd: i32) -> Result<Option<DescriptorKind>, Errno> {
            self.kind_calls.push(fd);
            match self.fail_kind {
                Some((at, e)) if at == fd => Err(e),
                _ => self.kinds.get(&fd).copied().ok_or(Errno::BADF),
            }
        }
    }
    fn inherited() -> Fake {
        let mut f = fake();
        f.kinds.insert(9, Some(DescriptorKind::Fifo));
        f.table.insert(9, FdFlags::empty());
        f.kinds.insert(10, Some(DescriptorKind::Socket));
        f.table.insert(10, FdFlags::empty());
        f.kinds.insert(11, Some(DescriptorKind::Regular));
        f.table.insert(11, FdFlags::CLOEXEC);
        f.kinds.insert(12, None);
        f.table.insert(12, FdFlags::empty());
        f
    }
    fn claims() -> Mutex<BTreeSet<i32>> {
        Mutex::new(BTreeSet::new())
    }
    fn adopt(
        f: &mut Fake,
        c: &Mutex<BTreeSet<i32>>,
        fd: i32,
        k: DescriptorKind,
    ) -> Result<(), AdoptionRefusal> {
        adopt_checked(f, c, fd, k)
    }
    #[test]
    fn negative_and_stdio_numbers_are_refused_without_touching_anything() {
        let mut f = inherited();
        let c = claims();
        assert_eq!(
            adopt(&mut f, &c, -1, DescriptorKind::Fifo),
            Err(AdoptionRefusal::Negative)
        );
        assert_eq!(
            adopt(&mut f, &c, i32::MIN, DescriptorKind::Fifo),
            Err(AdoptionRefusal::Negative)
        );
        for fd in 0..=2 {
            f.kinds.insert(fd, Some(DescriptorKind::Fifo)); // even a valid stdio pipe
            assert_eq!(
                adopt(&mut f, &c, fd, DescriptorKind::Fifo),
                Err(AdoptionRefusal::Reserved),
                "{fd}"
            );
        }
        assert!(
            f.marked.is_empty() && f.kind_calls.is_empty(),
            "no probing of refused numbers"
        );
        assert!(c.lock().unwrap().is_empty());
    }
    #[test]
    fn closed_descriptor_is_a_specific_refusal_and_other_errors_are_unknown_state() {
        let mut f = inherited();
        let c = claims();
        assert_eq!(
            adopt(&mut f, &c, 400, DescriptorKind::Fifo),
            Err(AdoptionRefusal::Closed)
        );
        f.fail_get = Some((9, Errno::IO));
        assert_eq!(
            adopt(&mut f, &c, 9, DescriptorKind::Fifo),
            Err(AdoptionRefusal::StateUnavailable)
        );
        f.fail_get = None;
        f.fail_kind = Some((9, Errno::IO));
        assert_eq!(
            adopt(&mut f, &c, 9, DescriptorKind::Fifo),
            Err(AdoptionRefusal::StateUnavailable)
        );
        f.fail_kind = Some((9, Errno::BADF)); // closed between the two probes
        assert_eq!(
            adopt(&mut f, &c, 9, DescriptorKind::Fifo),
            Err(AdoptionRefusal::Closed)
        );
        assert!(f.marked.is_empty() && c.lock().unwrap().is_empty());
    }
    #[test]
    fn wrong_kind_is_refused_before_any_change_or_claim() {
        for (fd, wanted) in [
            (10, DescriptorKind::Fifo),
            (11, DescriptorKind::Fifo),
            (12, DescriptorKind::Fifo),
            (9, DescriptorKind::Socket),
            (9, DescriptorKind::Regular),
            (12, DescriptorKind::Regular),
        ] {
            let mut f = inherited();
            let c = claims();
            assert_eq!(
                adopt(&mut f, &c, fd, wanted),
                Err(AdoptionRefusal::WrongKind),
                "{fd} {wanted:?}"
            );
            assert!(f.marked.is_empty(), "a refused kind must not be re-flagged");
            assert!(c.lock().unwrap().is_empty(), "a refusal claims nothing");
        }
    }
    #[test]
    fn accepted_kind_is_made_close_on_exec_exactly_when_needed() {
        let mut f = inherited();
        let c = claims();
        adopt(&mut f, &c, 9, DescriptorKind::Fifo).unwrap();
        adopt(&mut f, &c, 10, DescriptorKind::Socket).unwrap();
        adopt(&mut f, &c, 11, DescriptorKind::Regular).unwrap();
        assert_eq!(
            f.marked,
            [9, 10],
            "11 was already close-on-exec and is not rewritten"
        );
        assert!(f.table[&9].contains(FdFlags::CLOEXEC) && f.table[&10].contains(FdFlags::CLOEXEC));
    }
    #[test]
    fn close_on_exec_failure_or_a_flag_that_does_not_stick_refuses() {
        let mut f = inherited();
        f.fail_set = Some((9, Errno::PERM));
        assert_eq!(
            adopt(&mut f, &claims(), 9, DescriptorKind::Fifo),
            Err(AdoptionRefusal::CloexecFailed)
        );
        let mut f = inherited();
        f.ignore_set = true; // F_SETFD reports success but the flag is not there
        assert_eq!(
            adopt(&mut f, &claims(), 9, DescriptorKind::Fifo),
            Err(AdoptionRefusal::CloexecFailed)
        );
    }
    #[test]
    fn a_number_is_adopted_once_even_across_a_refused_retry_and_racing_threads() {
        let mut f = inherited();
        let c = claims();
        adopt(&mut f, &c, 9, DescriptorKind::Fifo).unwrap();
        assert_eq!(
            adopt(&mut f, &c, 9, DescriptorKind::Fifo),
            Err(AdoptionRefusal::AlreadyAdopted)
        );
        assert_eq!(
            adopt(&mut f, &c, 9, DescriptorKind::Socket),
            Err(AdoptionRefusal::WrongKind)
        );
        // 16 racing threads on one fresh number: exactly one wins.
        let c = claims();
        let wins = std::thread::scope(|s| {
            let handles: Vec<_> = (0..16)
                .map(|_| s.spawn(|| adopt(&mut inherited(), &c, 10, DescriptorKind::Socket)))
                .collect();
            handles
                .into_iter()
                .map(|h| h.join().unwrap())
                .filter(Result::is_ok)
                .count()
        });
        assert_eq!(wins, 1);
    }
    #[test]
    fn a_poisoned_claim_table_fails_closed() {
        let c = claims();
        std::thread::scope(|s| {
            drop(
                s.spawn(|| {
                    let _g = c.lock().unwrap();
                    panic!("poison");
                })
                .join(),
            );
        });
        assert_eq!(
            adopt(&mut inherited(), &c, 9, DescriptorKind::Fifo),
            Err(AdoptionRefusal::StateUnavailable)
        );
    }
    #[test]
    fn real_descriptor_kinds_are_distinguished() {
        use std::os::fd::AsRawFd;
        let (r, _w) = std::io::pipe().unwrap();
        let (a, _b) = std::os::unix::net::UnixStream::pair().unwrap();
        let file = std::fs::File::open("/dev/null").unwrap();
        let regular = std::fs::File::open(env!("CARGO_MANIFEST_PATH")).unwrap();
        let dir = std::fs::File::open(".").unwrap();
        let mut p = Process;
        assert_eq!(p.kind(r.as_raw_fd()), Ok(Some(DescriptorKind::Fifo)));
        assert_eq!(p.kind(a.as_raw_fd()), Ok(Some(DescriptorKind::Socket)));
        assert_eq!(
            p.kind(regular.as_raw_fd()),
            Ok(Some(DescriptorKind::Regular))
        );
        assert_eq!(
            p.kind(file.as_raw_fd()),
            Ok(None),
            "a character device is never adoptable"
        );
        assert_eq!(p.kind(dir.as_raw_fd()), Ok(None));
        let closed = {
            let f = std::fs::File::open("/dev/null").unwrap();
            f.as_raw_fd()
        };
        assert_eq!(p.kind(closed), Err(Errno::BADF));
    }
    /// The only test that uses the process-wide table (its claims are never released).
    #[test]
    fn public_entry_point_adopts_a_real_pipe_once() {
        use std::os::fd::AsRawFd;
        let (r, _w) = std::io::pipe().unwrap();
        let n = std::os::fd::OwnedFd::from(r);
        let raw = n.as_raw_fd();
        assert!(fcntl_getfd(&n).unwrap().contains(FdFlags::CLOEXEC));
        fcntl_setfd(&n, FdFlags::empty()).unwrap(); // as an exec'd child would find it
        let owned = std::mem::ManuallyDrop::new(n);
        let adopted = adopt_inherited_descriptor(raw, DescriptorKind::Fifo).unwrap();
        assert_eq!(adopted.as_raw_fd(), raw);
        assert!(
            fcntl_getfd(&adopted).unwrap().contains(FdFlags::CLOEXEC),
            "adopted fd is close-on-exec"
        );
        let again = adopt_inherited_descriptor(raw, DescriptorKind::Fifo).unwrap_err();
        assert_eq!(refusal(&again), Some(AdoptionRefusal::AlreadyAdopted));
        assert_eq!(again.kind(), io::ErrorKind::AlreadyExists);
        let _ = owned; // closed once, by `adopted`
        for fd in [0, 1, 2, -1] {
            let e = adopt_inherited_descriptor(fd, DescriptorKind::Fifo).unwrap_err();
            assert!(matches!(
                refusal(&e),
                Some(AdoptionRefusal::Reserved | AdoptionRefusal::Negative)
            ));
        }
        assert_eq!(
            AdoptionRefusal::WrongKind.sidecar_code(),
            "bootstrap_not_pipe"
        );
        for r in [
            AdoptionRefusal::Negative,
            AdoptionRefusal::Reserved,
            AdoptionRefusal::Closed,
            AdoptionRefusal::AlreadyAdopted,
        ] {
            assert_eq!(r.sidecar_code(), "invalid_bootstrap_fd");
        }
    }
}
