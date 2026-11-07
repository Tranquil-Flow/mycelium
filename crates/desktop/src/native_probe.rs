// SPDX-License-Identifier: AGPL-3.0-only
//! Development-shell only (`dev-shell` + `debug_assertions`; compiled into no packaged bundle):
//! drives the REAL AppKit Quit paths of our own window from inside our own process, because
//! posting a Command-Q to another process needs Accessibility permission and an active app
//! (verified: a `CGEventPostToPid` Command-Q reached nothing). Menu: the same
//! `performActionForItemAtIndex:` AppKit runs for a mouse click on the "Quit Mycelium" item.
//! Command-Q: a genuine key-down `NSEvent` (Command, "q", key code 12) given to the main menu's
//! `performKeyEquivalent:`, which succeeds only when the item's accelerator really is Command-Q.
#![allow(unsafe_code)]
use objc2::{
    Encode, Encoding, msg_send,
    rc::Retained,
    runtime::{AnyClass, AnyObject, Bool},
};
use objc2_foundation::NSString;

#[derive(Clone, Copy)]
#[repr(C)]
struct Point {
    x: f64,
    y: f64,
}
// SAFETY: `CGPoint` is two C doubles; this is its Objective-C type encoding.
unsafe impl Encode for Point {
    const ENCODING: Encoding = Encoding::Struct("CGPoint", &[Encoding::Double, Encoding::Double]);
}

const QUIT_ITEM_TITLE: &str = "Quit Mycelium";
const KEY_DOWN: usize = 10;
const MODIFIER_COMMAND: usize = 1 << 20;
const KEY_CODE_Q: u16 = 12;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Variant {
    Menu,
    CommandQ,
}
impl Variant {
    /// The variants one probe path performs, in order. `menu-then-command-q` performs both inside
    /// one main-thread turn: the first Quit revokes the window channel, so a second request over
    /// it could never arrive.
    pub fn from_path(path: &str) -> Option<&'static [Self]> {
        match path {
            "/l11-quit/menu" => Some(&[Self::Menu]),
            "/l11-quit/command-q" => Some(&[Self::CommandQ]),
            "/l11-quit/menu-then-command-q" => Some(&[Self::Menu, Self::CommandQ]),
            _ => None,
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            Self::Menu => "menu",
            Self::CommandQ => "command-q",
        }
    }
}

/// Main thread only. Returns what AppKit answered, as a receipt string.
pub fn dispatch(variant: Variant) -> Result<&'static str, &'static str> {
    // SAFETY: plain AppKit message sends on the main thread to objects AppKit returns; every
    // returned pointer is checked for nil before use and no object outlives this call.
    unsafe {
        let class = AnyClass::get(c"NSApplication").ok_or("no_nsapplication")?;
        let app: Option<Retained<AnyObject>> = msg_send![class, sharedApplication];
        let app = app.ok_or("no_shared_application")?;
        let menu: Option<Retained<AnyObject>> = msg_send![&*app, mainMenu];
        let menu = menu.ok_or("no_main_menu")?;
        let top: Option<Retained<AnyObject>> = msg_send![&*menu, itemAtIndex: 0isize];
        let submenu: Option<Retained<AnyObject>> = msg_send![&*top.ok_or("no_top_item")?, submenu];
        let submenu = submenu.ok_or("no_submenu")?;
        let item: Option<Retained<AnyObject>> = msg_send![&*submenu, itemAtIndex: 0isize];
        let item = item.ok_or("no_quit_item")?;
        let title: Retained<NSString> = msg_send![&*item, title];
        if title.to_string() != QUIT_ITEM_TITLE {
            return Err("quit_item_title_mismatch");
        }
        match variant {
            Variant::Menu => {
                let _: () = msg_send![&*submenu, performActionForItemAtIndex: 0isize];
                Ok("performed")
            }
            Variant::CommandQ => {
                let event_class = AnyClass::get(c"NSEvent").ok_or("no_nsevent")?;
                let characters = NSString::from_str("q");
                let event: Option<Retained<AnyObject>> = msg_send![event_class,
                    keyEventWithType: KEY_DOWN, location: Point { x: 0.0, y: 0.0 },
                    modifierFlags: MODIFIER_COMMAND, timestamp: 0.0f64, windowNumber: 0isize,
                    context: Option::<&AnyObject>::None, characters: &*characters,
                    charactersIgnoringModifiers: &*characters, isARepeat: Bool::NO,
                    keyCode: KEY_CODE_Q];
                let handled: Bool =
                    msg_send![&*menu, performKeyEquivalent: &*event.ok_or("no_key_event")?];
                if handled.as_bool() {
                    Ok("handled")
                } else {
                    Err("key_equivalent_not_handled")
                }
            }
        }
    }
}
