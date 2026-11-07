// SPDX-License-Identifier: AGPL-3.0-only
use budgets::Span;
use device_harness::{
    Result,
    effects::Clock,
    evidence_inputs::{Content, Inputs, Pins},
    native_io::*,
    native_session::*,
    preflight::ProcessIdentity,
    stranger::Candidate,
};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, VecDeque},
    net::{Ipv4Addr, SocketAddrV4},
};
fn inputs() -> Inputs {
    let reference = |name: &str| json!({"path":format!("/fixture/{name}"),"path_base":"absolute","size_bytes":1,"sha256":"a".repeat(64)});
    let n=serde_json::to_vec(&json!({"schema":"mycelium.foundation.native-input-evidence.v1","rust_candidate":{"source_commit":"b".repeat(40)},"native_inputs":[reference("libmlxc.dylib"),reference("libmlx.dylib"),reference("libjaccl.dylib"),reference("mlx.metallib")]})).unwrap();
    let o=serde_json::to_vec(&json!({"schema":"mycelium.foundation.oracle-evidence.v1","rust_candidate_source":"b".repeat(40),"accepted_source":{"commit":"c".repeat(40)},"certified_executable":reference("oracle"),"published_zip":reference("oracle.zip")})).unwrap();
    let id = |b: &[u8]| Content {
        size_bytes: b.len() as u64,
        sha256: format!("{:x}", Sha256::digest(b)),
    };
    Inputs::parse(
        &n,
        &o,
        Pins {
            native: id(&n),
            oracle: id(&o),
        },
    )
    .unwrap()
}
fn binding() -> Binding {
    Binding {
        candidate: Candidate {
            version: "0.2.0".into(),
            source_sha: "b".repeat(40),
            zip_sha256: "c".repeat(64),
            exe_sha256: "d".repeat(64),
        },
        process: ProcessIdentity {
            pid: 42,
            birth: "fixture".into(),
            executable: "/fixture/Mycelium.app/Contents/MacOS/mycelium".into(),
        },
        launch_epoch_ms: 100,
        desktop_manifest_sha256: "e".repeat(64),
        native_inputs_sha256: inputs().native_identity().sha256.clone(),
    }
}
fn private(bytes: Vec<u8>) -> PrivateFile {
    PrivateFile {
        identity: FileIdentity {
            device: 1,
            inode: 2,
        },
        uid: 501,
        mode: 0o600,
        links: 1,
        regular: true,
        symlink: false,
        modified_ms: 100,
        bytes,
    }
}
struct Store {
    files: BTreeMap<Slot, PrivateFile>,
    writes: usize,
    stale: bool,
}
impl Store {
    fn new() -> Self {
        Self{files:[
    (Slot::Control,private(br#"{"port":49152,"credential":"native-fixture"}"#.to_vec())),
    (Slot::Launch,private(br#"<!doctype html><form method="post" action="http://127.0.0.1:49152/session"><input type="hidden" name="launch" value="launch-fixture"><button type="submit">Open private demo</button></form>"#.to_vec()))
].into(),writes:0,stale:false}
    }
}
impl Files for Store {
    fn observe(&mut self, _: Span) -> Result<Observation> {
        Ok(Observation {
            binding: binding(),
            control_modified_ms: 100,
            port: 49152,
        })
    }
    fn read(&mut self, slot: Slot, max: u64, _: Span) -> Result<Option<PrivateFile>> {
        Ok(self.files.get(&slot).map(|f| {
            assert!(f.bytes.len() as u64 <= max);
            PrivateFile {
                identity: f.identity,
                uid: f.uid,
                mode: f.mode,
                links: f.links,
                regular: f.regular,
                symlink: f.symlink,
                modified_ms: f.modified_ms,
                bytes: f.bytes.clone(),
            }
        }))
    }
    fn replace_session(&mut self, old: Option<FileIdentity>, bytes: &[u8], _: Span) -> Result<()> {
        assert_eq!(old, self.files.get(&Slot::Session).map(|f| f.identity));
        self.writes += 1;
        if !self.stale {
            self.files.insert(Slot::Session, private(bytes.to_vec()));
        }
        Ok(())
    }
}
struct Network {
    replies: VecDeque<Reply>,
    requests: Vec<Request>,
}
impl Network {
    fn new() -> Self {
        Self {
            replies: vec![
                Reply {
                    http: 200,
                    json: json!({"launch_file_ready":true}),
                    headers: vec![],
                },
                Reply {
                    http: 303,
                    json: json!(null),
                    headers: vec![(
                        "Set-Cookie".into(),
                        "a13session=session-fixture; HttpOnly; SameSite=Strict".into(),
                    )],
                },
                Reply {
                    http: 200,
                    json: json!({"authority":{"membership_state":"current"}}),
                    headers: vec![],
                },
            ]
            .into(),
            requests: vec![],
        }
    }
}
impl Wire for Network {
    fn send(&mut self, address: SocketAddrV4, request: Request, bound: Span) -> Result<Reply> {
        assert_eq!(address, SocketAddrV4::new(Ipv4Addr::LOCALHOST, 49152));
        assert!(!request.follow_redirects);
        assert!(bound > Span::ZERO);
        self.requests.push(request);
        Ok(self
            .replies
            .pop_front()
            .expect("no unexpected network effect"))
    }
}
struct Tick;
impl Clock for Tick {
    fn now(&self) -> u64 {
        100
    }
    fn sleep(&mut self, _: Span) -> Result<()> {
        panic!("status does not sleep")
    }
}
fn adapter() -> Adapter<NativeIo<Store, Network, Tick>, Tick> {
    Adapter::new(
        NativeIo::new(
            Store::new(),
            Network::new(),
            501,
            binding(),
            &inputs(),
            Tick,
            // Synthetic bounded provider configuration, not operational defaults.
            FileLimits {
                control: budgets::Bytes::new(4096),
                launch: budgets::Bytes::new(16384),
                session: budgets::Bytes::new(16384),
            },
        )
        .unwrap(),
        Tick,
        binding(),
    )
    .unwrap()
}
fn total() -> Span {
    use device_harness::harness_budgets::{L1, Registry};
    L1.span("HarnessOwnerStatusCheck").unwrap()
}
#[derive(Default)]
struct Stage {
    events: Vec<String>,
    copies: Vec<(device_harness::Host, String)>,
    bad_source: bool,
    bad_target: bool,
    spent: bool,
}
impl device_harness::native_staging::Io for Stage {
    fn source_identity(
        &mut self,
        source: &device_harness::evidence_inputs::Reference,
        _: Span,
    ) -> Result<Content> {
        self.events.push("source".into());
        let mut content = source.content.clone();
        if self.bad_source {
            content.size_bytes += 1;
        }
        Ok(content)
    }
    fn create_root(&mut self, host: device_harness::Host, _: &str, _: Span) -> Result<()> {
        self.events.push(format!("root:{host:?}"));
        if self.spent {
            return Err(device_harness::Fault::new(
                "root_spent",
                device_harness::FaultKind::Observation,
            ));
        }
        Ok(())
    }
    fn copy(
        &mut self,
        _: &device_harness::evidence_inputs::Reference,
        host: device_harness::Host,
        _: &str,
        relative: &str,
        _: Span,
    ) -> Result<()> {
        self.events.push("copy".into());
        self.copies.push((host, relative.into()));
        Ok(())
    }
    fn readback(
        &mut self,
        host: device_harness::Host,
        _: &str,
        relative: &str,
        _: Span,
    ) -> Result<Content> {
        assert_eq!(self.copies.last().unwrap(), &(host, relative.into()));
        self.events.push("readback".into());
        Ok(Content {
            size_bytes: if self.bad_target { 2 } else { 1 },
            sha256: "a".repeat(64),
        })
    }
}
#[test]
fn raw_native_staging_checks_full_closure_on_both_hosts_and_never_promotes_release() {
    let mut f = Stage::default();
    let receipt =
        device_harness::native_staging::stage(&mut f, &Tick, &inputs(), "/fixture/stage", total())
            .unwrap();
    assert_eq!(receipt.resources.len(), 8);
    assert!(!receipt.release_qualified);
    assert_eq!(
        receipt.native_inputs_sha256,
        inputs().native_identity().sha256
    );
    assert_eq!(
        f.events[..6],
        [
            "source",
            "source",
            "source",
            "source",
            "root:Owner",
            "root:Member"
        ]
    );
    assert_eq!(
        f.copies
            .iter()
            .filter(|(_, p)| p.ends_with("libjaccl.dylib"))
            .count(),
        2
    );
    for mode in ["source", "target", "spent"] {
        let mut f = Stage {
            bad_source: mode == "source",
            bad_target: mode == "target",
            spent: mode == "spent",
            ..Stage::default()
        };
        assert!(
            device_harness::native_staging::stage(
                &mut f,
                &Tick,
                &inputs(),
                "/fixture/stage",
                total()
            )
            .is_err()
        );
        assert_eq!(f.copies.len(), if mode == "target" { 1 } else { 0 });
        if mode == "source" {
            assert_eq!(f.events, vec!["source"]);
        }
    }
}
#[test]
fn concrete_file_adapter_renews_from_native_records_and_reads_back_atomic_session() {
    let mut a = adapter();
    let result = a.check(total()).unwrap();
    assert!(!result.receipt.release_qualified);
    assert_eq!(result.receipt.renewals, 1);
    assert_eq!(a.io.files.writes, 1);
    assert_eq!(
        a.io.wire
            .requests
            .iter()
            .map(|r| r.path)
            .collect::<Vec<_>>(),
        vec!["/native-session", "/session", "/api/status"]
    );
    assert_eq!(a.io.wire.requests[1].body, b"launch=launch-fixture");
    assert_eq!(
        a.io.wire.requests[2].headers["Cookie"],
        "a13session=session-fixture"
    );
    assert!(
        !serde_json::to_string(&result.receipt)
            .unwrap()
            .contains("fixture")
    );
}
#[test]
fn concrete_member_start_composes_native_files_session_and_wire_guards() {
    let mut a = adapter();
    a.io.wire.replies.push_back(Reply {
        http: 200,
        json: json!({"state":"running"}),
        headers: vec![],
    });
    let result =
        device_harness::member::perform(&mut a, device_harness::member::Action::Start, total())
            .unwrap();
    assert_eq!(result["state"], "running");
    assert_eq!(a.io.wire.requests.len(), 4);
    assert_eq!(a.io.wire.requests[3].path, "/api/start");
    assert_eq!(a.io.wire.requests[3].body, b"{}");
}
#[derive(Clone)]
struct SharedClock(std::rc::Rc<std::cell::Cell<u64>>);
impl Clock for SharedClock {
    fn now(&self) -> u64 {
        self.0.get()
    }
    fn sleep(&mut self, _: Span) -> Result<()> {
        panic!("no sleep")
    }
}
struct SlowStore {
    store: Store,
    clock: SharedClock,
    reads: usize,
}
impl Files for SlowStore {
    fn observe(&mut self, bound: Span) -> Result<Observation> {
        self.clock.0.set(self.clock.now() + bound.millis());
        self.store.observe(bound)
    }
    fn read(&mut self, slot: Slot, max: u64, bound: Span) -> Result<Option<PrivateFile>> {
        self.reads += 1;
        self.store.read(slot, max, bound)
    }
    fn replace_session(
        &mut self,
        old: Option<FileIdentity>,
        bytes: &[u8],
        bound: Span,
    ) -> Result<()> {
        self.store.replace_session(old, bytes, bound)
    }
}
#[test]
fn native_suboperations_cannot_reset_spent_deadline_before_private_read() {
    let clock = SharedClock(std::rc::Rc::new(std::cell::Cell::new(100)));
    let mut io = NativeIo::new(
        SlowStore {
            store: Store::new(),
            clock: clock.clone(),
            reads: 0,
        },
        Network::new(),
        501,
        binding(),
        &inputs(),
        clock,
        FileLimits {
            control: budgets::Bytes::new(4096),
            launch: budgets::Bytes::new(16384),
            session: budgets::Bytes::new(16384),
        },
    )
    .unwrap();
    assert_eq!(io.observe(total()).unwrap_err().code, "native_io_timeout");
    assert_eq!(io.files.reads, 0);
    assert!(io.wire.requests.is_empty());
}
#[test]
fn native_wire_rejects_host_proxy_and_credential_route_injection_before_send() {
    for header in [
        "Host",
        "hOsT",
        "Proxy-Authorization",
        "Authorization",
        "X-Native-Authorization",
    ] {
        let mut a = adapter();
        a.io.observe(total()).unwrap();
        let result = a.io.exchange(
            Request {
                port: 49152,
                path: "/api/status",
                method: "GET",
                headers: [(header.into(), "untrusted".into())].into(),
                body: vec![],
                follow_redirects: false,
            },
            total(),
        );
        assert!(result.is_err(), "{header}");
        assert!(a.io.wire.requests.is_empty());
    }
}
#[test]
fn unsafe_control_metadata_or_port_refuses_before_transport() {
    for mutation in [
        "uid", "mode", "links", "symlink", "regular", "stale", "port",
    ] {
        let mut a = adapter();
        let f = a.io.files.files.get_mut(&Slot::Control).unwrap();
        match mutation {
            "uid" => f.uid = 0,
            "mode" => f.mode = 0o644,
            "links" => f.links = 2,
            "symlink" => f.symlink = true,
            "regular" => f.regular = false,
            "stale" => f.modified_ms = 0,
            _ => f.bytes = br#"{"port":49153,"credential":"native-fixture"}"#.to_vec(),
        }
        assert!(a.check(total()).is_err(), "{mutation}");
        assert!(a.io.wire.requests.is_empty());
    }
}
#[test]
fn forged_form_or_stale_atomic_readback_cannot_produce_a_status() {
    for html in [
        r#"<form method="post" action="https://outside/session"><input type="hidden" name="launch" value="x"></form>"#,
        r#"<form method="post" action="http://127.0.0.1:49152/session"><input type="hidden" name="launch" value="x"><input type="hidden" name="launch" value="y"></form>"#,
        r#"<form method="get" action="http://127.0.0.1:49152/session"><input type="hidden" name="launch" value="x"></form>"#,
    ] {
        let mut a = adapter();
        a.io.files.files.get_mut(&Slot::Launch).unwrap().bytes = html.as_bytes().into();
        assert!(a.check(total()).is_err());
        assert_eq!(a.io.wire.requests.len(), 1);
    }
    let mut a = adapter();
    a.io.files.stale = true;
    assert!(a.check(total()).is_err());
    assert_eq!(a.io.wire.requests.len(), 2);
}
