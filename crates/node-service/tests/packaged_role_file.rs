// SPDX-License-Identifier: AGPL-3.0-only
//! `installation-role.json` is validated as the shipped app validates it (`DemoApp.start`,
//! app.py:595-607 @ f4e366e9; chain: `DemoApp` app.py:505 -> `start()` -> `_load_private_json(root /
//! 'installation-role.json')`): a role file that is not exactly
//! `{"protocol":"mycelium.a13_installation_role.v1","role":"target"}` is `installation_role_invalid`;
//! a target role over an owner's authority state is `installation_role_conflict`; a target role
//! without the saved resource consent is `target_consent_missing`; a target's state without its role
//! file is `installation_role_missing`. The shipped app then refuses to start: the packaged host
//! keeps the authority refused (every authority action answers that code, nothing is created) and
//! its status never claims a role it could not read (review PH3a Q6 / F6).
#![cfg(feature = "desktop-shell")]
#[path = "support/packaged_macs.rs"]
mod macs;
use desktop::lifecycle::Poll;
use macs::{
    Mac, approve_and_join, consent, join_body, member, owner, private, state_root, test_network,
};
use node_service::packaged_network::NetworkConfig;
use serde_json::{Value, json};
use std::{
    os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt},
    path::Path,
};

const ROLE_PROTOCOL: &str = "mycelium.a13_installation_role.v1";

fn write_private(state: &Path, name: &str, bytes: &[u8]) {
    let dir = private(state);
    std::fs::DirBuilder::new()
        .mode(0o700)
        .recursive(true)
        .create(&dir)
        .unwrap();
    let file = dir.join(name);
    std::fs::write(&file, bytes).unwrap();
    std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o600)).unwrap();
}
/// Launch over `state`, then: the status's role, `/api/create`'s answer, whether a seed exists.
fn launch_and_create(state: &Path) -> (Value, (u16, Value), bool) {
    launch_and_create_with(state, 4_000_000_000)
}
/// [`launch_and_create`] with the consent's storage (a joined Mac's own saved consent).
fn launch_and_create_with(state: &Path, storage: u64) -> (Value, (u16, Value), bool) {
    let mac = Mac::launch(state, test_network());
    let role = mac.status()["installation_role"].clone();
    let created = mac.post("/api/create", json!({"consent": consent(storage)}));
    let seed = private(state).join("seed-identity.json").exists();
    assert_eq!(mac.quit(), Ok(Poll::Verified));
    (role, created, seed)
}
fn refused(created: &(u16, Value), code: &str) {
    assert_eq!(
        (created.0, created.1["error"].clone()),
        (409, json!(code)),
        "{}",
        created.1
    );
}

/// `{"protocol":…,"role":"owner"}` is not a silent owner that can create a seed (app.py:596-597).
#[test]
fn a_role_file_naming_owner_is_invalid() {
    let state = state_root("role-owner");
    write_private(
        &state,
        "installation-role.json",
        json!({"protocol": ROLE_PROTOCOL, "role": "owner"})
            .to_string()
            .as_bytes(),
    );
    let (role, created, seed) = launch_and_create(&state);
    std::fs::remove_dir_all(&state).unwrap_or_default();
    refused(&created, "installation_role_invalid");
    assert!(!seed, "a seed was created under an invalid role file");
    assert_eq!(role, Value::Null, "status role for an invalid role file");
}

