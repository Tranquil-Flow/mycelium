// SPDX-License-Identifier: AGPL-3.0-only
//! The swarm directory authority: a phonebook of owner-signed records,
//! mycelium_swarm_directory/directory.py @ f4e366e9. Transport-free:
//! `handle(method, path, body, client)`. Grants nothing itself; every right is a
//! signature by the swarm owner's key. No request content, IP or key is logged.
//! One mutex serialises requests (a refinement of the source's unlocked threads whose
//! observable tallies are pinned by tests/swarm_concurrency.rs); a storage failure is surfaced
//! to the service as `StoreError`, which drops the connection as in v0.1.4.
use super::{
    docs,
    json::{J, dumps_response},
    limits::{Limits, WindowLimiter},
    store::{Store, StoreError},
    wire::{Verifier, WireError, fingerprint, parse_json_object},
};
use std::sync::Mutex;

pub const PRIVACY_NOTE: &str = "Your prompt is seen by this swarm's nodes (the Macs that run it). It is not end-to-end encrypted from them. The directory never sees prompts and keeps no logs or analytics. Cloudflare terminates TLS for this site and for swarm entries on trycloudflare.com and can see your IP address, timing and plaintext prompts.";
const API_ROUTES: [&str; 9] = [
    "/records",
    "/delete",
    "/heartbeat",
    "/access-requests",
    "/owner-inbox",
    "/grants",
    "/deny",
    "/my-grants",
    "/entry-offers",
];
const QUERY_ROUTES: [&str; 2] = ["/owner-inbox", "/my-grants"];
const NONCE_PRUNE_EVERY: u64 = 256;

