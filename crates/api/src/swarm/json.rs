// SPDX-License-Identifier: AGPL-3.0-only
//! Order-preserving JSON value with the exact Python `json.loads`/`json.dumps`
//! behaviour the v0.1.4 swarm directory relies on (wire.py:96-122, store.py,
//! server.py:135-136 @ f4e366e9). serde_json maps sort keys, which would change
//! response bytes, so the directory keeps its own insertion-ordered value.
use super::wire::WireError;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum J {
    Null,
    Bool(bool),
    Int(i64),
    /// An integer that does not fit i64; always out of the 2^53-1 wire range.
    Huge,
    Str(String),
    /// A string holding a lone UTF-16 surrogate (Python keeps it; category Cs).
    Surrogate,
    Arr(Vec<J>),
    Obj(Vec<(String, J)>),
    /// A fully validated subtree nested deeper than `PARSE_DEPTH`. Python's `json.loads` returns
    /// it intact and `_check_value` then refuses the first node deeper than 6, so every consumer
    /// refuses it as `document_too_deep`; it is never serialised.
    Deep,
}
const PARSE_DEPTH: usize = 200;
impl J {
    pub fn str(s: &str) -> J {
        J::Str(s.to_string())
    }
    pub fn obj(pairs: Vec<(&str, J)>) -> J {
        J::Obj(pairs.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
    }
    pub fn get(&self, key: &str) -> Option<&J> {
        match self {
            J::Obj(p) => p.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
    pub fn as_str(&self) -> Option<&str> {
        match self {
            J::Str(s) => Some(s),
            _ => None,
        }
    }
    pub fn as_int(&self) -> Option<i64> {
        match self {
            J::Int(v) => Some(*v),
            _ => None,
        }
    }
    pub fn is_null(&self) -> bool {
        matches!(self, J::Null)
    }
    pub fn without(&self, key: &str) -> J {
        match self {
            J::Obj(p) => J::Obj(p.iter().filter(|(k, _)| k != key).cloned().collect()),
            other => other.clone(),
        }
    }
    /// Python `dict(row, name=value)` for an existing or new key.
    pub fn with(&self, key: &str, value: J) -> J {
        let mut pairs = match self {
            J::Obj(p) => p.clone(),
            _ => Vec::new(),
        };
        match pairs.iter_mut().find(|(k, _)| k == key) {
            Some(slot) => slot.1 = value,
            None => pairs.push((key.to_string(), value)),
        }
        J::Obj(pairs)
    }
}
/// Open container of `Parser::skip_deep`.
enum Frame {
    Arr,
    Obj {
        seen: std::collections::HashSet<String>,
        dup: bool,
    },
}
fn invalid() -> WireError {
    WireError::new("json_invalid", 400)
}
struct Parser<'a> {
    b: &'a [u8],
    i: usize,
}
impl Parser<'_> {
    fn ws(&mut self) {
        while self
            .b
            .get(self.i)
            .is_some_and(|c| matches!(c, b' ' | b'\t' | b'\n' | b'\r'))
        {
            self.i += 1;
        }
    }
    fn lit(&mut self, word: &[u8]) -> bool {
        if self.b[self.i..].starts_with(word) {
            self.i += word.len();
            true
        } else {
            false
        }
    }
    fn value(&mut self, depth: usize) -> Result<J, WireError> {
        match self.b.get(self.i).copied() {
            Some(b'{' | b'[') if depth > PARSE_DEPTH => self.skip_deep(),
            Some(b'{') => self.object(depth),
            Some(b'[') => self.array(depth),
            _ => self.scalar(),
        }
    }
    /// Validates (strictly, in stream order, like `json.loads` with the directory's hooks) a
    /// container nested too deep to recurse into, with an explicit stack. Duplicate keys are
    /// reported when the object closes, as Python's `object_pairs_hook` does.
    fn skip_deep(&mut self) -> Result<J, WireError> {
        let mut stack: Vec<Frame> = Vec::new();
        'value: loop {
            // a value starts here
            match self.b.get(self.i).copied() {
                Some(b'[') => {
                    self.i += 1;
                    self.ws();
                    if self.b.get(self.i) == Some(&b']') {
                        self.i += 1;
                    } else {
                        stack.push(Frame::Arr);
                        continue 'value;
                    }
                }
                Some(b'{') => {
                    self.i += 1;
                    self.ws();
                    if self.b.get(self.i) == Some(&b'}') {
                        self.i += 1;
                    } else {
                        stack.push(Frame::Obj {
                            seen: Default::default(),
                            dup: false,
                        });
                        self.skip_key(&mut stack)?;
                        continue 'value;
                    }
                }
                _ => {
                    self.scalar()?;
                }
            }
            // a value just completed: close containers or move to the next element
            loop {
                if stack.is_empty() {
                    return Ok(J::Deep);
                }
                self.ws();
                match (stack.last_mut(), self.b.get(self.i).copied()) {
                    (Some(Frame::Arr), Some(b',')) => {
                        self.i += 1;
                        self.ws();
                        continue 'value;
                    }
                    (Some(Frame::Arr), Some(b']')) => {
                        self.i += 1;
                        stack.pop();
                    }
                    (Some(Frame::Obj { .. }), Some(b',')) => {
                        self.i += 1;
                        self.ws();
                        self.skip_key(&mut stack)?;
                        continue 'value;
                    }
                    (Some(Frame::Obj { dup, .. }), Some(b'}')) => {
                        if *dup {
                            return Err(WireError::new("json_duplicate_key", 400));
                        }
                        self.i += 1;
                        stack.pop();
                    }
                    _ => return Err(invalid()),
                }
            }
        }
    }
    /// `"key" :` of the object on top of `stack`; leaves the cursor at the member value.
    fn skip_key(&mut self, stack: &mut [Frame]) -> Result<(), WireError> {
        if self.b.get(self.i) != Some(&b'"') {
            return Err(invalid());
        }
        let key = match self.raw_string()? {
            Ok(k) => k,
            Err(()) => "\u{FFFD}".to_string(),
        };
        if let Some(Frame::Obj { seen, dup }) = stack.last_mut()
            && !seen.insert(key)
        {
            *dup = true;
        }
        self.ws();
        if self.b.get(self.i) != Some(&b':') {
            return Err(invalid());
        }
        self.i += 1;
        self.ws();
        Ok(())
    }
    fn scalar(&mut self) -> Result<J, WireError> {
        match self.b.get(self.i).copied() {
            Some(b'"') => self.string(),
            Some(b'n') if self.lit(b"null") => Ok(J::Null),
            Some(b't') if self.lit(b"true") => Ok(J::Bool(true)),
            Some(b'f') if self.lit(b"false") => Ok(J::Bool(false)),
            Some(b'N') if self.lit(b"NaN") => Err(WireError::new("json_float_refused", 400)),
            Some(b'I') if self.lit(b"Infinity") => Err(WireError::new("json_float_refused", 400)),
            Some(b'-') if self.b[self.i..].starts_with(b"-Infinity") => {
                Err(WireError::new("json_float_refused", 400))
            }
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => Err(invalid()),
        }
    }
    fn number(&mut self) -> Result<J, WireError> {
        let start = self.i;
        if self.b[self.i] == b'-' {
            self.i += 1;
        }
        match self.b.get(self.i) {
            Some(b'0') => self.i += 1,
            Some(b'1'..=b'9') => {
                while self.b.get(self.i).is_some_and(u8::is_ascii_digit) {
                    self.i += 1;
                }
            }
            _ => return Err(invalid()),
        }
        let digits_end = self.i;
        let digit_at = |p: usize| self.b.get(p).is_some_and(u8::is_ascii_digit);
        let mut float = false;
        if self.b.get(self.i) == Some(&b'.') && digit_at(self.i + 1) {
            float = true;
        }
        if !float && matches!(self.b.get(self.i), Some(b'e' | b'E')) {
            let sign = usize::from(matches!(self.b.get(self.i + 1), Some(b'+' | b'-')));
            if digit_at(self.i + 1 + sign) {
                float = true;
            }
        }
        if float {
            return Err(WireError::new("json_float_refused", 400));
        }
        let text = std::str::from_utf8(&self.b[start..digits_end]).map_err(|_| invalid())?;
        Ok(text.parse::<i64>().map_or(J::Huge, J::Int))
    }
    fn hex4(&mut self) -> Result<u32, WireError> {
        let slice = self.b.get(self.i..self.i + 4).ok_or_else(invalid)?;
        let text = std::str::from_utf8(slice).map_err(|_| invalid())?;
        if !text.bytes().all(|c| c.is_ascii_hexdigit()) {
            return Err(invalid());
        }
        self.i += 4;
        u32::from_str_radix(text, 16).map_err(|_| invalid())
    }
    fn raw_string(&mut self) -> Result<Result<String, ()>, WireError> {
        self.i += 1; // opening quote
        let mut out = String::new();
        let mut bad = false;
        loop {
            let start = self.i;
            while self
                .b
                .get(self.i)
                .is_some_and(|c| *c != b'"' && *c != b'\\' && *c >= 0x20)
            {
                self.i += 1;
            }
            out.push_str(std::str::from_utf8(&self.b[start..self.i]).map_err(|_| invalid())?);
            match self.b.get(self.i).copied() {
                Some(b'"') => {
                    self.i += 1;
                    return Ok(if bad { Err(()) } else { Ok(out) });
                }
                Some(b'\\') => {
                    self.i += 1;
                    let esc = self.b.get(self.i).copied().ok_or_else(invalid)?;
                    self.i += 1;
                    match esc {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let unit = self.hex4()?;
                            if (0xD800..0xDC00).contains(&unit)
                                && self.b[self.i..].starts_with(b"\\u")
                            {
                                let save = self.i;
                                self.i += 2;
                                let low = self.hex4()?;
                                if (0xDC00..0xE000).contains(&low) {
                                    let c = 0x10000 + ((unit - 0xD800) << 10) + (low - 0xDC00);
                                    out.push(char::from_u32(c).ok_or_else(invalid)?);
                                } else {
                                    self.i = save;
                                    bad = true;
                                }
                            } else if let Some(c) = char::from_u32(unit) {
                                out.push(c);
                            } else {
                                bad = true;
                            }
                        }
                        _ => return Err(invalid()),
                    }
                }
                _ => return Err(invalid()), // unterminated or raw control character
            }
        }
    }
    fn string(&mut self) -> Result<J, WireError> {
        Ok(match self.raw_string()? {
            Ok(s) => J::Str(s),
            Err(()) => J::Surrogate,
        })
    }
    fn array(&mut self, depth: usize) -> Result<J, WireError> {
        self.i += 1;
        let mut items = Vec::new();
        self.ws();
        if self.b.get(self.i) == Some(&b']') {
            self.i += 1;
            return Ok(J::Arr(items));
        }
        loop {
            self.ws();
            items.push(self.value(depth + 1)?);
            self.ws();
            match self.b.get(self.i) {
                Some(b',') => self.i += 1,
                Some(b']') => {
                    self.i += 1;
                    return Ok(J::Arr(items));
                }
                _ => return Err(invalid()),
            }
        }
    }
    fn object(&mut self, depth: usize) -> Result<J, WireError> {
        self.i += 1;
        let mut pairs: Vec<(String, J)> = Vec::new();
        self.ws();
        if self.b.get(self.i) == Some(&b'}') {
            self.i += 1;
            return Ok(J::Obj(pairs));
        }
        loop {
            self.ws();
            if self.b.get(self.i) != Some(&b'"') {
                return Err(invalid());
            }
            let key = match self.raw_string()? {
                Ok(k) => k,
                Err(()) => "\u{FFFD}".to_string(), // fails the key grammar like Python's lone surrogate
            };
            self.ws();
            if self.b.get(self.i) != Some(&b':') {
                return Err(invalid());
            }
            self.i += 1;
            self.ws();
            let value = self.value(depth + 1)?;
            pairs.push((key, value));
            self.ws();
            match self.b.get(self.i) {
                Some(b',') => self.i += 1,
                Some(b'}') => {
                    self.i += 1;
                    let mut seen = std::collections::HashSet::new();
                    if pairs.iter().any(|(k, _)| !seen.insert(k.as_str())) {
                        return Err(WireError::new("json_duplicate_key", 400));
                    }
                    return Ok(J::Obj(pairs));
                }
                _ => return Err(invalid()),
            }
        }
    }
}
/// Python `json.loads(raw.decode('utf-8'), ...)` with the directory's hooks.
pub fn parse(raw: &[u8]) -> Result<J, WireError> {
    std::str::from_utf8(raw).map_err(|_| invalid())?;
    let mut parser = Parser { b: raw, i: 0 };
    parser.ws();
    let value = parser.value(0)?;
    parser.ws();
    if parser.i != raw.len() {
        return Err(invalid());
    }
    Ok(value)
}
/// Trusted stored text (our own `dumps_stored` output): no wire hooks.
pub fn parse_stored(raw: &str) -> Result<J, WireError> {
    parse(raw.as_bytes())
}
fn escape_into(out: &mut String, s: &str, ascii: bool) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (ascii && !(' '..='~').contains(&c)) => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}
fn write(out: &mut String, v: &J, sorted: bool, ascii: bool, item: &str, colon: &str) {
    match v {
        J::Null => out.push_str("null"),
        J::Bool(true) => out.push_str("true"),
        J::Bool(false) => out.push_str("false"),
        J::Int(n) => out.push_str(&n.to_string()),
        J::Huge | J::Surrogate | J::Deep => out.push_str("null"), // never serialised: refused at the wire
        J::Str(s) => escape_into(out, s, ascii),
        J::Arr(items) => {
            out.push('[');
            for (n, x) in items.iter().enumerate() {
                if n > 0 {
                    out.push_str(item);
                }
                write(out, x, sorted, ascii, item, colon);
            }
            out.push(']');
        }
        J::Obj(pairs) => {
            let mut order: Vec<&(String, J)> = pairs.iter().collect();
            if sorted {
                order.sort_by(|a, b| a.0.cmp(&b.0));
            }
            out.push('{');
            for (n, (k, x)) in order.into_iter().map(|p| (&p.0, &p.1)).enumerate() {
                if n > 0 {
                    out.push_str(item);
                }
                escape_into(out, k, ascii);
                out.push_str(colon);
                write(out, x, sorted, ascii, item, colon);
            }
            out.push('}');
        }
    }
}
/// `json.dumps(v, separators=(',', ':'), ensure_ascii=False)` — HTTP bodies.
pub fn dumps_response(v: &J) -> String {
    let mut out = String::new();
    write(&mut out, v, false, false, ",", ":");
    out
}
/// `json.dumps(v, sort_keys=True, separators=(',', ':'), ensure_ascii=False)` — canonical signing bytes.
pub fn dumps_canonical(v: &J) -> String {
    let mut out = String::new();
    write(&mut out, v, true, false, ",", ":");
    out
}
/// `json.dumps(v, sort_keys=True)` — the exact text store.py puts in SQLite.
pub fn dumps_stored(v: &J) -> String {
    let mut out = String::new();
    write(&mut out, v, true, true, ", ", ": ");
    out
}
