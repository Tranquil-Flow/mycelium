// SPDX-License-Identifier: AGPL-3.0-only
//! Feature-independent desktop response and nonblocking submission contract.
use crate::channel::Request;
pub struct Reply {
    pub status: u16,
    pub content_type: &'static str,
    pub body: Vec<u8>,
}
impl Reply {
    pub fn unavailable() -> Self {
        Self::error(503, "core_not_integrated")
    }
    pub fn error(status: u16, code: &'static str) -> Self {
        Self {
            status,
            content_type: "application/json",
            body: serde_json::json!({"error": code}).to_string().into_bytes(),
        }
    }
}
pub type Completion = Box<dyn FnOnce(Reply) + Send>;
/// Enqueue promptly, complete exactly once, and never perform backend work on
/// the caller (native GUI/protocol) thread. Cleanup owns admitted work as well.
pub trait Core: Send + Sync + 'static {
    fn submit(&self, request: Request, complete: Completion);
}
