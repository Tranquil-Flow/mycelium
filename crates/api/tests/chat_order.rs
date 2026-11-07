// SPDX-License-Identifier: AGPL-3.0-only
//! Chat responses keep the SERVICE's key order: v0.1.4 `chat._json` is
//! `json.dumps(value, separators=(',',':'))` without `sort_keys` (chat.py:612-613). Expected bytes are
//! the executed oracle's (`fixtures/oracle_chat_order.json`, `L10-evidence/r10-probe-chat-order.py`).
use api::{
    chat::{Chat, ChatError, Service},
    http::Request,
    wire::Json,
};
use serde_json::{Value, json};

fn oracle(label: &str) -> String {
    let doc: Value = serde_json::from_str(include_str!("fixtures/oracle_chat_order.json")).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    doc["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["label"] == label)
        .unwrap_or_else(|| panic!("no oracle case {label}"))["bytes"]
        .as_str()
        .unwrap()
        .to_owned()
}
fn pairs<const N: usize>(fields: [(&str, Json); N]) -> Json {
    Json::object(fields)
}
fn list(items: Vec<Json>) -> Json {
    Json::Arr(items)
}
fn state_like() -> Json {
    pairs([
        ("ready", true.into()),
        (
            "history",
            list(vec![
                pairs([("role", "user".into()), ("text", "zebra".into())]),
                pairs([("text", "α β".into()), ("role", "assistant".into())]),
            ]),
        ),
        ("busy", false.into()),
        (
            "limits",
            pairs([
                ("max_new_tokens", 256i64.into()),
                ("prompt_chars", 4000i64.into()),
            ]),
        ),
    ])
}
fn events_like() -> Json {
    pairs([
        (
            "events",
            list(vec![
                pairs([
                    ("seq", 2i64.into()),
                    ("kind", "token".into()),
                    ("text", "é\u{2014}😀".into()),
                ]),
                pairs([("kind", "done".into()), ("seq", 3i64.into())]),
            ]),
        ),
        ("next", 3i64.into()),
        ("done", true.into()),
    ])
}
fn reverse_sorted() -> Json {
    pairs([
        ("z", 1i64.into()),
        (
            "y",
            pairs([
                ("b", 1i64.into()),
                (
                    "a",
                    list(vec![
                        3i64.into(),
                        pairs([("d", 0i64.into()), ("c", Json::Null)]),
                    ]),
                ),
            ]),
        ),
        ("x", "λ".into()),
    ])
}

struct Ordered;
impl Service for Ordered {
    fn is_owner(&self) -> bool {
        true
    }
    fn state(&self) -> Result<Value, ChatError> {
        Ok(state_like().to_value())
    }
    fn send(&self, _: &str, _: Value, _: bool) -> Result<Value, ChatError> {
        Ok(reverse_sorted().to_value())
    }
    fn events(&self, _: Value) -> Result<Value, ChatError> {
        Ok(events_like().to_value())
    }
    fn state_json(&self) -> Result<Json, ChatError> {
        Ok(state_like())
    }
    fn send_json(&self, _: &str, _: Value, _: bool) -> Result<Json, ChatError> {
        Ok(reverse_sorted())
    }
    fn events_json(&self, _: Value) -> Result<Json, ChatError> {
        Ok(events_like())
    }
}
fn post(path: &str) -> Request {
    Request {
        method: "POST".into(),
        target: path.into(),
        headers: vec![
            ("x-mycelium-chat".into(), "1".into()),
            ("content-type".into(), "application/json".into()),
        ],
        body: b"{}".to_vec(),
    }
}
fn body(chat: &Chat<Ordered>, path: &str) -> String {
    String::from_utf8(chat.handle(post(path), false).unwrap().body).unwrap()
}

#[test]
fn chat_state_send_and_events_keep_the_services_key_order_like_the_oracle() {
    let chat = Chat::new(Ordered);
    assert_eq!(body(&chat, "/chat/api/state"), oracle("state_like"));
    assert_eq!(body(&chat, "/chat/api/send"), oracle("reverse_sorted"));
    assert_eq!(body(&chat, "/chat/api/events"), oracle("events_like"));
}

/// A service that only implements the sorted `Value` entry points still answers sorted, as before.
struct Sorted;
impl Service for Sorted {
    fn is_owner(&self) -> bool {
        true
    }
    fn state(&self) -> Result<Value, ChatError> {
        Ok(json!({"z": 1, "a": 2}))
    }
    fn send(&self, _: &str, _: Value, _: bool) -> Result<Value, ChatError> {
        Ok(json!({}))
    }
    fn events(&self, _: Value) -> Result<Value, ChatError> {
        Ok(json!({}))
    }
}
#[test]
fn a_value_only_service_is_unchanged() {
    let reply = Chat::new(Sorted)
        .handle(post("/chat/api/state"), false)
        .unwrap();
    assert_eq!(String::from_utf8(reply.body).unwrap(), "{\"a\":2,\"z\":1}");
}
