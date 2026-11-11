// SPDX-License-Identifier: AGPL-3.0-only
//! Round-14 relay against the EXECUTED oracle (`L10-evidence/r14-probe-relay.py` ->
//! `fixtures/oracle_relay.json`): the real `TemporaryHttpsRelay` (constructor / start / status /
//! lifetime step / close, both providers; the quick-tunnel provider runs the FAKE cloudflared
//! scripts the fixture carries - no real tunnel, no network), `provider_event`, and the real
//! `SeedRelayProxy` route table / framing / handler-result rules over a loopback socket.
use api::{
    relay::{
        BackendError, BackendReply, Hooks, LocalAnswer, LocalHandler, Proxy, Relay, RichAnswer,
        RichHandler, SeedBackend, provider_event,
    },
    wire::Json,
};
use serde_json::{Map, Value, json};
use std::{
    io::{Read, Write},
    net::{Shutdown, TcpListener, TcpStream},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

fn ms(millis: u64) -> Duration {
    Duration::from_millis(millis)
}
fn secs(seconds: u64) -> Duration {
    Duration::from_secs(seconds)
}
fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_relay.json")).expect("fixture")
}
fn scratch(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "relay-test-{tag}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.as_nanos())
    ));
    std::fs::create_dir_all(&dir).expect("scratch");
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o755)).expect("mode");
    dir
}
fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .expect("port")
}
/// Port numbers and the root are replaced by the fixture's tokens before comparing.
fn normalise(text: &str, ports: &[(&str, u16)], root: &Path) -> String {
    // a fake cloudflared prints `pwd`, the PHYSICAL path (`/private/var/...` for a `/var/...` temp dir)
    let physical = root
        .parent()
        .and_then(|p| p.canonicalize().ok())
        .zip(root.file_name())
        .map(|(p, name)| p.join(name));
    let mut out = text.to_owned();
    if let Some(physical) = physical {
        out = out.replace(physical.to_string_lossy().as_ref(), "@ROOT");
    }
    out = out.replace(root.to_string_lossy().as_ref(), "@ROOT");
    for (token, port) in ports {
        out = out.replace(&format!("127.0.0.1:{port}"), &format!("127.0.0.1:{token}"));
    }
    // the argv a fake cloudflared saw carries the proxy's own random port
    let mut result = String::new();
    let mut rest = out.as_str();
    while let Some(at) = rest.find("--url http://127.0.0.1:") {
        let (head, tail) = rest.split_at(at + "--url http://127.0.0.1:".len());
        result.push_str(head);
        let digits = tail.chars().take_while(char::is_ascii_digit).count();
        if digits > 0 {
            result.push_str("@PROXY");
        }
        rest = &tail[digits..];
    }
    result.push_str(rest);
    result
}
fn status_value(json: &Json) -> Value {
    json.to_value()
}

#[derive(Default)]
struct Sc {
    root: PathBuf,
    clock: Arc<Mutex<f64>>,
    serving: Arc<Mutex<u8>>, // 0 no, 1 yes, 2 raise
}