enum Fail {
    Wire(WireError),
    Store(StoreError),
}
impl From<WireError> for Fail {
    fn from(e: WireError) -> Self {
        Fail::Wire(e)
    }
}
impl From<StoreError> for Fail {
    fn from(e: StoreError) -> Self {
        Fail::Store(e)
    }
}
type R<T> = Result<T, Fail>;
fn wire(code: &'static str, status: u16) -> Fail {
    Fail::Wire(WireError::new(code, status))
}
fn corrupt() -> Fail {
    Fail::Store(StoreError("store_row_corrupt"))
}
fn f<'a>(v: &'a J, k: &str) -> R<&'a J> {
    v.get(k).ok_or_else(corrupt)
}
fn fs<'a>(v: &'a J, k: &str) -> R<&'a str> {
    f(v, k)?.as_str().ok_or_else(corrupt)
}
fn fi(v: &J, k: &str) -> R<i64> {
    f(v, k)?.as_int().ok_or_else(corrupt)
}
/// Python dict equality: key order does not matter.
fn same(a: &J, b: &J) -> bool {
    match (a, b) {
        (J::Obj(x), J::Obj(y)) => {
            x.len() == y.len()
                && x.iter()
                    .all(|(k, v)| y.iter().any(|(k2, v2)| k == k2 && same(v, v2)))
        }
        (J::Arr(x), J::Arr(y)) => x.len() == y.len() && x.iter().zip(y).all(|(p, q)| same(p, q)),
        _ => a == b,
    }
}
fn present(v: &J, k: &str) -> bool {
    v.get(k).is_some_and(|x| !x.is_null())
}
struct State<S> {
    store: S,
    ip_writes: WindowLimiter,
    key_writes: WindowLimiter,
    ip_reads: WindowLimiter,
    ip_queries: WindowLimiter,
    key_queries: WindowLimiter,
    writes: WindowLimiter,
    beats: WindowLimiter,
    nonce_inserts: u64,
}
pub struct Directory<S: Store> {
    state: Mutex<State<S>>,
    verifier: Box<Verifier>,
    clock: Box<dyn Fn() -> i64 + Send + Sync>,
    limits: Limits,
    allow_loopback: bool,
    allow_private: bool,
}
pub struct Config {
    pub limits: Limits,
    pub allow_loopback_entries: bool,
    pub allow_private_entries: bool,
}
impl<S: Store> Directory<S> {
    pub fn new(
        store: S,
        verifier: Box<Verifier>,
        clock: Box<dyn Fn() -> i64 + Send + Sync>,
        config: Config,
    ) -> Result<Self, &'static str> {
        let l = config.limits;
        Ok(Self {
            state: Mutex::new(State {
                store,
                ip_writes: WindowLimiter::new(l.writes_per_ip_per_minute, 8192)?,
                key_writes: WindowLimiter::new(l.writes_per_key_per_minute, 8192)?,
                ip_reads: WindowLimiter::new(l.reads_per_ip_per_minute, 8192)?,
                ip_queries: WindowLimiter::new(l.queries_per_ip_per_minute, 8192)?,
                key_queries: WindowLimiter::new(l.queries_per_key_per_minute, 8192)?,
                writes: WindowLimiter::new(l.writes_per_minute, 1)?,
                beats: WindowLimiter::new(l.heartbeats_per_minute, 1)?,
                nonce_inserts: 0,
            }),
            verifier,
            clock,
            limits: l,
            allow_loopback: config.allow_loopback_entries,
            allow_private: config.allow_private_entries,
        })
    }
    pub fn allow_loopback(&self) -> bool {
        self.allow_loopback
    }
    pub fn limits(&self) -> &Limits {
        &self.limits
    }
    fn now(&self) -> i64 {
        (self.clock)()
    }
    /// Returns the exact HTTP status and JSON document the source would send.
    pub fn handle(
        &self,
        method: &str,
        path: &str,
        body: &[u8],
        client: &str,
    ) -> Result<(u16, J), StoreError> {
        let mut st = self
            .state
            .lock()
            .map_err(|_| StoreError("directory_state_poisoned"))?;
        match self.route(&mut st, method, path, body, client) {
            Ok(v) => Ok((200, v)),
            Err(Fail::Wire(e)) => Ok((e.status, J::obj(vec![("error", J::str(e.code))]))),
            Err(Fail::Store(e)) => Err(e),
        }
    }
    pub fn render(
        &self,
        method: &str,
        path: &str,
        body: &[u8],
        client: &str,
    ) -> Result<(u16, Vec<u8>), StoreError> {
        let (status, value) = self.handle(method, path, body, client)?;
        Ok((status, dumps_response(&value).into_bytes()))
    }
    /// server.py:155-161 reads the row directly (no rate limit) for the QR link.
    pub fn owner_key_of(&self, swarm_id: &str) -> Result<Option<String>, StoreError> {
        let st = self
            .state
            .lock()
            .map_err(|_| StoreError("directory_state_poisoned"))?;
        Ok(st.store.get("swarm", swarm_id)?.and_then(|row| {
            row.get("record")?
                .get("owner_key")?
                .as_str()
                .map(str::to_string)
        }))
    }
    fn route(
        &self,
        st: &mut State<S>,
        method: &str,
        path: &str,
        body: &[u8],
        client: &str,
    ) -> R<J> {
        let now = self.now();
        if method == "GET" {
            let one = path
                .strip_prefix("/swarms/")
                .filter(|id| docs::is_swarm_id(&J::str(id)));
            if path == "/swarms" || one.is_some() {
                if !st.ip_reads.allow(client, now) {
                    return Err(wire("rate_limited_ip", 429));
                }
                if let Some(id) = one {
                    let row = st
                        .store
                        .get("swarm", id)?
                        .ok_or_else(|| wire("swarm_unknown", 404))?;
                    return Ok(J::obj(vec![("swarm", self.public(&row, now)?)]));
                }
                let swarms = self.list_swarms(st)?;
                return Ok(J::obj(vec![
                    ("swarms", swarms),
                    ("privacy", J::str(PRIVACY_NOTE)),
                    ("now", J::Int(now)),
                ]));
            }
            if API_ROUTES.contains(&path) {
                return Err(wire("method_not_allowed", 405));
            }
            return Err(wire("route_unknown", 404));
        }
        if method != "POST" {
            if path == "/swarms" || API_ROUTES.contains(&path) {
                return Err(wire("method_not_allowed", 405));
            }
            return Err(wire("route_unknown", 404));
        }
        if !API_ROUTES.contains(&path) {
            return Err(wire("route_unknown", 404));
        }
        if body.len() > self.limits.max_body {
            return Err(wire("body_too_large", 413));
        }
        let limiter = if QUERY_ROUTES.contains(&path) {
            &mut st.ip_queries
        } else {
            &mut st.ip_writes
        };
        if !limiter.allow(client, now) {
            return Err(wire("rate_limited_ip", 429));
        }
        let document = parse_json_object(body, self.limits.max_body)?;
        match path {
            "/records" => self.put_record(st, &document, now),
            "/delete" => self.delete(st, &document, now),
            "/heartbeat" => self.heartbeat(st, &document, now),
            "/access-requests" => self.access_request(st, &document, now),
            "/owner-inbox" => self.owner_inbox(st, &document, now),
            "/grants" => self.grant(st, &document, now),
            "/deny" => self.deny(st, &document, now),
            "/my-grants" => self.my_grants(st, &document, now),
            _ => self.entry_offer(st, &document, now),
        }
    }
    fn charge_write(&self, st: &mut State<S>, now: i64) -> R<()> {
        if st.writes.allow("*", now) {
            Ok(())
        } else {
            Err(wire("directory_busy", 503))
        }
    }
    fn key_rate(&self, st: &mut State<S>, key: &str, now: i64) -> R<()> {
        if st.key_writes.allow(key, now) {
            Ok(())
        } else {
            Err(wire("rate_limited_key", 429))
        }
    }
    fn query_rate(&self, st: &mut State<S>, key: &str, now: i64) -> R<()> {
        if st.key_queries.allow(key, now) {
            Ok(())
        } else {
            Err(wire("rate_limited_key", 429))
        }
    }
    fn owned(&self, st: &State<S>, swarm_id: &str, owner_key: &str) -> R<J> {
        let row = st
            .store
            .get("swarm", swarm_id)?
            .ok_or_else(|| wire("swarm_unknown", 404))?;
        if fs(f(&row, "record")?, "owner_key")? != owner_key {
            return Err(wire("swarm_owner_mismatch", 403));
        }
        Ok(row)
    }
    fn consume_nonce(&self, st: &mut State<S>, nonce: &str, now: i64) -> R<()> {
        if st.store.get("nonce", nonce)?.is_some() {
            return Err(wire("nonce_replayed", 409));
        }
        st.nonce_inserts += 1;
        if st.nonce_inserts.is_multiple_of(NONCE_PRUNE_EVERY) {
            let mut live = 0usize;
            for (key, value) in st.store.scan("nonce", "")? {
                if fi(&value, "expires")? <= now {
                    st.store.delete("nonce", &key)?;
                } else {
                    live += 1;
                }
            }
            if live >= self.limits.max_live_nonces {
                return Err(wire("directory_busy", 503));
            }
        }
        st.store.put(
            "nonce",
            nonce,
            &J::obj(vec![("expires", J::Int(now + 2 * self.limits.skew + 1))]),
        )?;
        Ok(())
    }
    fn check(&self, d: &J, protocol: &str) -> R<()> {
        docs::check(
            d,
            protocol,
            &*self.verifier,
            self.allow_loopback,
            self.allow_private,
        )?;
        Ok(())
    }
    // -- writes ---------------------------------------------------------------
    fn put_record(&self, st: &mut State<S>, document: &J, now: i64) -> R<J> {
        docs::check_record(
            document,
            &*self.verifier,
            self.allow_loopback,
            self.allow_private,
        )?;
        let record = document;
        let owner = fs(record, "owner_key")?;
        self.key_rate(st, owner, now)?;
        self.charge_write(st, now)?;
        let swarm_id = fs(record, "swarm_id")?;
        if st.store.get("tomb", swarm_id)?.is_some() {
            return Err(wire("swarm_id_retired", 409));
        }
        let row = match st.store.get("swarm", swarm_id)? {
            None => {
                let rows = st.store.scan("swarm", "")?;
                if rows.len() >= self.limits.max_swarms {
                    return Err(wire("directory_full", 429));
                }
                let mut owned = 0usize;
                for (_, value) in &rows {
                    if fs(f(value, "record")?, "owner_key")? == owner {
                        owned += 1;
                    }
                }
                if owned >= self.limits.max_swarms_per_owner {
                    return Err(wire("owner_swarm_cap", 429));
                }
                J::obj(vec![
                    ("record", record.clone()),
                    ("heartbeat", J::Null),
                    ("heartbeat_received", J::Null),
                    ("created", J::Int(now)),
                    ("updated", J::Int(now)),
                ])
            }
            Some(row) => {
                let old = f(&row, "record")?;
                if fs(old, "owner_key")? != owner {
                    return Err(wire("swarm_owner_mismatch", 403));
                }
                if fi(record, "seq")? <= fi(old, "seq")? {
                    return Err(wire("record_seq_stale", 409));
                }
                let mut row = row.clone();
                if !same(f(old, "entry")?, f(record, "entry")?)
                    && present(&row, "heartbeat")
                    && fs(f(&row, "heartbeat")?, "signer_key")? != owner
                {
                    row = row
                        .with("heartbeat", J::Null)
                        .with("heartbeat_received", J::Null);
                }
                let row = row
                    .with("record", record.clone())
                    .with("updated", J::Int(now));
                if present(record, "entry") {
                    let entry_key = fs(f(record, "entry")?, "key")?;
                    st.store
                        .delete("offer", &format!("{swarm_id}/{entry_key}"))?;
                }
                row
            }
        };
        st.store.put("swarm", swarm_id, &row)?;
        Ok(J::obj(vec![("swarm", self.public(&row, now)?)]))
    }
    fn delete(&self, st: &mut State<S>, document: &J, now: i64) -> R<J> {
        self.check(document, docs::DELETE)?;
        let owner = fs(document, "owner_key")?;
        self.key_rate(st, owner, now)?;
        self.charge_write(st, now)?;
        let swarm_id = fs(document, "swarm_id")?;
        let row = self.owned(st, swarm_id, owner)?;
        if fi(document, "seq")? <= fi(f(&row, "record")?, "seq")? {
            return Err(wire("record_seq_stale", 409));
        }
        st.store.put(
            "tomb",
            swarm_id,
            &J::obj(vec![
                ("owner_key", J::str(owner)),
                ("seq", J::Int(fi(document, "seq")?)),
                ("at", J::Int(now)),
            ]),
        )?;
        st.store.delete("swarm", swarm_id)?;
        for table in ["request", "offer"] {
            for (key, _) in st.store.scan(table, &format!("{swarm_id}/"))? {
                st.store.delete(table, &key)?;
            }
        }
        for (key, value) in st.store.scan("grant", "")? {
            if value.get("swarm_id").and_then(J::as_str) == Some(swarm_id) {
                st.store.delete("grant", &key)?;
            }
        }
        Ok(J::obj(vec![("deleted", J::str(swarm_id))]))
    }
    fn heartbeat(&self, st: &mut State<S>, document: &J, now: i64) -> R<J> {
        self.check(document, docs::HEARTBEAT)?;
        docs::fresh(fi(document, "at")?, now, self.limits.skew)?;
        let swarm_id = fs(document, "swarm_id")?;
        let row = st
            .store
            .get("swarm", swarm_id)?
            .ok_or_else(|| wire("swarm_unknown", 404))?;
        let record = f(&row, "record")?;
        let signer = fs(document, "signer_key")?;
        let entry_key = if present(record, "entry") {
            Some(fs(f(record, "entry")?, "key")?)
        } else {
            None
        };
        if signer != fs(record, "owner_key")? && Some(signer) != entry_key {
            return Err(wire("heartbeat_signer_not_authorized", 403));
        }
        self.key_rate(st, signer, now)?;
        if !st.beats.allow("*", now) {
            return Err(wire("directory_busy", 503));
        }
        if present(&row, "heartbeat") && fi(document, "at")? <= fi(f(&row, "heartbeat")?, "at")? {
            return Err(wire("heartbeat_stale", 409));
        }
        let row = row
            .with("heartbeat", document.clone())
            .with("heartbeat_received", J::Int(now));
        st.store.put("swarm", swarm_id, &row)?;
        Ok(J::obj(vec![("status", J::str(self.status(&row, now)?))]))
    }
    fn grantee_state(&self, st: &State<S>, grantee: &str, swarm_id: &str, value: J) -> R<()> {
        let key = format!("{grantee}/{swarm_id}");
        if st.store.get("grant", &key)?.is_none()
            && st.store.scan("grant", &format!("{grantee}/"))?.len()
                >= self.limits.max_grants_per_grantee
        {
            return Err(wire("grantee_grant_cap", 429));
        }
        st.store.put("grant", &key, &value)?;
        Ok(())
    }
    fn access_request(&self, st: &mut State<S>, document: &J, now: i64) -> R<J> {
        self.check(document, docs::ACCESS_REQUEST)?;
        docs::fresh(fi(document, "at")?, now, self.limits.skew)?;
        let requester = fs(document, "requester_key")?;
        self.key_rate(st, requester, now)?;
        self.charge_write(st, now)?;
        let swarm_id = fs(document, "swarm_id")?;
        let row = st
            .store
            .get("swarm", swarm_id)?
            .ok_or_else(|| wire("swarm_unknown", 404))?;
        match fs(f(&row, "record")?, "access_mode")? {
            "invite" => return Err(wire("swarm_invite_only", 403)),
            "open" => return Err(wire("swarm_is_open", 409)),
            _ => {}
        }
        let key = format!("{swarm_id}/{requester}");
        if st.store.get("request", &key)?.is_none()
            && st.store.scan("request", &format!("{swarm_id}/"))?.len()
                >= self.limits.max_requests_per_swarm
        {
            return Err(wire("swarm_request_queue_full", 429));
        }
        st.store.put(
            "request",
            &key,
            &J::obj(vec![
                ("request", document.clone()),
                ("state", J::str("pending")),
                ("received", J::Int(now)),
            ]),
        )?;
        let granted = st
            .store
            .get("grant", &format!("{requester}/{swarm_id}"))?
            .is_some_and(|s| s.get("state").and_then(J::as_str) == Some("granted"));
        if !granted {
            self.grantee_state(
                st,
                requester,
                swarm_id,
                J::obj(vec![
                    ("state", J::str("pending")),
                    ("swarm_id", J::str(swarm_id)),
                ]),
            )?;
        }
        Ok(J::obj(vec![("state", J::str("pending"))]))
    }
    fn grant(&self, st: &mut State<S>, document: &J, now: i64) -> R<J> {
        self.check(document, docs::GRANT)?;
        let swarm_id = fs(document, "swarm_id")?;
        let row = st
            .store
            .get("swarm", swarm_id)?
            .ok_or_else(|| wire("swarm_unknown", 404))?;
        let issuer = fs(document, "issuer_key")?;
        if issuer != fs(f(&row, "record")?, "owner_key")? {
            return Err(wire("grant_issuer_not_owner", 403));
        }
        self.key_rate(st, issuer, now)?;
        self.charge_write(st, now)?;
        if fi(document, "expires_at")? <= now {
            return Err(wire("grant_expired", 403));
        }
        let grantee = fs(document, "grantee_key")?;
        self.grantee_state(
            st,
            grantee,
            swarm_id,
            J::obj(vec![
                ("state", J::str("granted")),
                ("swarm_id", J::str(swarm_id)),
                ("grant", document.clone()),
            ]),
        )?;
        let key = format!("{swarm_id}/{grantee}");
        if let Some(request) = st.store.get("request", &key)? {
            st.store
                .put("request", &key, &request.with("state", J::str("granted")))?;
        }
        Ok(J::obj(vec![
            ("state", J::str("granted")),
            ("grant_id", f(document, "grant_id")?.clone()),
        ]))
    }
    fn deny(&self, st: &mut State<S>, document: &J, now: i64) -> R<J> {
        self.check(document, docs::DENY)?;
        docs::fresh(fi(document, "at")?, now, self.limits.skew)?;
        let swarm_id = fs(document, "swarm_id")?;
        let owner = fs(document, "owner_key")?;
        self.owned(st, swarm_id, owner)?;
        self.key_rate(st, owner, now)?;
        self.charge_write(st, now)?;
        let requester = fs(document, "requester_key")?;
        let key = format!("{swarm_id}/{requester}");
        let request = st
            .store
            .get("request", &key)?
            .ok_or_else(|| wire("request_unknown", 404))?;
        st.store
            .put("request", &key, &request.with("state", J::str("denied")))?;
        self.grantee_state(
            st,
            requester,
            swarm_id,
            J::obj(vec![
                ("state", J::str("denied")),
                ("swarm_id", J::str(swarm_id)),
            ]),
        )?;
        Ok(J::obj(vec![("state", J::str("denied"))]))
    }
    fn entry_offer(&self, st: &mut State<S>, document: &J, now: i64) -> R<J> {
        self.check(document, docs::ENTRY_OFFER)?;
        docs::fresh(fi(document, "at")?, now, self.limits.skew)?;
        let entry_key = fs(document, "entry_key")?;
        self.key_rate(st, entry_key, now)?;
        self.charge_write(st, now)?;
        let swarm_id = fs(document, "swarm_id")?;
        if st.store.get("swarm", swarm_id)?.is_none() {
            return Err(wire("swarm_unknown", 404));
        }
        let prefix = format!("{swarm_id}/");
        for (key, value) in st.store.scan("offer", &prefix)? {
            if now - fi(&value, "received")? > self.limits.offer_ttl {
                st.store.delete("offer", &key)?;
            }
        }
        let key = format!("{swarm_id}/{entry_key}");
        if st.store.get("offer", &key)?.is_none()
            && st.store.scan("offer", &prefix)?.len() >= self.limits.max_offers_per_swarm
        {
            return Err(wire("entry_offers_full", 429));
        }
        st.store.put(
            "offer",
            &key,
            &J::obj(vec![("offer", document.clone()), ("received", J::Int(now))]),
        )?;
        Ok(J::obj(vec![
            ("state", J::str("offered")),
            (
                "entry_fingerprint",
                J::Str(fingerprint(f(document, "entry_key")?)?),
            ),
        ]))
    }
    // -- signed reads -----------------------------------------------------------
    fn owner_inbox(&self, st: &mut State<S>, document: &J, now: i64) -> R<J> {
        self.check(document, docs::OWNER_QUERY)?;
        docs::fresh(fi(document, "at")?, now, self.limits.skew)?;
        let swarm_id = fs(document, "swarm_id")?;
        let owner = fs(document, "owner_key")?;
        self.owned(st, swarm_id, owner)?;
        self.query_rate(st, owner, now)?;
        self.consume_nonce(st, fs(document, "nonce")?, now)?;
        let prefix = format!("{swarm_id}/");
        let mut requests: Vec<(bool, i64, J)> = Vec::new();
        for (_, v) in st.store.scan("request", &prefix)? {
            let r = f(&v, "request")?;
            let state = fs(&v, "state")?;
            requests.push((
                state != "pending",
                -fi(r, "at")?,
                J::obj(vec![
                    ("requester_key", f(r, "requester_key")?.clone()),
                    (
                        "requester_fingerprint",
                        J::Str(fingerprint(f(r, "requester_key")?)?),
                    ),
                    ("note", f(r, "note")?.clone()),
                    ("at", f(r, "at")?.clone()),
                    ("state", J::str(state)),
                ]),
            ));
        }
        let mut offers = Vec::new();
        for (_, v) in st.store.scan("offer", &prefix)? {
            if now - fi(&v, "received")? <= self.limits.offer_ttl {
                let o = f(&v, "offer")?;
                offers.push(J::obj(vec![
                    ("entry_key", f(o, "entry_key")?.clone()),
                    (
                        "entry_fingerprint",
                        J::Str(fingerprint(f(o, "entry_key")?)?),
                    ),
                    ("origin", f(o, "origin")?.clone()),
                    ("at", f(o, "at")?.clone()),
                ]));
            }
        }
        requests.sort_by_key(|(a, b, _)| (*a, *b));
        Ok(J::obj(vec![
            (
                "requests",
                J::Arr(requests.into_iter().map(|r| r.2).collect()),
            ),
            ("entry_offers", J::Arr(offers)),
        ]))
    }
    fn my_grants(&self, st: &mut State<S>, document: &J, now: i64) -> R<J> {
        self.check(document, docs::GRANTEE_QUERY)?;
        docs::fresh(fi(document, "at")?, now, self.limits.skew)?;
        let grantee = fs(document, "grantee_key")?;
        self.query_rate(st, grantee, now)?;
        self.consume_nonce(st, fs(document, "nonce")?, now)?;
        let (mut grants, mut denied, mut pending) = (Vec::new(), Vec::new(), Vec::new());
        for (_, v) in st.store.scan("grant", &format!("{grantee}/"))? {
            match fs(&v, "state")? {
                "granted" if fi(f(&v, "grant")?, "expires_at")? > now => {
                    grants.push(f(&v, "grant")?.clone());
                }
                "denied" => denied.push(f(&v, "swarm_id")?.clone()),
                "pending" => pending.push(f(&v, "swarm_id")?.clone()),
                _ => {}
            }
        }
        Ok(J::obj(vec![
            ("grants", J::Arr(grants)),
            ("denied", J::Arr(denied)),
            ("pending", J::Arr(pending)),
        ]))
    }
    // -- public view --------------------------------------------------------------
    fn status(&self, row: &J, now: i64) -> R<&'static str> {
        let beat = row.get("heartbeat");
        let received = row.get("heartbeat_received").and_then(J::as_int);
        match (beat, received) {
            (Some(b), Some(r)) if !b.is_null() && now - r <= self.limits.heartbeat_ttl => {
                Ok(match fs(b, "status")? {
                    "serving" => "serving",
                    "warming" => "warming",
                    _ => "offline",
                })
            }
            _ => Ok("offline"),
        }
    }
    fn public(&self, row: &J, now: i64) -> R<J> {
        let record = f(row, "record")?;
        let status = self.status(row, now)?;
        let beat = row
            .get("heartbeat")
            .filter(|b| !b.is_null() && status != "offline");
        let entry = f(record, "entry")?;
        let received = row.get("heartbeat_received").and_then(J::as_int);
        let member = |k: &str, none: J| -> R<J> { beat.map_or(Ok(none), |b| f(b, k).cloned()) };
        Ok(J::obj(vec![
            ("swarm_id", f(record, "swarm_id")?.clone()),
            ("name", f(record, "name")?.clone()),
            ("model", f(record, "model")?.clone()),
            ("description", f(record, "description")?.clone()),
            ("access_mode", f(record, "access_mode")?.clone()),
            ("open_quota", f(record, "open_quota")?.clone()),
            ("owner_key", f(record, "owner_key")?.clone()),
            (
                "owner_fingerprint",
                J::Str(fingerprint(f(record, "owner_key")?)?),
            ),
            ("status", J::str(status)),
            ("members", member("members", J::Int(0))?),
            ("served_model", member("served_model", J::Null)?),
            (
                "entry_origin",
                if entry.is_null() {
                    J::Null
                } else {
                    f(entry, "origin")?.clone()
                },
            ),
            (
                "entry_fingerprint",
                if entry.is_null() {
                    J::Null
                } else {
                    J::Str(fingerprint(f(entry, "key")?)?)
                },
            ),
            (
                "last_seen_seconds",
                received.map_or(J::Null, |r| J::Int((now - r).max(0))),
            ),
            ("privacy", J::str(PRIVACY_NOTE)),
            ("record", record.clone()),
        ]))
    }
    fn list_swarms(&self, st: &State<S>) -> R<J> {
        let now = self.now();
        let mut rows = Vec::new();
        for (_, value) in st.store.scan("swarm", "")? {
            let public = self.public(&value, now)?;
            let order = match fs(&public, "status")? {
                "serving" => 0,
                "warming" => 1,
                _ => 2,
            };
            let name = fs(&public, "name")?.to_lowercase();
            let id = fs(&public, "swarm_id")?.to_string();
            rows.push(((order, name, id), public));
        }
        rows.sort_by(|a, b| a.0.cmp(&b.0));
        Ok(J::Arr(rows.into_iter().map(|r| r.1).collect()))
    }
}
