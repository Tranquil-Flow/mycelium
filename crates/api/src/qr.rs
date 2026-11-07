// SPDX-License-Identifier: AGPL-3.0-only
//! QR renderer of the directory's `link.svg` (server.py:55-64 @ f4e366e9). The oracle encodes
//! with the vendored Nayuki `qrcodegen` (`mycelium_a13_macos/vendor/qrcodegen.py`, MIT):
//! `QrCode.encode_text(text, Ecc.MEDIUM)` = automatic segment mode (numeric, else
//! alphanumeric, else UTF-8 bytes), smallest fitting version 1..=40, ECC boosted to
//! Q/H while the version does not grow, automatic mask by lowest penalty (first lowest wins).
//! This is a line-for-line port of that algorithm (pure code, no dependency); byte equality with
//! the executed oracle is proven on a corpus in `tests/qr.rs`.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct QrError;

const MIN_VERSION: usize = 1;
const MAX_VERSION: usize = 40;
/// Quiet zone, in modules (`qr_svg(text, border=4)`).
const BORDER: usize = 4;
const PENALTY_N1: i64 = 3;
const PENALTY_N2: i64 = 3;
const PENALTY_N3: i64 = 40;
const PENALTY_N4: i64 = 10;

/// Error correction level; `ordinal` indexes the tables, `format_bits` goes into the code.
#[derive(Clone, Copy)]
struct Ecc {
    ordinal: usize,
    format_bits: u32,
}
const MEDIUM: Ecc = Ecc {
    ordinal: 1,
    format_bits: 0,
};
const QUARTILE: Ecc = Ecc {
    ordinal: 2,
    format_bits: 3,
};
const HIGH: Ecc = Ecc {
    ordinal: 3,
    format_bits: 2,
};