fn run_scenario(fx: &Value, sc: &Value) -> Vec<String> {
    let mut diffs = Vec::new();
    let id = sc["id"].as_str().unwrap_or("?");
    let dir = scratch(id);
    let root = dir.join("root");
    let state = Sc {
        root: root.clone(),
        clock: Arc::new(Mutex::new(1000.0)),
        serving: Arc::new(Mutex::new(0)),
    };
    let busy = TcpListener::bind("127.0.0.1:0").expect("busy");
    let mut busy = Some(busy);
    let busy_port = busy
        .as_ref()
        .and_then(|b| b.local_addr().ok())
        .map_or(0, |a| a.port());
    let port = free_port();
    let ports = [("@PORT", port), ("@PORT_BUSY", busy_port)];
    let sub = |v: &Value| -> Value {
        match v.as_str() {
            Some("@PORT") => json!(port),
            Some("@PORT_BUSY") => json!(busy_port),
            _ => v.clone(),
        }
    };
    // the fake cloudflared, byte for byte the script the oracle ran
    let fake = sc["fake"].as_str().map(|name| {
        let path = dir.join(format!("fake-{name}"));
        std::fs::write(&path, fx["fakes"][name].as_str().expect("fake")).expect("write fake");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).expect("chmod");
        path
    });
    let report = dir.join("report.txt");
    let mut env = vec![
        ("PATH".to_owned(), "/usr/bin:/bin".to_owned()),
        (
            "FAKE_REPORT".to_owned(),
            report.to_string_lossy().into_owned(),
        ),
    ];
    for (k, v) in sc["env"].as_object().into_iter().flatten() {
        env.push((k.clone(), v.as_str().unwrap_or_default().to_owned()));
    }
    let clock = state.clock.clone();
    let mut relay: Option<Relay> = None;
    for (n, entry) in sc["ops"].as_array().expect("ops").iter().enumerate() {
        let op = &entry["op"];
        let want = &entry["result"];
        let name = op["op"].as_str().unwrap_or("?");
        let got: Value = if relay.is_none()
            && !["new", "clock", "serving", "sleep", "free_busy"].contains(&name)
        {
            json!({"no_relay": true})
        } else {
            match name {
                "new" => {
                    let kwargs: Map<String, Value> = op["kwargs"]
                        .as_object()
                        .map(|m| m.iter().map(|(k, v)| (k.clone(), sub(v))).collect())
                        .unwrap_or_default();
                    let clock = clock.clone();
                    let binary = fake.clone();
                    let hooks = Hooks {
                        clock: Arc::new(move || *clock.lock().expect("clock")),
                        lifetime_poll: None,
                        // the oracle probe capped the wait at 1 s; only the scenarios that never see an
                        // origin need it short, the others get room for a loaded host
                        ready_wait: if ["quick_silent_exit", "quick_huge_line"].contains(&id) {
                            secs(1)
                        } else {
                            secs(30)
                        },
                        binary: Arc::new(move || binary.clone()),
                        env: env.clone(),
                        release_observer: None,
                    };
                    match Relay::new(&root, &op["provider"], &kwargs, hooks) {
                        Ok(r) => {
                            let serving = state.serving.clone();
                            r.set_serving_probe(Arc::new(move || {
                                match *serving.lock().expect("s") {
                                    2 => Err(()),
                                    v => Ok(v == 1),
                                }
                            }));
                            relay = Some(r);
                            json!({"ok": true})
                        }
                        Err(code) => {
                            relay = None;
                            json!({"error": code})
                        }
                    }
                }
                "start" => {
                    let r = relay.as_ref().expect("relay");
                    match r.start(&op["acknowledged"], &op["max_seconds"]) {
                        Ok(()) => status(r),
                        Err(code) => json!({"error": code}),
                    }
                }
                "clock" => {
                    *clock.lock().expect("clock") = op["t"].as_f64().unwrap_or(0.0);
                    json!({"ok": true})
                }
                "serving" => {
                    *state.serving.lock().expect("s") = match &op["value"] {
                        Value::Bool(true) => 1,
                        Value::String(_) => 2,
                        _ => 0,
                    };
                    json!({"ok": true})
                }
                "step" => {
                    let r = relay.as_ref().expect("relay");
                    r.step();
                    status(r)
                }
                "status" => status(relay.as_ref().expect("relay")),
                "close" => {
                    let r = relay.as_ref().expect("relay");
                    r.close();
                    status(r)
                }
                "sleep" => {
                    std::thread::sleep(Duration::from_secs_f64(
                        op["seconds"].as_f64().unwrap_or(0.0),
                    ));
                    json!({"ok": true})
                }
                "wait_error" => {
                    let r = relay.as_ref().expect("relay");
                    let t = Instant::now();
                    while r.error_code().is_none()
                        && t.elapsed()
                            < Duration::from_secs_f64(op["seconds"].as_f64().unwrap_or(0.0))
                    {
                        std::thread::sleep(ms(50));
                    }
                    status(r)
                }
                "free_busy" => {
                    busy = None;
                    json!({"ok": true})
                }
                "files" => json!({"files": files(&state.root)}),
                "report" => {
                    let text = std::fs::read_to_string(&report).unwrap_or_default();
                    let lines: Vec<Value> = text.lines().map(|l| json!(l)).collect();
                    let raw = serde_json::to_string(&lines).unwrap_or_default();
                    serde_json::from_str(&format!(
                        "{{\"report\":{}}}",
                        normalise(&raw, &ports, &state.root)
                    ))
                    .unwrap_or(Value::Null)
                }
                other => panic!("unknown op {other}"),
            }
        };
        // compare after the same normalisation the probe applied
        let got_text = normalise(
            &serde_json::to_string(&got).unwrap_or_default(),
            &ports,
            &state.root,
        );
        let want_text = normalise(
            &serde_json::to_string(want).unwrap_or_default(),
            &ports,
            &state.root,
        );
        let (got_n, want_n): (Value, Value) = (
            serde_json::from_str(&got_text).unwrap_or(Value::Null),
            serde_json::from_str(&want_text).unwrap_or(Value::Null),
        );
        if got_n != want_n {
            diffs.push(format!(
                "{id}#{n} {}: got {got_text:.400} want {want_text:.400}",
                op
            ));
        }
    }
    if let Some(r) = &relay {
        r.close();
    }
    drop(busy);
    std::fs::remove_dir_all(&dir).unwrap_or(());
    diffs
}
fn status(r: &Relay) -> Value {
    let json = r.status();
    json!({"status": status_value(&json), "keys": json.keys()})
}
fn files(root: &Path) -> Value {
    fn walk(base: &Path, dir: &Path, out: &mut Map<String, Value>) {
        let Ok(read) = std::fs::read_dir(dir) else {
            return;
        };
        for entry in read.flatten() {
            let path = entry.path();
            let Ok(meta) = std::fs::symlink_metadata(&path) else {
                continue;
            };
            let rel = path
                .strip_prefix(base)
                .map_or_else(|_| String::new(), |p| p.to_string_lossy().into_owned());
            let mut e = Map::new();
            e.insert(
                "mode".into(),
                json!(format!("0o{:o}", meta.permissions().mode() & 0o7777)),
            );
            e.insert("dir".into(), json!(meta.is_dir()));
            if !meta.is_dir() && rel == "relay-tail.log" {
                e.insert(
                    "content".into(),
                    json!(std::fs::read_to_string(&path).unwrap_or_default()),
                );
            }
            out.insert(rel, Value::Object(e));
            if meta.is_dir() {
                walk(base, &path, out);
            }
        }
    }
    let mut out = Map::new();
    walk(root, root, &mut out);
    Value::Object(out)
}

