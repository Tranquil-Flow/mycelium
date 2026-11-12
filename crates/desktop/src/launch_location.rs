// SPDX-License-Identifier: AGPL-3.0-only
//! Where the packaged app is running from, ported from `mycelium_a13_macos/launch_location.py`
//! (f4e366e9). macOS "Open Anyway" may launch the downloaded bundle itself, translocated and
//! without installing; a bare launch there must start nothing and say so. Only a bare launch is
//! judged: any explicit flag keeps its meaning. Pure and import-cheap; `crate::entry::route` calls it
//! first thing in the shell executable.
use std::path::Path;

pub const CODE: &str = "not_installed";
pub const DOWNLOADED_MESSAGE: &str = "This is the downloaded copy. Mycelium is installed: open Mycelium from the Applications folder in your home folder.";
pub const MESSAGE: &str = "Mycelium is not installed yet, so it did not start. Open the folder you downloaded and double-click 'Install Mycelium.command' (macOS asks once to confirm it in System Settings > Privacy & Security > Open Anyway). After that, open 'Mycelium' from the Applications folder in your home folder.";

/// `~/Library/Application Support/org.mycelium.app/programs` below `home` (ruling 25; v0.1.4's
/// is `Mycelium/programs`).
pub fn programs_root(home: &Path) -> std::path::PathBuf {
    home.join("Library/Application Support")
        .join(crate::install::SUPPORT_FOLDER)
        .join("programs")
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Location {
    Installed,
    Translocated,
    Elsewhere,
}
#[derive(Debug)]
pub struct Refusal {
    pub code: &'static str,
    pub location: Location,
    pub message: &'static str,
}
fn resolved(path: &Path) -> std::path::PathBuf {
    path.canonicalize().unwrap_or_else(|_| path.to_path_buf())
}
/// `Installed` below `programs_root` (links resolved), `Translocated` under `AppTranslocation`.
pub fn classify(executable: &Path, programs_root: &Path) -> Location {
    let executable = resolved(executable);
    if executable
        .components()
        .any(|c| c.as_os_str() == "AppTranslocation")
    {
        Location::Translocated
    } else if executable.starts_with(resolved(programs_root)) {
        Location::Installed
    } else {
        Location::Elsewhere
    }
}
fn bare(argv: &[String]) -> bool {
    // Finder passes `-psn_<serial>` on some macOS versions; that is not a request for any mode.
    argv.iter().skip(1).all(|a| a.starts_with("-psn_"))
}
/// The refusal for a bare launch outside the install, or `None`. No path appears in it.
pub fn not_installed_refusal(
    argv: &[String],
    executable: &Path,
    programs_root: &Path,
    applications_entry: Option<&Path>,
) -> Option<Refusal> {
    if !bare(argv) {
        return None;
    }
    let location = classify(executable, programs_root);
    if location == Location::Installed {
        return None;
    }
    let installed = applications_entry.is_some_and(|entry| {
        ["mycelium", "Mycelium A13 Demo"].iter().any(|name| {
            let target = entry.join("Contents/MacOS").join(name);
            target.is_file() && classify(&target, programs_root) == Location::Installed
        })
    });
    Some(Refusal {
        code: CODE,
        location,
        message: if installed {
            DOWNLOADED_MESSAGE
        } else {
            MESSAGE
        },
    })
}
