// SPDX-License-Identifier: AGPL-3.0-only
//! Archive assertions transcribed from test_device_zip.py; all app bytes are inert.
use device_harness::{Fault, FaultKind, Result, package::*};
mod support;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    io::{Cursor, Read},
};
fn package() -> Package {
    let sha = "a".repeat(40);
    let root = format!("/fixture/l2-final-{sha}");
    let bytes = b"FAKE EXECUTABLE".to_vec();
    Package {
        root: root.clone(),
        source_sha: sha.clone(),
        checkout: format!("{root}/checkout"),
        dirty_lines: 0,
        version: "0.1.0-preview.2".into(),
        executable_path: format!("{root}/package/out/0.1.0-preview.2/{APP}/{EXE}"),
        exe_sha256: digest(&bytes),
        entries: BTreeMap::from([
            (
                EXE.into(),
                Entry::File {
                    bytes,
                    executable: true,
                },
            ),
            (
                "Contents/alias".into(),
                Entry::Link("MacOS/Mycelium A13 Demo".into()),
            ),
        ]),
    }
}
#[derive(Default)]
struct Final {
    calls: usize,
    mutate: bool,
    fail: bool,
}
impl Finalizer for Final {
    fn finalize(&mut self, p: &Package, version: &str) -> Result<BTreeMap<String, Entry>> {
        self.calls += 1;
        if self.fail {
            return Err(Fault::new("finalizer_failed_rc2", FaultKind::Observation));
        }
        let mut entries: BTreeMap<_, _> = p
            .entries
            .iter()
            .map(|(k, v)| (format!("{APP}/{k}"), v.clone()))
            .collect();
        let bd = bundle_digest(&p.entries).unwrap();
        for (name, v) in [
            (
                "install-source.json",
                json!({"artifact_name":format!("mycelium-{version}-macos-arm64"),"artifact_digest":bd}),
            ),
            (
                "installation-package-manifest.json",
                json!({"source_revision":p.source_sha,"package_digest":bd,"signature":"FAKE-NOT-CRYPTOGRAPHIC-PROOF"}),
            ),
            (
                "artifact-identity.json",
                json!({"source_revision":p.source_sha,"version":version,"bundle_digest":bd}),
            ),
        ] {
            entries.insert(name.into(), Entry::file(serde_json::to_vec(&v).unwrap()));
        }
        entries.insert(
            "Install Mycelium.command".into(),
            Entry::file(b"INERT INSTALLER".to_vec()),
        );
        entries.insert(
            "APP-FILES.sha256".into(),
            Entry::file(format!("{}  {EXE}\n", p.exe_sha256).into_bytes()),
        );
        let sums = entries
            .iter()
            .filter(|(k, _)| !k.contains('/'))
            .map(|(k, v)| format!("{}  {k}\n", digest(v.bytes())))
            .collect::<String>();
        entries.insert("SHA256SUMS".into(), Entry::file(sums.into_bytes()));
        if self.mutate {
            entries.insert(format!("{APP}/{EXE}"), Entry::file(b"wrong".to_vec()));
        }
        Ok(entries)
    }
}
fn build(p: &Package, f: &mut Final) -> Artifact {
    build_device(p, "0.1.4", false, f).unwrap()
}
#[test]
fn layout_bytes_links_manifest_and_source_are_bound() {
    let p = package();
    let a = build(&p, &mut Final::default());
    assert!(a.name.ends_with("-DEVICE-TEST-NOT-FOR-RELEASE.zip"));
    assert_eq!(a.manifest["exe_sha256"], p.exe_sha256);
    let mut z = zip::ZipArchive::new(Cursor::new(&a.bytes)).unwrap();
    let prefix = "mycelium-0.1.4-macos-arm64/";
    let mut bytes = vec![];
    z.by_name(&format!("{prefix}{APP}/{EXE}"))
        .unwrap()
        .read_to_end(&mut bytes)
        .unwrap();
    assert_eq!(bytes, b"FAKE EXECUTABLE");
    let mut alias = z.by_name(&format!("{prefix}{APP}/Contents/alias")).unwrap();
    assert_eq!(alias.unix_mode().unwrap() & 0o170000, 0o120000);
    let mut body = String::new();
    alias.read_to_string(&mut body).unwrap();
    assert_eq!(body, "MacOS/Mycelium A13 Demo");
    drop(alias);
    let source: Value =
        serde_json::from_reader(z.by_name(&format!("{prefix}install-source.json")).unwrap())
            .unwrap();
    assert_eq!(source["artifact_name"], "mycelium-0.1.4-macos-arm64");
    let manifest: Value = serde_json::from_reader(
        z.by_name(&format!("{prefix}DEVICE-TEST-MANIFEST.json"))
            .unwrap(),
    )
    .unwrap();
    assert_eq!(manifest["package_root"], p.root);
    assert_eq!(manifest["source_sha"], p.source_sha);
    assert_eq!(manifest["release_qualified"], false);
    assert_eq!(manifest["pre_ready_device_test"], true);
    assert_eq!(p.entries[EXE].bytes(), b"FAKE EXECUTABLE");
    assert_eq!(
        verify_device(&a.name, &a.bytes, "0.1.4", &digest(&a.bytes), &p.exe_sha256).unwrap()["source_sha"],
        p.source_sha
    );
}
#[test]
fn hash_mismatch_refuses_before_finalizer() {
    let mut p = package();
    p.entries
        .insert(EXE.into(), Entry::file(b"CHANGED".to_vec()));
    let mut f = Final::default();
    assert_eq!(
        build_device(&p, "0.1.4", false, &mut f).unwrap_err().code,
        "package_exe_hash_mismatch"
    );
    assert_eq!(f.calls, 0);
}
#[test]
fn source_path_and_dirty_binding_refuse() {
    for (field, code) in [
        (0, "package_source_mismatch"),
        (1, "package_exe_path_invalid"),
        (2, "package_checkout_dirty"),
    ] {
        let mut p = package();
        match field {
            0 => p.source_sha = "b".repeat(40),
            1 => p.executable_path = "/fixture/outside".into(),
            _ => p.dirty_lines = 1,
        }
        assert_eq!(package_identity(&p).unwrap_err().code, code);
    }
}
#[test]
fn finalized_copy_mutation_refuses() {
    assert_eq!(
        build_device(
            &package(),
            "0.1.4",
            false,
            &mut Final {
                mutate: true,
                ..Final::default()
            }
        )
        .unwrap_err()
        .code,
        "finalized_exe_hash_mismatch"
    );
}
#[test]
fn same_bytes_requires_ready_and_passed_device_result() {
    let p = package();
    let a = build(&p, &mut Final::default());
    let ready = json!({"verdict":"READY","sha":p.source_sha,"package_root":p.root,"dry_run":false,"fire_ready":true,"exe":{"path":p.executable_path,"sha256":p.exe_sha256}});
    let passed = json!({"stranger_path_passed":true,"pre_ready_device_test":true,"source_sha":p.source_sha,"package_root":p.root,"exe_sha256":p.exe_sha256,"zip_sha256":digest(&a.bytes)});
    assert_eq!(
        check_ready(&p, &a.manifest, &ready, &passed).unwrap()["same_bytes"],
        true
    );
    for (key, val) in [
        ("verdict", json!("NOT_READY")),
        ("dry_run", json!(true)),
        (
            "exe",
            json!({"path":p.executable_path,"sha256":"f".repeat(64)}),
        ),
    ] {
        let mut bad = ready.clone();
        bad[key] = val;
        assert!(check_ready(&p, &a.manifest, &bad, &passed).is_err());
    }
    let mut bad = passed;
    bad["stranger_path_passed"] = json!(false);
    assert_eq!(
        check_ready(&p, &a.manifest, &ready, &bad).unwrap_err().code,
        "device_run_not_passed"
    );
}
#[test]
fn escaping_dangling_and_cyclic_links_refuse_before_finalizer() {
    for target in ["/etc/passwd", "../../outside", "missing", "evil"] {
        let mut p = package();
        p.entries
            .insert("Contents/evil".into(), Entry::Link(target.into()));
        let mut f = Final::default();
        assert_eq!(
            build_device(&p, "0.1.4", false, &mut f).unwrap_err().code,
            "bundle_link_invalid"
        );
        assert_eq!(f.calls, 0);
    }
}
#[test]
fn finalizer_failure_is_static_and_output_is_not_overwritten() {
    let p = package();
    let before = build(&p, &mut Final::default());
    let mut f = Final::default();
    assert_eq!(
        build_device(&p, "0.1.4", true, &mut f).unwrap_err().code,
        "device_output_already_spent"
    );
    assert_eq!(f.calls, 0);
    assert_eq!(before.bytes, build(&p, &mut Final::default()).bytes);
    assert_eq!(
        build_device(
            &p,
            "0.1.4",
            false,
            &mut Final {
                fail: true,
                ..Final::default()
            }
        )
        .unwrap_err()
        .code,
        "finalizer_failed_rc2"
    );
}
fn rewrite_archive(a: &Artifact, target: Option<&str>) -> Vec<u8> {
    let mut z = zip::ZipArchive::new(Cursor::new(&a.bytes)).unwrap();
    let mut writer = zip::ZipWriter::new(Cursor::new(vec![]));
    for i in 0..z.len() {
        let mut entry = z.by_index(i).unwrap();
        let mut body = vec![];
        entry.read_to_end(&mut body).unwrap();
        let name = entry.name().to_owned();
        let mode = entry.unix_mode().unwrap();
        if target.is_some_and(|t| name.ends_with(t)) {
            body = b"TAMPERED".to_vec();
        }
        let options = zip::write::FileOptions::default().unix_permissions(mode);
        if mode & 0o170000 == 0o120000 {
            writer
                .add_symlink(name, String::from_utf8(body).unwrap(), options)
                .unwrap();
        } else {
            writer.start_file(name, options).unwrap();
            std::io::Write::write_all(&mut writer, &body).unwrap();
        }
    }
    writer.finish().unwrap().into_inner()
}
#[test]
fn archive_metadata_tamper_is_rejected_without_destroying_symlink_control() {
    let p = package();
    let a = build(&p, &mut Final::default());
    let control = rewrite_archive(&a, None);
    assert!(verify_device(&a.name, &control, "0.1.4", &digest(&control), &p.exe_sha256).is_ok());
    for target in [
        "installation-package-manifest.json",
        "artifact-identity.json",
        "Install Mycelium.command",
        "APP-FILES.sha256",
        "SHA256SUMS",
    ] {
        let bad = rewrite_archive(&a, Some(target));
        assert!(
            verify_device(&a.name, &bad, "0.1.4", &digest(&bad), &p.exe_sha256).is_err(),
            "accepted changed {target}"
        );
    }
}
use budgets::Span;
use device_harness::{
    Host,
    effects::{Command, Output, Transport},
    package_driver::*,
};
#[derive(Default)]
struct FinalizerFake {
    calls: Vec<Vec<String>>,
    read_roots: Vec<String>,
    rc: i32,
    tree: BTreeMap<String, Entry>,
}
impl Transport for FinalizerFake {
    fn execute(&mut self, c: &Command, bound: Span) -> Result<Output> {
        assert_eq!(bound, Span::from_millis(600000));
        self.calls.push(
            std::iter::once(c.program.clone())
                .chain(c.args.clone())
                .collect(),
        );
        Ok(Output {
            rc: self.rc,
            stdout: "PRIVATE-OUTPUT".into(),
            stderr: "PRIVATE-ERROR".into(),
        })
    }
}
impl FinalizerIo for FinalizerFake {
    fn read_finalized_tree(&mut self, root: &str) -> Result<BTreeMap<String, Entry>> {
        self.read_roots.push(root.into());
        Ok(self.tree.clone())
    }
}
#[test]
fn command_finalizer_redacts_failure_and_never_invokes_candidate() {
    let p = package();
    for rc in [0, 2, 77] {
        let mut f = CommandFinalizer {
            io: FinalizerFake {
                rc,
                tree: Final::default().finalize(&p, "0.1.4").unwrap(),
                ..Default::default()
            },
            helper: "/fixture/rust-finalizer".into(),
            output: "/fixture/finalized".into(),
            bound: Span::from_millis(600000),
            failure: None,
        };
        let result = build_device(&p, "0.1.4", false, &mut f);
        assert_eq!(result.is_ok(), rc == 0);
        assert_eq!(f.io.calls.len(), 1);
        assert_eq!(
            f.io.calls[0],
            [
                "/fixture/rust-finalizer",
                "finalize",
                "--package-root",
                &p.root,
                "--source-sha",
                &p.source_sha,
                "--version",
                "0.1.4",
                "--output",
                "/fixture/finalized"
            ]
        );
        assert!(f.io.calls.iter().all(|a| a[0] != p.executable_path));
        if rc != 0 {
            assert_eq!(
                f.failure.as_ref().unwrap().to_string(),
                format!("finalizer_failed_rc{rc}")
            );
            assert!(!format!("{:?}", f.failure).contains("PRIVATE"));
            assert!(f.io.read_roots.is_empty());
        } else {
            assert_eq!(f.io.read_roots, ["/fixture/finalized"]);
        }
    }
    for helper in [
        &p.executable_path,
        "/usr/bin/python3",
        "/bin/sh",
        "/fixture/finalize.py",
    ] {
        let mut f = CommandFinalizer {
            io: FinalizerFake::default(),
            helper: helper.into(),
            output: "/fixture/out".into(),
            bound: Span::from_millis(600000),
            failure: None,
        };
        assert!(build_device(&p, "0.1.4", false, &mut f).is_err());
        assert!(f.io.calls.is_empty());
    }
}
struct StageFake {
    local: Vec<u8>,
    remote: Vec<u8>,
    manifest: Value,
    roots: std::collections::BTreeSet<String>,
    copies: usize,
    unpacks: Vec<Host>,
    tamper_copy: bool,
    tamper_readback: bool,
}
impl StageIo for StageFake {
    fn read_archive(&mut self, h: Host, _: &str) -> Result<Vec<u8>> {
        Ok(if h == Host::Owner {
            self.local.clone()
        } else {
            self.remote.clone()
        })
    }
    fn create_root(&mut self, h: Host, path: &str) -> Result<()> {
        if !self.roots.insert(format!("{h:?}:{path}")) {
            return Err(Fault::new("stage_root_exists", FaultKind::Admission));
        }
        Ok(())
    }
    fn copy_archive(&mut self, _: &str, _: &str, bound: Span) -> Result<()> {
        assert_eq!(bound, Span::from_millis(600000));
        self.copies += 1;
        self.remote = if self.tamper_copy {
            b"TAMPERED".to_vec()
        } else {
            self.local.clone()
        };
        Ok(())
    }
    fn unpack(&mut self, h: Host, bytes: &[u8], _: &str, bound: Span) -> Result<()> {
        assert_eq!(bound, Span::from_millis(180000));
        assert_eq!(bytes, self.local);
        self.unpacks.push(h);
        Ok(())
    }
    fn readback_manifest(&mut self, _: Host, _: &str) -> Result<Value> {
        let mut m = self.manifest.clone();
        if self.tamper_readback {
            m["source_sha"] = "b".repeat(40).into();
        }
        Ok(m)
    }
}
fn staged() -> (StageFake, DeviceInput) {
    let p = package();
    let a = build(&p, &mut Final::default());
    let manifest =
        verify_device(&a.name, &a.bytes, "0.1.4", &digest(&a.bytes), &p.exe_sha256).unwrap();
    let input = DeviceInput {
        path: format!("/fixture/{}", a.name),
        version: "0.1.4".into(),
        zip_sha256: digest(&a.bytes),
        exe_sha256: p.exe_sha256,
    };
    (
        StageFake {
            local: a.bytes,
            remote: vec![],
            manifest,
            roots: Default::default(),
            copies: 0,
            unpacks: vec![],
            tamper_copy: false,
            tamper_readback: false,
        },
        input,
    )
}
fn staging_limits() -> StageLimits {
    StageLimits {
        copy: Span::from_millis(600000),
        unpack: Span::from_millis(180000),
    }
}
#[test]
fn package_preparation_is_unique_hash_checked_on_both_hosts_and_read_back() {
    let (mut io, input) = staged();
    let first = prepare(&mut io, &input, "/fixture/staging", "one", staging_limits()).unwrap();
    let second = prepare(&mut io, &input, "/fixture/staging", "two", staging_limits()).unwrap();
    assert_ne!(first.owner_root, second.owner_root);
    assert_eq!(io.copies, 2);
    assert!(io.roots.contains("Owner:/fixture/staging/unpack-one"));
    assert!(io.roots.contains("Owner:/fixture/staging/unpack-two"));
    assert_eq!(
        io.unpacks,
        [Host::Owner, Host::Member, Host::Owner, Host::Member]
    );
    assert!(prepare(&mut io, &input, "/fixture/staging", "one", staging_limits()).is_err());
    assert_eq!(io.copies, 2);
    for control in [0, 1, 2] {
        let (mut io, mut input) = staged();
        if control == 0 {
            input.zip_sha256 = "0".repeat(64);
        } else if control == 1 {
            io.tamper_copy = true;
        } else {
            io.tamper_readback = true;
        }
        assert!(prepare(&mut io, &input, "/fixture/staging", "one", staging_limits()).is_err());
        if control == 0 {
            assert!(io.roots.is_empty());
            assert_eq!(io.copies, 0);
        }
        if control == 1 {
            assert!(io.unpacks.is_empty());
        }
    }
}
#[test]
fn device_option_and_manifest_gate_precede_effects_and_bind_failed_receipt() {
    let (mut io, input) = staged();
    let args = [
        "--device-test-zip",
        &input.path,
        "--version",
        &input.version,
        "--zip-sha256",
        &input.zip_sha256,
        "--exe-sha256",
        &input.exe_sha256,
    ]
    .map(String::from);
    assert_eq!(parse_device(&args).unwrap(), input);
    let mut host_calls = 0;
    let mut bad = input.clone();
    bad.exe_sha256 = "f".repeat(64);
    assert!(
        with_device(
            &mut io,
            &bad,
            "/fixture/staging",
            "bad",
            staging_limits(),
            |_| {
                host_calls += 1;
                Ok(json!({}))
            }
        )
        .is_err()
    );
    assert_eq!(host_calls, 0);
    assert!(io.roots.is_empty());
    let receipt=with_device(&mut io,&input,"/fixture/staging","good",staging_limits(), |p| {
        host_calls+=1;assert_eq!(p.candidate.source_sha,"a".repeat(40));
        Ok(json!({"stranger_path_passed":false,"error":"fixture_preflight_refusal","release_qualified":true,"source_sha":"forged"}))
    }).unwrap();
    assert_eq!(host_calls, 1);
    assert_eq!(receipt["pre_ready_device_test"], true);
    assert_eq!(receipt["source_sha"], "a".repeat(40));
    assert_eq!(receipt["package_root"], package().root);
    assert_eq!(receipt["exe_sha256"], input.exe_sha256);
    assert_eq!(receipt["zip_sha256"], input.zip_sha256);
    assert_eq!(receipt["release_qualified"], false);
    assert_eq!(receipt["stranger_path_passed"], false);
    let failed = with_device(
        &mut io,
        &input,
        "/fixture/staging",
        "callback-fails",
        staging_limits(),
        |_| {
            Err(Fault::new(
                "fixture_callback_failed",
                FaultKind::Observation,
            ))
        },
    )
    .unwrap();
    assert_eq!(failed["error"], "fixture_callback_failed");
    assert_eq!(failed["source_sha"], "a".repeat(40));
    assert_eq!(failed["package_root"], package().root);
    assert_eq!(failed["stranger_path_passed"], false);
    assert_eq!(failed["incomplete"], true);
    for bad in [
        vec!["--device-test-zip".into()],
        [args.to_vec(), vec!["--unknown".into(), "x".into()]].concat(),
        [args.to_vec(), vec!["--version".into(), "0.1.4".into()]].concat(),
    ] {
        assert!(parse_device(&bad).is_err());
    }
}

