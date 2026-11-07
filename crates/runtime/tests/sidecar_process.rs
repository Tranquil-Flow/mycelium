// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-NET-09 (partial: bootstrap secret, endpoint secret, secure socket and loopback authorization equal the oracle's executed vectors).
//!
//! Process-boundary pieces of the v0.1.4 sidecar against vectors recorded by EXECUTING the
//! oracle's own `read_bootstrap_secret`, `parse_bootstrap_secret`, `read_endpoint_secret`,
//! `bind_secure_uds`, `SocketCleanup`, `same_effective_uid`, `authorize_loopback_client`,
//! `decode_owner_pid_table`, `parse_netstat_owner_rows`, `posix_ready_record`,
//! `loopback_ready_record`, `address_changed_line`, `await_advertised_address` and
//! `republish_address_changes` (`fixtures/sidecar-oracle-vectors.json`), plus two-process
//! tests for the inherited-pipe bootstrap and the local socket session.
#![cfg(all(feature = "iroh-adapter", unix))]
use iroh::{EndpointAddr, SecretKey};
use runtime::sidecar_process::{
    AddressLineSink, AddressReadiness, ConnectionOwnerTable, LoopbackListener, LoopbackRejection,
    SocketCleanup, TcpOwnerRow, address_changed_line, authorize_loopback_client,
    await_advertised_address, bind_secure_uds, decode_owner_pid_table, loopback_ready_record,
    parse_bootstrap_secret, parse_netstat_owner_rows, posix_ready_record, read_bootstrap_secret,
    read_endpoint_secret, republish_address_changes, same_effective_uid,
};
use serde_json::Value;
use std::{
    io::Write,
    net::{Ipv4Addr, SocketAddr, SocketAddrV4},
    os::{fd::OwnedFd, unix::fs::PermissionsExt},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::sync::watch;

fn oracle() -> Value {
    serde_json::from_str(include_str!("../fixtures/sidecar-oracle-vectors.json")).unwrap()
}
fn text(value: &Value) -> &str {
    value.as_str().unwrap()
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
}
fn pause(units: u64) -> Duration {
    Duration::from_millis(units)
}
/// A short private directory (socket paths are limited to ~100 bytes on macOS).
fn scratch(label: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("sp{}-{label}", std::process::id()));
    observed("std::fs::remove_dir_all", std::fs::remove_dir_all(&dir));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}
fn mode_of(path: &Path) -> Option<u32> {
    std::fs::symlink_metadata(path)
        .ok()
        .map(|m| std::os::unix::fs::MetadataExt::mode(&m) & 0o7777)
}
fn result_text<T>(
    result: Result<T, runtime::sidecar_process::SidecarError>,
    ok: impl Fn(&T) -> String,
) -> String {
    match result {
        Ok(value) => ok(&value),
        Err(error) => error.code().to_owned(),
    }
}

#[test]
fn error_codes_and_constants_are_the_oracles() {
    let codes = &oracle()["sidecar_error_codes"];
    use runtime::sidecar_process::SidecarError as E;
    for (name, error) in [
        ("InvalidBootstrapFd", E::InvalidBootstrapFd),
        ("BootstrapNotPipe", E::BootstrapNotPipe),
        ("InvalidBootstrapLength", E::InvalidBootstrapLength),
        ("Filesystem", E::Filesystem),
        ("SocketSecurity", E::SocketSecurity),
        ("SocketBind", E::SocketBind),
        ("EndpointBind", E::EndpointBind),
        ("EndpointSecretSecurity", E::EndpointSecretSecurity),
        ("InvalidNetworkMode", E::InvalidNetworkMode),
        ("ReadyOutput", E::ReadyOutput),
        ("Runtime", E::Runtime),
        ("ArtifactSource", E::ArtifactSource),
    ] {
        assert_eq!(error.code(), text(&codes[name]), "{name}");
    }
}

