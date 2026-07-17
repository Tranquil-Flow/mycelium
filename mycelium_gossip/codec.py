from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from .schema import RecordEnvelope, SchemaError, canonical_json_bytes, record_from_dict


MAX_RECORD_BYTES = 64 * 1024
MAX_DEPTH = 16
MAX_COLLECTION_ITEMS = 2_048
MAX_STRING_BYTES = 16 * 1024


class DecodeError(ValueError):
    pass


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _unique_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _check_bounds(value: Any, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise DecodeError("record nesting is too deep")
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_STRING_BYTES:
            raise DecodeError("record string is too large")
        return
    if isinstance(value, dict):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise DecodeError("record object has too many fields")
        for key, item in value.items():
            _check_bounds(key, depth + 1)
            _check_bounds(item, depth + 1)
        return
    if isinstance(value, list):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise DecodeError("record array has too many items")
        for item in value:
            _check_bounds(item, depth + 1)


def encode_record(record: RecordEnvelope) -> bytes:
    encoded = canonical_json_bytes(record.to_dict())
    if len(encoded) > MAX_RECORD_BYTES:
        raise SchemaError("encoded record is too large")
    return encoded


def decode_record(data: bytes) -> RecordEnvelope:
    if not isinstance(data, bytes):
        raise DecodeError("record must be bytes")
    if len(data) > MAX_RECORD_BYTES:
        raise DecodeError("record is too large")
    try:
        text = data.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DecodeError(str(exc)) from exc
    if not isinstance(value, dict):
        raise DecodeError("record root must be an object")
    _check_bounds(value)
    try:
        return record_from_dict(value)
    except SchemaError as exc:
        raise DecodeError(str(exc)) from exc
