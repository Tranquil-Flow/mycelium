# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural invariant pins for the ssh_tailscale_independence decision and
frozen plane separation (spec §2, §10.1).

These are absence-tests: the A8 internet-native package must not import,
invoke, or depend on SSH or Tailscale, must carry no tailnet addresses, and
the public route allowlist must never expose operator administration.
"""

from __future__ import annotations

from pathlib import Path
import re

from mycelium_internet.bootstrap import PUBLIC_ROUTE_ALLOWLIST

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "mycelium_internet"

_FORBIDDEN_IMPORTS = re.compile(
    r"^\s*(?:import|from)\s+"
    r"(?:tailscale|paramiko|fabric|sshtunnel|asyncssh|pexpect|scp)"
    r"(?:\s|\.|$)",
    re.MULTILINE,
)
_FORBIDDEN_SUBPROCESS = re.compile(
    r"subprocess\..*(?:ssh|scp|sftp|tailscale)",
    re.MULTILINE,
)
_CGNAT = re.compile(r"100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.")
_MAGIC_DNS = re.compile(r"\.ts\.net\b")
_OPERATOR_ROUTES = {
    "/seed/invite",
    "/seed/admin",
    "/seed/members",
    "/seed/revoke",
    "/seed/backup",
    "/seed/rotation/begin",
    "/seed/rotation/complete",
}


def _sources() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def test_a8_package_imports_no_ssh_or_tailscale_modules() -> None:
    for path in _sources():
        source = path.read_text("utf-8")
        assert _FORBIDDEN_IMPORTS.search(source) is None, path
        assert _FORBIDDEN_SUBPROCESS.search(source) is None, path


def test_a8_package_contains_no_tailnet_identity_literals() -> None:
    for path in _sources():
        source = path.read_text("utf-8")
        assert _CGNAT.search(source) is None, path
        assert _MAGIC_DNS.search(source) is None, path


def test_a8_package_never_binds_operator_or_fleet_ports() -> None:
    for path in _sources():
        source = path.read_text("utf-8")
        assert "8791" not in source, path
        assert "8876" not in source, path


def test_public_route_allowlist_never_exposes_operator_administration() -> None:
    for routes in PUBLIC_ROUTE_ALLOWLIST.values():
        assert _OPERATOR_ROUTES.isdisjoint(routes)
    flattened = {
        path
        for routes in PUBLIC_ROUTE_ALLOWLIST.values()
        for path in routes
    }
    assert flattened == {
        "/seed/identity",
        "/seed/rotation",
        "/seed/join",
        "/seed/resume",
        "/seed/message",
    }


def test_a8_package_has_no_plaintext_http_origin_literal() -> None:
    # The boundary may reference the http:// scheme only in rejection
    # docstrings/patterns, never as an accepted origin constant.
    for path in _sources():
        source = path.read_text("utf-8")
        for match in re.finditer(r"https?://[A-Za-z0-9.\[\]-]+(?::\d+)?", source):
            literal = match.group(0)
            assert not literal.startswith("http://"), (path, literal)
