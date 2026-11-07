// SPDX-License-Identifier: AGPL-3.0-only
// A locally declared bool const used as the condition is the same guard in disguise.
const SLOW_PATH: bool = false;
const FAST_PATH: bool = true;
pub fn pick(len: usize) -> usize {
    if SLOW_PATH { // expect: CG001
        return 0;
    }
    if len > 3 && FAST_PATH { // expect: CG001
        return 1;
    }
    while !FAST_PATH { // expect: CG001
        return 2;
    }
    len
}
