// SPDX-License-Identifier: AGPL-3.0-only
//! Private preference files against the executed oracle (`L10-evidence/r6-oracle-prefs.txt`, made
//! by `r6-probe-prefs.py`, which runs authority.py `_atomic_private_json`/`_load_private_json`,
//! fast_mode.py and run_model.py from f4e366e9 on the shipped Python 3.14.7).
use api::preferences::{fast_mode, private_state, run_model};
use serde_json::{Map, Value, json};
use std::{
    fs,
    os::unix::fs::{MetadataExt, PermissionsExt, symlink},
    path::{Path, PathBuf},
};

fn root(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("l10-prefs-{tag}-{}", std::process::id()));
    if dir.exists() {
        fs::remove_dir_all(&dir).unwrap();
    }
    fs::create_dir_all(&dir).unwrap();
    dir
}
fn body(v: Value) -> Map<String, Value> {
    v.as_object().cloned().unwrap_or_default()
}
fn put(path: &Path, raw: &[u8], mode: u32) {
    if path.symlink_metadata().is_ok() {
        fs::remove_file(path).unwrap();
    }
    fs::write(path, raw).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
}
const GOOD: &[u8] = br#"{"enabled":true,"protocol":"mycelium.a13_fast_mode.v1"}"#;
fn refused(r: Result<Value, api::schema::PublicError>) -> (u16, &'static str) {
    let e = r.unwrap_err();
    (e.status, e.code)
}