#[derive(Default)]
struct RefusingHost {
    effects: usize,
    snapshots: usize,
}
impl device_harness::observe::Observer for RefusingHost {
    fn status(&mut self, _: Host) -> Result<(u16, Value)> {
        self.snapshots += 1;
        Ok((200, json!({})))
    }
    fn metrics(&mut self, _: Host) -> Result<device_harness::observe::Metrics> {
        Err(Fault::new("unused_metrics", FaultKind::Observation))
    }
    fn mailbox(&mut self, _: Host) -> Result<device_harness::observe::Mailbox> {
        Err(Fault::new("unused_mailbox", FaultKind::Observation))
    }
}
impl device_harness::stranger::Adapter for RefusingHost {
    fn perform(
        &mut self,
        _: device_harness::stranger::Effect,
        _: Span,
    ) -> Result<device_harness::stranger::Reply> {
        self.effects += 1;
        Err(Fault::new("fixture_host_refusal", FaultKind::Admission))
    }
    fn runtime_guard(&mut self) -> Result<()> {
        Ok(())
    }
    fn battery_percentages(&self) -> [Option<u8>; 2] {
        [None; 2]
    }
}
#[derive(Default)]
struct PackageClock(u64);
impl device_harness::effects::Clock for PackageClock {
    fn now(&self) -> u64 {
        self.0
    }
    fn sleep(&mut self, s: Span) -> Result<()> {
        self.0 += s.millis();
        Ok(())
    }
}
#[test]
fn owned_device_stranger_persists_manifest_bound_failure_and_cleans_marker() {
    use std::fs;
    let scratch = support::PrivateRoot::new("package-owned");
    for invalid in [true, false] {
        let (mut stager, mut input) = staged();
        if invalid {
            input.exe_sha256 = "f".repeat(64);
        }
        let out = scratch.join(if invalid { "invalid" } else { "valid" });
        let markers = scratch.join(if invalid {
            "invalid-markers"
        } else {
            "valid-markers"
        });
        let identity = device_harness::preflight::ProcessIdentity {
            pid: std::process::id(),
            birth: "fixture birth".into(),
            executable: "fixture harness".into(),
        };
        let attempt = device_harness::attempt::Attempt::open(
            &out,
            &markers,
            "stranger",
            identity,
            device_harness::attempt::Parallel::Quiet,
            scratch.uid(),
        )
        .unwrap();
        let mut store = device_harness::private_fs::FileSnapshot {
            path: out.join("failure-snapshot.json"),
        };
        let mut host = RefusingHost::default();
        let value = device_harness::stranger::run_device_owned(
            &mut stager,
            &mut host,
            &mut store,
            &mut PackageClock::default(),
            device_harness::stranger::DeviceOptions {
                input: input.clone(),
                staging_root: "/fixture/staging".into(),
                attempt_id: "attempt".into(),
                staging_limits: staging_limits(),
                limits: device_harness::stranger::Limits {
                    total: Span::from_millis(3600000),
                    action: Span::from_millis(20000),
                    pair: Span::from_millis(300000),
                    serving: Span::from_millis(900000),
                    configuration: Span::from_millis(20000),
                    poll: Span::from_millis(1000),
                    relay_window: Span::from_millis(1800000),
                    member_wall: Span::from_millis(600000),
                    pair_window: Span::from_millis(600000),
                },
                relay: device_harness::stranger::Relay::Quick,
            },
            attempt,
        )
        .unwrap();
        assert_eq!(value["stranger_path_passed"], false);
        let saved: Value =
            serde_json::from_slice(&fs::read(out.join("RESULT.json")).unwrap()).unwrap();
        assert_eq!(saved, value);
        assert!(!markers.join("DEVICE.busy").exists());
        if invalid {
            assert_eq!(host.effects, 0);
            assert_eq!(host.snapshots, 0);
            assert!(stager.roots.is_empty());
        } else {
            assert_eq!(host.effects, 1);
            assert_eq!(host.snapshots, 2);
            assert_eq!(saved["pre_ready_device_test"], true);
            assert_eq!(saved["source_sha"], "a".repeat(40));
            assert_eq!(saved["package_root"], package().root);
            assert_eq!(saved["exe_sha256"], input.exe_sha256);
            assert_eq!(saved["error"], "fixture_host_refusal");
            assert_eq!(saved["release_qualified"], false);
        }
    }
}

