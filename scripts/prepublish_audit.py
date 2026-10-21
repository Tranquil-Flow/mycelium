#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Pre-publish audit: refuse to publish a tree or history that leaks anything.

Scans (1) every tracked file as committed in the index and (2) every commit in a
revision range (default ``origin/main..HEAD``: the history that a push would
publish), including deleted files, commit messages and author/committer
identities. Blobs are de-duplicated, so a blob that appears in many commits is
scanned once.

Categories: private_key, api_token, generic_secret, signing_grant,
cloudflared_credential, tailscale_ip, private_hostname, home_path, deny_string,
email, spdx_or_later, spdx_missing, spdx_inconsistent, license_missing,
commit_trailer.

Commit trailers: a commit message in the scanned range must carry no trailer
(``Co-authored-by:``, ``Signed-off-by:``, ``Reviewed-by:``, a trailing block of
``Key: value`` lines, ...). The one exception is a trailer that is passed, verbatim,
with ``--allowed-trailer`` (repeatable; exact full-line match, case and spacing
included): it is accepted at most once per commit. Anything else, a near-miss of an
allowed line included, is a ``commit_trailer`` error.

The report never contains a matched value: only file, commit, line, category, a
short sha256 fingerprint of the match and its length. Forbidden strings (for
example a legal name) are read from an untracked ``--deny-file`` so the strings
themselves are never committed.

