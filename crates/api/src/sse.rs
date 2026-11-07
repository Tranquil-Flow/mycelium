// SPDX-License-Identifier: AGPL-3.0-only
//! Request SSE serializer at request_gateway/asgi.py:146-152. This owns a
//! single connection's framing/order, NOT the request terminal or cancellation.
//! L5 protocol::request::event supplies the validator after integration.
use crate::schema::PublicError;
use serde_json::Value;
use std::io::{ErrorKind, Write};
pub trait EventValidator {
    fn validate(&self, event: &Value) -> Result<(), PublicError>;
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StreamError {
    pub code: &'static str,
    pub io_kind: Option<ErrorKind>,
}
impl From<PublicError> for StreamError {
    fn from(e: PublicError) -> Self {
        Self {
            code: e.code,
            io_kind: None,
        }
    }
}
fn error(code: &'static str) -> StreamError {
    StreamError {
        code,
        io_kind: None,
    }
}
pub struct EventStream<V: EventValidator> {
    validator: V,
    credentials: Vec<Vec<u8>>,
    expected_request: Option<String>,
    last: Option<(String, u64, u64)>,
    terminal: bool,
    write_failed: bool,
}
impl<V: EventValidator> EventStream<V> {
    pub fn new(validator: V, credentials: Vec<Vec<u8>>) -> Self {
        Self {
            validator,
            credentials,
            expected_request: None,
            last: None,
            terminal: false,
            write_failed: false,
        }
    }
    pub fn for_request(
        validator: V,
        credentials: Vec<Vec<u8>>,
        request: String,
        cursor: Option<(u64, u64)>,
    ) -> Self {
        let mut stream = Self::new(validator, credentials);
        stream.last = cursor.map(|(generation, sequence)| (request.clone(), generation, sequence));
        stream.expected_request = Some(request);
        stream
    }
    pub fn write(
        &mut self,
        writer: &mut (impl Write + ?Sized),
        event: Value,
    ) -> Result<(), StreamError> {
        if self.terminal || self.write_failed {
            return Err(error("stream_already_terminal"));
        }
        self.validator.validate(&event)?;
        let request = event["request_id"]
            .as_str()
            .ok_or(error("invalid_stream_event"))?;
        let generation = event["publisher_generation"]
            .as_u64()
            .ok_or(error("invalid_stream_event"))?;
        let sequence = event["sequence"]
            .as_u64()
            .ok_or(error("invalid_stream_event"))?;
        let kind = event["type"]
            .as_str()
            .ok_or(error("invalid_stream_event"))?;
        if !matches!(
            kind,
            "accepted" | "token" | "lifecycle" | "completed" | "failed" | "cancelled"
        ) {
            return Err(error("invalid_stream_event"));
        }
        if self
            .expected_request
            .as_ref()
            .is_some_and(|id| id != request)
            || self
                .last
                .as_ref()
                .is_some_and(|(id, g, s)| id != request || *g != generation || *s >= sequence)
        {
            return Err(error("stream_order_invalid"));
        }
        let data = crate::wire::dumps_sorted(&event, crate::wire::Text::Utf8);
        if self.credentials.iter().any(|secret| {
            !secret.is_empty() && data.windows(secret.len()).any(|window| window == secret)
        }) {
            return Err(error("credential_leak_blocked"));
        }
        let mut frame = format!("id: {generation}:{sequence}\nevent: {kind}\ndata: ").into_bytes();
        frame.extend(data);
        frame.extend_from_slice(b"\n\n");
        if let Err(e) = writer.write_all(&frame).and_then(|()| writer.flush()) {
            self.write_failed = true;
            return Err(StreamError {
                code: "stream_write_failed",
                io_kind: Some(e.kind()),
            });
        }
        self.last = Some((request.into(), generation, sequence));
        self.terminal = matches!(kind, "completed" | "failed" | "cancelled");
        Ok(())
    }
}
