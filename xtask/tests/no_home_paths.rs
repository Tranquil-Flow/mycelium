// SPDX-License-Identifier: AGPL-3.0-only
//! `xtask no-home-paths`: personal home-folder paths are refused, placeholders are accepted, and the
//! repository itself holds none. Every rejected name is assembled at run time, so this file never
//! holds a real-looking home path itself.
use std::{collections::BTreeSet, path::PathBuf};
use xtask::no_home_paths::{PLACEHOLDERS, audit, current_user, parse_placeholders, scan};

fn placeholders() -> BTreeSet<String> {
    parse_placeholders("# comment\nShared\nmycelium-dev\n\nbuilder\n")
}
fn users(name: &str) -> String {
    ["/", "Users", "/", name].concat()
}
fn home(name: &str) -> String {
    ["/", "home", "/", name].concat()
}

#[test]
fn a_personal_home_path_is_refused_by_name_and_line() {
    let text = format!(
        "ok\ncd {}/Projects/x\n{}/.ssh/id\n",
        users("alice-mac"),
        home("bob")
    );
    let found = scan(text.as_bytes(), &placeholders(), None);
    assert_eq!(
        found,
        vec![
            (2, "home_path_users", "alice-mac".to_string()),
            (3, "home_path_home", "bob".to_string()),
        ]
    );
}

#[test]
fn placeholders_and_non_paths_are_accepted() {
    let text = format!(
        "{}/x {}/y {}/Projects /Users/ {} /iso{}relay.log {}base\n",
        users("Shared"),
        users("mycelium-dev"),
        users("builder"),
        "/Users",
        "/home/",
        "root/home/"
    );
    assert_eq!(scan(text.as_bytes(), &placeholders(), None), vec![]);
}

#[test]
fn the_running_account_is_refused_by_bare_name_unless_short_or_a_placeholder() {
    let who = ["car", "ol-", "dev"].concat();
    let text = format!("drwx------  3 {who}  staff  96 notes\n");
    assert_eq!(
        scan(text.as_bytes(), &placeholders(), Some(&who)),
        vec![(1, "current_user_name", who.clone())]
    );
    assert_eq!(scan(text.as_bytes(), &placeholders(), Some("dev")), vec![]);
    assert_eq!(
        scan(b"builder staff", &placeholders(), Some("builder")),
        vec![]
    );
    // A home path naming the account is reported once, as a home path.
    let line = format!("{}/x\n", users(&who));
    assert_eq!(
        scan(line.as_bytes(), &placeholders(), Some(&who)),
        vec![(1, "home_path_users", who)]
    );
}

#[test]
fn the_repository_holds_no_personal_home_path() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("workspace root")
        .to_path_buf();
    assert!(root.join(PLACEHOLDERS).is_file(), "{PLACEHOLDERS} missing");
    let (files, findings) = audit(&root, current_user().as_deref()).expect("audit runs");
    assert!(
        files > 100,
        "only {files} tracked files read: not a real work tree"
    );
    let shown: Vec<String> = findings
        .iter()
        .take(20)
        .map(|f| format!("{}:{}: {} {}", f.path, f.line, f.code, f.name))
        .collect();
    assert!(
        findings.is_empty(),
        "{} findings:\n{}",
        findings.len(),
        shown.join("\n")
    );
}