#[test]
fn choose_writes_the_canonical_private_file_and_returns_the_saved_document() {
    let dir = root("choose");
    let file = dir.join(fast_mode::FILE);
    assert_eq!(
        fast_mode::document(&dir, None).unwrap(),
        json!({"enabled":false,"source":"default","locked":false})
    );
    assert_eq!(
        fast_mode::choose(&dir, true, false, None, &body(json!({"enabled":true}))).unwrap(),
        json!({"enabled":true,"source":"saved","locked":false})
    );
    assert_eq!(fs::read(&file).unwrap(), GOOD);
    let meta = fs::metadata(&file).unwrap();
    assert_eq!((meta.mode() & 0o7777, meta.nlink()), (0o600, 1));
    let first_inode = meta.ino();
    assert_eq!(
        fast_mode::choose(&dir, true, false, None, &body(json!({"enabled":false}))).unwrap(),
        json!({"enabled":false,"source":"saved","locked":false})
    );
    assert_eq!(
        fs::read(&file).unwrap(),
        br#"{"enabled":false,"protocol":"mycelium.a13_fast_mode.v1"}"#
    );
    assert_ne!(
        fs::metadata(&file).unwrap().ino(),
        first_inode,
        "atomic replace"
    );
    let leftovers: Vec<_> = fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    assert_eq!(leftovers, [fast_mode::FILE], "no temporary file survives");
    fs::remove_dir_all(dir).unwrap();
}
#[test]
fn choose_refusals_keep_the_oracle_precedence_and_status_classes() {
    let dir = root("order");
    let ok = body(json!({"enabled":true}));
    // DemoError => 400 closed code; builtin ValueError => 409 action_not_completed (app.py:2011-2037)
    assert_eq!(
        refused(fast_mode::choose(
            &dir,
            false,
            true,
            Some("on"),
            &body(json!({}))
        )),
        (400, "owner_role_required")
    );
    for bad in [
        json!({}),
        json!({"enabled":1}),
        json!({"enabled":true,"x":1}),
        json!({"enable":true}),
    ] {
        assert_eq!(
            refused(fast_mode::choose(&dir, true, true, Some("on"), &body(bad))),
            (400, "invalid_request")
        );
    }
    assert_eq!(
        refused(fast_mode::choose(&dir, true, true, Some("on"), &ok)),
        (400, "fast_mode_run_active")
    );
    assert_eq!(
        refused(fast_mode::choose(&dir, true, false, Some("on"), &ok)),
        (400, "fast_mode_locked_by_environment")
    );
    assert_eq!(
        refused(fast_mode::choose(&dir, true, false, Some("off"), &ok)),
        (400, "fast_mode_locked_by_environment")
    );
    assert_eq!(
        refused(fast_mode::choose(&dir, true, false, Some("maybe"), &ok)),
        (409, "action_not_completed")
    );
    assert_eq!(
        fast_mode::document(&dir, Some("on")).unwrap(),
        json!({"enabled":true,"source":"environment","locked":true})
    );
    assert_eq!(
        fast_mode::document(&dir, Some("off")).unwrap(),
        json!({"enabled":false,"source":"environment","locked":true})
    );
    assert_eq!(
        fast_mode::document(&dir, Some("maybe")).unwrap_err(),
        "token_path_activation_invalid"
    );
    assert!(
        !dir.join(fast_mode::FILE).exists(),
        "refusals write nothing"
    );
    fs::remove_dir_all(dir).unwrap();
}
#[test]
fn every_tampered_state_file_is_fast_mode_state_invalid() {
    let dir = root("tamper");
    let file = dir.join(fast_mode::FILE);
    let pretty =
        serde_json::to_vec_pretty(&serde_json::from_slice::<Value>(GOOD).unwrap()).unwrap();
    let big = format!("{{\"a\":\"{}\"}}", "x".repeat(4090)).into_bytes();
    let cases: Vec<(&str, Vec<u8>, u32)> = vec![
        ("mode 0644", GOOD.to_vec(), 0o644),
        ("mode 0640", GOOD.to_vec(), 0o640),
        ("empty", vec![], 0o600),
        ("pretty", pretty, 0o600),
        ("trailing newline", [GOOD, b"\n"].concat(), 0o600),
        (
            "extra key",
            br#"{"enabled":true,"extra":1,"protocol":"mycelium.a13_fast_mode.v1"}"#.to_vec(),
            0o600,
        ),
        (
            "wrong protocol",
            br#"{"enabled":true,"protocol":"x"}"#.to_vec(),
            0o600,
        ),
        (
            "enabled int",
            br#"{"enabled":1,"protocol":"mycelium.a13_fast_mode.v1"}"#.to_vec(),
            0o600,
        ),
        ("not an object", b"[1]".to_vec(), 0o600),
        ("invalid utf8", vec![0xff, 0xfe], 0o600),
        ("4097 bytes", big, 0o600),
        (
            "key order swapped",
            br#"{"protocol":"mycelium.a13_fast_mode.v1","enabled":true}"#.to_vec(),
            0o600,
        ),
        (
            "escaped",
            br#"{"enabled":true,"protocol":"mycelium.a13_fast_mode.v\u0031"}"#.to_vec(),
            0o600,
        ),
    ];
    for (name, raw, mode) in cases {
        put(&file, &raw, mode);
        assert_eq!(
            fast_mode::document(&dir, None).unwrap_err(),
            "fast_mode_state_invalid",
            "{name}"
        );
        assert_eq!(
            refused(fast_mode::choose(
                &dir,
                true,
                false,
                None,
                &body(json!({"enabled":false}))
            )),
            (409, "action_not_completed"),
            "{name}"
        );
        assert_eq!(
            fs::read(&file).unwrap(),
            raw,
            "{name}: a refusal leaves the file alone"
        );
    }
    put(&file, GOOD, 0o600);
    assert_eq!(
        fast_mode::document(&dir, None).unwrap(),
        json!({"enabled":true,"source":"saved","locked":false})
    );
    fs::remove_file(&file).unwrap();
    symlink(dir.join("elsewhere"), &file).unwrap();
    assert_eq!(
        fast_mode::document(&dir, None).unwrap_err(),
        "fast_mode_state_invalid"
    );
    assert_eq!(
        refused(fast_mode::choose(
            &dir,
            true,
            false,
            None,
            &body(json!({"enabled":true}))
        )),
        (409, "action_not_completed")
    );
    assert!(
        fs::symlink_metadata(&file)
            .unwrap()
            .file_type()
            .is_symlink()
    );
    fs::remove_file(&file).unwrap();
    put(&file, GOOD, 0o600);
    fs::hard_link(&file, dir.join("second-name")).unwrap();
    assert_eq!(
        fast_mode::document(&dir, None).unwrap_err(),
        "fast_mode_state_invalid"
    );
    fs::remove_dir_all(dir).unwrap();
}
#[test]
fn private_state_load_and_store_follow_authority_py() {
    let dir = root("state");
    let file = dir.join("x.json");
    assert_eq!(private_state::load(&file, 4096), Ok(None));
    private_state::store(&file, &json!({"b":1,"a":"é"}), 4096).unwrap();
    assert_eq!(fs::read(&file).unwrap(), "{\"a\":\"é\",\"b\":1}".as_bytes());
    assert_eq!(fs::metadata(&file).unwrap().mode() & 0o7777, 0o600);
    assert_eq!(
        private_state::load(&file, 4096).unwrap().unwrap().get("b"),
        Some(&json!(1))
    );
    // oversize writes are refused before anything is replaced
    assert_eq!(
        private_state::store(&file, &json!({"a":"x".repeat(100)}), 20),
        Err(private_state::StateError("authority_state_write_failed"))
    );
    assert_eq!(fs::read(&file).unwrap(), "{\"a\":\"é\",\"b\":1}".as_bytes());
    // a limit outside (0, 4 MiB] is a programming error with its own code
    assert_eq!(
        private_state::load(&file, 0),
        Err(private_state::StateError(
            "authority_state_size_limit_invalid"
        ))
    );
    assert_eq!(
        private_state::store(&file, &json!({}), 4 * 1024 * 1024 + 1),
        Err(private_state::StateError(
            "authority_state_size_limit_invalid"
        ))
    );
    // a missing directory fails the write, never creates it
    assert_eq!(
        private_state::store(&dir.join("no/such/x.json"), &json!({}), 4096),
        Err(private_state::StateError("authority_state_write_failed"))
    );
    fs::remove_dir_all(dir).unwrap();
}
#[test]
fn run_model_choice_persistence_matches_run_model_py() {
    let dir = root("run-model");
    assert_eq!(run_model::load_choice(&dir), Ok(None));
    run_model::save_choice(&dir, "qwen3-8b").unwrap();
    let file = dir.join(run_model::FILE);
    assert_eq!(
        fs::read(&file).unwrap(),
        br#"{"choice":"qwen3-8b","protocol":"mycelium.a13_run_model_choice.v1"}"#
    );
    assert_eq!(fs::metadata(&file).unwrap().mode() & 0o7777, 0o600);
    assert_eq!(run_model::load_choice(&dir), Ok(Some("qwen3-8b")));
    assert_eq!(
        run_model::CHOICES,
        ["a9-route", "qwen3-0.6b", "qwen3-8b", "qwen3-14b"]
    );
    // Integrator ruling (b): a builtin ValueError never leaks its message under 409; the wire
    // collapses it to action_not_completed exactly like document/choose (see run_model_errors).
    assert_eq!(
        run_model::save_choice(&dir, "gpt").unwrap_err(),
        api::schema::PublicError::new(409, "action_not_completed")
    );
    for raw in [
        &br#"{"choice":"zzz","protocol":"mycelium.a13_run_model_choice.v1"}"#[..],
        br#"{"choice":"qwen3-8b","extra":1,"protocol":"mycelium.a13_run_model_choice.v1"}"#,
        br#"{"choice":"qwen3-8b","protocol":"mycelium.a13_fast_mode.v1"}"#,
    ] {
        put(&file, raw, 0o600);
        assert_eq!(
            run_model::load_choice(&dir).unwrap_err(),
            api::schema::PublicError::new(409, "action_not_completed")
        );
    }
    fs::remove_dir_all(dir).unwrap();
}
