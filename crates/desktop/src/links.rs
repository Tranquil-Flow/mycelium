// SPDX-License-Identifier: AGPL-3.0-only
//! Never grant an external document the native window capability.
#[derive(Debug, PartialEq, Eq)]
pub enum Destination {
    Pinned,
    Browser,
    Refused,
}
#[derive(Debug, PartialEq, Eq)]
pub enum LinkError {
    SystemBrowser,
}
pub trait Browser {
    fn open(&self, url: &tauri::Url) -> Result<(), LinkError>;
}
pub fn destination(url: &tauri::Url) -> Destination {
    if crate::channel::navigation_allowed(url.as_str()) {
        Destination::Pinned
    } else if matches!(url.scheme(), "http" | "https")
        && url.host_str().is_some()
        && url.username().is_empty()
        && url.password().is_none()
    {
        Destination::Browser
    } else {
        Destination::Refused
    }
}
pub fn navigate(url: &tauri::Url, browser: &impl Browser) -> Result<bool, LinkError> {
    match destination(url) {
        Destination::Pinned => Ok(true),
        Destination::Browser => {
            browser.open(url)?;
            Ok(false)
        }
        Destination::Refused => Ok(false),
    }
}