#[test]
fn bootstrap_secret_parsing_and_the_inherited_pipe_descriptor_match_the_oracle() {
    let v = oracle();
    let inputs: Vec<(&str, Vec<u8>)> = vec![
        ("exactly_32", vec![5; 32]),
        ("empty", vec![]),
        ("one", vec![5]),
        ("31", vec![5; 31]),
        ("33", vec![5; 33]),
        ("64", vec![5; 64]),
        ("32_plus_newline", [vec![5; 32], b"\n".to_vec()].concat()),
    ];
    for case in v["bootstrap_stdin_parse"].as_array().unwrap() {
        let name = text(&case["name"]);
        let bytes = &inputs.iter().find(|(n, _)| *n == name).unwrap().1;
        let got = result_text(
            parse_bootstrap_secret(std::io::Cursor::new(bytes.clone())),
            |s| format!("ok:{}", hex(s.as_bytes())),
        );
        assert_eq!(got, text(&case["result"]), "{name}");
    }
    // A reader that fails is refused like short input.
    struct Broken;
    impl std::io::Read for Broken {
        fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
            Err(std::io::Error::other("broken pipe"))
        }
    }
    assert_eq!(
        parse_bootstrap_secret(Broken).err().map(|e| e.code()),
        Some("invalid_bootstrap_length")
    );

    // Real pipe descriptors: each is consumed (closed) on success and on failure. Closure is
    // proven on the far end (the kernel, not a descriptor number another test could reuse).
    for case in v["bootstrap_fd"].as_array().unwrap() {
        let name = text(&case["name"]);
        let result = if let Some(pipe_case) = name.strip_prefix("pipe_") {
            let bytes = &inputs.iter().find(|(n, _)| *n == pipe_case).unwrap().1;
            let (read, write) = rustix::pipe::pipe().unwrap();
            let mut writer = std::fs::File::from(write);
            writer.write_all(bytes).unwrap();
            // EOF must be visible to the reader (a held-open writer would block it forever).
            drop(writer);
            result_text(read_bootstrap_secret(read), |s| {
                format!("ok:{}", hex(s.as_bytes()))
            })
        } else if name == "regular_file" {
            // A non-pipe: a socket pair stands in (the oracle used a regular file); the
            // peer sees EOF once the descriptor is consumed.
            let (ours, theirs) = std::os::unix::net::UnixStream::pair().unwrap();
            let got = result_text(read_bootstrap_secret(OwnedFd::from(ours)), |_| {
                "ok".to_owned()
            });
            let mut theirs = theirs;
            let mut byte = [0_u8; 1];
            assert_eq!(
                std::io::Read::read(&mut theirs, &mut byte).unwrap(),
                0,
                "non-pipe descriptor consumed"
            );
            got
        } else if name == "negative_fd" {
            // Raw descriptor numbers are adopted outside this crate (seam S-L9-2); the oracle
            // answers invalid_bootstrap_fd and, for a number that is not open, aborts.
            continue;
        } else {
            panic!("unknown case {name}")
        };
        assert_eq!(result, text(&case["result"]), "{name}");
    }
}

