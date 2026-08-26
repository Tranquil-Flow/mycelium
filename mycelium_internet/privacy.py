# SPDX-License-Identifier: AGPL-3.0-or-later
"""Projection privacy scans (spec §11).

Two layers of enforcement over every projection:

- forbidden KEYS (prompt, output, token, tensor, activation, KV content,
  credentials, secrets, raw identities) are rejected by name;
- forbidden VALUES (URLs, IPs, tailnet references, bearer tokens, private
  paths) are detected by bounded patterns;

plus per-scan exact needles (invite tokens, EndpointIDs, hostnames) supplied
by the caller. Violation reports carry only field paths and pattern classes -
never the offending material itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
from typing import Any

from .contracts import _FORBIDDEN_KEY_RE  # noqa: PLC2701 - same-package shared constant

# Pattern classes only; the matched text is never included in reports.
_FORBIDDEN_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("url", re.compile(r"(?:https?://|iroh://|ftp://|wss?://)")),
    ("ip_address", re.compile(r"(?:\d{1,3}\.){3}\d{1,3}")),
    (
        "tailscale_cgnat",
        re.compile(r"100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\."),
    ),
    ("tailnet_magicdns", re.compile(r"\.ts\.net\b")),
    ("bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9]{8,}\b")),
    ("query_token", re.compile(r"token=", re.IGNORECASE)),
    (
        "private_path",
        re.compile(
            r"(?:^|[\\/])(?:Users|home|private|var|etc)(?:[\\/]|$)"
            r"|(?:^|[\s~\\/])\.(?:ssh|gnupg|aws|config|mycelium)(?:[\\/]|$)"
        ),
    ),
)
_MAX_VIOLATIONS = 64


class PrivacyViolation(ValueError):
    """A bounded projection privacy failure."""

    def __init__(self, code: str, violations: list[str]) -> None:
        if not isinstance(code, str) or not re.fullmatch(
            r"[a-z][a-z0-9_]{0,63}", code
        ):
            raise ValueError("privacy violation code is invalid")
        self.code = code
        self.violations = list(violations)
        super().__init__(code)


def _forbidden_needles(
    forbidden_needles: Iterable[str] | None,
) -> tuple[str, ...]:
    if forbidden_needles is None:
        return ()
    needles = []
    for needle in forbidden_needles:
        if not isinstance(needle, str) or not needle:
            raise ValueError("forbidden needle is invalid")
        needles.append(needle)
    return tuple(needles)


def scan_projection(
    document: Any,
    *,
    forbidden_needles: Iterable[str] | None = None,
) -> list[str]:
    """Return bounded violation paths for one projection document.

    Violations name the field path and the pattern class only. The offending
    values are never embedded in the report.
    """

    needles = _forbidden_needles(forbidden_needles)
    violations: list[str] = []

    def walk(node: Any, path: str) -> None:
        if len(violations) >= _MAX_VIOLATIONS:
            return
        if isinstance(node, Mapping):
            for key, value in node.items():
                if not isinstance(key, str):
                    violations.append(f"{path}.<non-string-key>")
                    continue
                if _FORBIDDEN_KEY_RE.fullmatch(key) is not None:
                    violations.append(f"{path}.{key} (forbidden key)")
                walk(value, f"{path}.{key}")
            return
        if isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")
            return
        if isinstance(node, str):
            for label, pattern in _FORBIDDEN_VALUE_PATTERNS:
                if pattern.search(node) is not None:
                    violations.append(f"{path} (forbidden {label})")
                    break
            for needle in needles:
                if needle in node:
                    violations.append(f"{path} (forbidden needle)")
                    break

    walk(document, "$")
    return violations


def ensure_privacy_clean(
    document: Any,
    *,
    code: str = "projection_privacy_violation",
    forbidden_needles: Iterable[str] | None = None,
) -> None:
    violations = scan_projection(
        document,
        forbidden_needles=forbidden_needles,
    )
    if violations:
        raise PrivacyViolation(code, violations)


__all__ = [
    "PrivacyViolation",
    "ensure_privacy_clean",
    "scan_projection",
]
