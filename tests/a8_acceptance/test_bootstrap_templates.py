# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drift gates for the source-controlled A8 public bootstrap templates
(spec §3, §10). The templates are deliverables: they must forward exactly
the five closed routes to the loopback seed, reject cleartext without a
redirect, bound frames and rates, force no-store, and contain no secrets."""

from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = ROOT / "release" / "a8-tls-bootstrap"
NGINX_TEMPLATE = TEMPLATE_DIR / "nginx-a8-bootstrap.conf.template"
CLOUDFLARED_TEMPLATE = TEMPLATE_DIR / "cloudflared-config.yml.template"
PROVISION_SCRIPT = ROOT / "scripts" / "a8_provision_public_origin.sh"
README = TEMPLATE_DIR / "README.md"

ALLOWLIST_ROUTES = {
    "/seed/identity",
    "/seed/rotation",
    "/seed/join",
    "/seed/resume",
    "/seed/message",
}


def test_template_directory_is_a_real_deliverable() -> None:
    assert TEMPLATE_DIR.is_dir()
    assert NGINX_TEMPLATE.is_file()
    assert README.is_file()


def test_nginx_template_forwards_exactly_the_five_closed_routes() -> None:
    source = NGINX_TEMPLATE.read_text("utf-8")
    locations = set(re.findall(r"location\s*=?\s*([^ {]+)", source))
    assert {path for path in locations if path.startswith("/")} == (
        ALLOWLIST_ROUTES | {"/"}
    )
    for route in ALLOWLIST_ROUTES:
        assert f"location = {route}" in source


def test_nginx_template_rejects_cleartext_without_redirect() -> None:
    source = NGINX_TEMPLATE.read_text("utf-8")
    assert "listen 80" in source
    assert "return 444" in source
    assert "301" not in source
    assert "302" not in source
    assert "307" not in source


def test_nginx_template_serves_tls_only_and_never_plaintext() -> None:
    source = NGINX_TEMPLATE.read_text("utf-8")
    assert "listen 443 ssl" in source
    assert "ssl_certificate" in source


def test_nginx_template_bounds_frames_rates_and_caches_never() -> None:
    source = NGINX_TEMPLATE.read_text("utf-8")
    assert "client_max_body_size 1m" in source
    assert "limit_req" in source
    assert 'Cache-Control "no-store"' in source


def test_nginx_template_proxies_only_the_loopback_seed() -> None:
    source = NGINX_TEMPLATE.read_text("utf-8")
    for upstream in re.findall(r"proxy_pass\s+([^;]+);", source):
        assert "127.0.0.1" in upstream, upstream
    assert "proxy_set_header Upgrade" not in source


def test_templates_carry_no_secrets_or_provider_accounts() -> None:
    for path in (NGINX_TEMPLATE, README):
        source = path.read_text("utf-8")
        assert "PRIVATE KEY" not in source
        assert "password" not in source.lower()
        assert "api_key" not in source.lower()
        assert "account@example" not in source
        for placeholder in ("__A8_PUBLIC_HOSTNAME__", "__A8_SEED_LOOPBACK_PORT__"):
            assert placeholder in NGINX_TEMPLATE.read_text("utf-8")
        # Only the documented __...__ placeholders are allowed to be
        # substituted; no shell/env-style secrets.
        for match in re.findall(r"\$\{([^}]+)\}", source):
            assert match.startswith("A8_") is False or match in {
                "A8_PUBLIC_HOSTNAME",
                "A8_SEED_LOOPBACK_PORT",
            }


def test_readme_documents_certificate_rotation_without_seed_authority() -> None:
    source = README.read_text("utf-8")
    assert "renewal" in source.lower()
    assert "no seed authority" in source.lower()
    assert "design_only" in source


def test_cloudflared_template_forwards_the_origin_to_loopback_only() -> None:
    source = CLOUDFLARED_TEMPLATE.read_text("utf-8")
    assert "__A8_PUBLIC_HOSTNAME__" in source
    assert "__A8_TUNNEL_ID__" in source
    assert "__A8_SEED_LOOPBACK_PORT__" in source
    for service in re.findall(r"service:\s*(\S+)", source):
        assert service.startswith("http://127.0.0.1:") or service == "http_status:404", service
    assert "http_status:404" in source
    assert "https://" not in source.split("service:")[0]


def test_cloudflared_template_carries_no_credentials() -> None:
    source = CLOUDFLARED_TEMPLATE.read_text("utf-8")
    assert "eyJ" not in source  # no base64 json credential blobs
    assert "password" not in source.lower()
    assert "__A8_CREDENTIALS_FILE__" in source
    assert "cred.json" not in source


def test_provisioning_script_is_owner_private_and_login_gated() -> None:
    source = PROVISION_SCRIPT.read_text("utf-8")
    assert source.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in source
    assert "cloudflared tunnel route dns" in source
    assert "cloudflared tunnel login" in source
    assert ".mycelium/a8-tls" in source
    assert "--dry-run" in source
    assert "chmod 700" in source
    assert "eyJ" not in source
    assert "password" not in source.lower()
