// SPDX-License-Identifier: AGPL-3.0-only
//! request_gateway/contracts.py:397-456 @ f4e366e9: the HTTP adapter cannot
//! delegate away the closed event schema, even when a host validator is lax.
use api::{
    http::{Request, Response},
    requests::{Accepted, Gateway, Service, Subscription},
    schema::PublicError,
};
use serde_json::{Value, json};
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};
struct Source {
    event: Value,
    acks: Arc<AtomicUsize>,
}
struct One {
    event: Option<Value>,
    acks: Arc<AtomicUsize>,
}
impl Subscription for One {
    fn next(&mut self) -> Result<Option<Value>, PublicError> {
        Ok(self.event.take())
    }
    fn ack(&mut self, _: u64) -> Result<(), PublicError> {
        self.acks.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
    fn close(&mut self) -> Result<(), PublicError> {
        Ok(())
    }
}
impl Service for Source {
    fn authorized(&self, _: &Request) -> bool {
        true
    }
    fn health(&self) -> Result<Value, PublicError> {
        Ok(json!({}))
    }
    fn qualification(&self) -> Result<Value, PublicError> {
        unreachable!()
    }
    fn recovery(&self) -> Result<Value, PublicError> {
        unreachable!()
    }
    fn submit(&self, _: Value) -> Result<Accepted, PublicError> {
        unreachable!()
    }
    fn cancel(&self, _: &str, _: Option<&str>) -> Result<bool, PublicError> {
        unreachable!()
    }
    fn subscribe(
        &self,
        _: &str,
        _: Option<(u64, u64)>,
        _: Option<&str>,
    ) -> Result<Box<dyn Subscription>, PublicError> {
        Ok(Box::new(One {
            event: Some(self.event.clone()),
            acks: self.acks.clone(),
        }))
    }
    fn validate_event(&self, _: &Value) -> Result<(), PublicError> {
        Ok(())
    }
}
fn render(event: Value) -> (Result<(), api::sse::StreamError>, Vec<u8>, usize) {
    let acks = Arc::new(AtomicUsize::new(0));
    let g = Gateway::new(Source {
        event,
        acks: acks.clone(),
    });
    let r = Request {
        method: "GET".into(),
        target: "/v1/inference/r/events".into(),
        headers: vec![],
        body: vec![],
    };
    let Response::Stream { mut body, .. } = g.handle(r).unwrap() else {
        panic!("stream")
    };
    let mut bytes = vec![];
    let result = body.write(&mut bytes);
    body.close().unwrap();
    (result, bytes, acks.load(Ordering::SeqCst))
}
fn accepted() -> Value {
    json!({"protocol":"mycelium.request_event.v1","request_id":"r","publisher_generation":1,"sequence":0,"type":"accepted"})
}
#[test]
fn host_cannot_bypass_exact_event_validation_or_disclose_private_fields() {
    let mut cases = vec![];
    let mut extra = accepted();
    extra["private_key"] = json!("must-not-leak");
    cases.push(extra);
    let mut wrong = accepted();
    wrong["protocol"] = json!("wrong");
    cases.push(wrong);
    let mut boolean = accepted();
    boolean["sequence"] = json!(true);
    cases.push(boolean);
    let mut token = accepted();
    token["type"] = json!("token");
    token["text"] = json!("x");
    cases.push(token);
    for event in cases {
        let (result, bytes, acks) = render(event);
        assert_eq!(result.unwrap_err().code, "invalid_stream_event");
        assert!(bytes.is_empty());
        assert_eq!(acks, 0);
    }
}
#[test]
fn both_event_protocols_keep_their_exact_fields() {
    for protocol in ["mycelium.request_event.v1", "mycelium.request_event.v2"] {
        let mut event = accepted();
        event["protocol"] = json!(protocol);
        let (result, bytes, acks) = render(event);
        result.unwrap();
        assert_eq!(acks, 1);
        assert!(
            String::from_utf8(bytes)
                .unwrap()
                .starts_with("id: 1:0\nevent: accepted\ndata: ")
        );
    }
}
