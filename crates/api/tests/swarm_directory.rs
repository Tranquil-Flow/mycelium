// SPDX-License-Identifier: AGPL-3.0-only
//! Directory authority behaviour against tests/swarm_directory/test_directory_core.py,
//! test_directory_write_budgets.py and directory.py/docs.py/wire.py/store.py @ f4e366e9.
//! Documents are signed here with an independent canonicaliser (serde_json's sorted
//! compact output) so the production canonical bytes are not checked against themselves.
use api::swarm::{
    core::{Config, Directory, PRIVACY_NOTE},
    json::{J, dumps_response, dumps_stored, parse},
    limits::Limits,
    store::{MemoryStore, SqliteStore, Store},
    wire::{ed25519_verify, fingerprint},
};
use ed25519_dalek::{Signer as _, SigningKey};
use serde_json::{Value, json};
use std::sync::{
    Arc,
    atomic::{AtomicI64, Ordering},
};

const NOW: i64 = 1_800_000_000;
const SW: &str = "sw-aaaaaaaaaaaaaaaaaaaa";
pub struct Who(SigningKey);
impl Who {
    pub fn new(seed: u8) -> Self {
        Self(SigningKey::from_bytes(&[seed; 32]))
    }
    pub fn key(&self) -> String {
        let raw = self.0.verifying_key().to_bytes();
        format!("ed25519:{}", b64(&raw))
    }
}
fn b64(raw: &[u8]) -> String {
    use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
    URL_SAFE_NO_PAD.encode(raw)
}
fn sign(who: &Who, body: Value) -> Value {
    let mut message = b"mycelium-swarm-sig-v1\n".to_vec();
    message.extend(serde_json::to_vec(&body).unwrap()); // sorted, compact: independent canonical form
    let mut out = body;
    out["sig"] = json!(b64(&who.0.sign(&message).to_bytes()));
    out
}
fn record(who: &Who, id: &str, seq: u64, extra: Value) -> Value {
    let mut body = json!({"protocol":"mycelium.swarm.record.v1","swarm_id":id,"owner_key":who.key(),"seq":seq,"name":"Berlin coworking","model":"Qwen/Qwen3-0.6B","description":"","access_mode":"request","open_quota":null,"entry":null});
    for (k, v) in extra.as_object().unwrap() {
        body[k] = v.clone();
    }
    sign(who, body)
}
fn heartbeat(who: &Who, id: &str, status: &str, members: u64, model: Value, at: i64) -> Value {
    sign(
        who,
        json!({"protocol":"mycelium.swarm.heartbeat.v1","swarm_id":id,"signer_key":who.key(),"status":status,"members":members,"served_model":model,"at":at}),
    )
}
fn request(who: &Who, id: &str, note: &str, at: i64) -> Value {
    sign(
        who,
        json!({"protocol":"mycelium.swarm.access_request.v1","swarm_id":id,"requester_key":who.key(),"note":note,"at":at}),
    )
}
fn owner_query(who: &Who, id: &str, at: i64, nonce: &str) -> Value {
    sign(
        who,
        json!({"protocol":"mycelium.swarm.owner_query.v1","swarm_id":id,"owner_key":who.key(),"at":at,"nonce":nonce}),
    )
}
fn grantee_query(who: &Who, at: i64, nonce: &str) -> Value {
    sign(
        who,
        json!({"protocol":"mycelium.swarm.grantee_query.v1","grantee_key":who.key(),"at":at,"nonce":nonce}),
    )
}
fn grant(issuer: &Who, id: &str, grantee: &Who, at: i64, expires: i64) -> Value {
    sign(
        issuer,
        json!({"protocol":"mycelium.swarm.grant.v1","grant_id":"gr-AAAAAAAAAAAAAAAAAAAAAA","swarm_id":id,"grantee_key":grantee.key(),"issuer_key":issuer.key(),"issued_at":at,"expires_at":expires,"max_requests":5,"max_tokens":500}),
    )
}
fn deny(owner: &Who, id: &str, requester: &Who, at: i64) -> Value {
    sign(
        owner,
        json!({"protocol":"mycelium.swarm.deny.v1","swarm_id":id,"owner_key":owner.key(),"requester_key":requester.key(),"at":at}),
    )
}
fn offer(entry: &Who, id: &str, origin: &str, at: i64) -> Value {
    sign(
        entry,
        json!({"protocol":"mycelium.swarm.entry_offer.v1","swarm_id":id,"entry_key":entry.key(),"origin":origin,"at":at}),
    )
}
fn nonce(n: u32) -> String {
    format!("nonce-{n:011}-padding")
}
struct Env {
    dir: Directory<MemoryStore>,
    clock: Arc<AtomicI64>,
}
fn env_with(limits: Limits, loopback: bool) -> Env {
    let clock = Arc::new(AtomicI64::new(NOW));
    let c = clock.clone();
    let dir = Directory::new(
        MemoryStore::new(),
        Box::new(ed25519_verify),
        Box::new(move || c.load(Ordering::SeqCst)),
        Config {
            limits,
            allow_loopback_entries: loopback,
            allow_private_entries: false,
        },
    )
    .unwrap();
    Env { dir, clock }
}
fn env() -> Env {
    env_with(Limits::source().unwrap(), false)
}
impl Env {
    fn at(&self) -> i64 {
        self.clock.load(Ordering::SeqCst)
    }
    fn tick(&self, secs: i64) {
        self.clock.fetch_add(secs, Ordering::SeqCst);
    }
    fn raw(&self, method: &str, path: &str, body: &[u8], client: &str) -> (u16, Value) {
        let (status, value) = self.dir.handle(method, path, body, client).unwrap();
        (
            status,
            serde_json::from_str(&dumps_response(&value)).unwrap(),
        )
    }
    fn call(&self, method: &str, path: &str, body: Option<&Value>, client: &str) -> (u16, Value) {
        let raw = body.map_or(Vec::new(), |b| serde_json::to_vec(b).unwrap());
        self.raw(method, path, &raw, client)
    }
    fn post(&self, path: &str, body: &Value) -> (u16, Value) {
        self.call("POST", path, Some(body), "ip:1")
    }
    fn swarms(&self) -> Vec<Value> {
        self.call("GET", "/swarms", None, "ip:1").1["swarms"]
            .as_array()
            .unwrap()
            .clone()
    }
}
fn err(status: u16, code: &str) -> (u16, Value) {
    (status, json!({"error":code}))
}