#[test]
fn endpoint_secret_files_follow_the_oracle_rules() {
    let dir = scratch("sec");
    let make = |name: &str, bytes: &[u8], mode: u32| -> PathBuf {
        let path = dir.join(name);
        std::fs::write(&path, bytes).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode)).unwrap();
        path
    };
    let good = make("secret-good", &[7; 32], 0o600);
    let paths: Vec<(&str, PathBuf)> = vec![
        ("good", good.clone()),
        ("mode_0644", make("m644", &[7; 32], 0o644)),
        ("mode_0660", make("m660", &[7; 32], 0o660)),
        ("mode_0700", make("m700", &[7; 32], 0o700)),
        ("short_31", make("s31", &[7; 31], 0o600)),
        ("long_33", make("l33", &[7; 33], 0o600)),
        ("symlink_to_good", {
            let link = dir.join("link");
            std::os::unix::fs::symlink(&good, &link).unwrap();
            link
        }),
        ("directory", {
            let d = dir.join("dir");
            std::fs::create_dir(&d).unwrap();
            std::fs::set_permissions(&d, std::fs::Permissions::from_mode(0o600)).unwrap();
            d
        }),
        ("missing", dir.join("nope")),
    ];
    for case in oracle()["endpoint_secret_file"].as_array().unwrap() {
        let name = text(&case["name"]);
        let path = &paths.iter().find(|(n, _)| *n == name).unwrap().1;
        let got = result_text(read_endpoint_secret(path), |s| {
            format!("ok:{}", hex(s.as_bytes()))
        });
        assert_eq!(got, text(&case["result"]), "{name}");
    }
    observed("std::fs::remove_dir_all", std::fs::remove_dir_all(dir));
}

#[test]
fn the_unix_socket_binder_enforces_the_oracle_ownership_and_mode_rules() {
    let rt = runtime();
    let _guard = rt.enter();
    let root = scratch("uds");
    let owned = root.join("owned-file");
    std::fs::write(&owned, [1u8; 32]).unwrap();
    std::fs::set_permissions(&owned, std::fs::Permissions::from_mode(0o600)).unwrap();
    let mut counter = 0;
    let mut dir_for = || {
        counter += 1;
        let dir = root.join(format!("u{counter}"));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    };
    for case in oracle()["bind_secure_uds"].as_array().unwrap() {
        let name = text(&case["name"]);
        let d = dir_for();
        let mut observed = serde_json::Map::new();
        let verdict = match name {
            "fresh_nested_parent" => {
                let socket = d.join("a/b/sock");
                let r = bind_secure_uds(&socket);
                observed.insert(
                    "parent_mode".into(),
                    mode_of(socket.parent().unwrap()).into(),
                );
                observed.insert("socket_mode".into(), mode_of(&socket).into());
                result_text(r.map(|_| ()), |()| "ok".to_owned())
            }
            "loose_parent_is_tightened" => {
                std::fs::set_permissions(&d, std::fs::Permissions::from_mode(0o755)).unwrap();
                let socket = d.join("sock");
                let r = bind_secure_uds(&socket);
                observed.insert("parent_mode".into(), mode_of(&d).into());
                observed.insert("socket_mode".into(), mode_of(&socket).into());
                result_text(r.map(|_| ()), |()| "ok".to_owned())
            }
            "regular_file_at_path" => {
                let socket = d.join("sock");
                std::fs::write(&socket, b"x").unwrap();
                let r = bind_secure_uds(&socket);
                observed.insert("file_survives".into(), socket.exists().into());
                observed.insert("parent_mode".into(), mode_of(&d).into());
                result_text(r.map(|_| ()), |()| "ok".to_owned())
            }
            "stale_socket_replaced" => {
                let socket = d.join("sock");
                std::mem::forget(bind_secure_uds(&socket).unwrap());
                let r = bind_secure_uds(&socket);
                observed.insert("socket_mode".into(), mode_of(&socket).into());
                result_text(r.map(|_| ()), |()| "ok".to_owned())
            }
            "symlink_at_path" => {
                let socket = d.join("sock");
                std::os::unix::fs::symlink(&owned, &socket).unwrap();
                let r = bind_secure_uds(&socket);
                observed.insert("target_untouched".into(), mode_of(&owned).into());
                result_text(r.map(|_| ()), |()| "ok".to_owned())
            }
            "parent_is_symlink" => {
                let real = d.join("real");
                std::fs::create_dir_all(&real).unwrap();
                std::os::unix::fs::symlink(&real, d.join("linked")).unwrap();
                let r = bind_secure_uds(&d.join("linked/sock"));
                observed.insert("real_parent_mode".into(), mode_of(&real).into());
                result_text(r.map(|_| ()), |()| "ok".to_owned())
            }
            "parent_is_file" => {
                std::fs::write(d.join("notadir"), b"x").unwrap();
                result_text(bind_secure_uds(&d.join("notadir/sock")).map(|_| ()), |()| {
                    "ok".to_owned()
                })
            }
            "path_over_sun_path" => result_text(
                bind_secure_uds(&d.join("x".repeat(120))).map(|_| ()),
                |()| "ok".to_owned(),
            ),
            "cleanup_removes_socket_only" => {
                let socket = d.join("sock");
                drop(bind_secure_uds(&socket).unwrap());
                observed.insert(
                    "socket_exists_after_listener_drop".into(),
                    socket.exists().into(),
                );
                drop(SocketCleanup(socket.clone()));
                observed.insert(
                    "socket_removed_by_cleanup".into(),
                    (!socket.exists()).into(),
                );
                let plain = d.join("plain");
                std::fs::write(&plain, b"x").unwrap();
                drop(SocketCleanup(plain.clone()));
                observed.insert("plain_file_kept".into(), plain.exists().into());
                "n/a".to_owned()
            }
            other => panic!("unknown oracle case {other}"),
        };
        if name != "cleanup_removes_socket_only" {
            assert_eq!(verdict, text(&case["result"]), "{name}");
        }
        for (key, value) in observed {
            assert_eq!(&case[&key], &value, "{name}: {key}");
        }
    }
    // The peer credential check on a real connection from this same process.
    rt.block_on(async {
        let socket = root.join("peer-sock");
        let listener = bind_secure_uds(&socket).unwrap();
        let client = tokio::net::UnixStream::connect(&socket).await.unwrap();
        let (server_side, _) = listener.accept().await.unwrap();
        assert_eq!(
            same_effective_uid(&server_side),
            oracle()["same_effective_uid_for_own_connection"]
                .as_bool()
                .unwrap()
        );
        drop(client);
    });
    drop(_guard);
    observed("std::fs::remove_dir_all", std::fs::remove_dir_all(root));
}

