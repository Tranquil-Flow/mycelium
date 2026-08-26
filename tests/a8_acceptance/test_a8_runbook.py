# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drift gate for the A8 physical runbook: every physical case has an
operator-executable section with capture and seal steps, and the runbook
carries no execution claim (the lane cannot execute without the unrelated
network + public origin)."""

from __future__ import annotations

import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs" / "handover" / "A8_PHYSICAL_RUNBOOK.md"
INVENTORY = ROOT / "tests" / "a8_acceptance" / "inventory.v1.json"
BROWSER_GATE = ROOT / "ui" / "web" / "scripts" / "a8-product-browser-gate.mjs"


def _physical_case_ids() -> set[str]:
    inventory = json.loads(INVENTORY.read_text("utf-8"))
    return {
        case["case_id"]
        for section in ("physical_positive_cases", "physical_negative_cases")
        for case in inventory[section]
    }


def test_runbook_exists_and_names_qualification_protocol() -> None:
    source = RUNBOOK.read_text("utf-8")
    assert "mycelium.internet_native_qualification.v1" in source
    assert "--spec-digest <SPEC_DIGEST>" in source
    assert "--source-digest <SOURCE_DIGEST>" in source


def test_runbook_covers_every_physical_case() -> None:
    source = RUNBOOK.read_text("utf-8")
    covered = set(re.findall(r"^## ([a-z][a-z0-9_]+)", source, re.MULTILINE))
    assert _physical_case_ids() <= covered


def test_every_case_section_has_capture_and_seal_steps() -> None:
    source = RUNBOOK.read_text("utf-8")
    sections = re.split(r"^## ", source, flags=re.MULTILINE)[1:]
    by_id = {
        section.splitlines()[0].strip(): section
        for section in sections
        if section.strip()
    }
    for case_id in _physical_case_ids():
        section = by_id[case_id]
        assert "Capture:" in section, case_id
        assert f"a8_run_physical_gate.py run {case_id}" in section, case_id
        assert "--spec-digest <SPEC_DIGEST>" in section, case_id
        assert "--source-digest <SOURCE_DIGEST>" in section, case_id
        assert "--seal" in section, case_id


def test_runbook_carries_no_execution_claims() -> None:
    source = RUNBOOK.read_text("utf-8")
    for claim in (
        '"result": "passed"',
        '"executed": true',
        "executed: true",
        "gate passed",
        "A8 complete",
    ):
        assert claim not in source, claim
    # "not executed" / "not_executed" statements are the honest form.
    lowered = source.lower()
    assert "not executed" in lowered or "not_executed" in lowered


def test_product_proxy_overwrites_client_identity_after_edge_authentication() -> None:
    source = RUNBOOK.read_text("utf-8")
    assert "allow 127.0.0.1;" in source
    assert "deny all;" in source
    assert 'auth_basic "Mycelium A8 qualification";' in source
    assert "auth_basic_user_file <OWNER_PRIVATE_HTPASSWD_FILE>;" in source
    assert "proxy_set_header X-Mycelium-Authenticated-User $remote_user;" in source
    assert 'secrets.token_hex(32)' in source
    assert 'proxy_set_header Authorization "";' in source
    assert 'proxy_set_header Proxy-Authorization "";' in source
    assert "Never use `proxy_add_header`" in source
    browser_gate = BROWSER_GATE.read_text("utf-8")
    assert "A8_BROWSER_HTTP_USERNAME" in browser_gate
    assert "A8_BROWSER_HTTP_PASSWORD" in browser_gate


def test_runbook_pins_transport_browser_authorities_and_source_bindings() -> None:
    source = RUNBOOK.read_text("utf-8")
    assert "a8_source_manifest.py --check" in source
    assert "build-transport-authority" in source
    assert "--endpoint-secret-file <NODE_0_ENDPOINT_KEY>" in source
    assert "--endpoint-secret-file <NODE_2_ENDPOINT_KEY>" in source
    assert "Reuse these exact endpoint key files" in source
    assert "build-browser-authority" in source
    assert "--signing-key-file <BROWSER_SIGNING_KEY>" in source
    assert "--case-id <BROWSER_CASE_ID>" in source
    assert "--deployment-id <DEPLOYMENT_ID>" in source
    assert "--request-count <1_OR_2>" in source
    assert "--evidence-signing-key <BROWSER_SIGNING_KEY>" in source
    assert "--browser-authority <BROWSER_AUTHORITY_FILE>" in source
    assert "--browser-authority-file <BROWSER_AUTHORITY_FILE>" in source
    assert "--spec-digest <SPEC_DIGEST>" in source
    assert "--source-digest <SOURCE_DIGEST>" in source
    for case_id in (
        "direct_path_qualified_browser_inference",
        "forced_relay_privacy_reduced_browser_inference",
        "observed_path_transition_and_reconnect",
    ):
        section = source.split(f"## {case_id}\n", 1)[1].split("\n## ", 1)[0]
        assert "--transport-authority-file <TRANSPORT_AUTHORITY_FILE>" in section
        if case_id == "observed_path_transition_and_reconnect":
            assert "--transport-report-file <SIGNED_TRANSPORT_REPORT_BEFORE>" in section
            assert "--transport-report-file <SIGNED_TRANSPORT_REPORT_AFTER>" in section
        else:
            assert "--transport-report-file <SIGNED_TRANSPORT_REPORT>" in section
        assert "--browser-authority-file <BROWSER_AUTHORITY_FILE>" in section
    transition = source.split(
        "## observed_path_transition_and_reconnect\n", 1
    )[1].split("\n## ", 1)[0]
    assert transition.count("--transport-report-file") == 2
    assert "--request-count 2" in transition


def test_claim_cases_require_retained_live_case_probe_reports() -> None:
    source = RUNBOOK.read_text("utf-8")
    for case_id in (
        "unrelated_https_invite_without_tailscale",
        "invalid_or_replayed_invitation",
        "revoked_active_member",
        "tailscale_unavailable",
        "ssh_unavailable",
    ):
        section = source.split(f"## {case_id}\n", 1)[1].split("\n## ", 1)[0]
        assert "--case-probe-program <LIVE_CASE_PROBE_PROGRAM>" in section, case_id
        assert "--case-probe-output-file <OWNER_PRIVATE_PROBE_REPORT>" in section, case_id
    endpoint_section = source.split(
        "## endpoint_identity_mismatch\n", 1
    )[1].split("\n## ", 1)[0]
    assert "scripts/a8_endpoint_mismatch_probe.py" in endpoint_section
    assert "Do not supply an arbitrary probe program" in " ".join(
        endpoint_section.split()
    )
    assert "--sidecar-binary <EXACT_SIDECAR_BINARY>" in endpoint_section
    assert "--receiver-endpoint-secret-file <ENDPOINT_MISMATCH_RECEIVER_KEY>" in endpoint_section
    assert "--transport-authority-file <TRANSPORT_AUTHORITY_FILE>" in endpoint_section
    assert "--case-probe-output-file <OWNER_PRIVATE_PROBE_REPORT>" in endpoint_section
    assert "receives the case id and enrolled member id" in source
    assert "retains the exact canonical probe report at mode `0600`" in source
