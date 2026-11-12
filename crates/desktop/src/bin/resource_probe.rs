// SPDX-License-Identifier: AGPL-3.0-only
//! `native/resource_probe`: the packaged app's one-shot, read-only resource observation, the Rust
//! port of v0.1.4's `mycelium_a13_macos/native/resource_probe.m` (@ f4e366e9). It reads three OS
//! facts and prints one JSON line; `src/resource_probe.rs` decides what they mean, and
//! `api::resources::ProbeObserver` runs this file from `<exe dir>/native/resource_probe` with its own
//! bound. Only system libraries are used (libSystem, libobjc, Foundation, Network): no crate, no
//! compiler at runtime, no write anywhere, no network traffic (a path monitor only reads state).
#![allow(unsafe_code)]
// The decisions are compiled into this helper from their one source file instead of through the
// `desktop` library, so the pinned helper bytes do not move whenever unrelated library code does.
#[path = "../resource_probe.rs"]
mod resource_probe;
use std::io::Write;
use std::{
    ffi::{CStr, c_char, c_void},
    process::{Command, ExitCode, Stdio},
    sync::atomic::{AtomicBool, AtomicI32, AtomicPtr, Ordering},
};

type Id = *mut c_void;
type Sel = *mut c_void;

#[link(name = "objc")]
unsafe extern "C" {
    fn objc_getClass(name: *const c_char) -> Id;
    fn sel_registerName(name: *const c_char) -> Sel;
    fn objc_msgSend();
}
#[link(name = "Foundation", kind = "framework")]
unsafe extern "C" {}
#[link(name = "Network", kind = "framework")]
unsafe extern "C" {
    fn nw_path_monitor_create() -> Id;
    fn nw_path_monitor_set_queue(monitor: Id, queue: Id);
    fn nw_path_monitor_set_update_handler(monitor: Id, handler: *mut Block);
    fn nw_path_monitor_start(monitor: Id);
    fn nw_path_monitor_cancel(monitor: Id);
    fn nw_path_get_status(path: Id) -> i32;
    fn nw_path_is_expensive(path: Id) -> bool;
    fn nw_path_is_constrained(path: Id) -> bool;
}
unsafe extern "C" {
    fn dispatch_queue_create(label: *const c_char, attr: Id) -> Id;
    fn dispatch_semaphore_create(value: isize) -> Id;
    fn dispatch_semaphore_signal(semaphore: Id) -> isize;
    fn dispatch_semaphore_wait(semaphore: Id, timeout: u64) -> isize;
    fn dispatch_time(when: u64, delta: i64) -> u64;
    fn dispatch_sync_f(queue: Id, context: *mut c_void, work: extern "C" fn(*mut c_void));
    static _NSConcreteGlobalBlock: c_void;
}
const DISPATCH_TIME_NOW: u64 = 0;

/// `thermalObservation`: `[NSProcessInfo processInfo].thermalState`.
fn thermal_state() -> isize {
    // SAFETY: the class and selectors exist in Foundation (linked above); `objc_msgSend` is called
    // through function pointers of the exact ObjC signatures (`+processInfo` returns an object,
    // `-thermalState` an NSInteger), as the arm64 ABI requires.
    unsafe {
        let class = objc_getClass(c"NSProcessInfo".as_ptr());
        if class.is_null() {
            return -1;
        }
        let send_object: unsafe extern "C" fn(Id, Sel) -> Id =
            std::mem::transmute(objc_msgSend as unsafe extern "C" fn());
        let send_integer: unsafe extern "C" fn(Id, Sel) -> isize =
            std::mem::transmute(objc_msgSend as unsafe extern "C" fn());
        let info = send_object(class, sel_registerName(c"processInfo".as_ptr()));
        if info.is_null() {
            return -1;
        }
        send_integer(info, sel_registerName(c"thermalState".as_ptr()))
    }
}

/// `powerObservation`: `/usr/bin/pmset -g batt`, stdout only when it ran and exited 0.
fn pmset_output() -> Option<Vec<u8>> {
    let output = Command::new("/usr/bin/pmset")
        .args(["-g", "batt"])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    output.status.success().then_some(output.stdout)
}

// The update handler is a global block (no captures): its state lives in these statics.
static UPDATED: AtomicBool = AtomicBool::new(false);
static STATUS: AtomicI32 = AtomicI32::new(0);
static EXPENSIVE: AtomicBool = AtomicBool::new(false);
static CONSTRAINED: AtomicBool = AtomicBool::new(false);
static READY: AtomicPtr<c_void> = AtomicPtr::new(std::ptr::null_mut());

