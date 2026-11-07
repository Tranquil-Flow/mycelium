// SPDX-License-Identifier: AGPL-3.0-only
//! Bounded JSON ingress. Call before any Value-based verifier.
use serde::{
    Deserializer,
    de::{self, MapAccess, Visitor},
};
use serde_json::{Map, Value, value::RawValue};

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct ParseError(pub &'static str);

pub fn object(raw: &[u8], maximum_bytes: usize) -> Result<Value, ParseError> {
    if raw.is_empty() || raw.len() > maximum_bytes {
        return Err(ParseError("frame_invalid"));
    }
    let raw: Box<RawValue> = serde_json::from_slice(raw).map_err(|_| ParseError("json_invalid"))?;
    let value = decode(raw.get(), 0).map_err(|_| ParseError("json_invalid"))?;
    if !value.is_object() {
        return Err(ParseError("frame_invalid"));
    }
    crate::canonical::to_bytes(&value).map_err(|_| ParseError("json_invalid"))?;
    Ok(value)
}

pub fn canonical_object(raw: &[u8], maximum_bytes: usize) -> Result<Value, ParseError> {
    let value = object(raw, maximum_bytes)?;
    if crate::canonical::to_bytes(&value).map_err(|_| ParseError("json_invalid"))? != raw {
        return Err(ParseError("frame_noncanonical"));
    }
    Ok(value)
}

/// Any JSON value (not only an object) with unique keys, whose canonical bytes are exactly `raw`
/// (`mycelium_seed.http._canonical_json_loads`). `frame_invalid` only for an over-long frame.
pub fn canonical_value(raw: &[u8], maximum_bytes: usize) -> Result<Value, ParseError> {
    if raw.len() > maximum_bytes {
        return Err(ParseError("frame_invalid"));
    }
    let token: Box<RawValue> =
        serde_json::from_slice(raw).map_err(|_| ParseError("json_invalid"))?;
    let value = decode(token.get(), 0).map_err(|_| ParseError("json_invalid"))?;
    if crate::canonical::to_bytes(&value).map_err(|_| ParseError("json_invalid"))? != raw {
        return Err(ParseError("frame_noncanonical"));
    }
    Ok(value)
}

// RawValue preserves arbitrary integer/float tokens without interpreting a
// user-supplied `$serde_json::private::Number` object as a synthetic number.
fn decode(raw: &str, depth: usize) -> Result<Value, serde_json::Error> {
    if depth >= 128 {
        return Err(de::Error::custom("nesting_limit"));
    }
    match raw.as_bytes().first() {
        Some(b'{') => {
            struct Object(usize);
            impl<'de> Visitor<'de> for Object {
                type Value = Value;
                fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                    f.write_str("unique-key object")
                }
                fn visit_map<A: MapAccess<'de>>(self, mut input: A) -> Result<Value, A::Error> {
                    let mut map = Map::new();
                    while let Some(key) = input.next_key::<String>()? {
                        if map.contains_key(&key) {
                            return Err(de::Error::custom("duplicate_key"));
                        }
                        let raw = input.next_value::<Box<RawValue>>()?;
                        let value = decode(raw.get(), self.0 + 1).map_err(de::Error::custom)?;
                        map.insert(key, value);
                    }
                    Ok(Value::Object(map))
                }
            }
            let mut deserializer = serde_json::Deserializer::from_str(raw);
            let value = deserializer.deserialize_map(Object(depth))?;
            deserializer.end()?;
            Ok(value)
        }
        Some(b'[') => {
            let items: Vec<Box<RawValue>> = serde_json::from_str(raw)?;
            items
                .iter()
                .map(|item| decode(item.get(), depth + 1))
                .collect::<Result<Vec<_>, _>>()
                .map(Value::Array)
        }
        _ => serde_json::from_str(raw),
    }
}
