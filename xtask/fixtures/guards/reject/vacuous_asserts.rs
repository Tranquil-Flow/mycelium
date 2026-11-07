// SPDX-License-Identifier: AGPL-3.0-only
// Assertions that cannot fail, or cannot be reached by a failing input.
pub fn check(ready: bool) {
    assert!(true); // expect: CG001
    debug_assert!(!false, "never"); // expect: CG001
    assert!(ready || true, "always satisfied"); // expect: CG001
}
