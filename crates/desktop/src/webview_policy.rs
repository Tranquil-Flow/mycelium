// SPDX-License-Identifier: AGPL-3.0-only
//! Narrow macOS ABI boundary for the web view's scheduling while its window is hidden.
//! WebKit's default (`WKInactiveSchedulingPolicySuspend`) lets RunningBoard suspend the
//! WebContent process roughly eight seconds after the window is occluded, so a chat page
//! with a held request never sees the reply and never schedules its next one. A browser
//! tab (how v0.1.4 hosts the same page) is throttled, never suspended, while it has a
//! request in flight; `Throttle` gives this web view the same behavior. All calls are
//! main-thread-only and go through public WebKit/AppKit selectors.
#![allow(unsafe_code)]
use objc2::{msg_send, rc::Retained, runtime::AnyObject, sel};
use std::ffi::c_void;
/// `WKInactiveSchedulingPolicy` (WebKit/WKPreferences.h): Suspend 0, Throttle 1, None 2.
const THROTTLE: isize = 1;
/// `NSWindowOcclusionStateVisible` (AppKit/NSWindow.h).
#[cfg(any(
    feature = "qualification-probe",
    all(feature = "dev-shell", debug_assertions)
))]
const OCCLUSION_VISIBLE: usize = 1 << 1;
/// Applied policy as read back from the live preferences object.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Applied {
    Suspend,
    Throttle,
    None,
    Unknown,
}
/// Set the inactive scheduling policy of a live `WKWebView` to throttle and read it back.
///
/// # Safety
/// `webview` must be the live `WKWebView` pointer wry hands to `with_webview`, used on the
/// main thread that owns it.
unsafe fn keep_inactive_page_scheduled(webview: *mut c_void) -> Result<Applied, &'static str> {
    if webview.is_null() {
        return Err("webview_null");
    }
    // SAFETY: caller contract above; `AnyObject` is only messaged, never dereferenced.
    let view: &AnyObject = unsafe { &*webview.cast::<AnyObject>() };
    let configuration: Option<Retained<AnyObject>> = unsafe { msg_send![view, configuration] };
    let configuration = configuration.ok_or("webview_configuration_missing")?;
    let preferences: Option<Retained<AnyObject>> =
        unsafe { msg_send![&*configuration, preferences] };
    let preferences = preferences.ok_or("webview_preferences_missing")?;
    let supported: bool =
        unsafe { msg_send![&*preferences, respondsToSelector: sel!(setInactiveSchedulingPolicy:)] };
    if !supported {
        return Err("inactive_scheduling_policy_unsupported");
    }
    // SAFETY: selector exists (checked); the argument is an NSInteger enum value.
    unsafe {
        let _: () = msg_send![&*preferences, setInactiveSchedulingPolicy: THROTTLE];
    }
    let now: isize = unsafe { msg_send![&*preferences, inactiveSchedulingPolicy] };
    Ok(match now {
        0 => Applied::Suspend,
        1 => Applied::Throttle,
        2 => Applied::None,
        _ => Applied::Unknown,
    })
}
/// # Safety
/// `window` must be the live `NSWindow` pointer, used on the main thread.
#[cfg(any(
    feature = "qualification-probe",
    all(feature = "dev-shell", debug_assertions)
))]
unsafe fn occlusion_visible(window: *mut c_void) -> Option<bool> {
    if window.is_null() {
        return None;
    }
    // SAFETY: caller contract above.
    let window: &AnyObject = unsafe { &*window.cast::<AnyObject>() };
    let state: usize = unsafe { msg_send![window, occlusionState] };
    Some(state & OCCLUSION_VISIBLE != 0)
}

/// Apply the throttle policy to the window's web view. wry runs the closure on the main
/// thread with the live `WKWebView`; the applied value is read back and, for the development
/// probe, printed as a receipt. A failure is named on stderr and never silent.
pub fn keep_page_scheduled(window: &tauri::WebviewWindow, report: bool) -> Result<(), String> {
    window
        .with_webview(move |webview| {
            // SAFETY: main thread, live WKWebView from wry (see the function contract).
            match unsafe { keep_inactive_page_scheduled(webview.inner()) } {
                Ok(applied) => receipt(report, applied),
                Err(error) => eprintln!("webview_inactive_policy_failed:{error}"),
            }
        })
        .map_err(|error| format!("webview_policy_dispatch:{error}"))
}
/// The applied-policy receipt exists only in builds that name probe hooks (debug `dev-shell` or
/// `qualification-probe`); the shipped bundle applies the policy silently.
#[cfg(any(
    feature = "qualification-probe",
    all(feature = "dev-shell", debug_assertions)
))]
fn receipt(report: bool, applied: Applied) {
    if report {
        println!("L11_WEBVIEW_INACTIVE_POLICY:{applied:?}");
    }
}
#[cfg(not(any(
    feature = "qualification-probe",
    all(feature = "dev-shell", debug_assertions)
)))]
fn receipt(_: bool, _: Applied) {}
/// Whether AppKit reports any part of the window visible (not fully occluded). Main thread.
#[cfg(any(
    feature = "qualification-probe",
    all(feature = "dev-shell", debug_assertions)
))]
pub fn window_visible(window: &tauri::WebviewWindow) -> Option<bool> {
    let ns_window = window.ns_window().ok()?;
    // SAFETY: tauri returns the live NSWindow; the shell event loop runs on the main thread.
    unsafe { occlusion_visible(ns_window) }
}
