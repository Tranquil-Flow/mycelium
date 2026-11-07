// SPDX-License-Identifier: AGPL-3.0-only
//! Concurrency parity of the single directory mutex against v0.1.4 (INTEGRATOR RULING 1).
//!
//! The source runs one unlocked `Directory.handle` per request thread (server.py
//! ThreadingHTTPServer, 32 slots). The Rust authority serialises requests instead. This is only an
//! internal choice if the *observable* outcomes under concurrent requests are the ones the source
//! produces. The expected tallies below were measured on the unmodified oracle (Python 3.14.7,
//! real `DirectoryServer` + `SqliteStore`, 32 concurrent connections, Ed25519 verification
//! stubbed): `L10-evidence/r6-oracle-conc.txt` (probe `r6-probe-oracle.py`, 15 trials each, every
//! trial identical for the order-independent scenarios).
use api::swarm::{
    core::{Config, Directory},
    limits::Limits,
    store::{SqliteStore, Store},
    wire::ed25519_verify,
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use ed25519_dalek::{Signer as _, SigningKey};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    sync::{Barrier, Mutex},
    thread,
};

const NOW: i64 = 1_800_000_000;
const THREADS: usize = 32;
const SW: &str = "sw-aaaaaaaaaaaaaaaaaaaa";

fn b64(raw: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(raw)
}
fn who(seed: u8) -> SigningKey {
    SigningKey::from_bytes(&[seed; 32])
}
fn key(sk: &SigningKey) -> String {
    format!("ed25519:{}", b64(&sk.verifying_key().to_bytes()))
}
fn sign(sk: &SigningKey, body: Value) -> Vec<u8> {
    let mut message = b"mycelium-swarm-sig-v1\n".to_vec();
    message.extend(serde_json::to_vec(&body).unwrap());
    let mut out = body;
    out["sig"] = json!(b64(&sk.sign(&message).to_bytes()));
    serde_json::to_vec(&out).unwrap()
}
fn record(sk: &SigningKey, id: &str, seq: u64) -> Vec<u8> {
    sign(
        sk,
        json!({"protocol":"mycelium.swarm.record.v1","swarm_id":id,"owner_key":key(sk),"seq":seq,"name":"Berlin coworking","model":"Qwen/Qwen3-0.6B","description":"","access_mode":"request","open_quota":null,"entry":null}),
    )
}
fn owner_query(sk: &SigningKey, id: &str, nonce: &str) -> Vec<u8> {
    sign(
        sk,
        json!({"protocol":"mycelium.swarm.owner_query.v1","swarm_id":id,"owner_key":key(sk),"at":NOW,"nonce":nonce}),
    )
}
fn directory(tag: &str) -> (Directory<SqliteStore>, std::path::PathBuf) {
    let dir = std::env::temp_dir().join(format!("l10-conc-{tag}-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let store = SqliteStore::open(&dir.join("directory.sqlite3")).unwrap();
    let d = Directory::new(
        store,
        Box::new(ed25519_verify),
        Box::new(|| NOW),
        Config {
            limits: Limits::source().unwrap(),
            allow_loopback_entries: false,
            allow_private_entries: false,
        },
    )
    .unwrap();
    (d, dir)
}
type Job = (&'static str, &'static str, Vec<u8>, String);
/// Fires every job from `THREADS` threads released together; returns the outcome per job index.
fn fire<S: Store>(d: &Directory<S>, jobs: &[Job]) -> Vec<(u16, String)> {
    let next = Mutex::new(0usize);
    let out = Mutex::new(vec![(0u16, String::new()); jobs.len()]);
    let gate = Barrier::new(THREADS);
    thread::scope(|s| {
        for _ in 0..THREADS {
            s.spawn(|| {
                gate.wait();
                loop {
                    let i = {
                        let mut n = next.lock().unwrap();
                        *n += 1;
                        *n - 1
                    };
                    let Some((method, path, body, client)) = jobs.get(i) else {
                        return;
                    };
                    let (status, value) = d.render(method, path, body, client).unwrap();
                    out.lock().unwrap()[i] = (status, String::from_utf8(value).unwrap());
                }
            });
        }
    });
    out.into_inner().unwrap()
}
fn tally(results: &[(u16, String)]) -> BTreeMap<String, usize> {
    let mut map = BTreeMap::new();
    for (status, body) in results {
        let class = match serde_json::from_str::<Value>(body)
            .ok()
            .and_then(|v| v.get("error").and_then(Value::as_str).map(str::to_string))
        {
            Some(code) => format!("{status}:{code}"),
            None => status.to_string(),
        };
        *map.entry(class).or_insert(0) += 1;
    }
    map
}
fn expect(pairs: &[(&str, usize)]) -> BTreeMap<String, usize> {
    pairs.iter().map(|(k, n)| (k.to_string(), *n)).collect()
}
fn cleanup(dir: std::path::PathBuf) {
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn identical_record_race_yields_the_oracle_tally() {
    // Oracle x15: {'200': 1, '409:record_seq_stale': 19, '429:rate_limited_ip': 2, '429:rate_limited_key': 10}
    let (d, dir) = directory("same-record");
    let sk = who(1);
    let body = record(&sk, SW, 1);
    let jobs: Vec<Job> = (0..32)
        .map(|_| ("POST", "/records", body.clone(), "ip:1".to_string()))
        .collect();
    assert_eq!(
        tally(&fire(&d, &jobs)),
        expect(&[
            ("200", 1),
            ("409:record_seq_stale", 19),
            ("429:rate_limited_ip", 2),
            ("429:rate_limited_key", 10),
        ])
    );
    cleanup(dir);
}
#[test]
fn nonce_replay_race_accepts_exactly_one() {
    // Oracle x15: {'200': 1, '409:nonce_replayed': 31}
    let (d, dir) = directory("nonce");
    let sk = who(2);
    assert_eq!(
        d.handle("POST", "/records", &record(&sk, SW, 1), "ip:setup")
            .unwrap()
            .0,
        200
    );
    let body = owner_query(&sk, SW, "nonce-race-0123456789");
    let jobs: Vec<Job> = (0..32)
        .map(|_| ("POST", "/owner-inbox", body.clone(), "ip:1".to_string()))
        .collect();
    assert_eq!(
        tally(&fire(&d, &jobs)),
        expect(&[("200", 1), ("409:nonce_replayed", 31)])
    );
    cleanup(dir);
}
#[test]
fn read_budget_race_admits_exactly_the_budget() {
    // Oracle x15: 300 concurrent GET /swarms from one address: {'200': 240, '429:rate_limited_ip': 60}
    let (d, dir) = directory("reads");
    let jobs: Vec<Job> = (0..300)
        .map(|_| ("GET", "/swarms", Vec::new(), "ip:1".to_string()))
        .collect();
    assert_eq!(
        tally(&fire(&d, &jobs)),
        expect(&[("200", 240), ("429:rate_limited_ip", 60)])
    );
    cleanup(dir);
}
#[test]
fn rising_sequence_race_is_a_serial_execution() {
    // Oracle x15 (varies by interleaving: 3..9 acceptances): the budget classes are fixed
    // (2 ip, 10 key) and the other 20 split into accepted/stale in some arrival order.
    // Any arrival order is a valid serial history; the stored result must equal that history.
    let (d, dir) = directory("ladder");
    let sk = who(3);
    let jobs: Vec<Job> = (1..=32)
        .map(|seq| ("POST", "/records", record(&sk, SW, seq), "ip:1".to_string()))
        .collect();
    let results = fire(&d, &jobs);
    let counts = tally(&results);
    assert_eq!(counts.get("429:rate_limited_ip"), Some(&2), "{counts:?}");
    assert_eq!(counts.get("429:rate_limited_key"), Some(&10), "{counts:?}");
    let accepted: Vec<u64> = results
        .iter()
        .enumerate()
        .filter(|(_, (s, _))| *s == 200)
        .map(|(i, _)| i as u64 + 1)
        .collect();
    let stale = counts.get("409:record_seq_stale").copied().unwrap_or(0);
    assert_eq!(accepted.len() + stale, 20, "{counts:?}");
    assert!(!accepted.is_empty());
    let (_, listing) = d.render("GET", "/swarms", b"", "ip:2").unwrap();
    let listing: Value = serde_json::from_slice(&listing).unwrap();
    let stored = listing["swarms"][0]["record"]["seq"].as_u64().unwrap();
    assert_eq!(stored, *accepted.iter().max().unwrap());
    // a stale answer is only ever given for a sequence not above the stored one
    for (i, (status, _)) in results.iter().enumerate() {
        if *status == 409 {
            assert!((i as u64) < stored);
        }
    }
    cleanup(dir);
}
#[test]
fn independent_writers_in_parallel_equal_the_serial_listing() {
    let ids: Vec<(String, SigningKey)> = (1..=32u8)
        .map(|n| (format!("sw-{}", format!("{n:02}").repeat(10)), who(n + 10)))
        .collect();
    let jobs: Vec<Job> = ids
        .iter()
        .enumerate()
        .map(|(n, (id, sk))| ("POST", "/records", record(sk, id, 1), format!("ip:{n}")))
        .collect();
    let (serial, d1) = directory("serial");
    for (method, path, body, client) in &jobs {
        assert_eq!(serial.handle(method, path, body, client).unwrap().0, 200);
    }
    let (parallel, d2) = directory("parallel");
    let results = fire(&parallel, &jobs);
    assert!(
        results.iter().all(|(s, _)| *s == 200),
        "{:?}",
        tally(&results)
    );
    assert_eq!(
        parallel.render("GET", "/swarms", b"", "ip:reader").unwrap(),
        serial.render("GET", "/swarms", b"", "ip:reader").unwrap()
    );
    // each parallel answer is byte-identical to the serial answer for the same document
    for (i, (_, body)) in results.iter().enumerate() {
        let (_, want) = {
            let (d, dir) = directory(&format!("one-{i}"));
            let r = d
                .render("POST", "/records", &jobs[i].2, &jobs[i].3)
                .unwrap();
            cleanup(dir);
            r
        };
        assert_eq!(body.as_bytes(), want.as_slice());
    }
    cleanup(d1);
    cleanup(d2);
}
