# SPDX-License-Identifier: AGPL-3.0-only
"""Ed25519 signing/verification for the Python side (``cryptography``).

The pure core never imports this module directly; it receives ``verify`` as a
``wire.Verifier``.  The browser uses WebCrypto (non-extractable keys).
"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from .wire import key_text


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True


class Ed25519Signer:
    def __init__(self, private: Ed25519PrivateKey) -> None:
        self._private = private
        raw = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.public_key = key_text(raw)

    @classmethod
    def generate(cls) -> 'Ed25519Signer':
        return cls(Ed25519PrivateKey.generate())

    def sign(self, message: bytes) -> bytes:
        return self._private.sign(message)

    @classmethod
    def load_or_create(cls, path: Path | str) -> 'Ed25519Signer':
        """A 0600 raw 32-byte seed file; created atomically, never overwritten."""
        path = Path(path)
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            private = Ed25519PrivateKey.generate()
            raw = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
            return cls(private)
        with os.fdopen(fd, 'rb') as stream:
            if os.fstat(stream.fileno()).st_mode & 0o077:
                raise ValueError('swarm_key_permissions_invalid')
            raw = stream.read(33)
        if len(raw) != 32:
            raise ValueError('swarm_key_invalid')
        return cls(Ed25519PrivateKey.from_private_bytes(raw))
