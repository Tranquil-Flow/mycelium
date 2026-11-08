// SPDX-License-Identifier: AGPL-3.0-only
//! Line-framed local tokenizer helper over the product's `tokenizers` crate and
//! the selected snapshot's `tokenizer.json`. Private stdin/stdout only; no
//! template, trimming or added special tokens (the caller sends finished ChatML).
use crate::loopback::Result;
use serde_json::{Value, json};
use std::io::{BufRead, Write};
pub fn serve(tokenizer: &std::path::Path) -> Result<()> {
    let codec = tokenizers::Tokenizer::from_file(tokenizer)?;
    let mut out = std::io::stdout().lock();
    for line in std::io::stdin().lock().lines() {
        let request: Value = serde_json::from_str(&line?)?;
        let reply = if let Some(text) = request["encode"].as_str() {
            json!({"ids":codec.encode(text, false)?.get_ids()})
        } else if let Some(id) = request["decode"].as_u64() {
            json!({"text":codec.decode(&[u32::try_from(id)?], false)?})
        } else {
            json!({"error":"tokenizer_request_invalid"})
        };
        writeln!(out, "{reply}")?;
        out.flush()?;
    }
    Ok(())
}