fn replay(filter: impl Fn(&str) -> bool) -> (usize, Vec<String>) {
    let fx = fixture();
    let mut total = 0;
    let mut diffs = Vec::new();
    for sc in fx["scenarios"].as_array().expect("scenarios") {
        if filter(sc["id"].as_str().unwrap_or("")) {
            total += 1;
            diffs.extend(run_scenario(&fx, sc));
        }
    }
    (total, diffs)
}
fn assert_clean(total: usize, expected: usize, diffs: Vec<String>) {
    assert_eq!(total, expected, "scenario count");
    assert!(
        diffs.is_empty(),
        "{} differences:\n{}",
        diffs.len(),
        diffs.join("\n")
    );
}

#[test]
fn constructor_validation_equals_the_oracle() {
    let (total, diffs) = replay(|id| id.starts_with("ctor_"));
    assert_clean(total, 33, diffs);
}
#[test]
fn start_argument_validation_equals_the_oracle() {
    let (total, diffs) = replay(|id| id.starts_with("start_args_"));
    assert_clean(total, 14, diffs);
}
#[test]
fn external_state_machine_and_lifetime_equal_the_oracle() {
    let (total, diffs) = replay(|id| id.starts_with("ext_"));
    assert_clean(total, 16, diffs);
}
#[test]
fn quick_tunnel_provider_with_a_fake_cloudflared_equals_the_oracle() {
    let (total, diffs) = replay(|id| id.starts_with("quick_"));
    assert_clean(total, 10, diffs);
}
#[test]
fn provider_event_equals_the_oracle() {
    let fx = fixture();
    let events = fx["events"].as_array().expect("events");
    assert_eq!(events.len(), 51);
    let mut bad = Vec::new();
    for e in events {
        let got = provider_event(
            e["provider"].as_str().unwrap_or(""),
            e["line"].as_str().unwrap_or(""),
        );
        let want = e["event"]["origin"].as_str().map(str::to_owned);
        if got != want {
            bad.push(format!(
                "{} {:?}: got {got:?} want {want:?}",
                e["provider"], e["line"]
            ));
        }
    }
    assert!(bad.is_empty(), "{}", bad.join("\n"));
}

