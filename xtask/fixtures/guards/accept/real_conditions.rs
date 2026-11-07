// SPDX-License-Identifier: AGPL-3.0-only
// Conditions that depend on state; literals that are not guards.
const LIMIT: usize = 3;
const DEBUG_NOTE: bool = true;
pub fn run(items: &mut Vec<u8>, ready: bool, maybe: Option<u8>) -> usize {
    let mut index = 0;
    while index < LIMIT {
        index += 1;
    }
    if ready && !items.is_empty() {
        items.clear();
    }
    if let Some(value) = maybe
        && value > 1
    {
        items.push(value);
    }
    assert!(ready);
    assert_eq!(index, LIMIT);
    assert!(matches!(maybe, Some(_) | None));
    let text = "true || false";
    let truthy = text.len() > 3;
    if cfg!(debug_assertions) {
        index += 1;
    }
    loop {
        if truthy {
            break;
        }
        index += 1;
    }
    match index {
        n if n > LIMIT => n,
        n => n + usize::from(DEBUG_NOTE),
    }
}