#[test]
fn known_answers_for_canonical_fingerprint_stored_text_and_response_text() {
    // Generated independently by runs/v2/v02/L10-evidence/r5-kat-generator.py (stdlib json/hashlib).
    let key = format!("ed25519:{}", b64(&(0u8..32).collect::<Vec<_>>()));
    assert_eq!(key, "ed25519:AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8");
    assert_eq!(fingerprint(&J::Str(key)).unwrap(), "630D-CD29-66C4-3366");
    let doc = parse(r#"{"b":1,"a":["x",null,true],"c":{"z":"ä","y":"a/b"}}"#.as_bytes()).unwrap();
    assert_eq!(
        api::swarm::wire::canonical(&doc).unwrap(),
        r#"{"a":["x",null,true],"b":1,"c":{"y":"a/b","z":"ä"}}"#.as_bytes()
    );
    let row = J::obj(vec![
        (
            "record",
            J::obj(vec![
                ("name", J::str("Ünï ☃ 😀 \u{7f}\"\\\n")),
                ("a", J::Arr(vec![J::Int(1), J::Null, J::Bool(true)])),
            ]),
        ),
        ("n", J::Int(-2)),
    ]);
    assert_eq!(
        dumps_stored(&row),
        r#"{"n": -2, "record": {"a": [1, null, true], "name": "\u00dcn\u00ef \u2603 \ud83d\ude00 \u007f\"\\\n"}}"#
    );
    assert_eq!(
        parse(dumps_stored(&row).as_bytes()).unwrap().get("n"),
        Some(&J::Int(-2))
    );
    let listing = J::obj(vec![
        ("swarms", J::Arr(vec![])),
        ("privacy", J::str("x\"é")),
        ("now", J::Int(5)),
    ]);
    assert_eq!(
        dumps_response(&listing),
        "{\"swarms\":[],\"privacy\":\"x\\\"é\",\"now\":5}"
    );
}
#[test]
fn ed25519_primitive_matches_rfc8032_test_vector_1_and_rejects_tamper() {
    let public = hex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a");
    let signature = hex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    );
    assert!(ed25519_verify(&public, b"", &signature));
    assert!(!ed25519_verify(&public, b"x", &signature));
    assert!(!ed25519_verify(&public[..31], b"", &signature));
}
fn hex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}
#[test]
fn create_and_list_shows_every_swarm_with_public_fields() {
    let e = env();
    let (alice, bob) = (Who::new(1), Who::new(2));
    assert_eq!(e.post("/records", &record(&alice, SW, 1, json!({}))).0, 200);
    let invite = record(
        &bob,
        "sw-bbbbbbbbbbbbbbbbbbbb",
        1,
        json!({"access_mode":"invite"}),
    );
    assert_eq!(e.post("/records", &invite).0, 200);
    let rows = e.swarms();
    assert_eq!(rows.len(), 2); // invite-only is visible too
    let row = rows.iter().find(|r| r["swarm_id"] == SW).unwrap();
    assert_eq!(row["name"], "Berlin coworking");
    assert_eq!(
        row["owner_fingerprint"],
        fingerprint(&J::Str(alice.key())).unwrap()
    );
    assert_eq!(
        (
            row["status"].as_str(),
            row["members"].as_i64(),
            row["access_mode"].as_str()
        ),
        (Some("offline"), Some(0), Some("request"))
    );
    assert_eq!(row["privacy"], PRIVACY_NOTE);
    assert!(PRIVACY_NOTE.contains("seen by this swarm"));
    assert!(row["record"]["sig"].is_string() && row["record"]["owner_key"] == alice.key().as_str());
}
#[test]
fn unsigned_forged_and_tampered_records_are_refused_in_source_order() {
    let e = env();
    let (alice, mallory) = (Who::new(1), Who::new(3));
    let good = record(&alice, SW, 1, json!({}));
    let mut unsigned = good.clone();
    unsigned.as_object_mut().unwrap().remove("sig");
    assert_eq!(
        e.post("/records", &unsigned),
        err(400, "document_fields_invalid")
    );
    let mut tampered = good.clone();
    tampered["name"] = json!("Free GPUs");
    assert_eq!(e.post("/records", &tampered), err(403, "signature_invalid"));
    let mut forged = record(&mallory, SW, 1, json!({}));
    forged["owner_key"] = json!(alice.key());
    assert_eq!(e.post("/records", &forged), err(403, "signature_invalid"));
    assert!(e.swarms().is_empty());
    // field validation precedes the signature check
    let long = record(&alice, SW, 1, json!({"name":"x".repeat(49)}));
    assert_eq!(e.post("/records", &long), err(400, "name_invalid"));
}
#[test]
fn only_the_owner_updates_and_seq_must_grow() {
    let e = env();
    let (alice, mallory) = (Who::new(1), Who::new(3));
    assert_eq!(e.post("/records", &record(&alice, SW, 1, json!({}))).0, 200);
    assert_eq!(
        e.post("/records", &record(&mallory, SW, 1, json!({}))),
        err(403, "swarm_owner_mismatch")
    );
    assert_eq!(
        e.post(
            "/records",
            &record(&alice, SW, 1, json!({"name":"Renamed"}))
        ),
        err(409, "record_seq_stale")
    );
    assert_eq!(
        e.post(
            "/records",
            &record(&alice, SW, 2, json!({"name":"Renamed"}))
        )
        .0,
        200
    );
    assert_eq!(
        e.call("GET", &format!("/swarms/{SW}"), None, "ip:1").1["swarm"]["name"],
        "Renamed"
    );
}
#[test]
fn delete_by_owner_retires_the_id_and_removes_requests_offers_and_grants() {
    let e = env();
    let (alice, carol) = (Who::new(1), Who::new(4));
    let first = record(&alice, SW, 1, json!({}));
    e.post("/records", &first);
    e.post("/access-requests", &request(&carol, SW, "hi", NOW));
    let del = |who: &Who, seq: u64| {
        sign(
            who,
            json!({"protocol":"mycelium.swarm.delete.v1","swarm_id":SW,"owner_key":who.key(),"seq":seq}),
        )
    };
    assert_eq!(
        e.post("/delete", &del(&Who::new(9), 2)),
        err(403, "swarm_owner_mismatch")
    );
    assert_eq!(
        e.post("/delete", &del(&alice, 1)),
        err(409, "record_seq_stale")
    );
    assert_eq!(
        e.post("/delete", &del(&alice, 2)),
        (200, json!({"deleted":SW}))
    );
    assert!(e.swarms().is_empty());
    assert_eq!(e.post("/records", &first), err(409, "swarm_id_retired"));
    let mine = e
        .post("/my-grants", &grantee_query(&carol, NOW, &nonce(1)))
        .1;
    assert_eq!(mine, json!({"grants":[],"denied":[],"pending":[]}));
}
#[test]
fn heartbeat_status_serving_warming_expiry_stale_and_stranger() {
    let e = env();
    let (alice, entry, stranger) = (Who::new(1), Who::new(5), Who::new(6));
    let with_entry =
        json!({"entry":{"key":entry.key(),"origin":"https://quiet-river.trycloudflare.com"}});
    e.post("/records", &record(&alice, SW, 1, with_entry));
    let hb = heartbeat(&entry, SW, "warming", 2, Value::Null, e.at());
    assert_eq!(
        e.post("/heartbeat", &hb),
        (200, json!({"status":"warming"}))
    );
    assert_eq!(
        (
            e.swarms()[0]["status"].clone(),
            e.swarms()[0]["members"].clone()
        ),
        (json!("warming"), json!(2))
    );
    e.tick(10);
    let hb = heartbeat(&entry, SW, "serving", 2, json!("Qwen/Qwen3-0.6B"), e.at());
    e.post("/heartbeat", &hb);
    assert_eq!(e.swarms()[0]["status"], "serving");
    assert_eq!(e.swarms()[0]["served_model"], "Qwen/Qwen3-0.6B");
    assert_eq!(e.post("/heartbeat", &hb), err(409, "heartbeat_stale"));
    let bad = heartbeat(&stranger, SW, "serving", 9, Value::Null, e.at() + 1);
    assert_eq!(
        e.post("/heartbeat", &bad),
        err(403, "heartbeat_signer_not_authorized")
    );
    e.tick(Limits::source().unwrap().heartbeat_ttl + 1);
    let row = &e.swarms()[0];
    assert_eq!(
        (
            row["status"].as_str(),
            row["members"].as_i64(),
            row["served_model"].is_null()
        ),
        (Some("offline"), Some(0), true)
    );
    assert_eq!(
        row["last_seen_seconds"],
        Limits::source().unwrap().heartbeat_ttl + 1
    );
}
#[test]
fn heartbeat_with_skewed_clock_is_refused() {
    let e = env();
    let alice = Who::new(1);
    e.post("/records", &record(&alice, SW, 1, json!({})));
    let hb = heartbeat(&alice, SW, "serving", 1, Value::Null, e.at() + 3600);
    assert_eq!(e.post("/heartbeat", &hb), err(400, "clock_skew"));
}
#[test]
fn detaching_the_entry_drops_its_heartbeat_but_an_owner_heartbeat_survives() {
    let e = env();
    let (alice, entry) = (Who::new(1), Who::new(5));
    let attached =
        json!({"entry":{"key":entry.key(),"origin":"https://quiet-river.trycloudflare.com"}});
    e.post("/records", &record(&alice, SW, 1, attached));
    e.post(
        "/heartbeat",
        &heartbeat(&entry, SW, "serving", 3, Value::Null, e.at()),
    );
    assert_eq!(e.swarms()[0]["members"], 3);
    e.post("/records", &record(&alice, SW, 2, json!({})));
    assert_eq!(
        (
            e.swarms()[0]["status"].clone(),
            e.swarms()[0]["last_seen_seconds"].clone()
        ),
        (json!("offline"), Value::Null)
    );
    e.post(
        "/heartbeat",
        &heartbeat(&alice, SW, "serving", 1, Value::Null, e.at() + 1),
    );
    e.post("/records", &record(&alice, SW, 3, json!({"name":"Again"})));
    assert_eq!(e.swarms()[0]["status"], "serving");
}
#[test]
fn access_request_owner_inbox_grant_and_deny() {
    let e = env();
    let (alice, carol, dave) = (Who::new(1), Who::new(4), Who::new(7));
    e.post("/records", &record(&alice, SW, 1, json!({})));
    assert_eq!(
        e.post(
            "/access-requests",
            &request(&carol, SW, "hi, demo visitor", NOW)
        ),
        (200, json!({"state":"pending"}))
    );
    e.post("/access-requests", &request(&dave, SW, "", NOW));
    assert_eq!(
        e.post("/owner-inbox", &owner_query(&carol, SW, NOW, &nonce(1)))
            .0,
        403
    );
    let inbox = e.post("/owner-inbox", &owner_query(&alice, SW, NOW, &nonce(2)));
    assert_eq!(inbox.0, 200);
    let keys: Vec<_> = inbox.1["requests"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["requester_key"].as_str().unwrap().to_string())
        .collect();
    assert_eq!(keys.len(), 2);
    assert!(keys.contains(&carol.key()) && keys.contains(&dave.key()));
    assert_eq!(
        inbox.1["requests"][0]["requester_fingerprint"]
            .as_str()
            .unwrap()
            .len(),
        19
    );
    let g = grant(&alice, SW, &carol, NOW, NOW + 3600);
    assert_eq!(
        e.post("/grants", &g),
        (
            200,
            json!({"state":"granted","grant_id":"gr-AAAAAAAAAAAAAAAAAAAAAA"})
        )
    );
    assert_eq!(
        e.post("/deny", &deny(&alice, SW, &dave, NOW)),
        (200, json!({"state":"denied"}))
    );
    let mine = e.post("/my-grants", &grantee_query(&carol, NOW, &nonce(3)));
    assert_eq!(mine, (200, json!({"grants":[g],"denied":[],"pending":[]})));
    let states: Vec<_> = e
        .post("/owner-inbox", &owner_query(&alice, SW, NOW + 1, &nonce(4)))
        .1["requests"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| {
            (
                r["requester_key"].as_str().unwrap().to_string(),
                r["state"].as_str().unwrap().to_string(),
            )
        })
        .collect();
    assert!(
        states.contains(&(carol.key(), "granted".into()))
            && states.contains(&(dave.key(), "denied".into()))
    );
    let theirs = e
        .post("/my-grants", &grantee_query(&dave, NOW, &nonce(5)))
        .1;
    assert_eq!(theirs, json!({"grants":[],"denied":[SW],"pending":[]}));
    assert_eq!(
        e.post("/deny", &deny(&alice, SW, &Who::new(8), NOW)),
        err(404, "request_unknown")
    );
}
#[test]
fn owner_inbox_sorts_pending_first_then_newest_and_open_invite_modes_refuse_requests() {
    let e = env();
    let alice = Who::new(1);
    e.post("/records", &record(&alice, SW, 1, json!({})));
    let (a, b, c) = (Who::new(11), Who::new(12), Who::new(13));
    e.post("/access-requests", &request(&a, SW, "old", NOW - 30));
    e.post("/access-requests", &request(&b, SW, "new", NOW - 5));
    e.post("/access-requests", &request(&c, SW, "mid", NOW - 10));
    e.post("/deny", &deny(&alice, SW, &b, NOW));
    let notes: Vec<String> = e
        .post("/owner-inbox", &owner_query(&alice, SW, NOW, &nonce(1)))
        .1["requests"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["note"].as_str().unwrap().to_string())
        .collect();
    assert_eq!(notes, ["mid", "old", "new"]);
    let open = json!({"access_mode":"open","open_quota":{"max_requests":20,"max_tokens":4000,"ttl_seconds":86400}});
    e.post(
        "/records",
        &record(&alice, "sw-bbbbbbbbbbbbbbbbbbbb", 1, open),
    );
    e.post(
        "/records",
        &record(
            &alice,
            "sw-cccccccccccccccccccc",
            1,
            json!({"access_mode":"invite"}),
        ),
    );
    assert_eq!(
        e.post(
            "/access-requests",
            &request(&a, "sw-bbbbbbbbbbbbbbbbbbbb", "", NOW)
        ),
        err(409, "swarm_is_open")
    );
    assert_eq!(
        e.post(
            "/access-requests",
            &request(&a, "sw-cccccccccccccccccccc", "", NOW)
        ),
        err(403, "swarm_invite_only")
    );
    assert_eq!(
        e.post(
            "/access-requests",
            &request(&a, "sw-dddddddddddddddddddd", "", NOW)
        ),
        err(404, "swarm_unknown")
    );
}
#[test]
fn grant_deposit_must_be_owner_signed_unexpired_and_bounded() {
    let e = env();
    let (alice, mallory, carol) = (Who::new(1), Who::new(3), Who::new(4));
    e.post("/records", &record(&alice, SW, 1, json!({})));
    assert_eq!(
        e.post("/grants", &grant(&mallory, SW, &carol, NOW, NOW + 60)),
        err(403, "grant_issuer_not_owner")
    );
    assert_eq!(
        e.post("/grants", &grant(&alice, SW, &carol, NOW - 100, NOW - 50)),
        err(403, "grant_expired")
    );
    let long = grant(&alice, SW, &carol, NOW, NOW + 30 * 86400 + 1);
    assert_eq!(e.post("/grants", &long), err(400, "grant_lifetime_invalid"));
}
#[test]
fn query_replay_is_refused_and_expired_grants_disappear() {
    let e = env();
    let (alice, carol) = (Who::new(1), Who::new(4));
    e.post("/records", &record(&alice, SW, 1, json!({})));
    let query = owner_query(&alice, SW, NOW, &nonce(1));
    assert_eq!(e.post("/owner-inbox", &query).0, 200);
    assert_eq!(e.post("/owner-inbox", &query), err(409, "nonce_replayed"));
    e.post("/grants", &grant(&alice, SW, &carol, NOW, NOW + 100));
    assert_eq!(
        e.post("/my-grants", &grantee_query(&carol, NOW, &nonce(2)))
            .1["grants"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    e.tick(101);
    assert_eq!(
        e.post("/my-grants", &grantee_query(&carol, e.at(), &nonce(3)))
            .1["grants"],
        json!([])
    );
}
#[test]
fn entry_offer_then_owner_attaches_entry_and_expired_offers_hide() {
    let e = env();
    let (alice, mac) = (Who::new(1), Who::new(5));
    e.post("/records", &record(&alice, SW, 1, json!({})));
    let origin = "https://quiet-river.trycloudflare.com";
    let reply = e.post("/entry-offers", &offer(&mac, SW, origin, NOW));
    assert_eq!(
        reply,
        (
            200,
            json!({"state":"offered","entry_fingerprint":fingerprint(&J::Str(mac.key())).unwrap()})
        )
    );
    let inbox = e
        .post("/owner-inbox", &owner_query(&alice, SW, NOW, &nonce(1)))
        .1;
    assert_eq!(
        inbox["entry_offers"][0]["entry_fingerprint"],
        fingerprint(&J::Str(mac.key())).unwrap()
    );
    e.tick(Limits::source().unwrap().offer_ttl + 1);
    let later = e
        .post("/owner-inbox", &owner_query(&alice, SW, e.at(), &nonce(2)))
        .1;
    assert_eq!(later["entry_offers"], json!([]));
    let attached = record(
        &alice,
        SW,
        2,
        json!({"entry":{"key":mac.key(),"origin":origin}}),
    );
    assert_eq!(e.post("/records", &attached).0, 200);
    let row = &e.swarms()[0];
    assert_eq!(
        (
            row["entry_origin"].as_str(),
            row["entry_fingerprint"].as_str()
        ),
        (
            Some(origin),
            Some(fingerprint(&J::Str(mac.key())).unwrap().as_str())
        )
    );
}
#[test]
fn entry_origin_must_be_public_https_unless_loopback_enabled() {
    let e = env();
    let alice = Who::new(1);
    for origin in [
        "http://evil.example",
        "http://127.0.0.1:4555",
        "https://localhost",
        "https://-a.example.com",
        "https://a.b",
        "https://UPPER.example.com",
    ] {
        let bad = record(
            &alice,
            SW,
            1,
            json!({"entry":{"key":alice.key(),"origin":origin}}),
        );
        assert_eq!(
            e.post("/records", &bad),
            err(400, "entry_origin_invalid"),
            "{origin}"
        );
    }
    let dev = env_with(Limits::source().unwrap(), true);
    let local = record(
        &alice,
        SW,
        1,
        json!({"entry":{"key":alice.key(),"origin":"http://127.0.0.1:4555"}}),
    );
    assert_eq!(dev.post("/records", &local).0, 200);
    let zero = record(
        &alice,
        "sw-bbbbbbbbbbbbbbbbbbbb",
        1,
        json!({"entry":{"key":alice.key(),"origin":"http://127.0.0.1:0555"}}),
    );
    assert_eq!(
        dev.post("/records", &zero),
        err(400, "entry_origin_invalid")
    );
}
#[test]
fn size_caps_owner_cap_and_directory_cap() {
    let limits = Limits {
        max_swarms: 3,
        max_swarms_per_owner: 2,
        ..Limits::source().unwrap()
    };
    let e = env_with(limits, false);
    let alice = Who::new(1);
    assert_eq!(
        e.raw(
            "POST",
            "/records",
            &[b"{".as_slice(), &[b' '; 9000], b"}"].concat(),
            "ip:1"
        )
        .0,
        413
    );
    e.post(
        "/records",
        &record(&alice, "sw-aaaaaaaaaaaaaaaaaaa1", 1, json!({})),
    );
    e.post(
        "/records",
        &record(&alice, "sw-aaaaaaaaaaaaaaaaaaa2", 1, json!({})),
    );
    assert_eq!(
        e.post(
            "/records",
            &record(&alice, "sw-aaaaaaaaaaaaaaaaaaa3", 1, json!({}))
        ),
        err(429, "owner_swarm_cap")
    );
    e.post(
        "/records",
        &record(&Who::new(2), "sw-aaaaaaaaaaaaaaaaaaa4", 1, json!({})),
    );
    assert_eq!(
        e.post(
            "/records",
            &record(&Who::new(3), "sw-aaaaaaaaaaaaaaaaaaa5", 1, json!({}))
        ),
        err(429, "directory_full")
    );
    let big = record(
        &Who::new(4),
        "sw-aaaaaaaaaaaaaaaaaaa6",
        1,
        json!({"description":"d".repeat(280),"name":"n".repeat(48)}),
    );
    assert!(serde_json::to_vec(&big).unwrap().len() < 8192);
}
#[test]
fn per_ip_and_per_key_write_rate_limits_with_one_minute_window() {
    let limits = Limits {
        writes_per_ip_per_minute: 3,
        writes_per_key_per_minute: 2,
        ..Limits::source().unwrap()
    };
    let e = env_with(limits, false);
    let alice = Who::new(1);
    e.call(
        "POST",
        "/records",
        Some(&record(&alice, SW, 1, json!({}))),
        "ip:9",
    );
    for n in 20..22 {
        e.call(
            "POST",
            "/access-requests",
            Some(&request(&Who::new(n), SW, "", NOW + 1)),
            "ip:9",
        );
    }
    let fourth = request(&Who::new(30), SW, "", NOW);
    assert_eq!(
        e.call("POST", "/access-requests", Some(&fourth), "ip:9"),
        err(429, "rate_limited_ip")
    );
    let carol = Who::new(4);
    for i in 0..2 {
        e.call(
            "POST",
            "/access-requests",
            Some(&request(&carol, SW, &i.to_string(), NOW)),
            &format!("ip:{i}"),
        );
    }
    assert_eq!(
        e.call(
            "POST",
            "/access-requests",
            Some(&request(&carol, SW, "x", NOW)),
            "ip:7"
        ),
        err(429, "rate_limited_key")
    );
    e.tick(61);
    assert_eq!(
        e.call(
            "POST",
            "/access-requests",
            Some(&request(&carol, SW, "y", e.at())),
            "ip:9"
        )
        .0,
        200
    );
}
#[test]
fn signed_read_queries_have_their_own_budgets_and_stay_bounded() {
    let limits = Limits {
        writes_per_ip_per_minute: 3,
        writes_per_key_per_minute: 2,
        ..Limits::source().unwrap()
    };
    let e = env_with(limits, false);
    let alice = Who::new(1);
    e.call(
        "POST",
        "/records",
        Some(&record(&alice, SW, 1, json!({}))),
        "ip:9",
    );
    for i in 0..30u32 {
        assert_eq!(
            e.call(
                "POST",
                "/my-grants",
                Some(&grantee_query(&alice, NOW, &nonce(i))),
                "ip:9"
            )
            .0,
            200,
            "{i}"
        );
        assert_eq!(
            e.call(
                "POST",
                "/owner-inbox",
                Some(&owner_query(&alice, SW, NOW, &nonce(100 + i))),
                "ip:9"
            )
            .0,
            200,
            "{i}"
        );
    }
    for n in 40..42 {
        assert_eq!(
            e.call(
                "POST",
                "/access-requests",
                Some(&request(&Who::new(n), SW, "", NOW)),
                "ip:9"
            )
            .0,
            200
        );
    }
    assert_eq!(
        e.call(
            "POST",
            "/access-requests",
            Some(&request(&Who::new(43), SW, "", NOW)),
            "ip:9"
        ),
        err(429, "rate_limited_ip")
    );
    let by_key = env_with(
        Limits {
            queries_per_key_per_minute: 5,
            queries_per_ip_per_minute: 100,
            ..Limits::source().unwrap()
        },
        false,
    );
    let codes: Vec<u16> = (0..7u32)
        .map(|i| {
            by_key
                .call(
                    "POST",
                    "/my-grants",
                    Some(&grantee_query(&alice, NOW, &nonce(i))),
                    "ip:9",
                )
                .0
        })
        .collect();
    assert_eq!(codes, [200, 200, 200, 200, 200, 429, 429]);
    let by_ip = env_with(
        Limits {
            queries_per_key_per_minute: 100,
            queries_per_ip_per_minute: 3,
            ..Limits::source().unwrap()
        },
        false,
    );
    let codes: Vec<u16> = (0..5u32)
        .map(|i| {
            by_ip
                .call(
                    "POST",
                    "/my-grants",
                    Some(&grantee_query(&Who::new(50), NOW, &nonce(i))),
                    "ip:4",
                )
                .0
        })
        .collect();
    assert_eq!(codes, [200, 200, 200, 429, 429]);
}
#[test]
fn junk_posts_do_not_starve_a_valid_heartbeat_and_only_signed_writes_spend_the_global_budget() {
    let e = env();
    let owner = Who::new(1);
    e.call(
        "POST",
        "/records",
        Some(&record(&owner, SW, 1, json!({}))),
        "ip:owner",
    );
    let mut tampered = heartbeat(&owner, SW, "serving", 2, json!("Qwen/Qwen3-0.6B"), NOW);
    tampered["members"] = json!(99);
    let junk: Vec<Vec<u8>> = vec![
        b"{".to_vec(),
        b"not json".to_vec(),
        br#"{"protocol":"x"}"#.to_vec(),
        serde_json::to_vec(&heartbeat(
            &Who::new(9),
            "sw-zzzzzzzzzzzzzzzzzzzz",
            "serving",
            1,
            Value::Null,
            NOW,
        ))
        .unwrap(),
        serde_json::to_vec(&tampered).unwrap(),
    ];
    let outcomes: Vec<u16> = (0..700)
        .map(|n| {
            e.raw(
                "POST",
                "/heartbeat",
                &junk[n % 5],
                &format!("ip:junk{}", n % 30),
            )
            .0
        })
        .collect();
    assert!(!outcomes.contains(&503));
    let ok = e.call(
        "POST",
        "/heartbeat",
        Some(&heartbeat(
            &owner,
            SW,
            "serving",
            2,
            json!("Qwen/Qwen3-0.6B"),
            NOW,
        )),
        "ip:honest",
    );
    assert_eq!(ok, (200, json!({"status":"serving"})));
    let small = env_with(
        Limits {
            writes_per_minute: 3,
            ..Limits::source().unwrap()
        },
        false,
    );
    let statuses: Vec<u16> = (0..5u8)
        .map(|n| {
            small
                .call(
                    "POST",
                    "/records",
                    Some(&record(
                        &Who::new(60 + n),
                        &format!("sw-{:020}", n),
                        1,
                        json!({}),
                    )),
                    &format!("ip:w{n}"),
                )
                .0
        })
        .collect();
    assert_eq!(statuses, [200, 200, 200, 503, 503]);
}
#[test]
fn spent_write_budget_does_not_stop_heartbeats_and_heartbeats_have_their_own_budget() {
    let e = env_with(
        Limits {
            writes_per_minute: 1,
            heartbeats_per_minute: 10,
            ..Limits::source().unwrap()
        },
        false,
    );
    let owner = Who::new(1);
    e.call(
        "POST",
        "/records",
        Some(&record(&owner, SW, 1, json!({}))),
        "ip:owner",
    );
    let spam = record(
        &Who::new(2),
        "sw-bbbbbbbbbbbbbbbbbbbb",
        1,
        json!({"name":"Spam"}),
    );
    assert_eq!(
        e.call("POST", "/records", Some(&spam), "ip:spam"),
        err(503, "directory_busy")
    );
    for s in 0..3 {
        assert_eq!(
            e.call(
                "POST",
                "/heartbeat",
                Some(&heartbeat(&owner, SW, "serving", 2, Value::Null, NOW + s)),
                "ip:honest"
            )
            .0,
            200
        );
    }
    let own = env_with(
        Limits {
            heartbeats_per_minute: 2,
            ..Limits::source().unwrap()
        },
        false,
    );
    own.call(
        "POST",
        "/records",
        Some(&record(&owner, SW, 1, json!({}))),
        "ip:owner",
    );
    let codes: Vec<u16> = (0..4)
        .map(|s| {
            own.call(
                "POST",
                "/heartbeat",
                Some(&heartbeat(&owner, SW, "serving", 2, Value::Null, NOW + s)),
                &format!("ip:h{s}"),
            )
            .0
        })
        .collect();
    assert_eq!(codes, [200, 200, 503, 503]);
    let stranger = Who::new(77);
    let fresh = env_with(
        Limits {
            heartbeats_per_minute: 2,
            ..Limits::source().unwrap()
        },
        false,
    );
    fresh.call(
        "POST",
        "/records",
        Some(&record(&owner, SW, 1, json!({}))),
        "ip:owner",
    );
    for n in 0..20 {
        assert_eq!(
            fresh
                .call(
                    "POST",
                    "/heartbeat",
                    Some(&heartbeat(&stranger, SW, "serving", 2, Value::Null, NOW)),
                    &format!("ip:s{n}")
                )
                .0,
            403
        );
        assert_eq!(
            fresh
                .raw("POST", "/heartbeat", b"{}", &format!("ip:t{n}"))
                .0,
            400
        );
    }
    assert_eq!(
        fresh
            .call(
                "POST",
                "/heartbeat",
                Some(&heartbeat(&owner, SW, "serving", 2, Value::Null, NOW)),
                "ip:honest"
            )
            .0,
        200
    );
    let queries = env_with(
        Limits {
            writes_per_minute: 1,
            heartbeats_per_minute: 1,
            ..Limits::source().unwrap()
        },
        false,
    );
    queries.call(
        "POST",
        "/records",
        Some(&record(&owner, SW, 1, json!({}))),
        "ip:owner",
    );
    for n in 0..5u32 {
        assert_eq!(
            queries
                .call(
                    "POST",
                    "/my-grants",
                    Some(&grantee_query(&stranger, NOW, &nonce(n))),
                    &format!("ip:q{n}")
                )
                .0,
            200
        );
    }
}
#[test]
fn malformed_json_consumes_the_ip_write_budget_before_parsing_like_the_source() {
    let e = env_with(
        Limits {
            writes_per_ip_per_minute: 2,
            ..Limits::source().unwrap()
        },
        false,
    );
    assert_eq!(
        e.raw("POST", "/records", b"{", "ip:1"),
        err(400, "json_invalid")
    );
    assert_eq!(
        e.raw("POST", "/records", b"{\"a\":1,\"a\":2}", "ip:1"),
        err(400, "json_duplicate_key")
    );
    assert_eq!(
        e.raw("POST", "/records", b"{}", "ip:1"),
        err(429, "rate_limited_ip")
    );
    assert_eq!(
        e.raw("POST", "/records", b"{}", "ip:2"),
        err(400, "protocol_invalid")
    );
}
#[test]
fn wire_refusals_match_python_json_rules() {
    let e = env();
    for (raw, code) in [
        (r#"{"ttl":1.25}"#, "json_float_refused"),
        (r#"{"ttl":1e2}"#, "json_float_refused"),
        (r#"{"ttl":NaN}"#, "json_float_refused"),
        (r#"{"ttl":-Infinity}"#, "json_float_refused"),
        (r#"{"ttl":9007199254740992}"#, "integer_out_of_range"),
        (
            r#"{"ttl":-99999999999999999999999}"#,
            "integer_out_of_range",
        ),
        (r#"[]"#, "json_invalid"),
        (r#"{"a":1,}"#, "json_invalid"),
        (r#"{"ttl":01}"#, "json_invalid"),
        (r#"{"Bad":1}"#, "key_invalid"),
        (r#"{"a":"\u0007"}"#, "text_invalid"),
        (r#"{"a":"\ud800"}"#, "text_invalid"),
        (
            r#"{"a":{"b":{"c":{"d":{"e":{"f":{"g":1}}}}}}}"#,
            "document_too_deep",
        ),
    ] {
        assert_eq!(
            e.raw("POST", "/records", raw.as_bytes(), "ip:1"),
            err(400, code),
            "{raw}"
        );
    }
    assert_eq!(
        e.raw("POST", "/records", &[0xff, 0xfe], "ip:1"),
        err(400, "json_invalid")
    );
    assert_eq!(
        e.raw("POST", "/records", b"\xef\xbb\xbf{}", "ip:1"),
        err(400, "json_invalid")
    );
    // Python whitespace between tokens is accepted; the document then fails on its protocol.
    assert_eq!(
        e.raw(
            "POST",
            "/records",
            b" \t\r\n{\"protocol\":\"x\"}\n ",
            "ip:1"
        ),
        err(400, "protocol_invalid")
    );
}
#[test]
fn unknown_routes_and_methods() {
    let e = env();
    assert_eq!(
        e.raw("GET", "/nope", b"", "ip:1"),
        err(404, "route_unknown")
    );
    assert_eq!(
        e.raw("DELETE", "/swarms", b"", "ip:1"),
        err(405, "method_not_allowed")
    );
    assert_eq!(
        e.raw("PUT", "/records", b"", "ip:1"),
        err(405, "method_not_allowed")
    );
    assert_eq!(
        e.raw("GET", "/records", b"", "ip:1"),
        err(405, "method_not_allowed")
    );
    assert_eq!(
        e.raw("PUT", "/nope", b"", "ip:1"),
        err(404, "route_unknown")
    );
    assert_eq!(
        e.raw("POST", "/swarms", b"{}", "ip:1"),
        err(404, "route_unknown")
    );
    assert_eq!(
        e.raw("GET", "/swarms/sw-short", b"", "ip:1"),
        err(404, "route_unknown")
    );
    assert_eq!(
        e.raw("GET", "/swarms/sw-AAAAAAAAAAAAAAAAAAAA", b"", "ip:1"),
        err(404, "route_unknown")
    );
}
#[test]
fn reads_are_rate_limited_per_ip() {
    let e = env_with(
        Limits {
            reads_per_ip_per_minute: 2,
            ..Limits::source().unwrap()
        },
        false,
    );
    assert_eq!(e.raw("GET", "/swarms", b"", "ip:1").0, 200);
    assert_eq!(
        e.raw("GET", &format!("/swarms/{SW}"), b"", "ip:1"),
        err(404, "swarm_unknown")
    );
    assert_eq!(
        e.raw("GET", "/swarms", b"", "ip:1"),
        err(429, "rate_limited_ip")
    );
    assert_eq!(e.raw("GET", "/swarms", b"", "ip:2").0, 200);
}
#[test]
fn listing_orders_by_status_then_lowercase_name_then_id() {
    let e = env();
    let mk = |id: &str, name: &str| {
        e.post(
            "/records",
            &record(&Who::new(id.as_bytes()[22]), id, 1, json!({"name":name})),
        )
    };
    mk("sw-aaaaaaaaaaaaaaaaaaa1", "beta");
    mk("sw-aaaaaaaaaaaaaaaaaaa2", "Alpha");
    mk("sw-aaaaaaaaaaaaaaaaaaa3", "alpha");
    mk("sw-aaaaaaaaaaaaaaaaaaa4", "zed");
    let live = Who::new(b'4');
    e.post(
        "/heartbeat",
        &heartbeat(
            &live,
            "sw-aaaaaaaaaaaaaaaaaaa4",
            "serving",
            1,
            Value::Null,
            NOW,
        ),
    );
    let order: Vec<String> = e
        .swarms()
        .iter()
        .map(|r| r["swarm_id"].as_str().unwrap()[22..].to_string())
        .collect();
    assert_eq!(order, ["4", "2", "3", "1"]); // serving first, then lowercase name, then id
}
#[test]
fn response_bodies_keep_source_key_order_and_post_records_keeps_client_record_order() {
    let e = env();
    let alice = Who::new(1);
    let raw = e.dir.render("GET", "/swarms", b"", "ip:1").unwrap();
    assert_eq!(
        String::from_utf8(raw.1).unwrap(),
        format!(
            "{{\"swarms\":[],\"privacy\":{},\"now\":{NOW}}}",
            serde_json::to_string(PRIVACY_NOTE).unwrap()
        )
    );
    // The client's record key order (sig first, entry last) is echoed verbatim in the write response.
    let doc = record(&alice, SW, 1, json!({}));
    let mut ordered = String::from("{\"sig\":");
    ordered.push_str(&doc["sig"].to_string());
    for (k, v) in doc.as_object().unwrap() {
        if k != "sig" && k != "entry" {
            ordered.push_str(&format!(",{}:{}", serde_json::to_string(k).unwrap(), v));
        }
    }
    ordered.push_str(",\"entry\":null}");
    let (status, body) = e
        .dir
        .render("POST", "/records", ordered.as_bytes(), "ip:1")
        .unwrap();
    assert_eq!(status, 200);
    let body = String::from_utf8(body).unwrap();
    assert!(body.starts_with("{\"swarm\":{\"swarm_id\":\"sw-aaaaaaaaaaaaaaaaaaaa\",\"name\":\"Berlin coworking\",\"model\":\"Qwen/Qwen3-0.6B\",\"description\":\"\",\"access_mode\":\"request\",\"open_quota\":null,\"owner_key\":"), "{body}");
    assert!(
        body.contains(&format!(",\"record\":{ordered}}}}}")),
        "client order echoed: {body}"
    );
    // After a store round-trip (a later read) the record is sorted, like json.dumps(sort_keys=True).
    let read = String::from_utf8(
        e.dir
            .render("GET", &format!("/swarms/{SW}"), b"", "ip:1")
            .unwrap()
            .1,
    )
    .unwrap();
    assert!(
        read.contains("\"record\":{\"access_mode\":\"request\",\"description\":\"\""),
        "{read}"
    );
}
#[test]
fn sqlite_store_is_durable_across_restart_mode_0600_and_oracle_text_compatible() {
    use std::os::unix::fs::PermissionsExt;
    let dir = std::env::temp_dir().join(format!("l10r5-sqlite-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("directory.sqlite3");
    let alice = Who::new(1);
    let clock = Arc::new(AtomicI64::new(NOW));
    let build = |store: SqliteStore| {
        let c = clock.clone();
        Directory::new(
            store,
            Box::new(ed25519_verify),
            Box::new(move || c.load(Ordering::SeqCst)),
            Config {
                limits: Limits::source().unwrap(),
                allow_loopback_entries: false,
                allow_private_entries: false,
            },
        )
        .unwrap()
    };
    let first = build(SqliteStore::open(&path).unwrap());
    let doc = record(&alice, SW, 1, json!({"description":"héllo ☃"}));
    assert_eq!(
        first
            .handle(
                "POST",
                "/records",
                &serde_json::to_vec(&doc).unwrap(),
                "ip:1"
            )
            .unwrap()
            .0,
        200
    );
    drop(first);
    let second = build(SqliteStore::open(&path).unwrap());
    let (_, listing) = second.handle("GET", "/swarms", b"", "ip:1").unwrap();
    assert_eq!(
        listing.get("swarms"),
        Some(&J::Arr(vec![
            second
                .handle("GET", &format!("/swarms/{SW}"), b"", "ip:1")
                .unwrap()
                .1
                .get("swarm")
                .unwrap()
                .clone()
        ]))
    );
    assert_eq!(
        std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    // Stored text is exactly json.dumps(sort_keys=True) with ensure_ascii.
    let raw = rusqlite::Connection::open(&path).unwrap();
    let text: String = raw
        .query_row("SELECT v FROM kv WHERE tbl='swarm' AND k=?1", [SW], |r| {
            r.get(0)
        })
        .unwrap();
    assert!(text.starts_with("{\"created\": 1800000000, \"heartbeat\": null, \"heartbeat_received\": null, \"record\": {\"access_mode\": \"request\""), "{text}");
    assert!(
        text.contains("\"description\": \"h\\u00e9llo \\u2603\""),
        "{text}"
    );
    // A row written by the oracle's Python (same schema/format) is readable and tombstones persist.
    raw.execute("INSERT INTO kv VALUES ('tomb', 'sw-zzzzzzzzzzzzzzzzzzzz', '{\"at\": 1, \"owner_key\": \"k\", \"seq\": 2}')", []).unwrap();
    drop(second);
    let third = build(SqliteStore::open(&path).unwrap());
    let retired = record(&alice, "sw-zzzzzzzzzzzzzzzzzzzz", 1, json!({}));
    assert_eq!(
        third
            .handle(
                "POST",
                "/records",
                &serde_json::to_vec(&retired).unwrap(),
                "ip:1"
            )
            .unwrap()
            .1,
        J::obj(vec![("error", J::str("swarm_id_retired"))])
    );
    // Existing symlink at the path is never followed on creation.
    let link = dir.join("link.sqlite3");
    std::os::unix::fs::symlink(dir.join("missing-target"), &link).unwrap();
    assert!(SqliteStore::open(&link).is_err());
    std::fs::remove_dir_all(&dir).unwrap();
}
#[test]
fn store_failures_are_explicit_not_silent_success() {
    struct Broken;
    impl Store for Broken {
        fn get(&self, _: &str, _: &str) -> api::swarm::store::StoreResult<Option<J>> {
            Err(api::swarm::store::StoreError("store_sqlite_failed"))
        }
        fn put(&self, _: &str, _: &str, _: &J) -> api::swarm::store::StoreResult<()> {
            Err(api::swarm::store::StoreError("store_sqlite_failed"))
        }
        fn delete(&self, _: &str, _: &str) -> api::swarm::store::StoreResult<()> {
            Err(api::swarm::store::StoreError("store_sqlite_failed"))
        }
        fn scan(&self, _: &str, _: &str) -> api::swarm::store::StoreResult<Vec<(String, J)>> {
            Err(api::swarm::store::StoreError("store_sqlite_failed"))
        }
    }
    let dir = Directory::new(
        Broken,
        Box::new(ed25519_verify),
        Box::new(|| NOW),
        Config {
            limits: Limits::source().unwrap(),
            allow_loopback_entries: false,
            allow_private_entries: false,
        },
    )
    .unwrap();
    assert_eq!(
        dir.handle("GET", "/swarms", b"", "ip:1"),
        Err(api::swarm::store::StoreError("store_sqlite_failed"))
    );
    let doc = record(&Who::new(1), SW, 1, json!({}));
    assert!(
        dir.handle(
            "POST",
            "/records",
            &serde_json::to_vec(&doc).unwrap(),
            "ip:1"
        )
        .is_err()
    );
}

/// INTEGRATOR RULING 3: deep nesting. The oracle was run on the shipped interpreter (Python
/// 3.14.7, real `DirectoryServer`; `L10-evidence/r6-oracle-deep*.txt`, `r6-oracle-special.txt`).
/// Within the 8192-byte body cap Python never hits RecursionError: `json.loads` always completes,
/// so the outcome is exactly the strict-parse result (syntax, duplicate key, float/NaN refusal,
/// in stream order) and otherwise `_check_value`'s traversal (key grammar before reaching the
/// first node deeper than 6, then `document_too_deep`). A top-level array is `json_invalid`.
/// There is no connection drop and no depth at which the class changes, so no bound may show.
fn nest(n: usize, inner: &str) -> String {
    format!("{}{}{}", "{\"a\":".repeat(n), inner, "}".repeat(n))
}
#[test]
fn deep_nesting_has_the_oracle_outcome_at_every_depth() {
    let arr = |n: usize| format!("{}{}", "[".repeat(n), "]".repeat(n));
    let cases: Vec<(String, String, &str)> = vec![
        ("obj 6".into(), nest(6, "1"), "protocol_invalid"),
        ("obj 7".into(), nest(7, "1"), "document_too_deep"),
        ("obj 200".into(), nest(200, "1"), "document_too_deep"),
        ("obj 201".into(), nest(201, "1"), "document_too_deep"),
        ("obj 250".into(), nest(250, "1"), "document_too_deep"),
        ("obj 1200".into(), nest(1200, "1"), "document_too_deep"),
        (
            "arr under obj 4090".into(),
            format!("{{\"a\":{}}}", arr(4090)),
            "document_too_deep",
        ),
        ("top-level arr 250".into(), arr(250), "json_invalid"),
        ("top-level arr 4095".into(), arr(4095), "json_invalid"),
        ("top-level arr 1000".into(), arr(1000), "json_invalid"),
        (
            "dup 250".into(),
            nest(250, "{\"b\":1,\"b\":2}"),
            "json_duplicate_key",
        ),
        (
            "dup 1000".into(),
            nest(1000, "{\"b\":1,\"b\":2}"),
            "json_duplicate_key",
        ),
        ("float 250".into(), nest(250, "1.5"), "json_float_refused"),
        ("nan 250".into(), nest(250, "NaN"), "json_float_refused"),
        ("exp 250".into(), nest(250, "1e5"), "json_float_refused"),
        ("syntax 250".into(), nest(250, "[1,]"), "json_invalid"),
        (
            "unterminated 250".into(),
            "{\"a\":".repeat(250) + "1",
            "json_invalid",
        ),
        ("trailing 250".into(), nest(250, "1") + "x", "json_invalid"),
        (
            "trailing whitespace 250".into(),
            nest(250, "1") + " \n",
            "document_too_deep",
        ),
        (
            "bad escape 250".into(),
            nest(250, "\"\\q\""),
            "json_invalid",
        ),
        (
            "huge int 250".into(),
            nest(250, "123456789012345678901234567890"),
            "document_too_deep",
        ),
        (
            "bad key before deep".into(),
            format!("{{\"A\":1,\"a\":{}}}", nest(250, "1")),
            "key_invalid",
        ),
        (
            "bad key after deep".into(),
            format!("{{\"a\":{},\"A\":1}}", nest(250, "1")),
            "document_too_deep",
        ),
        (
            "empty arrays 250".into(),
            format!("{{\"a\":{}}}", arr(250)),
            "document_too_deep",
        ),
        (
            "dup after deep".into(),
            format!("{{\"a\":{},\"a\":1}}", nest(250, "1")),
            "json_duplicate_key",
        ),
        (
            "top arr, dup inside".into(),
            format!(
                "{}{}{}",
                "[".repeat(250),
                "{\"b\":1,\"b\":2}",
                "]".repeat(250)
            ),
            "json_duplicate_key",
        ),
        (
            "top arr, float inside".into(),
            format!("{}1.5{}", "[".repeat(250), "]".repeat(250)),
            "json_float_refused",
        ),
    ];
    for (n, (name, body, code)) in cases.iter().enumerate() {
        assert!(body.len() <= 8192, "{name} exceeds the body cap");
        for path in ["/records", "/heartbeat"] {
            let e = env();
            assert_eq!(
                e.raw("POST", path, body.as_bytes(), &format!("ip:{n}")),
                err(400, code),
                "{name} on {path}"
            );
        }
    }
}
#[test]
fn deep_documents_do_not_exhaust_the_stack_in_a_small_thread() {
    // the oracle's handler threads are also small-stack threads; the largest in-cap body must be
    // answered, not crash the process.
    let body = format!("{{\"a\":{}{}}}", "[".repeat(4090), "]".repeat(4090));
    let answer = std::thread::Builder::new()
        .stack_size(256 * 1024)
        .spawn(move || env().raw("POST", "/records", body.as_bytes(), "ip:1"))
        .unwrap()
        .join()
        .expect("no stack overflow");
    assert_eq!(answer, err(400, "document_too_deep"));
}
