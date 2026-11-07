// SPDX-License-Identifier: AGPL-3.0-only
//! The oracle-equal answer property of the packaged chat gate (FX-L11-chat-answer): the answer
//! the node recorded for the oracle question must be a prefix of v0.1.4's greedy answer
//! (`tests/fixtures/chat-answer-oracle.json`, executed oracle output). Round 9's bundle_chat
//! passed with the answer "\n\n" because it only asserted that some token streamed.
#![allow(dead_code)]
use serde_json::Value;

pub const ORACLE: &str = include_str!("../fixtures/chat-answer-oracle.json");

/// `result` is the node's `result.json` of the launch: `token_ids` and `text` of the answer up to
/// where Stop landed. Exact up to the fixture's `agreement_horizon` tokens.
pub fn check(result: &Value) -> Result<usize, String> {
    let oracle: Value = serde_json::from_str(ORACLE).map_err(|e| e.to_string())?;
    let horizon = oracle["agreement_horizon"].as_u64().ok_or("horizon")? as usize;
    let expected: Vec<u64> = oracle["token_ids"]
        .as_array()
        .ok_or("oracle ids")?
        .iter()
        .map(|v| v.as_u64().ok_or("oracle id"))
        .collect::<Result<_, _>>()?;
    let pieces: Vec<&str> = oracle["pieces"]
        .as_array()
        .ok_or("oracle pieces")?
        .iter()
        .map(|v| v.as_str().ok_or("oracle piece"))
        .collect::<Result<_, _>>()?;
    let ids: Vec<u64> = result["token_ids"]
        .as_array()
        .ok_or("result has no token_ids")?
        .iter()
        .map(|v| v.as_u64().ok_or("token id"))
        .collect::<Result<_, _>>()?;
    let text = result["text"].as_str().ok_or("result has no text")?;
    if ids.is_empty() {
        return Err("no token was recorded".into());
    }
    if ids.len() > expected.len() {
        return Err(format!(
            "{} tokens exceed the {} requested",
            ids.len(),
            expected.len()
        ));
    }
    if text.trim().is_empty() {
        return Err(format!("whitespace-only answer {text:?}"));
    }
    let n = ids.len().min(horizon);
    if ids[..n] != expected[..n] {
        return Err(format!(
            "tokens {:?} differ from the oracle's {:?}",
            &ids[..n],
            &expected[..n]
        ));
    }
    // Exact text for the asserted prefix; the piece boundaries are the oracle's incremental decode.
    let want: String = pieces[..n].concat();
    if !text.starts_with(&want) {
        return Err(format!(
            "answer {text:?} does not start with the oracle's {want:?}"
        ));
    }
    if ids.len() <= horizon {
        let full: String = pieces[..ids.len()].concat();
        if text != full {
            return Err(format!(
                "answer {text:?} != the oracle's text for these tokens {full:?}"
            ));
        }
    }
    Ok(n)
}
