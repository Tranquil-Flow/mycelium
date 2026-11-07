// SPDX-License-Identifier: AGPL-3.0-only
//! External migration observations, never imports or links the old application.
use budgets::{BudgetId, Entries};
use serde_json::{Map, Value, json};
use std::time::Duration;

pub mod boundaries;
pub mod http;
pub mod process;
pub mod scenario;
pub mod twohost;
pub type Result<T> = std::result::Result<T, String>;

pub fn duration(id: BudgetId) -> Result<Duration> {
    let span = id
        .span(Entries::new(0))
        .map_err(|e| format!("budget:{id:?}:{e:?}"))?;
    Ok(Duration::from_millis(span.millis()))
}
pub fn io(phase: &str, error: std::io::Error) -> String {
    format!("{phase}:io:{:?}", error.kind())
}
pub fn require(ok: bool, code: &str) -> Result<()> {
    if ok { Ok(()) } else { Err(code.to_owned()) }
}
/// No DNS, redirects, URL credentials, alternate host spellings or proxy use.
pub fn loopback_origin(input: &str) -> Result<u16> {
    let raw = input
        .strip_prefix("http://127.0.0.1:")
        .ok_or("origin:not_loopback")?;
    require(
        !raw.is_empty() && raw.bytes().all(|c| c.is_ascii_digit()),
        "origin:invalid_port",
    )?;
    let port = raw.parse::<u16>().map_err(|_| "origin:invalid_port")?;
    require(
        port != 0 && raw == port.to_string(),
        "origin:noncanonical_port",
    )?;
    Ok(port)
}
/// Call sites declare scalar JSON pointers; nested objects/unknown fields never escape.
pub fn project(input: &Value, fields: &[&str]) -> Result<Value> {
    let mut result = Map::new();
    for pointer in fields {
        let value = input
            .pointer(pointer)
            .ok_or_else(|| format!("projection:missing:{pointer}"))?;
        require(
            !value.is_object() && !value.is_array(),
            "projection:non_scalar",
        )?;
        require(
            result
                .insert((*pointer).to_owned(), value.clone())
                .is_none(),
            "projection:duplicate",
        )?;
    }
    Ok(Value::Object(result))
}
/// Exact ordered observation equality. Runtime/provenance are NOT observations.
pub fn compare(old: &Value, new: &Value) -> Result<()> {
    let a = old.as_array().ok_or("compare:old_not_array")?;
    let b = new.as_array().ok_or("compare:new_not_array")?;
    require(!a.is_empty(), "compare:empty_corpus")?;
    require(a.len() == b.len(), "compare:observation_count")?;
    for (index, (left, right)) in a.iter().zip(b).enumerate() {
        if left != right {
            return Err(format!("compare:observation_mismatch:{index}"));
        }
    }
    Ok(())
}
/// Strict completed-frame SSE reader. Incomplete terminal frames never count as success.
/// `id` persists across frames; comments are ignored; multi-line data retains ordering.
pub fn parse_sse(input: &str) -> Result<Vec<Value>> {
    let mut result = Vec::new();
    let mut event = String::new();
    let mut id = String::new();
    let mut data = Vec::new();
    let mut pending = false;
    for line in input.lines() {
        if line.is_empty() {
            if !data.is_empty() {
                result.push(json!({"event":if event.is_empty(){"message"}else{&event},"id":id,"data":data.join("\n")}));
            }
            event.clear();
            data.clear();
            pending = false;
        } else if !line.starts_with(':') {
            pending = true;
            let (key, value) = line.split_once(':').unwrap_or((line, ""));
            let value = value.strip_prefix(' ').unwrap_or(value);
            match key {
                "event" => event = value.to_owned(),
                "id" => {
                    require(!value.contains('\0'), "sse:nul_id")?;
                    id = value.to_owned();
                }
                "data" => data.push(value.to_owned()),
                _ => {}
            }
        }
    }
    require(!pending, "sse:incomplete_frame")?;
    Ok(result)
}
