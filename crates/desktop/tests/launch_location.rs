// SPDX-License-Identifier: AGPL-3.0-only
//! Where the packaged app runs from (port of `mycelium_a13_macos/launch_location.py` at
//! f4e366e9): a bare launch from anywhere but `programs/<version>` is not an install.
use desktop::launch_location::{self, Location};
use std::{fs, os::unix::fs::symlink, path::PathBuf};

fn scratch(name: &str) -> PathBuf {
    let p = std::env::temp_dir().join(format!("v02-l11-loc-{}-{name}", std::process::id()));
    if p.exists() {
        fs::remove_dir_all(&p).unwrap();
    }
    fs::create_dir_all(&p).unwrap();
    p
}
fn exe_in(root: &std::path::Path, version: &str) -> PathBuf {
    let exe = root
        .join(version)
        .join("Mycelium.app/Contents/MacOS/mycelium");
    fs::create_dir_all(exe.parent().unwrap()).unwrap();
    fs::write(&exe, b"x").unwrap();
    exe
}
fn args(list: &[&str]) -> Vec<String> {
    list.iter().map(|s| s.to_string()).collect()
}

#[test]
fn installed_translocated_and_elsewhere_are_told_apart() {
    let dir = scratch("classify");
    let programs = dir.join("programs");
    let inside = exe_in(&programs, "0.2.0");
    assert_eq!(
        launch_location::classify(&inside, &programs),
        Location::Installed
    );
    let translocated = PathBuf::from(
        "/private/var/folders/ab/AppTranslocation/0000-1111/d/Mycelium.app/Contents/MacOS/mycelium",
    );
    assert_eq!(
        launch_location::classify(&translocated, &programs),
        Location::Translocated
    );
    let downloaded = exe_in(&dir.join("Downloads/bundle"), "x");
    assert_eq!(
        launch_location::classify(&downloaded, &programs),
        Location::Elsewhere
    );
    // A link into the programs root counts by where it resolves, not by its own name.
    let link = dir.join("link");
    symlink(&inside, &link).unwrap();
    assert_eq!(
        launch_location::classify(&link, &programs),
        Location::Installed
    );
    // `programs-evil` is not below `programs`.
    let lookalike = exe_in(&dir.join("programs-evil"), "0.2.0");
    assert_eq!(
        launch_location::classify(&lookalike, &programs),
        Location::Elsewhere
    );
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn only_a_bare_launch_outside_the_install_is_refused() {
    let dir = scratch("bare");
    let programs = dir.join("programs");
    let inside = exe_in(&programs, "0.2.0");
    let outside = exe_in(&dir.join("dl"), "x");
    let refuse = |argv: &[&str], exe: &PathBuf| {
        launch_location::not_installed_refusal(&args(argv), exe, &programs, None)
    };
    assert!(refuse(&["mycelium"], &inside).is_none());
    let refused = refuse(&["mycelium"], &outside).unwrap();
    assert_eq!(refused.code, "not_installed");
    assert_eq!(refused.location, Location::Elsewhere);
    assert_eq!(refused.message, launch_location::MESSAGE);
    assert!(
        refuse(&["mycelium", "-psn_0_4242"], &outside).is_some(),
        "Finder serial is not a mode"
    );
    for flag in ["--install", "--headless", "--state-root", "-m"] {
        assert!(refuse(&["mycelium", flag], &outside).is_none(), "{flag}");
    }
    assert!(
        !refused.message.contains(dir.to_str().unwrap()),
        "no paths in the public message"
    );
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn a_downloaded_copy_points_at_the_installed_app_only_when_the_entry_really_resolves_there() {
    let dir = scratch("entry");
    let programs = dir.join("programs");
    let installed = exe_in(&programs, "0.2.0");
    let outside = exe_in(&dir.join("dl"), "x");
    let entry = dir.join("apps/Mycelium.app");
    fs::create_dir_all(dir.join("apps")).unwrap();
    let ask = |entry: Option<&std::path::Path>| {
        launch_location::not_installed_refusal(&args(&["mycelium"]), &outside, &programs, entry)
            .unwrap()
            .message
    };
    assert_eq!(ask(None), launch_location::MESSAGE);
    assert_eq!(ask(Some(&entry)), launch_location::MESSAGE, "missing entry");
    symlink(dir.join("nowhere"), &entry).unwrap();
    assert_eq!(ask(Some(&entry)), launch_location::MESSAGE, "broken entry");
    fs::remove_file(&entry).unwrap();
    symlink(outside.ancestors().nth(3).unwrap(), &entry).unwrap();
    assert_eq!(
        ask(Some(&entry)),
        launch_location::MESSAGE,
        "entry outside the programs root"
    );
    fs::remove_file(&entry).unwrap();
    symlink(installed.ancestors().nth(3).unwrap(), &entry).unwrap();
    assert_eq!(ask(Some(&entry)), launch_location::DOWNLOADED_MESSAGE);
    fs::remove_dir_all(dir).unwrap();
}