struct Fixed(std::io::Result<Vec<TcpOwnerRow>>);
impl ConnectionOwnerTable for Fixed {
    fn rows(&self) -> std::io::Result<Vec<TcpOwnerRow>> {
        match &self.0 {
            Ok(rows) => Ok(rows.clone()),
            Err(error) => Err(std::io::Error::new(error.kind(), "fake failure")),
        }
    }
}

#[test]
fn loopback_authorization_owner_tables_and_the_listener_match_the_oracle() {
    let client = SocketAddrV4::new(Ipv4Addr::LOCALHOST, 52_000);
    let server = SocketAddrV4::new(Ipv4Addr::LOCALHOST, 60_000);
    let other = SocketAddrV4::new(Ipv4Addr::LOCALHOST, 52_001);
    let remote = SocketAddrV4::new(Ipv4Addr::new(192, 0, 2, 1), 52_000);
    let loopback2 = SocketAddrV4::new(Ipv4Addr::new(127, 0, 0, 2), 52_000);
    let row = |l, r, p| TcpOwnerRow {
        local: l,
        remote: r,
        owning_pid: p,
    };
    let auth = |rows: std::io::Result<Vec<TcpOwnerRow>>, allowed: u32| {
        authorize_loopback_client(
            &Fixed(rows),
            SocketAddr::V4(client),
            SocketAddr::V4(server),
            allowed,
        )
        .map_or_else(|e| e.code(), |()| "ok")
    };
    for case in oracle()["loopback_authorize"].as_array().unwrap() {
        let name = text(&case["name"]);
        let got = match name {
            "owner_matches" => auth(
                Ok(vec![row(client, server, 4242), row(server, client, 7)]),
                4242,
            ),
            "naming_the_sidecar_pid" => auth(
                Ok(vec![row(client, server, 4242), row(server, client, 7)]),
                7,
            ),
            "foreign_owner" => auth(Ok(vec![row(client, server, 4242)]), 99),
            "mirror_row_only" => auth(Ok(vec![row(server, client, 4242)]), 4242),
            "other_client_port" => auth(Ok(vec![row(other, server, 4242)]), 4242),
            "two_owners" => auth(
                Ok(vec![row(client, server, 4242), row(client, server, 4243)]),
                4242,
            ),
            "duplicate_same_owner" => auth(
                Ok(vec![row(client, server, 4242), row(client, server, 4242)]),
                4242,
            ),
            "lookup_failed" => auth(Err(std::io::Error::other("denied")), 4242),
            "pid_zero" => auth(Ok(vec![row(client, server, 0)]), 0),
            "client_not_loopback" => authorize_loopback_client(
                &Fixed(Ok(vec![])),
                SocketAddr::V4(remote),
                SocketAddr::V4(server),
                4242,
            )
            .map_or_else(|e| e.code(), |()| "ok"),
            "server_not_localhost" => authorize_loopback_client(
                &Fixed(Ok(vec![])),
                SocketAddr::V4(client),
                SocketAddr::V4(remote),
                4242,
            )
            .map_or_else(|e| e.code(), |()| "ok"),
            "client_127_0_0_2" => authorize_loopback_client(
                &Fixed(Ok(vec![row(loopback2, server, 4242)])),
                SocketAddr::V4(loopback2),
                SocketAddr::V4(server),
                4242,
            )
            .map_or_else(|e| e.code(), |()| "ok"),
            "ipv6_client" => authorize_loopback_client(
                &Fixed(Ok(vec![])),
                SocketAddr::from((std::net::Ipv6Addr::LOCALHOST, 52_000)),
                SocketAddr::V4(server),
                4242,
            )
            .map_or_else(|e| e.code(), |()| "ok"),
            other => panic!("unknown oracle case {other}"),
        };
        assert_eq!(got, text(&case["result"]), "{name}");
    }
    assert_eq!(
        LoopbackRejection::ForeignOwner.code(),
        "loopback_owner_foreign"
    );

    let tables = &oracle()["owner_pid_table"];
    let describe = |bytes: &[u8]| match decode_owner_pid_table(bytes) {
        Ok(rows) => {
            serde_json::json!({"rows": rows.iter().map(|r| serde_json::json!({"local": r.local.to_string(), "remote": r.remote.to_string(), "pid": r.owning_pid})).collect::<Vec<_>>()})
        }
        Err(_) => serde_json::json!({"err": "invalid_data"}),
    };
    let valid = (0..text(&tables["valid_hex"]).len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text(&tables["valid_hex"])[i..i + 2], 16).unwrap())
        .collect::<Vec<u8>>();
    let mut truncated = valid.clone();
    truncated.truncate(valid.len() - 1);
    let mut lying = valid.clone();
    lying[..4].copy_from_slice(&u32::MAX.to_le_bytes());
    assert_eq!(describe(&valid), tables["valid"]);
    assert_eq!(describe(&truncated), tables["truncated"]);
    assert_eq!(describe(&lying), tables["lying_count"]);
    assert_eq!(describe(&[1, 0]), tables["too_short"]);
    assert_eq!(describe(&[0, 0, 0, 0]), tables["empty_zero_rows"]);
    assert_eq!(describe(&[]), tables["no_bytes"]);

    let netstat = &oracle()["netstat_rows"];
    let parsed = parse_netstat_owner_rows(text(&netstat["text"]));
    let shown: Vec<Value> = parsed.iter().map(|r| serde_json::json!({"local": r.local.to_string(), "remote": r.remote.to_string(), "pid": r.owning_pid})).collect();
    assert_eq!(Value::Array(shown), netstat["rows"]);

    let rt = runtime();
    let _guard = rt.enter();
    let listener = LoopbackListener::bind().unwrap();
    let expected = &oracle()["loopback_listener"];
    assert_eq!(
        listener.local_addr().ip().to_string(),
        text(&expected["ip"])
    );
    assert_eq!(
        listener.local_port() != 0,
        expected["port_nonzero"].as_bool().unwrap()
    );
    rt.block_on(async {
        let connection = tokio::net::TcpStream::connect(SocketAddr::V4(listener.local_addr()))
            .await
            .unwrap();
        let (_server, peer) = listener.accept().await.unwrap();
        assert_eq!(peer, connection.local_addr().unwrap());
    });
}