#[test]
fn tampered_archive_executable_manifest_and_duplicate_path_refuse() {
    let p = package();
    let a = build(&p, &mut Final::default());
    for what in ["exe", "source", "duplicate", "escape"] {
        let mut z = zip::ZipArchive::new(Cursor::new(&a.bytes)).unwrap();
        let mut writer = zip::ZipWriter::new(Cursor::new(vec![]));
        for i in 0..z.len() {
            let mut entry = z.by_index(i).unwrap();
            let mut body = vec![];
            entry.read_to_end(&mut body).unwrap();
            let name = entry.name().to_owned();
            if what == "exe" && name.ends_with(EXE) {
                body = b"TAMPERED".to_vec();
            }
            if what == "source" && name.ends_with("DEVICE-TEST-MANIFEST.json") {
                let mut v: Value = serde_json::from_slice(&body).unwrap();
                v["source_sha"] = json!("NOT-A-SHA");
                body = serde_json::to_vec(&v).unwrap();
            }
            writer
                .start_file(&name, zip::write::FileOptions::default())
                .unwrap();
            std::io::Write::write_all(&mut writer, &body).unwrap();
            if what == "duplicate" && i == 0 {
                writer
                    .start_file(&name, zip::write::FileOptions::default())
                    .unwrap();
            }
        }
        if what == "escape" {
            writer
                .start_file("../evil", zip::write::FileOptions::default())
                .unwrap();
        }
        let bytes = writer.finish().unwrap().into_inner();
        assert!(
            verify_device(&a.name, &bytes, "0.1.4", &digest(&bytes), &p.exe_sha256).is_err(),
            "{what}"
        );
    }
}