#[repr(C)]
struct Descriptor {
    reserved: usize,
    size: usize,
    signature: *const c_char,
}
#[repr(C)]
struct Block {
    isa: *const c_void,
    flags: i32,
    reserved: i32,
    invoke: extern "C" fn(*mut Block, Id),
    descriptor: *const Descriptor,
}
const BLOCK_IS_GLOBAL: i32 = 1 << 28;
const BLOCK_HAS_SIGNATURE: i32 = 1 << 30;

/// `^(nw_path_t path)`: the same mapping inputs v0.1.4 reads, then signal the waiting thread.
extern "C" fn on_update(_block: *mut Block, path: Id) {
    // SAFETY: Network passes a valid `nw_path_t` for the duration of the handler call.
    let (status, expensive, constrained) = unsafe {
        (
            nw_path_get_status(path),
            nw_path_is_expensive(path),
            nw_path_is_constrained(path),
        )
    };
    STATUS.store(status, Ordering::SeqCst);
    EXPENSIVE.store(expensive, Ordering::SeqCst);
    CONSTRAINED.store(constrained, Ordering::SeqCst);
    UPDATED.store(true, Ordering::SeqCst);
    let ready = READY.load(Ordering::SeqCst);
    if !ready.is_null() {
        // SAFETY: `ready` is the live semaphore created before the monitor started.
        unsafe { dispatch_semaphore_signal(ready) };
    }
}
extern "C" fn drained(_context: *mut c_void) {}

fn wait_nanos() -> i64 {
    budgets::BudgetId::ResourceProbeNetworkWait
        .span(budgets::Entries::ZERO)
        .map_or(0, |s| {
            i64::try_from(s.millis()).map_or(i64::MAX, |ms| ms.saturating_mul(1_000_000))
        })
}

/// `networkObservation`: the latest update seen once the first one arrived or the wait ended.
fn network_update() -> Option<(i32, bool, bool)> {
    let descriptor = Box::leak(Box::new(Descriptor {
        reserved: 0,
        size: std::mem::size_of::<Block>(),
        signature: c"v16@?0@8".as_ptr(),
    }));
    // SAFETY: a global block literal (static isa, no captures, never copied or freed) with the
    // documented Block ABI layout; the dispatch and Network objects are used as their headers
    // declare and live until the process exits.
    unsafe {
        let block = Box::leak(Box::new(Block {
            isa: &raw const _NSConcreteGlobalBlock,
            flags: BLOCK_IS_GLOBAL | BLOCK_HAS_SIGNATURE,
            reserved: 0,
            invoke: on_update,
            descriptor,
        }));
        let ready = dispatch_semaphore_create(0);
        READY.store(ready, Ordering::SeqCst);
        let label: &CStr = c"org.mycelium.a13.resource-probe";
        let queue = dispatch_queue_create(label.as_ptr(), std::ptr::null_mut());
        let monitor = nw_path_monitor_create();
        if ready.is_null() || queue.is_null() || monitor.is_null() {
            return None;
        }
        nw_path_monitor_set_queue(monitor, queue);
        nw_path_monitor_set_update_handler(monitor, block);
        nw_path_monitor_start(monitor);
        // The wait's own result is not consulted (v0.1.4 casts it to void): the snapshot is.
        dispatch_semaphore_wait(ready, dispatch_time(DISPATCH_TIME_NOW, wait_nanos()));
        nw_path_monitor_cancel(monitor);
        // `dispatch_sync(queue, ^{ snapshot = result; })`: every handler already queued has run.
        dispatch_sync_f(queue, std::ptr::null_mut(), drained);
    }
    UPDATED.load(Ordering::SeqCst).then(|| {
        (
            STATUS.load(Ordering::SeqCst),
            EXPENSIVE.load(Ordering::SeqCst),
            CONSTRAINED.load(Ordering::SeqCst),
        )
    })
}

fn main() -> ExitCode {
    let power = resource_probe::power(pmset_output().as_deref());
    let thermal = resource_probe::thermal(thermal_state());
    let network = resource_probe::network(network_update());
    // v0.1.4 exits 2 when it cannot produce its JSON; a failed write is the same outcome here.
    let mut out = std::io::stdout().lock();
    match out
        .write_all(resource_probe::line(power, thermal, network).as_bytes())
        .and_then(|()| out.flush())
    {
        Ok(()) => ExitCode::SUCCESS,
        Err(_) => ExitCode::from(2),
    }
}
