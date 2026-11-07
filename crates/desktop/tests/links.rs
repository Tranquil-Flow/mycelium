// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "tauri-shell")]
use desktop::links::{Browser, Destination, LinkError, destination, navigate};
use std::cell::RefCell;
struct Recorder {
    calls: RefCell<Vec<String>>,
    fail: bool,
}
impl Browser for Recorder {
    fn open(&self, url: &tauri::Url) -> Result<(), LinkError> {
        self.calls.borrow_mut().push(url.to_string());
        if self.fail {
            Err(LinkError::SystemBrowser)
        } else {
            Ok(())
        }
    }
}
#[test]
fn external_links_open_once_in_browser_never_in_native_window() {
    let browser = Recorder {
        calls: RefCell::default(),
        fail: false,
    };
    for raw in [
        "https://example.org/chat?public=1#part",
        "http://127.0.0.1:9000/chat",
        "https://relay.example.org/chat",
    ] {
        let url = raw.parse().unwrap();
        assert_eq!(destination(&url), Destination::Browser);
        assert_eq!(navigate(&url, &browser), Ok(false));
    }
    assert_eq!(browser.calls.borrow().len(), 3);
    for raw in ["mycelium://localhost/", "mycelium://localhost/chat"] {
        assert_eq!(navigate(&raw.parse().unwrap(), &browser), Ok(true));
    }
    assert_eq!(browser.calls.borrow().len(), 3);
}
#[test]
fn refused_targets_never_reach_system_browser() {
    let browser = Recorder {
        calls: RefCell::default(),
        fail: false,
    };
    for raw in [
        "file:///etc/passwd",
        "data:text/html,x",
        "blob:https://example.org/x",
        "javascript:alert(1)",
        "mailto:user@example.org",
        "mycelium://localhost/frame",
        "mycelium://localhost/chat/",
        "https://user:password@example.org/",
        "https://user@example.org/",
        "about:blank",
    ] {
        let url = raw.parse().unwrap();
        assert_eq!(destination(&url), Destination::Refused, "{raw}");
        assert_eq!(navigate(&url, &browser), Ok(false));
    }
    assert!(browser.calls.borrow().is_empty());
}
#[test]
fn system_browser_failure_is_observable_and_cannot_enable_navigation() {
    let browser = Recorder {
        calls: RefCell::default(),
        fail: true,
    };
    assert_eq!(
        navigate(&"https://example.org/".parse().unwrap(), &browser),
        Err(LinkError::SystemBrowser)
    );
    assert_eq!(browser.calls.borrow().len(), 1);
}
