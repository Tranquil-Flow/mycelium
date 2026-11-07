// SPDX-License-Identifier: AGPL-3.0-only
//! AppKit dispatch, not a shell command, browser credential or child process.
#![allow(unsafe_code)]
use crate::links::{Browser, Destination, LinkError, destination};
use objc2::{class, msg_send, rc::Retained};
use objc2_foundation::{NSObject, NSString, NSURL};

pub struct SystemBrowser;
impl Browser for SystemBrowser {
    fn open(&self, url: &tauri::Url) -> Result<(), LinkError> {
        if destination(url) != Destination::Browser {
            return Err(LinkError::SystemBrowser);
        }
        let value = NSURL::URLWithString(&NSString::from_str(url.as_str()))
            .ok_or(LinkError::SystemBrowser)?;
        // SAFETY: NSWorkspace is linked by Tauri/AppKit. sharedWorkspace returns
        // a retained NSObject; openURL: takes NSURL* and returns Objective-C BOOL.
        // Only native navigation callbacks call this on the main event thread.
        let opened: bool = unsafe {
            let workspace: Retained<NSObject> = msg_send![class!(NSWorkspace), sharedWorkspace];
            msg_send![&*workspace, openURL: &*value]
        };
        if opened {
            Ok(())
        } else {
            Err(LinkError::SystemBrowser)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_opener_refuses_non_browser_destinations_before_os_dispatch() {
        for url in [
            "mycelium://localhost/chat",
            "file:///unused",
            "data:text/html,x",
            "https://user@example.invalid/",
        ] {
            assert_eq!(
                SystemBrowser.open(&url.parse().unwrap()),
                Err(LinkError::SystemBrowser)
            );
        }
    }

    #[test]
    #[ignore = "explicit read-only LaunchServices qualification; no browser launch"]
    fn system_browser_handler_resolves_without_launch() {
        // openURL: would open a visible browser tab/window on the owner's screen.
        // Ask the actual OS for its handler, but do not send an open AppleEvent.
        // .invalid is never requested: LaunchServices only resolves the scheme.
        for scheme in ["http", "https"] {
            let url = NSURL::URLWithString(&NSString::from_str(&format!(
                "{scheme}://example.invalid/l11"
            )))
            .unwrap();
            // SAFETY: documented NSWorkspace lookup takes NSURL*, returning an
            // optional autoreleased NSURL*. objc2 retains it; no app is launched.
            let handler: Option<Retained<NSURL>> = unsafe {
                let workspace: Retained<NSObject> = msg_send![class!(NSWorkspace), sharedWorkspace];
                msg_send![&*workspace, URLForApplicationToOpenURL: &*url]
            };
            assert!(handler.is_some(), "no system handler for {scheme}");
            println!("L11_SYSTEM_HANDLER:{scheme}:RESOLVED_NO_LAUNCH");
        }
    }
}
