# SPDX-License-Identifier: AGPL-3.0-only
"""Redaction for the support bundle.

Two layers, because the bundle holds two kinds of data:

* the diagnostics document is structured (counts, codes, timings): every value under a key that names a
  secret or free text is replaced wholesale, and every remaining string is scrubbed;
* the app log is free text: it is scrubbed by pattern (keys, grants, tokens, join links and codes, prompt/reply
  JSON fields, URLs and hosts, IP addresses, home paths, this Mac's host and user names) and by exact match
  for secrets the app itself knows (its session, native credential and launch code, recent chat text).

Over-redaction is the safe direction; loopback addresses and plain error codes are left readable.
"""
from __future__ import annotations

import getpass
import os
import platform
import re
import socket
from typing import Any, Iterable

REDACTED = "[redacted]"
_SENSITIVE_WORDS = frozenset({
    "key", "keys", "secret", "secrets", "token", "tokens", "grant", "grants", "password", "passphrase",
    "credential", "credentials", "signature", "cookie", "authorization", "prompt", "prompts", "text", "output",
    "content", "message", "messages", "invite", "join", "link", "bearer", "nonce", "reply", "completion",
    "session", "cipher", "ciphertext", "handoff",
})
_WORD = re.compile(r"[A-Za-z0-9]+")


def _sensitive_key(key: Any) -> bool:
    words = {w.lower() for w in _WORD.findall(str(key))}
    return bool(words & _SENSITIVE_WORDS)


_PEM = re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.S)
_APP_LINK = re.compile(r"\bmycelium-([a-z]+):[^\s\"'<>]+")
_AUTH_HEADER = re.compile(r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\b(\s*[:=]\s*)[^\r\n]+")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_TEXT_FIELD = re.compile(
    r"""(?i)(["'])(prompt|text|content|output|message|reply|completion|answer)\1(\s*:\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')""")
_PREFIXED = re.compile(r"\b(?:claim|session|launch|native|sk|pk|ghp|gho|xox[a-z])[-_][A-Za-z0-9_-]{16,}")
_LABELLED_BLOB = re.compile(r"\b[A-Za-z][A-Za-z0-9_-]{2,30}:[A-Za-z0-9+/=_-]{24,}")
_SECRET_KV = re.compile(
    r"(?i)\b([a-z0-9_]*(?:key|secret|token|password|credential|grant)[a-z0-9_]*)(\s*[=:]\s*)(?!<)[^\s,;\"'}\]]+")
_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_JWT = re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_BLOB = re.compile(r"(?=[A-Za-z0-9_\-+]*\d)(?=[A-Za-z0-9_\-+]*[A-Za-z])[A-Za-z0-9_\-+]{40,}={0,2}")
_JOIN_CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}\b")
_JOIN_WORDS = re.compile(r"\b[a-z]{3,10}(?:-[a-z]{3,10}){3}\b")
_URL = re.compile(r"(?i)\b(https?|wss?)://([^\s/:?#\"'<>]+)(:\d+)?([^\s\"'<>]*)")
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "[::1]"})
_HOME_UNIX = re.compile(r"/(?:Users|home)/[^/\s\"'<>:]+")
_HOME_WIN = re.compile(r"(?i)[A-Z]:\\+Users\\+[^\\\s\"'<>:]+")
_LOCAL_HOST = re.compile(r"(?i)\b[\w-]+(?:\.[\w-]+)*\.(?:local|lan|home|internal|localdomain)\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"(?i)\b(?:[0-9a-f]{1,4}:){4,7}[0-9a-f]{1,4}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")


def _url(match: "re.Match[str]") -> str:
    scheme, host, port, rest = match.group(1), match.group(2), match.group(3) or "", match.group(4)
    if "?" in rest:
        rest = rest.split("?", 1)[0] + "?<QUERY>"
    if host.lower() in _LOOPBACK:
        return f"{scheme}://{host}{port}{rest}"
    return f"{scheme}://<HOST>{port}{rest}"


def default_hostnames() -> list[str]:
    names: set[str] = set()
    for source in (socket.gethostname, platform.node):
        try:
            value = source()
        except Exception:
            continue
        if value:
            names.add(value)
            names.add(value.split(".")[0])
    return sorted(n for n in names if len(n) >= 4)


class Redactor:
    def __init__(self, *, known_secrets: Iterable[str] = (), hostnames: Iterable[str] | None = None,
                 home: str | None = None, users: Iterable[str] | None = None) -> None:
        self._known = sorted({s for s in known_secrets if isinstance(s, str) and len(s) >= 6}, key=len, reverse=True)
        self._hosts = sorted({h for h in (default_hostnames() if hostnames is None else hostnames)
                              if isinstance(h, str) and len(h) >= 4}, key=len, reverse=True)
        self._home = home if home is not None else os.path.expanduser("~")
        if users is None:
            users = {os.path.basename(self._home.rstrip("/\\"))}
            try:
                users.add(getpass.getuser())
            except Exception:
                pass
        self._users = sorted({u for u in users if isinstance(u, str) and len(u) >= 4}, key=len, reverse=True)

    def text(self, value: str) -> str:
        out = value
        for secret in self._known:
            out = out.replace(secret, "<REDACTED>")
        if self._home and self._home not in ("/", "~") and len(self._home) > 3:
            out = out.replace(self._home, "<HOME>")
        out = _PEM.sub("<REDACTED-KEY>", out)
        out = _APP_LINK.sub(lambda m: "<JOIN-LINK>" if m.group(1) in {"join", "invite", "swarm", "pair"} else "<TOKEN>", out)
        out = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}<TOKEN>", out)
        out = _BEARER.sub(lambda m: f"{m.group(1)} <TOKEN>", out)
        out = _TEXT_FIELD.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}\"<REDACTED-TEXT>\"", out)
        out = _PREFIXED.sub("<TOKEN>", out)
        out = _LABELLED_BLOB.sub("<TOKEN>", out)
        out = _SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}<TOKEN>", out)
        out = _HEX.sub("<HEX>", out)
        out = _JWT.sub("<TOKEN>", out)
        out = _BLOB.sub("<TOKEN>", out)
        out = _URL.sub(_url, out)
        out = _JOIN_CODE.sub("<JOIN-CODE>", out)
        out = _JOIN_WORDS.sub("<JOIN-WORDS>", out)
        out = _HOME_UNIX.sub("<HOME>", out)
        out = _HOME_WIN.sub("<HOME>", out)
        for host in self._hosts:
            out = re.sub(re.escape(host), "<HOST>", out, flags=re.I)
        out = _LOCAL_HOST.sub("<HOST>", out)
        for user in self._users:
            out = re.sub(r"(?<![A-Za-z0-9])" + re.escape(user) + r"(?![A-Za-z0-9])", "<USER>", out)
        out = _IPV4.sub(lambda m: m.group(0) if m.group(0) in {"127.0.0.1", "0.0.0.0"} else "<IP>", out)
        out = _IPV6.sub("<IP>", out)
        out = _EMAIL.sub("<EMAIL>", out)
        return out

    def json(self, value: Any) -> Any:
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if _sensitive_key(key) and not isinstance(item, (bool, int, float, type(None))):
                    out[key] = REDACTED
                else:
                    out[key] = self.json(item)
            return out
        if isinstance(value, (list, tuple)):
            return [self.json(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value