// ------------------------------------------------------------------------------------------------ the proxy
fn raw_request(port: u16, data: &[u8], read_timeout: Duration) -> Value {
    let mut s = TcpStream::connect(("127.0.0.1", port)).expect("connect");
    s.set_read_timeout(Some(read_timeout)).expect("timeout");
    s.write_all(data).unwrap_or(());
    s.shutdown(Shutdown::Write).unwrap_or(());
    let mut buf = Vec::new();
    let mut chunk = [0u8; 65536];
    loop {
        match s.read(&mut chunk) {
            Ok(0) | Err(_) => break,
            Ok(n) => buf.extend_from_slice(&chunk[..n]),
        }
    }
    if buf.is_empty() {
        return json!({"closed_without_reply": true});
    }
    let split = buf
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .unwrap_or(buf.len());
    let head = String::from_utf8_lossy(&buf[..split]).into_owned();
    let body = if split + 4 <= buf.len() {
        &buf[split + 4..]
    } else {
        &[][..]
    };
    let mut lines = head.split("\r\n");
    let status: u64 = lines
        .next()
        .and_then(|l| l.split(' ').nth(1))
        .and_then(|c| c.parse().ok())
        .unwrap_or(0);
    let headers: Vec<Value> = lines
        .filter(|l| {
            let lower = l.to_ascii_lowercase();
            !lower.starts_with("server:") && !lower.starts_with("date:")
        })
        .map(|l| {
            let (k, v) = l.split_once(": ").unwrap_or((l, ""));
            json!([k, v])
        })
        .collect();
    let text = if body.len() < 2000 {
        json!(String::from_utf8_lossy(body))
    } else {
        json!(format!("LEN:{}", body.len()))
    };
    json!({"status": status, "headers": headers, "body": text, "body_len": body.len()})
}
type Calls = Arc<Mutex<Vec<Value>>>;
fn local_handler(calls: Calls) -> LocalHandler {
    Arc::new(move |method, path, body| {
        calls.lock().expect("calls").push(json!([
            "local",
            method,
            path,
            String::from_utf8_lossy(body)
        ]));
        let a = |status: i64, raw: &[u8], kind: Option<&str>| {
            Ok(LocalAnswer {
                status,
                raw: raw.to_vec(),
                kind: kind.map(str::to_owned),
            })
        };
        if path.ends_with("/html") {
            a(200, b"<p>x</p>", Some("text/html; charset=utf-8"))
        } else if path.ends_with("/svg") {
            a(200, b"<svg/>", Some("image/svg+xml"))
        } else if path.ends_with("/bad-type") {
            a(200, b"x", Some("text/plain"))
        } else if path.ends_with("/low") {
            a(199, b"{}", None)
        } else if path.ends_with("/high") {
            a(600, b"{}", None)
        } else if path.ends_with("/big") {
            a(200, &vec![b'z'; 1_048_577], None)
        } else if path.ends_with("/exact") {
            a(200, &vec![b'z'; 1_048_576], None)
        } else if path.ends_with("/raise") {
            Err(())
        } else if path.ends_with("/404") {
            a(404, br#"{"error":"gone"}"#, None)
        } else {
            a(200, br#"{"ok":"local"}"#, None)
        }
    })
}
fn picked(headers: &[(String, String)], names: &[&str]) -> Value {
    let mut m = Map::new();
    for (k, v) in headers {
        let lower = k.to_ascii_lowercase();
        if names.contains(&lower.as_str()) {
            m.insert(lower, json!(v));
        }
    }
    Value::Object(m)
}
fn chat_handler(calls: Calls) -> RichHandler {
    Arc::new(move |method, path, headers, body| {
        calls.lock().expect("calls").push(json!([
            "chat",
            method,
            path,
            String::from_utf8_lossy(body),
            picked(headers, &["cf-connecting-ip", "x-mycelium-chat", "x-a"])
        ]));
        // `/bad` returns a non-bytes body in the source: unrepresentable here, so it is a failing handler
        if path.ends_with("/raise") || path.ends_with("/bad") {
            return Err(());
        }
        if path.ends_with("/low") {
            return Ok(RichAnswer {
                status: 100,
                kind: "text/plain".into(),
                raw: b"x".to_vec(),
            });
        }
        Ok(RichAnswer {
            status: 200,
            kind: "text/html; charset=utf-8".into(),
            raw: b"<chat/>".to_vec(),
        })
    })
}
fn swarm_handler(calls: Calls) -> RichHandler {
    Arc::new(move |method, path, headers, body| {
        calls.lock().expect("calls").push(json!([
            "swarm",
            method,
            path,
            String::from_utf8_lossy(body),
            picked(headers, &["cf-connecting-ip", "x-a"])
        ]));
        if path.ends_with("/xml") {
            return Ok(RichAnswer {
                status: 200,
                kind: "application/xml".into(),
                raw: b"x".to_vec(),
            });
        }
        if path.ends_with("/raise") {
            return Err(());
        }
        Ok(RichAnswer {
            status: 202,
            kind: "application/json".into(),
            raw: br#"{"swarm":"ok"}"#.to_vec(),
        })
    })
}
/// The probe's TLS echo server, as a backend: records what the proxy forwarded.
struct Echo {
    seen: Arc<Mutex<Vec<Value>>>,
}
impl SeedBackend for Echo {
    fn request(
        &self,
        method: &str,
        path: &str,
        content_type: &str,
        body: &[u8],
    ) -> Result<BackendReply, BackendError> {
        let text = String::from_utf8_lossy(body).into_owned();
        self.seen.lock().expect("seen").push(json!({
            "method": method, "path": path,
            "body": if text.len() > 100 { format!("LEN:{}", text.len()) } else { text.clone() },
            "ctype": content_type,
        }));
        let (status, ctype, raw): (u16, &str, Vec<u8>) = match path {
            "/seed/big" => (200, "application/json", vec![b'x'; 1_048_577]),
            "/seed/fail" => (500, "application/json", br#"{"error":"nope"}"#.to_vec()),
            "/seed/text" => (201, "text/plain", b"plain".to_vec()),
            "/seed/exact" => (200, "application/json", vec![b'y'; 1_048_576]),
            _ => (
                200,
                "application/json",
                format!("{{\"echo\": {}, \"path\": {}}}", json!(text), json!(path)).into_bytes(),
            ),
        };
        Ok(BackendReply {
            status,
            content_type: ctype.into(),
            body: raw,
        })
    }
}
struct Failing(BackendError);
impl SeedBackend for Failing {
    fn request(&self, _: &str, _: &str, _: &str, _: &[u8]) -> Result<BackendReply, BackendError> {
        Err(self.0)
    }
}
fn hex_decode(text: &str) -> Vec<u8> {
    (0..text.len() / 2)
        .filter_map(|i| u8::from_str_radix(&text[2 * i..2 * i + 2], 16).ok())
        .collect()
}

fn request_bytes(case: &Value) -> Vec<u8> {
    if let Some(synth) = case.get("request_synth") {
        let mut out = hex_decode(synth["prefix_hex"].as_str().unwrap_or(""));
        let fill = synth["fill"]
            .as_str()
            .and_then(|f| f.bytes().next())
            .unwrap_or(b'q');
        out.extend(std::iter::repeat_n(
            fill,
            usize::try_from(synth["count"].as_u64().unwrap_or(0)).unwrap_or(0),
        ));
        return out;
    }
    hex_decode(case["request_hex"].as_str().unwrap_or(""))
}
fn proxy_group(
    prefix: &str,
    expected: usize,
    setup: impl Fn(&Proxy, Calls, Arc<Mutex<Vec<Value>>>),
    trust: bool,
) {
    let fx = fixture();
    let proxy = Proxy::start(0, trust).expect("proxy");
    let calls: Calls = Arc::new(Mutex::new(Vec::new()));
    let seen = Arc::new(Mutex::new(Vec::new()));
    setup(&proxy, calls.clone(), seen.clone());
    let mut total = 0;
    let mut diffs = Vec::new();
    for case in fx["proxy"].as_array().expect("proxy") {
        let label = case["label"].as_str().unwrap_or("");
        if !label.starts_with(prefix) {
            continue;
        }
        total += 1;
        let request = request_bytes(case);
        let (n0, s0) = (
            calls.lock().expect("c").len(),
            seen.lock().expect("s").len(),
        );
        let got = raw_request(proxy.port(), &request, secs(6));
        let got_calls: Vec<Value> = calls.lock().expect("c")[n0..].to_vec();
        let got_seen: Vec<Value> = seen.lock().expect("s")[s0..].to_vec();
        if got != case["result"] {
            diffs.push(format!(
                "{label}: got {got:.500} want {:.500}",
                case["result"]
            ));
        }
        if Value::Array(got_calls.clone()) != case["calls"] {
            diffs.push(format!(
                "{label}: calls got {got_calls:?} want {}",
                case["calls"]
            ));
        }
        if !case["backend_saw"].is_null() {
            let want: Vec<Value> = case["backend_saw"]
                .as_array()
                .map(|a| {
                    a.iter()
                        .map(|s| {
                            json!({"method": s["method"], "path": s["path"], "body": s["body"],
                                   "ctype": s["ctype"]})
                        })
                        .collect()
                })
                .unwrap_or_default();
            if got_seen != want {
                diffs.push(format!("{label}: backend saw {got_seen:?} want {want:?}"));
            }
        }
    }
    proxy.close();
    assert_clean(total, expected, diffs);
}

#[test]
fn proxy_with_nothing_bound_equals_the_oracle() {
    proxy_group("a_", 45, |_, _, _| {}, true);
}
#[test]
fn proxy_with_handlers_bound_trusting_the_edge_equals_the_oracle() {
    proxy_group("b_trust_", 39, bind_handlers, true);
}
#[test]
fn proxy_with_handlers_bound_not_trusting_the_edge_equals_the_oracle() {
    proxy_group("b_notrust_", 39, bind_handlers, false);
}
fn bind_handlers(p: &Proxy, calls: Calls, _: Arc<Mutex<Vec<Value>>>) {
    p.bind_local(Some(local_handler(calls.clone())));
    p.bind_chat(Some(chat_handler(calls.clone())));
    p.bind_swarm_entry(Some(swarm_handler(calls)));
}
#[test]
fn proxy_forwarding_to_the_seed_backend_equals_the_oracle() {
    proxy_group(
        "c_",
        12,
        |p, _, seen| p.bind_seed(Some(Arc::new(Echo { seen }))),
        true,
    );
}
#[test]
fn proxy_with_an_unreachable_seed_backend_is_a_502() {
    proxy_group(
        "d_dead",
        2,
        |p, _, _| p.bind_seed(Some(Arc::new(Failing(BackendError::Unavailable)))),
        true,
    );
}
#[test]
fn proxy_with_a_backend_whose_certificate_is_rejected_is_a_400() {
    // `ssl.SSLCertVerificationError` is a ValueError: the handler's first `except` clause answers it
    proxy_group(
        "d_untrusted",
        1,
        |p, _, _| p.bind_seed(Some(Arc::new(Failing(BackendError::CertificateRejected)))),
        true,
    );
}
#[test]
fn proxy_serves_sixteen_connections_and_drops_the_seventeenth() {
    let fx = fixture();
    let proxy = Proxy::start(0, true).expect("proxy");
    let held: Vec<TcpStream> = (0..16)
        .map(|_| TcpStream::connect(("127.0.0.1", proxy.port())).expect("held"))
        .collect();
    std::thread::sleep(ms(300));
    let cases: Vec<&Value> = fx["proxy"]
        .as_array()
        .expect("p")
        .iter()
        .filter(|c| c["label"].as_str().is_some_and(|l| l.starts_with("e_")))
        .collect();
    assert_eq!(cases.len(), 2);
    let request = hex_decode(cases[0]["request_hex"].as_str().unwrap_or(""));
    let got17 = raw_request(proxy.port(), &request, secs(3));
    assert_eq!(
        got17, cases[0]["result"],
        "the 17th connection is closed without a reply"
    );
    drop(held);
    std::thread::sleep(ms(500));
    let after = raw_request(proxy.port(), &request, secs(6));
    assert_eq!(
        after, cases[1]["result"],
        "slots are released with the connections"
    );
    proxy.close();
}
