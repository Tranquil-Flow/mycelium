# SPDX-License-Identifier: AGPL-3.0-only
"""C2-UI: every pairing_* / join_* / relay_* code raised anywhere in the tree has plain-language copy in the page.

Static contract: the set of raised codes is read from the source (not hard-coded), so a new code without a message
fails here.  Product (non-test) source only.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "mycelium_a13_macos" / "web" / "app.js"
SKIP_DIRS = {"tests", ".git", "native", "evidence", "artifacts", "research", "drafts", "release", "node_modules",
             "__pycache__", "deploy", "docs", "contracts", "scenarios", "tolerances", "ui", ".venv"}
FAMILY = r"(?:pairing|join|relay)_[a-z0-9_]+"
# codes are raised directly, or travel in the error-carrying constructs of this tree
RAISED = re.compile(r"""raise\s+[\w.]+\(\s*[fF]?['"](%s)['"]""" % FAMILY)
CARRIED = re.compile(r"""(?:Error|_error|error_code|code|"error"|'error')\W{0,12}['"](%s)['"]""" % FAMILY)
ERROR_SUFFIX = re.compile(
    r"_(invalid|required|expired|revoked|closed|unknown|mismatch|failed|unavailable|exhausted|changed|stale|locked|"
    r"missing|reused|refused|unsupported|pending|claimed|timeout|nonpublic|startable|capacity|large|socket|"
    r"closed|stalled)$")
NOT_CODES = {"relay_acknowledged", "relay_wait"}  # request keys / status fields that merely look alike


def raised_codes() -> dict[str, str]:
    found: dict[str, str] = {}
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            path = Path(base, name)
            text = path.read_text(errors="ignore")
            for match in RAISED.finditer(text):
                found.setdefault(match.group(1), str(path.relative_to(ROOT)))
            for match in CARRIED.finditer(text):
                code = match.group(1)
                if ERROR_SUFFIX.search(code) and code not in NOT_CODES:
                    found.setdefault(code, str(path.relative_to(ROOT)))
    return found


def human_errors() -> dict[str, str]:
    script = APP_JS.read_text()
    start = script.index("const HUMAN_ERRORS={")
    end = script.index("\n};", start)
    body = script[start:end]
    return {m.group(1): m.group(2) for m in re.finditer(r"^\s{2}(%s):'((?:[^'\\]|\\.)*)'" % FAMILY, body, re.M)}


def test_the_contract_actually_finds_the_codes():
    codes = raised_codes()
    assert {"pairing_window_unknown", "join_code_invalid", "relay_start_failed", "relay_required"} <= set(codes)
    assert len(codes) >= 60


def test_every_raised_pairing_join_relay_code_has_a_plain_language_message():
    messages = human_errors()
    missing = sorted(code for code in raised_codes() if code not in messages)
    assert not missing, f"codes without a HUMAN_ERRORS message: {missing}"


def test_messages_are_plain_language_not_the_code_restated():
    for code, message in human_errors().items():
        words = code.replace("_", " ")
        assert len(message) >= 20 and message.strip().rstrip(".") != words, code
        assert code not in message and "_" not in message, code
        assert message.endswith((".", "?", "”")), code
