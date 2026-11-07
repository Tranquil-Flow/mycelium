// SPDX-License-Identifier: AGPL-3.0-only
//! Narrow macOS ABI boundary. All methods are main-thread-only. The event
//! callback only sets a flag; it cannot unwind or run cleanup inside AppKit.
#![allow(unsafe_code)]
use objc2::{MainThreadMarker, MainThreadOnly, define_class, msg_send, rc::Retained, sel};
use objc2_foundation::{NSAppleEventDescriptor, NSAppleEventManager, NSObject};
use std::sync::atomic::{AtomicBool, Ordering};
static QUIT: AtomicBool = AtomicBool::new(false);
const EVENT_CLASS: u32 = u32::from_be_bytes(*b"aevt");
const EVENT_ID: u32 = u32::from_be_bytes(*b"quit");
define_class!(
    #[unsafe(super(NSObject))]
    #[thread_kind = MainThreadOnly]
    struct QuitHandler;
    impl QuitHandler {
        #[unsafe(method(handleQuit:reply:))]
        fn handle_quit(&self, _event: &NSAppleEventDescriptor, _reply: &NSAppleEventDescriptor) {
            QUIT.store(true, Ordering::Release);
        }
    }
);
pub struct NativeQuit {
    handler: Retained<QuitHandler>,
}
impl NativeQuit {
    pub fn new() -> Result<Self, &'static str> {
        let mtm = MainThreadMarker::new().ok_or("native_quit_not_main_thread")?;
        // SAFETY: allocate/init NSObject subclass on main thread; retained for
        // the complete event-loop lifetime and removed before it is dropped.
        let handler = unsafe { msg_send![QuitHandler::alloc(mtm), init] };
        let this = Self { handler };
        this.arm();
        Ok(this)
    }
    pub fn arm(&self) {
        // SAFETY: Foundation signature is (id, SEL, AEEventClass=u32,
        // AEEventID=u32). Selector exists above with two object arguments and
        // void return. MainThreadOnly receiver cannot be moved across threads.
        unsafe {
            let _: () = msg_send![&*NSAppleEventManager::sharedAppleEventManager(),
                setEventHandler: &*self.handler, andSelector: sel!(handleQuit:reply:),
                forEventClass: EVENT_CLASS, andEventID: EVENT_ID];
        }
    }
    pub fn take_request(&self) -> bool {
        QUIT.swap(false, Ordering::AcqRel)
    }
}
impl Drop for NativeQuit {
    fn drop(&mut self) {
        // SAFETY: same main-thread manager and literal event identifiers. Remove
        // registration before releasing the retained target.
        unsafe {
            let _: () = msg_send![&*NSAppleEventManager::sharedAppleEventManager(),
                removeEventHandlerForEventClass: EVENT_CLASS, andEventID: EVENT_ID];
        }
    }
}