/// `{"role":"target"}` without its protocol is not a target (app.py:596-597).
#[test]
fn a_role_file_without_its_protocol_is_invalid() {
    let state = state_root("role-noproto");
    write_private(&state, "installation-role.json", br#"{"role":"target"}"#);
    let (role, created, _) = launch_and_create(&state);
    std::fs::remove_dir_all(&state).unwrap_or_default();
    refused(&created, "installation_role_invalid");
    assert_eq!(role, Value::Null, "status role for an invalid role file");
}

/// A role file that cannot be read keeps its own code, and the status claims no role.
#[test]
fn an_unreadable_role_file_claims_no_role() {
    let state = state_root("role-trunc");
    write_private(
        &state,
        "installation-role.json",
        br#"{"protocol":"mycelium.a13_install"#,
    );
    let (role, created, seed) = launch_and_create(&state);
    std::fs::remove_dir_all(&state).unwrap_or_default();
    assert_eq!(created.0, 409, "{}", created.1);
    assert!(!seed);
    assert_eq!(role, Value::Null, "status role for an unreadable role file");
}

/// A valid target role without the saved resource consent (app.py:603-604).
#[test]
fn a_target_role_without_its_consent_is_refused() {
    let state = state_root("role-noconsent");
    write_private(
        &state,
        "installation-role.json",
        json!({"protocol": ROLE_PROTOCOL, "role": "target"})
            .to_string()
            .as_bytes(),
    );
    let (role, created, _) = launch_and_create(&state);
    std::fs::remove_dir_all(&state).unwrap_or_default();
    refused(&created, "target_consent_missing");
    assert_eq!(role, Value::Null);
}

/// A target role written over an owner that created its swarm (app.py:600-602).
#[test]
fn a_target_role_over_an_owners_swarm_conflicts() {
    let state = state_root("role-conflict");
    let mac = Mac::launch(&state, test_network());
    let created = mac.post("/api/create", json!({"consent": consent(4_000_000_000)}));
    assert_eq!(created.0, 200, "create: {}", created.1);
    assert_eq!(mac.quit(), Ok(Poll::Verified));
    write_private(
        &state,
        "installation-role.json",
        json!({"protocol": ROLE_PROTOCOL, "role": "target"})
            .to_string()
            .as_bytes(),
    );
    let (role, created, _) = launch_and_create(&state);
    std::fs::remove_dir_all(&state).unwrap_or_default();
    refused(&created, "installation_role_conflict");
    assert_eq!(role, Value::Null);
}

/// A joined Mac whose role file is gone (app.py:609-610 `installation_role_missing`): it is not
/// an owner that can create a seed.
#[test]
fn a_targets_state_without_its_role_file_is_refused() {
    let base = state_root("role-missing");
    let owner = owner(&base);
    let member = member(&base, &owner);
    let waiting = member.post("/api/join/window", join_body(&member, &owner.link, 600));
    assert_eq!(waiting.0, 200, "join: {}", waiting.1);
    approve_and_join(&owner, &member);
    let state = member.state.clone();
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    std::fs::remove_file(private(&state).join("installation-role.json")).unwrap();
    let (role, created, seed) = launch_and_create_with(&state, macs::STORAGE);
    std::fs::remove_dir_all(&base).unwrap_or_default();
    refused(&created, "installation_role_missing");
    assert!(!seed, "a joined Mac without its role file created a seed");
    assert_eq!(role, Value::Null);
}

/// Each installation file's name with its bytes and inode, `None` when absent.
type Files = Vec<(&'static str, Option<(Vec<u8>, u64)>)>;
/// The installation files the action routes persist (consent, role, join window, standing grant),
/// each with its bytes and inode (a rewrite with the same bytes is still a rewrite).
fn installation_files(state: &Path) -> Files {
    [
        "installation-role.json",
        "resource-policy.json",
        "standing-consent.json",
        "join-window.json",
    ]
    .into_iter()
    .map(|name| {
        let path = private(state).join(name);
        let file = std::fs::read(&path)
            .ok()
            .map(|bytes| (bytes, std::fs::metadata(&path).unwrap().ino()));
        (name, file)
    })
    .collect()
}

/// The shipped app does not start over a refused installation state (`DemoApp.start` raises,
/// app.py:595-611), so nothing can rewrite it. The packaged window stays up (B3F item 4, class C):
/// its action routes must leave the refused state as it is. `/api/create` and `/api/join/window`
/// persist the consent and the role before they reach the refused authority (review B3F 4
/// follow-up); a saved consent would even turn this refused state into an accepted target at the
/// next launch.
#[test]
fn a_refused_installation_state_is_not_rewritten_by_an_action() {
    let base = state_root("role-norewrite");
    let owner = owner(&base);
    let state = base.join("member");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&state)
        .unwrap();
    write_private(
        &state,
        "installation-role.json",
        json!({"protocol": ROLE_PROTOCOL, "role": "target"})
            .to_string()
            .as_bytes(),
    );
    let before = installation_files(&state);
    let mac = Mac::launch(
        &state,
        NetworkConfig {
            seams: owner.seams.clone(),
            ..test_network()
        },
    );
    let created = mac.post("/api/create", json!({"consent": consent(macs::STORAGE)}));
    let after_create = installation_files(&state);
    let joined = mac.post("/api/join/window", join_body(&mac, &owner.link, 600));
    let after_join = installation_files(&state);
    assert_eq!(mac.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    std::fs::remove_dir_all(&base).unwrap_or_default();
    let changed = |after: &Files| -> Vec<&'static str> {
        before
            .iter()
            .zip(after)
            .filter(|(was, now)| was != now)
            .map(|(was, _)| was.0)
            .collect()
    };
    assert_eq!(
        (changed(&after_create), changed(&after_join)),
        (vec![], vec![]),
        "files /api/create (answered {}) and /api/join/window (answered {}) rewrote over the \
         refused state",
        created.1,
        joined.1
    );
    refused(&created, "target_consent_missing");
    refused(&joined, "target_consent_missing");
}
