// SPDX-License-Identifier: AGPL-3.0-only
// Match guards decided by a literal.
pub fn classify(value: u32) -> u32 {
    match value {
        n if true => n, // expect: CG001
        n if n > 1 && false => n + 1, // expect: CG001
        n => n + 2,
    }
}