#[test]
fn ready_records_and_address_lines_are_byte_identical_to_the_oracles() {
    let v = oracle();
    let id = SecretKey::from_bytes(&[0x40; 32]).public();
    let relay: iroh::RelayUrl = "https://relay.example.invalid./".parse().unwrap();
    let addrs: Vec<(&str, EndpointAddr)> = vec![
        ("id_only", EndpointAddr::new(id)),
        (
            "one_ip",
            EndpointAddr::new(id).with_ip_addr(SocketAddr::from((Ipv4Addr::LOCALHOST, 4433))),
        ),
        (
            "ip_and_relay",
            EndpointAddr::new(id)
                .with_ip_addr(SocketAddr::from((Ipv4Addr::new(192, 0, 2, 7), 1)))
                .with_relay_url(relay),
        ),
        (
            "ipv6",
            EndpointAddr::new(id)
                .with_ip_addr(SocketAddr::from((std::net::Ipv6Addr::LOCALHOST, 7))),
        ),
    ];
    assert_eq!(id.to_string(), text(&v["endpoint_id"]));
    for case in v["ready_records"].as_array().unwrap() {
        let name = text(&case["name"]);
        let address = &addrs.iter().find(|(n, _)| *n == name).unwrap().1;
        let alpn = text(&v["alpn"]);
        assert_eq!(
            serde_json::to_string(address).unwrap(),
            text(&case["endpoint_addr_json"]),
            "{name}"
        );
        assert_eq!(
            posix_ready_record(&id.to_string(), address, alpn).unwrap(),
            text(&case["posix_ready"]),
            "{name}"
        );
        assert_eq!(
            loopback_ready_record(&id.to_string(), address, alpn, 61234).unwrap(),
            text(&case["loopback_ready"]),
            "{name}"
        );
        assert_eq!(
            address_changed_line(&id.to_string(), address),
            text(&case["address_changed"]),
            "{name}"
        );
    }
    assert_eq!(
        runtime::sidecar_process::event_line("local_peer_rejected"),
        "{\"event\":\"local_peer_rejected\"}"
    );
}

