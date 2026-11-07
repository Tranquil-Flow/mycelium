// SPDX-License-Identifier: AGPL-3.0-only
//! Round 10: shape guard for the one place two lanes edited at once. SLICE-1 (RU-020) moved the
//! owner's token loop into a failure closure so a dead worker still settles the session from
//! the signed quorum; L11 (`f857d47`) replaced the whole-list tokenizer decode with v0.1.4's
//! incremental UTF-8 detokenizer. The integration preview 8 merge kept both
//! (`52c74db`); this guard fails if a later merge keeps only one. It reads source only: the
//! behaviour itself is L11's `tests/detokenizer.rs` (recorded oracle) and the native run.
use std::{fs, path::PathBuf};

fn native() -> String {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src/native.rs");
    fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}
/// The text of the token-loop closure: from `let looped = (|| ...` to its call `})();`.
fn closure(text: &str) -> &str {
    let start = text
        .find("let looped = (|| -> Result<()> {")
        .expect("the token loop is a failure closure (RU-020)");
    let end = text[start..]
        .find("})();")
        .expect("the closure is called in place")
        + start;
    &text[start..end]
}

#[test]
fn the_token_loop_is_a_failure_closure_that_streams_through_the_detokenizer() {
    let text = native();
    let body = closure(&text);
    for needed in [
        "detok.push(next,",
        "detok.finish()",
        "tokenizer.id_to_token(next)",
        "job.token(index as usize, suffix)",
        "watch.check()?",
        "receipts.sign(",
    ] {
        assert!(body.contains(needed), "token loop lost `{needed}`");
    }
    assert!(
        !body.contains(".decode(&token_ids"),
        "the whole-list decode failed a request whose character spans tokens (v0.1.4 streams)"
    );
    assert!(
        !body.contains("nonappend_token_decode"),
        "v0.1.4 has no such refusal"
    );
}
#[test]
fn the_detokenizer_state_outlives_the_closure_and_knows_the_stop_ids() {
    let text = native();
    let decl = text
        .find("let mut detok = crate::detok::Detokenizer::new(&[151643, 151645]);")
        .expect("one detokenizer per request, built with the Qwen2 stop ids");
    let looped = text.find("let looped = (|| -> Result<()> {").unwrap();
    assert!(decl < looped, "declared before the closure that borrows it");
    assert_eq!(text.matches("detok::Detokenizer::new(").count(), 1);
    // The stop test and the detokenizer use the same two ids.
    assert!(closure(&text).contains("matches!(next, 151643 | 151645)"));
}