Exit codes: 0 no error findings, 1 error findings, 2 usage/config error.
``spdx_or_later`` seen only in old history is a ``note`` (history cannot be
re-licensed without a rewrite) and does not fail the audit.
"""

from __future__ import annotations

import argparse
import bisect
import fnmatch
import hashlib
import json
import math
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

OWNER_EMAIL = "tranquil_flow@protonmail.com"
MAX_SCAN_BYTES = 8 * 1024 * 1024
SPDX_ONLY = "AGPL-3.0-only"
# Names are assembled so this file does not contain what it searches for.
BUILTIN_DENY = ("mac-" + "node", "mycelium-" + "laptop", "evino" + "va")
LICENSE_NAMES = ("LICENSE", "LICENSE.txt", "LICENSE.md", "COPYING")
SPDX_EXTENSIONS = {
    ".py", ".rs", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".sh", ".ps1",
    ".c", ".h", ".m", ".swift", ".kt", ".java",
}
SPDX_EXCLUDED_DIRS = {"node_modules", "vendor", "third_party"}
ALLOWED_EMAIL_DOMAINS = (
    "example.com", "example.org", "example.net", "example", "localhost", "invalid", "test",
    "mycelium.now",
)
# "name@2x.png" style asset names and the like are not addresses.
NON_EMAIL_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "icns", "avif"}
HOME_PLACEHOLDERS = {
    "shared", "guest", "you", "user", "username", "name", "me", "test", "example", "runner",
    "yourname", "your-name", "someone", "alice", "bob", "admin", "foo", "ci", "build",
    "public", "default", "administrator", "deploy", "owner", "x", "xxx", "home",
    # fictional account names used in fixtures and docs
    "member", "operator", "operator_a", "operator_b", "controller", "canary", "remote",
    "different_user", "pilot", "p", "my", "...",
}
NAME_EXCLUDE = re.compile(
    r"(?i)hash|sha\d*|digest|fingerprint|checksum|path|file|dir\b|url|uri|count|len|ttl|"
    r"version|kind|state|status|_id\b|public|pub\b|label|name|type|schema|ref\b|redacted|header|tokens\b|max_|min_"
)
PLACEHOLDER_VALUE = re.compile(
    r"(?i)example|xxxx|your[_-]|placeholder|changeme|dummy|fake|sample|redacted|not[_-]?a[_-]?secret|"
    r"0000000|1234567|abcdefgh|<|\$\{|\{\{"
)

SEVERITY_ERROR = "error"
SEVERITY_NOTE = "note"


class AuditConfigError(Exception):
    """Bad usage or configuration (exit code 2)."""


@dataclass(frozen=True)
class Hit:
    category: str
    line: int
    fingerprint: str
    length: int


def _fp(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    n = len(value)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


# --- patterns ---------------------------------------------------------------------

_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b",
        r"\bgithub_pat_[A-Za-z0-9_]{22,255}",
        r"\bhf_[A-Za-z0-9]{30,}\b",
        r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b",
        r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
        r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}",
        r"\bcfut_[A-Za-z0-9]{30,}",
        r"\bglpat-[A-Za-z0-9_-]{20,}",
        r"\bnpm_[A-Za-z0-9]{36}\b",
        r"\bAIza[0-9A-Za-z_-]{35}\b",
        r"\bpypi-[A-Za-z0-9_-]{50,}",
        r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    )
)
_AWS_SECRET = re.compile(r"(?i)aws_?secret_?access_?key[\"']?\s{0,3}[:=]\s{0,3}[\"']?([A-Za-z0-9/+=]{40})\b")
_CLOUDFLARE_TOKEN = re.compile(
    r"(?i)\b(?:cloudflare|cf)[_-]?(?:api[_-]?)?(?:token|key)[A-Za-z0-9_.-]{0,20}[\"']?\s{0,3}[:=]\s{0,3}[\"']?([A-Za-z0-9_-]{37,60})\b"
)
_BEARER = re.compile(r"(?i)authorization[\"']?\s{0,3}[:=]\s{0,3}[\"']?bearer\s+([A-Za-z0-9_\-.=]{30,})")
_PEM = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
_AGE = re.compile(r"\bAGE-SECRET-KEY-1[QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L]{58}")
_ARGO = re.compile("-----BEGIN ARGO TUNNEL " + "TOKEN-----")
_SEED = re.compile(
    r"(?i)(?:seed|private[_-]?key|secret[_-]?key|signing[_-]?key|release[_-]?key)([A-Za-z0-9_.-]{0,20})[\"']?\s{0,3}[:=]\s{0,3}(?:b?[\"'])?([0-9a-f]{64})\b"
)
_SIGNING_GRANT = re.compile(r"(?i)signing[_-]?grant[A-Za-z0-9_.-]{0,20}[\"']?\s{0,3}[:=]\s{0,3}[\"']?([A-Za-z0-9+/_=.-]{40,})")
_TUNNEL_SECRET = re.compile(r"\"TunnelSecret\"\s*:\s*\"([^\"]{8,})\"")
_TUNNEL_TOKEN = re.compile(r"\beyJhIjoi[A-Za-z0-9+/=]{50,}")
_GENERIC = re.compile(
    r"(?i)(?:api[_-]?key|secret|token|passw(?:or)?d|pwd|credential)([A-Za-z0-9_.-]{0,30})[\"']?\s{0,3}(?::=|[:=])\s{0,3}[\"']?([A-Za-z0-9+/_=.~-]{20,})"
)
_TAILSCALE = re.compile(r"(?<![\d.])100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}(?![\d]|/)")
_HOME_UNIX = re.compile(r"(?<![A-Za-z0-9_])/Users/([A-Za-z0-9._-]+)")
_HOME_WIN = re.compile(r"(?i)\b[A-Z]:\\+Users\\+([A-Za-z0-9._-]+)")
_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+)@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![A-Za-z0-9-])")
_OR_LATER = re.compile("AGPL-3\\.0(?:-" + "or-later|\\+)")
_SPDX = re.compile(r"SPDX-License-Identifier:\s*([A-Za-z0-9.+-]+)")

_PATH_CF = re.compile(r"(?i)(^|/)(cert\.pem|\.cloudflared/.*|cloudflared[^/]*\.json)$")
_PATH_KEY = re.compile(
    r"(?i)(^|/)(id_(?:rsa|dsa|ecdsa|ed25519)|[^/]*\.(?:pem|key|p12|pfx|p8|jks|keystore|age|mobileprovision))$"
)
_PATH_GRANT = re.compile(r"(?i)(^|/)[^/]*signing[-_]?grant[^/]*\.(?:json|txt|grant|b64|sig|bin)$")
_PATH_ENV = re.compile(r"(^|/)\.env(\.[^/]+)?$")


def _line_index(text: str) -> list[int]:
    return [m.start() for m in re.finditer("\n", text)]


def _line_of(newlines: list[int], pos: int) -> int:
    return bisect.bisect_left(newlines, pos) + 1


def _name_context(text: str, start: int, suffix: str) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    prefix = text[max(line_start, start - 40) : start]
    tail = re.search(r"[A-Za-z0-9_.-]*$", prefix)
    return (tail.group(0) if tail else "") + suffix


def email_allowed(email_local: str, domain: str) -> bool:
    address = f"{email_local}@{domain}".lower()
    if address == OWNER_EMAIL:
        return True
    if email_local.lower() in {"noreply", "no-reply", "git"} or domain.lower() == "users.noreply.github.com":
        return True
    if domain.rsplit(".", 1)[-1].lower() in NON_EMAIL_TLDS:
        return True
    d = domain.lower()
    return any(d == a or d.endswith("." + a) for a in ALLOWED_EMAIL_DOMAINS)


def scan_text(text: str, deny: tuple[str, ...]) -> list[Hit]:
    """Scan decoded text; returns hits without values."""
    hits: list[Hit] = []
    newlines = _line_index(text)
    covered: list[tuple[int, int]] = []

    def add(category: str, pos: int, value: str, span: tuple[int, int] | None = None, fp: str | None = None) -> None:
        hits.append(Hit(category, _line_of(newlines, pos), fp or _fp(value), len(value)))
        if span:
            covered.append(span)

    for m in _PEM.finditer(text):
        add("private_key", m.start(), m.group(0))
    for m in _AGE.finditer(text):
        add("private_key", m.start(), m.group(0), m.span())
    for m in _ARGO.finditer(text):
        add("cloudflared_credential", m.start(), m.group(0))
    for m in _SEED.finditer(text):
        ctx = _name_context(text, m.start(), m.group(1))
        value = m.group(2)
        if NAME_EXCLUDE.search(ctx) or _entropy(value) < 3.0:
            continue
        add("private_key", m.start(2), value, m.span(2))
    for pat in _TOKEN_PATTERNS:
        for m in pat.finditer(text):
            add("api_token", m.start(), m.group(0), m.span())
    for pat in (_AWS_SECRET, _CLOUDFLARE_TOKEN, _BEARER):
        for m in pat.finditer(text):
            value = m.group(1)
            if PLACEHOLDER_VALUE.search(value) or _entropy(value) < 3.2:
                continue
            add("api_token", m.start(1), value, m.span(1))
    for m in _SIGNING_GRANT.finditer(text):
        value = m.group(1)
        if _entropy(value) >= 3.0:
            add("signing_grant", m.start(1), value, m.span(1))
    for m in _TUNNEL_SECRET.finditer(text):
        add("cloudflared_credential", m.start(1), m.group(1), m.span(1))
    for m in _TUNNEL_TOKEN.finditer(text):
        add("cloudflared_credential", m.start(), m.group(0), m.span())
    for m in _GENERIC.finditer(text):
        value = m.group(2)
        ctx = _name_context(text, m.start(), m.group(1))
        if any(a < m.end(2) and m.start(2) < b for a, b in covered):
            continue
        if len(value) < 20 or NAME_EXCLUDE.search(ctx) or PLACEHOLDER_VALUE.search(value):
            continue
        if not (re.search(r"\d", value) and re.search(r"[A-Za-z]", value)):
            continue
        is_hex = re.fullmatch(r"[0-9a-fA-F]+", value) is not None
        if _entropy(value) < (3.3 if is_hex else 3.7):
            continue
        if re.fullmatch(r"[A-Za-z_]+(?:\.[A-Za-z_]+)+", value):
            continue  # attribute access
        add("generic_secret", m.start(2), value, m.span(2))
    for m in _TAILSCALE.finditer(text):
        add("tailscale_ip", m.start(), m.group(0))
    for rx in (_HOME_UNIX, _HOME_WIN):
        for m in rx.finditer(text):
            if m.group(1).lower() in HOME_PLACEHOLDERS:
                continue
            add("home_path", m.start(), m.group(0))
    for m in _EMAIL.finditer(text):
        if not email_allowed(m.group(1), m.group(2)):
            add("email", m.start(), m.group(0))
    for m in _OR_LATER.finditer(text):
        add("spdx_or_later", m.start(), m.group(0))
    lowered = text.lower()
    for name in BUILTIN_DENY:
        start = 0
        while (pos := lowered.find(name, start)) != -1:
            add("private_hostname", pos, name)
            start = pos + len(name)
    for index, entry in enumerate(deny):
        start = 0
        while (pos := lowered.find(entry, start)) != -1:
            add("deny_string", pos, entry, fp=f"deny-{index}")
            start = pos + len(entry)
    return hits


_KNOWN_TRAILER = re.compile(
    r"(?i)^(co-authored-by|signed-off-by|reviewed-by|acked-by|tested-by|reported-by|suggested-by|"
    r"helped-by|generated-by|assisted-by|change-id|cc|fixes|closes|resolves)\s*:"
)
_TRAILER_LINE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*\s*:\s*\S")


def trailer_lines(message: str) -> list[str]:
    """Trailer-looking lines of a commit message (never the subject line)."""
    text = message.replace("\r\n", "\n").strip("\n")
    lines = text.split("\n")
    found = [ln for ln in lines[1:] if _KNOWN_TRAILER.match(ln)]
    paragraphs = re.split(r"\n[ \t]*\n", text)
    if len(paragraphs) > 1:
        tail = paragraphs[-1].split("\n")
        if all(_TRAILER_LINE.match(ln) or ln[:1] in (" ", "\t") for ln in tail):
            found += [ln for ln in tail if _TRAILER_LINE.match(ln) and ln not in found]
    return found


def trailer_hits(message: str, allowed: tuple[str, ...]) -> list[Hit]:
    hits: list[Hit] = []
    seen_allowed: set[str] = set()
    for ln in trailer_lines(message):
        if ln in allowed and ln not in seen_allowed:
            seen_allowed.add(ln)
            continue
        hits.append(Hit("commit_trailer", 0, _fp(ln), len(ln)))
    return hits


def scan_path(path: str, deny: tuple[str, ...]) -> list[Hit]:
    hits: list[Hit] = []
    base = path.rsplit("/", 1)[-1]
    if _PATH_CF.search(path):
        hits.append(Hit("cloudflared_credential", 0, _fp(path), len(path)))
    elif _PATH_KEY.search(path) and not base.endswith(".pub"):
        hits.append(Hit("private_key", 0, _fp(path), len(path)))
    if _PATH_GRANT.search(path):
        hits.append(Hit("signing_grant", 0, _fp(path), len(path)))
    if _PATH_ENV.search(path) and not base.endswith((".example", ".sample", ".template")):
        hits.append(Hit("generic_secret", 0, _fp(path), len(path)))
    hits.extend(h for h in scan_text(path, deny) if h.category in {"deny_string", "private_hostname", "home_path", "email"})
    return [Hit(h.category, 0, h.fingerprint, h.length) for h in hits]


def spdx_hits(path: str, text: str) -> list[Hit]:
    """Header presence/consistency for a source file (tree only)."""
    ext = "." + path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
    if ext not in SPDX_EXTENSIONS or SPDX_EXCLUDED_DIRS & set(path.split("/")):
        return []
    if not text.strip():
        return []
    head = "\n".join(text.splitlines()[:12])
    found = _SPDX.search(head)
    if not found:
        return [Hit("spdx_missing", 1, _fp(path), 0)]
    ident = found.group(1)
    if ident == SPDX_ONLY or _OR_LATER.search(ident):
        return []
    return [Hit("spdx_inconsistent", 1, _fp(ident), len(ident))]


# --- git access -------------------------------------------------------------------


def _git(root: Path, *args: str, check: bool = True) -> bytes:
    done = subprocess.run(["git", "-C", str(root), "-c", "core.quotepath=false", *args], capture_output=True)
    if check and done.returncode != 0:
        raise AuditConfigError(f"git {' '.join(args[:2])} failed: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def _tree_entries(root: Path) -> list[tuple[str, str]]:
    raw = _git(root, "ls-files", "-s", "-z")
    entries = []
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        meta, _, path = rec.partition(b"\t")
        mode, sha, _stage = meta.decode().split()
        if mode == "160000":
            continue
        entries.append((sha, path.decode("utf-8", "replace")))
    return entries


def _history(root: Path, rev_range: str) -> tuple[list[dict[str, str]], dict[tuple[str, str], str]]:
    try:
        _git(root, "rev-parse", "--verify", "--quiet", "HEAD")
        revs = _git(root, "rev-list", rev_range).decode().split()
    except AuditConfigError as exc:
        raise AuditConfigError(f"cannot resolve revision range {rev_range!r} (is origin/main fetched?): {exc}") from None
    meta: list[dict[str, str]] = []
    raw = _git(root, "log", "-z", "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B", rev_range)
    for rec in raw.split(b"\0"):
        if not rec.strip():
            continue
        parts = rec.decode("utf-8", "replace").lstrip("\n").split("\x1f", 5)
        if len(parts) == 6:
            meta.append(dict(zip(("sha", "an", "ae", "cn", "ce", "msg"), parts)))
    introduced: dict[tuple[str, str], str] = {}
    raw = _git(root, "log", "--raw", "-r", "--no-abbrev", "--no-renames", "--root", "--format=%x01%H", rev_range)
    commit = ""
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("\x01"):
            commit = line[1:]
        elif line.startswith(":"):
            head, _, path = line.partition("\t")
            fields = head.split()
            new_sha, status = fields[-2], fields[-1]
            if status.startswith("D") or set(new_sha) == {"0"} or fields[1] == "160000":
                continue
            introduced[(new_sha, path)] = commit  # git log is newest first: last write = oldest
    if len(meta) != len(revs):
        meta = [m for m in meta if m["sha"] in set(revs)]
    return meta, introduced


def _read_blobs(root: Path, shas: Iterable[str]) -> Iterable[tuple[str, bytes | None, int]]:
    """Yield (sha, data|None, size). data None = too large or missing."""
    shas = list(shas)
    if not shas:
        return
    check = subprocess.run(
        ["git", "-C", str(root), "cat-file", "--batch-check"],
        input=("\n".join(shas) + "\n").encode(),
        capture_output=True,
    )
    sizes: dict[str, int] = {}
    for line in check.stdout.decode().splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == "blob":
            sizes[parts[0]] = int(parts[2])
    wanted = [s for s in shas if s in sizes and sizes[s] <= MAX_SCAN_BYTES]
    for s in shas:
        if s not in sizes or sizes[s] > MAX_SCAN_BYTES:
            yield s, None, sizes.get(s, 0)
    proc = subprocess.Popen(
        ["git", "-C", str(root), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout
    step = 400
    for i in range(0, len(wanted), step):
        chunk = wanted[i : i + step]
        proc.stdin.write(("\n".join(chunk) + "\n").encode())
        proc.stdin.flush()
        for sha in chunk:
            header = proc.stdout.readline().split()
            size = int(header[2])
            data = proc.stdout.read(size)
            proc.stdout.read(1)
            yield sha, data, size
    proc.stdin.close()
    proc.wait()


# --- allowlist / deny -------------------------------------------------------------


def load_deny(path: Path | None) -> tuple[str, ...]:
    if path is None:
        return ()
    if not path.is_file():
        raise AuditConfigError(f"deny file not found: {path}")
    entries = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip().lower()
        if s and not s.startswith("#"):
            entries.append(s)
    return tuple(entries)


def load_allowlist(path: Path | None) -> list[dict[str, str]]:
    if path is None:
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuditConfigError(f"allowlist unreadable: {exc}") from None
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise AuditConfigError("allowlist must be an object with an 'entries' list")
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or not str(entry.get("reason", "")).strip():
            raise AuditConfigError(f"allowlist entry {i} needs a non-empty reason")
        if not entry.get("category") or not entry.get("file"):
            raise AuditConfigError(f"allowlist entry {i} needs category and file")
    return entries


def _allow_reason(finding: dict, entries: list[dict[str, str]]) -> str | None:
    for e in entries:
        if e["category"] != finding["category"] or not fnmatch.fnmatchcase(finding["file"], e["file"]):
            continue
        if e.get("fingerprint") and e["fingerprint"] != finding["fingerprint"]:
            continue
        if e.get("line") and int(e["line"]) != finding["line"]:
            continue
        return e["reason"]
    return None


# --- main audit -------------------------------------------------------------------


def run_audit(
    root: Path,
    *,
    rev_range: str | None,
    deny: tuple[str, ...],
    allowlist: list[dict[str, str]],
    allowed_trailers: tuple[str, ...] = (),
) -> dict:
    tree = _tree_entries(root)
    tree_pairs = set(tree)
    meta: list[dict[str, str]] = []
    introduced: dict[tuple[str, str], str] = {}
    if rev_range:
        meta, introduced = _history(root, rev_range)
    history_pairs = {pair: c for pair, c in introduced.items() if pair not in tree_pairs}

    unique = sorted({s for s, _ in tree_pairs} | {s for s, _ in history_pairs})
    content_hits: dict[str, list[Hit]] = {}
    texts_for_spdx: dict[str, str] = {}
    binary = large = 0
    tree_shas = {s for s, _ in tree_pairs}
    for sha, data, size in _read_blobs(root, unique):
        if data is None:
            large += 1
            content_hits[sha] = []
            continue
        if b"\0" in data[:8192]:
            binary += 1
            content_hits[sha] = []
            continue
        text = data.decode("utf-8", "replace")
        content_hits[sha] = scan_text(text, deny)
        if sha in tree_shas:
            texts_for_spdx[sha] = text

    raw: list[dict] = []

    def emit(hit: Hit, *, file: str, commit: str | None, scope: str, blob: str | None) -> None:
        severity = SEVERITY_NOTE if (scope == "history" and hit.category == "spdx_or_later") else SEVERITY_ERROR
        raw.append(
            {
                "category": hit.category,
                "severity": severity,
                "scope": scope,
                "file": file,
                "line": hit.line,
                "commit": commit,
                "blob": blob,
                "fingerprint": hit.fingerprint,
                "length": hit.length,
            }
        )

    for sha, path in sorted(tree_pairs, key=lambda p: p[1]):
        for h in content_hits.get(sha, []):
            emit(h, file=path, commit=None, scope="tree", blob=sha)
        for h in scan_path(path, deny):
            emit(h, file=path, commit=None, scope="tree", blob=sha)
        if sha in texts_for_spdx:
            for h in spdx_hits(path, texts_for_spdx[sha]):
                emit(h, file=path, commit=None, scope="tree", blob=sha)
    for (sha, path), commit in sorted(history_pairs.items(), key=lambda kv: (kv[1], kv[0][1])):
        for h in content_hits.get(sha, []):
            emit(h, file=path, commit=commit, scope="history", blob=sha)
        for h in scan_path(path, deny):
            emit(h, file=path, commit=commit, scope="history", blob=sha)
    for m in meta:
        for label, name, mail in (("<commit-author>", m["an"], m["ae"]), ("<commit-committer>", m["cn"], m["ce"])):
            local, _, domain = mail.partition("@")
            if mail and not email_allowed(local, domain):
                emit(Hit("email", 0, _fp(mail), len(mail)), file=label, commit=m["sha"], scope="history", blob=None)
            for h in scan_text(name, deny):
                emit(Hit(h.category, 0, h.fingerprint, h.length), file=label, commit=m["sha"], scope="history", blob=None)
        for h in scan_text(m["msg"], deny):
            emit(h, file="<commit-message>", commit=m["sha"], scope="history", blob=None)
        for h in trailer_hits(m["msg"], allowed_trailers):
            emit(h, file="<commit-message>", commit=m["sha"], scope="history", blob=None)

    if not any(p.rsplit("/", 1)[-1] in LICENSE_NAMES and "/" not in p for _, p in tree):
        emit(Hit("license_missing", 0, _fp("LICENSE"), 0), file="LICENSE", commit=None, scope="tree", blob=None)
    else:
        lic_sha = next(s for s, p in tree if "/" not in p and p in LICENSE_NAMES)
        lic = next(iter(_read_blobs(root, [lic_sha])), (None, None, 0))[1] or b""
        text = lic.decode("utf-8", "replace")
        if "GNU AFFERO GENERAL PUBLIC LICENSE" not in text or "Version 3, 19 November 2007" not in text:
            emit(Hit("license_missing", 0, _fp("LICENSE-text"), len(text)), file="LICENSE", commit=None, scope="tree", blob=lic_sha)

    findings: list[dict] = []
    allowlisted: list[dict] = []
    seen: set[tuple] = set()
    for f in raw:
        key = (f["category"], f["file"], f["line"], f["fingerprint"], f["commit"], f["scope"])
        if key in seen:
            continue
        seen.add(key)
        reason = _allow_reason(f, allowlist)
        if reason is not None:
            allowlisted.append({**f, "reason": reason})
        else:
            findings.append(f)

    errors = [f for f in findings if f["severity"] == SEVERITY_ERROR]
    notes = [f for f in findings if f["severity"] == SEVERITY_NOTE]
    head = _git(root, "rev-parse", "HEAD", check=False).decode().strip()
    return {
        "tool": "scripts/prepublish_audit.py",
        "head": head,
        "rev_range": rev_range,
        "summary": {
            "commits_scanned": len(meta),
            "tree_files": len(tree),
            "unique_blobs_scanned": len(unique),
            "binary_blobs_skipped": binary,
            "large_blobs_skipped": large,
            "errors": len(errors),
            "notes": len(notes),
            "allowlisted": len(allowlisted),
            "by_category": dict(sorted(Counter(f["category"] for f in findings).items())),
        },
        "findings": findings,
        "allowlisted": allowlisted,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--rev-range", default="origin/main..HEAD", help="history to scan (default: origin/main..HEAD)")
    ap.add_argument("--no-history", action="store_true", help="scan tracked files only")
    ap.add_argument("--deny-file", type=Path, help="untracked file of forbidden strings, one per line")
    ap.add_argument("--allowlist", type=Path, help="JSON {'entries': [{category,file,fingerprint?,line?,reason}]}")
    ap.add_argument("--output", type=Path, help="write the JSON report here (default: stdout)")
    ap.add_argument(
        "--allowed-trailer",
        action="append",
        default=[],
        metavar="LINE",
        help="one exact commit-message trailer line that is accepted (once per commit); repeatable. "
        "Every other trailer is a commit_trailer error.",
    )
    args = ap.parse_args(argv)
    try:
        deny = load_deny(args.deny_file)
        allow = load_allowlist(args.allowlist)
        report = run_audit(args.root, rev_range=None if args.no_history else args.rev_range, deny=deny, allowlist=allow,
                           allowed_trailers=tuple(args.allowed_trailer))
    except AuditConfigError as exc:
        print(f"prepublish_audit: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    s = report["summary"]
    line = f"prepublish_audit: errors={s['errors']} notes={s['notes']} allowlisted={s['allowlisted']} categories={s['by_category']}"
    if args.output:
        args.output.write_text(text)
        print(line)
    else:
        sys.stdout.write(text)
        print(line, file=sys.stderr)
    return 1 if s["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
