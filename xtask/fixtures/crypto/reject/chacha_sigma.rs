// SPDX-License-Identifier: AGPL-3.0-only
// RFC 8439 section 2.3: the ChaCha20 constant words / "expand 32-byte k".
pub const SIGMA_TEXT: &[u8; 16] = b"expand 32-byte k"; // expect: CR001
pub const SIGMA_SHORT: &str = "expand 16-byte k"; // expect: CR001
pub const SIGMA_WORDS: [u32; 4] = [0x61707865, 0x3320646e, 0x79622d32, 0x6b206574]; // expect: CR001