#[rustfmt::skip]
const ECC_CODEWORDS_PER_BLOCK: [[i32; 41]; 4] = [
    [-1,  7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
    [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28],
    [-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
    [-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
];
#[rustfmt::skip]
const NUM_ERROR_CORRECTION_BLOCKS: [[i32; 41]; 4] = [
    [-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4,  4,  4,  4,  4,  6,  6,  6,  6,  7,  8,  8,  9,  9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25],
    [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5,  5,  8,  9,  9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49],
    [-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8,  8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68],
    [-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81],
];

const ALPHANUMERIC: &str = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:";

#[derive(Clone, Copy, Eq, PartialEq)]
enum Mode {
    Numeric,
    Alphanumeric,
    Byte,
}
impl Mode {
    fn mode_bits(self) -> u32 {
        match self {
            Self::Numeric => 0x1,
            Self::Alphanumeric => 0x2,
            Self::Byte => 0x4,
        }
    }
    fn char_count_bits(self, version: usize) -> usize {
        let counts = match self {
            Self::Numeric => [10, 12, 14],
            Self::Alphanumeric => [9, 11, 13],
            Self::Byte => [8, 16, 16],
        };
        counts[(version + 7) / 17]
    }
}
struct Segment {
    mode: Mode,
    num_chars: usize,
    data: Vec<bool>,
}
fn append_bits(bits: &mut Vec<bool>, value: u32, count: usize) {
    bits.extend((0..count).rev().map(|i| (value >> i) & 1 != 0));
}
/// `QrSegment.make_segments`: one segment in the most compact single mode, none for "".
fn make_segments(text: &str) -> Vec<Segment> {
    if text.is_empty() {
        return Vec::new();
    }
    let mut data = Vec::new();
    if text.bytes().all(|b| b.is_ascii_digit()) {
        let digits = text.as_bytes();
        for chunk in digits.chunks(3) {
            let value = chunk.iter().fold(0u32, |a, d| a * 10 + u32::from(d - b'0'));
            append_bits(&mut data, value, chunk.len() * 3 + 1);
        }
        return vec![Segment {
            mode: Mode::Numeric,
            num_chars: digits.len(),
            data,
        }];
    }
    if text.chars().all(|c| ALPHANUMERIC.contains(c)) {
        let values: Vec<u32> = text
            .chars()
            .filter_map(|c| ALPHANUMERIC.find(c).map(|i| i as u32))
            .collect();
        for pair in values.chunks(2) {
            match pair {
                [a, b] => append_bits(&mut data, a * 45 + b, 11),
                [a] => append_bits(&mut data, *a, 6),
                _ => {}
            }
        }
        return vec![Segment {
            mode: Mode::Alphanumeric,
            num_chars: values.len(),
            data,
        }];
    }
    for byte in text.bytes() {
        append_bits(&mut data, u32::from(byte), 8);
    }
    vec![Segment {
        mode: Mode::Byte,
        num_chars: text.len(),
        data,
    }]
}
/// `QrSegment.get_total_bits`: `None` when a character count does not fit its field.
fn total_bits(segments: &[Segment], version: usize) -> Option<usize> {
    let mut total = 0;
    for seg in segments {
        let bits = seg.mode.char_count_bits(version);
        if seg.num_chars >= (1usize << bits) {
            return None;
        }
        total += 4 + bits + seg.data.len();
    }
    Some(total)
}

fn raw_data_modules(version: usize) -> usize {
    let mut result = (16 * version + 128) * version + 64;
    if version >= 2 {
        let align = version / 7 + 2;
        result -= (25 * align - 10) * align - 55;
        if version >= 7 {
            result -= 36;
        }
    }
    result
}
fn table(values: &[[i32; 41]; 4], ecc: Ecc, version: usize) -> usize {
    usize::try_from(values[ecc.ordinal][version]).unwrap_or(0)
}
fn data_codewords(version: usize, ecc: Ecc) -> usize {
    raw_data_modules(version) / 8
        - table(&ECC_CODEWORDS_PER_BLOCK, ecc, version)
            * table(&NUM_ERROR_CORRECTION_BLOCKS, ecc, version)
}

fn rs_multiply(x: u32, y: u32) -> u32 {
    let mut z = 0u32;
    for i in (0..8).rev() {
        z = (z << 1) ^ ((z >> 7) * 0x11D);
        z ^= ((y >> i) & 1) * x;
    }
    z
}
fn rs_divisor(degree: usize) -> Vec<u8> {
    let mut result = vec![0u8; degree - 1];
    result.push(1);
    let mut root = 1u32;
    for _ in 0..degree {
        for j in 0..degree {
            result[j] = rs_multiply(u32::from(result[j]), root) as u8;
            if j + 1 < degree {
                result[j] ^= result[j + 1];
            }
        }
        root = rs_multiply(root, 0x02);
    }
    result
}
fn rs_remainder(data: &[u8], divisor: &[u8]) -> Vec<u8> {
    let mut result = vec![0u8; divisor.len()];
    for b in data {
        let factor = u32::from(*b ^ result.remove(0));
        result.push(0);
        for (i, coef) in divisor.iter().enumerate() {
            result[i] ^= rs_multiply(u32::from(*coef), factor) as u8;
        }
    }
    result
}

struct Code {
    version: usize,
    size: usize,
    ecc: Ecc,
    modules: Vec<Vec<bool>>,
    is_function: Vec<Vec<bool>>,
}
impl Code {
    fn new(version: usize, ecc: Ecc, data: &[u8]) -> Self {
        let size = version * 4 + 17;
        let mut code = Self {
            version,
            size,
            ecc,
            modules: vec![vec![false; size]; size],
            is_function: vec![vec![false; size]; size],
        };
        code.draw_function_patterns();
        let all = code.add_ecc_and_interleave(data);
        code.draw_codewords(&all);
        // Automatic mask: the first lowest penalty wins.
        let mut best = 0;
        let mut min_penalty = i64::MAX;
        for mask in 0..8 {
            code.apply_mask(mask);
            code.draw_format_bits(mask);
            let penalty = code.penalty_score();
            if penalty < min_penalty {
                best = mask;
                min_penalty = penalty;
            }
            code.apply_mask(mask);
        }
        code.apply_mask(best);
        code.draw_format_bits(best);
        code
    }
    fn set_function(&mut self, x: usize, y: usize, dark: bool) {
        self.modules[y][x] = dark;
        self.is_function[y][x] = true;
    }
    fn draw_function_patterns(&mut self) {
        let size = self.size;
        for i in 0..size {
            self.set_function(6, i, i % 2 == 0);
            self.set_function(i, 6, i % 2 == 0);
        }
        self.draw_finder(3, 3);
        self.draw_finder(size - 4, 3);
        self.draw_finder(3, size - 4);
        let positions = self.alignment_positions();
        let n = positions.len();
        // Do not draw on the three finder corners.
        let skips = [(0, 0), (0, n.wrapping_sub(1)), (n.wrapping_sub(1), 0)];
        for i in 0..n {
            for j in 0..n {
                if !skips.contains(&(i, j)) {
                    self.draw_alignment(positions[i], positions[j]);
                }
            }
        }
        self.draw_format_bits(0);
        self.draw_version();
    }
    fn draw_format_bits(&mut self, mask: u32) {
        let data = self.ecc.format_bits << 3 | mask;
        let mut rem = data;
        for _ in 0..10 {
            rem = (rem << 1) ^ ((rem >> 9) * 0x537);
        }
        let bits = (data << 10 | rem) ^ 0x5412;
        let bit = |i: usize| (bits >> i) & 1 != 0;
        let size = self.size;
        for i in 0..6 {
            self.set_function(8, i, bit(i));
        }
        self.set_function(8, 7, bit(6));
        self.set_function(8, 8, bit(7));
        self.set_function(7, 8, bit(8));
        for i in 9..15 {
            self.set_function(14 - i, 8, bit(i));
        }
        for i in 0..8 {
            self.set_function(size - 1 - i, 8, bit(i));
        }
        for i in 8..15 {
            self.set_function(8, size - 15 + i, bit(i));
        }
        self.set_function(8, size - 8, true);
    }
    fn draw_version(&mut self) {
        if self.version < 7 {
            return;
        }
        let version = self.version as u32;
        let mut rem = version;
        for _ in 0..12 {
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25);
        }
        let bits = version << 12 | rem;
        for i in 0..18usize {
            let bit = (bits >> i) & 1 != 0;
            let a = self.size - 11 + i % 3;
            let b = i / 3;
            self.set_function(a, b, bit);
            self.set_function(b, a, bit);
        }
    }
    fn draw_finder(&mut self, x: usize, y: usize) {
        let limit = self.size as i32;
        for dy in -4i32..=4 {
            for dx in -4i32..=4 {
                let (xx, yy) = (x as i32 + dx, y as i32 + dy);
                if (0..limit).contains(&xx) && (0..limit).contains(&yy) {
                    let dist = dx.abs().max(dy.abs());
                    self.set_function(xx as usize, yy as usize, dist != 2 && dist != 4);
                }
            }
        }
    }
    fn draw_alignment(&mut self, x: usize, y: usize) {
        for dy in -2i32..=2 {
            for dx in -2i32..=2 {
                let dist = dx.abs().max(dy.abs());
                self.set_function(
                    (x as i32 + dx) as usize,
                    (y as i32 + dy) as usize,
                    dist != 1,
                );
            }
        }
    }
    fn alignment_positions(&self) -> Vec<usize> {
        let ver = self.version;
        if ver == 1 {
            return Vec::new();
        }
        let num_align = ver / 7 + 2;
        let step = if ver == 32 {
            26
        } else {
            (ver * 4 + num_align * 2 + 1) / (num_align * 2 - 2) * 2
        };
        let mut result: Vec<usize> = (0..num_align - 1)
            .map(|i| self.size - 7 - i * step)
            .collect();
        result.push(6);
        result.reverse();
        result
    }
    fn add_ecc_and_interleave(&self, data: &[u8]) -> Vec<u8> {
        let version = self.version;
        let blocks_n = table(&NUM_ERROR_CORRECTION_BLOCKS, self.ecc, version);
        let block_ecc = table(&ECC_CODEWORDS_PER_BLOCK, self.ecc, version);
        let raw = raw_data_modules(version) / 8;
        let short_blocks = blocks_n - raw % blocks_n;
        let short_len = raw / blocks_n;
        let divisor = rs_divisor(block_ecc);
        let mut blocks: Vec<Vec<u8>> = Vec::new();
        let mut k = 0;
        for i in 0..blocks_n {
            let take = short_len - block_ecc + usize::from(i >= short_blocks);
            let mut dat = data[k..k + take].to_vec();
            k += take;
            let ecc = rs_remainder(&dat, &divisor);
            if i < short_blocks {
                dat.push(0);
            }
            dat.extend(ecc);
            blocks.push(dat);
        }
        let mut result = Vec::with_capacity(raw);
        for i in 0..blocks[0].len() {
            for (j, block) in blocks.iter().enumerate() {
                // Skip the padding byte in short blocks.
                if i != short_len - block_ecc || j >= short_blocks {
                    result.push(block[i]);
                }
            }
        }
        result
    }
    fn draw_codewords(&mut self, data: &[u8]) {
        let size = self.size;
        let mut i = 0usize;
        // `range(size - 1, 0, -2)`: the loop variable is adjusted per iteration, never carried.
        let mut pair = size as i32 - 1;
        while pair > 0 {
            let right = if pair <= 6 { pair - 1 } else { pair };
            for vert in 0..size {
                for j in 0..2 {
                    let x = (right - j) as usize;
                    let upward = (right + 1) & 2 == 0;
                    let y = if upward { size - 1 - vert } else { vert };
                    if !self.is_function[y][x] && i < data.len() * 8 {
                        self.modules[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 != 0;
                        i += 1;
                    }
                }
            }
            pair -= 2;
        }
    }
    fn apply_mask(&mut self, mask: u32) {
        for y in 0..self.size {
            for x in 0..self.size {
                let (xi, yi) = (x as u64, y as u64);
                let invert = match mask {
                    0 => (xi + yi) % 2 == 0,
                    1 => yi % 2 == 0,
                    2 => xi % 3 == 0,
                    3 => (xi + yi) % 3 == 0,
                    4 => (xi / 3 + yi / 2) % 2 == 0,
                    5 => xi * yi % 2 + xi * yi % 3 == 0,
                    6 => (xi * yi % 2 + xi * yi % 3) % 2 == 0,
                    _ => ((xi + yi) % 2 + xi * yi % 3) % 2 == 0,
                };
                self.modules[y][x] ^= invert && !self.is_function[y][x];
            }
        }
    }
    /// One row or column scan of `_get_penalty_score` (runs and finder-like patterns).
    fn line_penalty(&self, line: impl Iterator<Item = bool>) -> i64 {
        let (mut color, mut run, mut history) = (false, 0usize, [0usize; 7]);
        let mut result = 0i64;
        for cell in line {
            if cell == color {
                run += 1;
                if run == 5 {
                    result += PENALTY_N1;
                } else if run > 5 {
                    result += 1;
                }
            } else {
                self.finder_add_history(run, &mut history);
                if !color {
                    result += self.finder_count_patterns(&history) * PENALTY_N3;
                }
                color = cell;
                run = 1;
            }
        }
        result + self.finder_terminate_and_count(color, run, &mut history) * PENALTY_N3
    }
    fn penalty_score(&self) -> i64 {
        let size = self.size;
        let mut result = 0i64;
        for y in 0..size {
            result += self.line_penalty((0..size).map(|x| self.modules[y][x]));
        }
        for x in 0..size {
            result += self.line_penalty((0..size).map(|y| self.modules[y][x]));
        }
        for y in 0..size - 1 {
            for x in 0..size - 1 {
                let c = self.modules[y][x];
                if c == self.modules[y][x + 1]
                    && c == self.modules[y + 1][x]
                    && c == self.modules[y + 1][x + 1]
                {
                    result += PENALTY_N2;
                }
            }
        }
        let dark = self.modules.iter().flatten().filter(|cell| **cell).count() as i64;
        let total = (size * size) as i64;
        let k = ((dark * 20 - total * 10).abs() + total - 1) / total - 1;
        result + k * PENALTY_N4
    }
    fn finder_count_patterns(&self, history: &[usize; 7]) -> i64 {
        let n = history[1];
        let core =
            n > 0 && history[2] == n && history[4] == n && history[5] == n && history[3] == n * 3;
        i64::from(core && history[0] >= n * 4 && history[6] >= n)
            + i64::from(core && history[6] >= n * 4 && history[0] >= n)
    }
    fn finder_terminate_and_count(
        &self,
        color: bool,
        mut run: usize,
        history: &mut [usize; 7],
    ) -> i64 {
        if color {
            self.finder_add_history(run, history);
            run = 0;
        }
        run += self.size;
        self.finder_add_history(run, history);
        self.finder_count_patterns(history)
    }
    /// `runhistory.appendleft` of a 7-slot deque.
    fn finder_add_history(&self, mut run: usize, history: &mut [usize; 7]) {
        if history[0] == 0 {
            run += self.size;
        }
        history.rotate_right(1);
        history[0] = run;
    }
}

/// `QrCode.encode_text(text, QrCode.Ecc.MEDIUM)`; `Err` is `DataTooLongError`.
fn encode_text(text: &str) -> Result<Code, QrError> {
    let segments = make_segments(text);
    let mut version = MIN_VERSION;
    let used = loop {
        let capacity = data_codewords(version, MEDIUM) * 8;
        match total_bits(&segments, version) {
            Some(used) if used <= capacity => break used,
            _ if version >= MAX_VERSION => return Err(QrError),
            _ => version += 1,
        }
    };
    let mut ecc = MEDIUM;
    for candidate in [MEDIUM, QUARTILE, HIGH] {
        if used <= data_codewords(version, candidate) * 8 {
            ecc = candidate;
        }
    }
    let mut bits: Vec<bool> = Vec::new();
    for seg in &segments {
        append_bits(&mut bits, seg.mode.mode_bits(), 4);
        append_bits(
            &mut bits,
            seg.num_chars as u32,
            seg.mode.char_count_bits(version),
        );
        bits.extend_from_slice(&seg.data);
    }
    let capacity = data_codewords(version, ecc) * 8;
    let terminator = 4.min(capacity - bits.len());
    append_bits(&mut bits, 0, terminator);
    let pad = (8 - bits.len() % 8) % 8;
    append_bits(&mut bits, 0, pad);
    let mut pad_byte = [0xECu32, 0x11].into_iter().cycle();
    while bits.len() < capacity {
        append_bits(&mut bits, pad_byte.next().unwrap_or(0xEC), 8);
    }
    let mut data = vec![0u8; bits.len() / 8];
    for (i, bit) in bits.iter().enumerate() {
        data[i >> 3] |= u8::from(*bit) << (7 - (i & 7));
    }
    Ok(Code::new(version, ecc, &data))
}

/// `qr_svg(text, border=4)` of server.py:55-64, byte for byte.
pub fn svg(text: &str) -> Result<Vec<u8>, QrError> {
    let code = encode_text(text)?;
    let mut path = String::new();
    for y in 0..code.size {
        for x in 0..code.size {
            if code.modules[y][x] {
                path.push_str(&format!("M{},{}h1v1h-1z", x + BORDER, y + BORDER));
            }
        }
    }
    let total = code.size + 2 * BORDER;
    Ok(format!(
        "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 {total} {total}\" role=\"img\" aria-label=\"Swarm link QR\"><rect width=\"100%\" height=\"100%\" fill=\"#fff\"/><path d=\"{path}\" fill=\"#000\"/></svg>"
    )
    .into_bytes())
}