struct Collect(Arc<Mutex<Vec<String>>>);
impl AddressLineSink for Collect {
    fn emit(&mut self, line: &str) {
        self.0.lock().unwrap().push(line.to_owned());
    }
}

#[test]
fn the_address_readiness_gate_and_republisher_follow_the_oracle() {
    let v = oracle();
    let id = SecretKey::from_bytes(&[0x40; 32]).public();
    let relay: iroh::RelayUrl = "https://relay.example.invalid./".parse().unwrap();
    let id_only = EndpointAddr::new(id);
    let one_ip = EndpointAddr::new(id).with_ip_addr(SocketAddr::from((Ipv4Addr::LOCALHOST, 4433)));
    let with_relay = EndpointAddr::new(id)
        .with_ip_addr(SocketAddr::from((Ipv4Addr::new(192, 0, 2, 7), 1)))
        .with_relay_url(relay);
    let rt = runtime();
    let window = pause(150);
    let gate = |name: &str| {
        v["readiness_gate"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["name"] == name)
            .unwrap()
            .clone()
    };
    let check = |name: &str, readiness: AddressReadiness| {
        let case = gate(name);
        assert_eq!(format!("{readiness:?}"), text(&case["readiness"]), "{name}");
        assert_eq!(
            readiness.event(),
            case["event"].as_str().unwrap_or(readiness.event()),
            "{name}"
        );
        if let Some(reason) = case.get("reason") {
            assert_eq!(readiness.reason(), reason.as_str(), "{name}");
        }
    };
    rt.block_on(async {
        let (_tx, mut rx) = watch::channel(one_ip.clone());
        let (found, state) =
            await_advertised_address(&mut rx, false, std::future::ready(()), window).await;
        check("local_only_with_ip", state);
        assert_eq!(
            serde_json::to_string(&found).unwrap(),
            text(&gate("local_only_with_ip")["addr_json"])
        );
        let (_tx, mut rx) = watch::channel(id_only.clone());
        check(
            "local_only_no_addresses",
            await_advertised_address(&mut rx, false, std::future::ready(()), window)
                .await
                .1,
        );
        let (_tx, mut rx) = watch::channel(with_relay.clone());
        check(
            "relay_required_relay_present",
            await_advertised_address(&mut rx, true, std::future::ready(()), window)
                .await
                .1,
        );
        let (_tx, mut rx) = watch::channel(one_ip.clone());
        check(
            "relay_required_only_ip",
            await_advertised_address(&mut rx, true, std::future::pending(), window)
                .await
                .1,
        );
        let (tx, mut rx) = watch::channel(one_ip.clone());
        let later = with_relay.clone();
        let sender = tokio::spawn(async move {
            tokio::time::sleep(pause(40)).await;
            tx.send(later)
                .expect("the readiness gate is still listening");
        });
        let (found, state) =
            await_advertised_address(&mut rx, true, std::future::ready(()), pause(500)).await;
        check("relay_appears_inside_window", state);
        assert_eq!(
            serde_json::to_string(&found).unwrap(),
            text(&gate("relay_appears_inside_window")["addr_json"])
        );
        sender.await.unwrap();
        // The oracle returns Online when the sender is dropped even with no relay address
        // (wait_for_usable returns on a closed channel and the caller maps Ok -> Online).
        let (tx, mut rx) = watch::channel(one_ip.clone());
        drop(tx);
        check(
            "sender_dropped_before_relay",
            await_advertised_address(&mut rx, true, std::future::pending(), pause(500))
                .await
                .1,
        );

        // Republisher: the value `ready` carried is not repeated; real changes are, in order.
        let lines = Arc::new(Mutex::new(Vec::new()));
        let (tx, rx) = watch::channel(id_only.clone());
        let task = tokio::spawn(republish_address_changes(
            id.to_string(),
            rx,
            id_only.clone(),
            Collect(lines.clone()),
        ));
        for next in [
            &id_only,
            &one_ip,
            &one_ip,
            &with_relay,
            &with_relay,
            &id_only,
        ] {
            tokio::time::sleep(pause(30)).await;
            tx.send(next.clone())
                .expect("the republisher is still listening");
        }
        tokio::time::sleep(pause(60)).await;
        drop(tx);
        task.await
            .expect("the republisher ends when its sender drops");
        let got = lines.lock().unwrap().clone();
        let expected: Vec<String> = v["republish_lines"]
            .as_array()
            .unwrap()
            .iter()
            .map(|l| text(l).to_owned())
            .collect();
        assert_eq!(got, expected);
    });
    assert_eq!(
        (
            v["ready_address_constants"]["window_ms"].as_u64(),
            v["ready_address_constants"]["incomplete_reason"].as_str()
        ),
        (Some(3000), Some("relay_address_not_advertised"))
    );
}

/// A result whose failure is tolerated by design (a cleanup, a peer that already ended the
/// call): the outcome is printed to the test log, never dropped.
fn observed<T, E: std::fmt::Debug>(what: &str, result: Result<T, E>) {
    if let Err(error) = result {
        eprintln!("tolerated {what}: {error:?}");
    }
}
