# SPDX-License-Identifier: AGPL-3.0-only
"""Signed document wire format shared by the directory, the entry and the browser.

Portable by construction (the same rules are re-implemented in
``web/swarms.js`` and are meant for a Cloudflare Worker port):

* canonical bytes = UTF-8 of ``JSON.stringify`` with keys sorted, no spaces;
  only objects, arrays, strings, booleans, null and *safe integers* (no
  floats), strings without control characters or lone surrogates -- so Python
  ``json.dumps(sort_keys=True, ensure_ascii=False)`` and a JS stable stringify
  produce identical bytes;
* signature = Ed25519 over ``b"mycelium-swarm-sig-v1\\n" + canonical(doc - sig)``;
* every document carries a ``protocol`` naming its type and has an exact key
  set, so a signature for one type can never be replayed as another;
* keys are ``ed25519:<base64url(32 raw bytes)>``; signatures are base64url.

Verification is injected (``Verifier``) so the core stays free of any crypto
library: the Python service passes ``crypto.verify`` (``cryptography``), a
Worker passes WebCrypto.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
from typing import Any, Callable, Mapping

SIG_PREFIX = b'mycelium-swarm-sig-v1\n'
KEY_PREFIX = 'ed25519:'
MAX_SAFE_INT = 2 ** 53 - 1
_B64URL = re.compile(r'[A-Za-z0-9_-]+\Z')

Verifier = Callable[[bytes, bytes, bytes], bool]


class WireError(ValueError):
    """A closed, public error code (``[a-z][a-z0-9_]*``)."""

    def __init__(self, code: str, status: int = 400) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def unb64url(value: Any, *, length: int | None = None, code: str = 'encoding_invalid') -> bytes:
    if not isinstance(value, str) or not value or len(value) > 4096 or not _B64URL.fullmatch(value):
        raise WireError(code)
    try:
        raw = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
    except (ValueError, TypeError):
        raise WireError(code) from None
    if b64url(raw) != value or (length is not None and len(raw) != length):
        raise WireError(code)
    return raw


def _check_value(value: Any, depth: int = 0) -> None:
    if depth > 6:
        raise WireError('document_too_deep')
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if not -MAX_SAFE_INT <= value <= MAX_SAFE_INT:
            raise WireError('integer_out_of_range')
        return
    if isinstance(value, str):
        for ch in value:
            if unicodedata.category(ch) in ('Cc', 'Cs'):
                raise WireError('text_invalid')
        return
    if isinstance(value, list):
        for item in value:
            _check_value(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', key):
                raise WireError('key_invalid')
            _check_value(item, depth + 1)
        return
    raise WireError('value_type_invalid')  # floats included


def canonical(document: Any) -> bytes:
    _check_value(document)
    return json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def parse_json_object(raw: bytes, *, limit: int) -> dict[str, Any]:
    """Strict JSON object: size cap, no duplicate keys, no NaN/float."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > limit:
        raise WireError('body_too_large', 413)

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise WireError('json_duplicate_key')
            result[key] = item
        return result

    def no_float(_text):
        raise WireError('json_float_refused')

    try:
        value = json.loads(bytes(raw).decode('utf-8'), object_pairs_hook=pairs, parse_float=no_float,
                           parse_constant=no_float)
    except WireError:
        raise
    except (ValueError, UnicodeError):
        raise WireError('json_invalid') from None
    if not isinstance(value, dict):
        raise WireError('json_invalid')
    _check_value(value)
    return value


def key_bytes(key: Any) -> bytes:
    if not isinstance(key, str) or not key.startswith(KEY_PREFIX):
        raise WireError('key_invalid')
    return unb64url(key[len(KEY_PREFIX):], length=32, code='key_invalid')


def key_text(raw: bytes) -> str:
    if len(raw) != 32:
        raise WireError('key_invalid')
    return KEY_PREFIX + b64url(raw)


def fingerprint(key: str) -> str:
    """Short grouped fingerprint people compare by eye (16 hex, 4 groups)."""
    digest = hashlib.sha256(key_bytes(key)).hexdigest()[:16].upper()
    return '-'.join(digest[i:i + 4] for i in range(0, 16, 4))


def signing_bytes(document: Mapping[str, Any]) -> bytes:
    body = {k: v for k, v in document.items() if k != 'sig'}
    return SIG_PREFIX + canonical(body)


def verify_document(document: Mapping[str, Any], signer_key: str, verifier: Verifier) -> None:
    signature = unb64url(document.get('sig'), length=64, code='signature_invalid')
    public = key_bytes(signer_key)
    try:
        ok = verifier(public, signing_bytes(document), signature)
    except Exception:
        ok = False
    if ok is not True:
        raise WireError('signature_invalid', 403)


def exact_keys(document: Any, protocol: str, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(document, dict) or document.get('protocol') != protocol:
        raise WireError('protocol_invalid')
    if set(document) != keys:
        raise WireError('document_fields_invalid')
    return document


def text(value: Any, *, low: int, high: int, code: str) -> str:
    if not isinstance(value, str) or not low <= len(value) <= high or value != value.strip():
        raise WireError(code)
    if low and not value:
        raise WireError(code)
    _check_value(value)
    return value


def integer(value: Any, *, low: int, high: int, code: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise WireError(code)
    return value


def ident(value: Any, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise WireError(code)
    return value
