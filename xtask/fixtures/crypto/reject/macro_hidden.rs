// SPDX-License-Identifier: AGPL-3.0-only
// Constants hidden inside macro invocations and nested modules are still seen.
mod tables {
    pub fn init() -> Vec<u32> {
        vec![0x6a09e667, 0xbb67ae85] // expect: CR001
    }
    pub fn rounds() -> [u32; 2] {
        let words = [0x428a2f98_u32, 0x71374491_u32]; // expect: CR001
        words
    }
}
pub fn show() {
    println!("{:x}", 0x5a827999_u32); // expect: CR001
}
