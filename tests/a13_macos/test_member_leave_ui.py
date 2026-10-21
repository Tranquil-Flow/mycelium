# SPDX-License-Identifier: AGPL-3.0-only
"""The member 'Leave swarm' control: two choices with a one-line explainer each (static contract)."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "mycelium_a13_macos" / "web"
HTML = (WEB / "index.html").read_text()
JS = (WEB / "app.js").read_text()


def _section() -> str:
    match = re.search(r'<section id="leave-swarm".*?</section>', HTML, re.S)
    assert match, "leave-swarm card missing"
    return match.group(0)


def test_leave_card_offers_both_choices_with_explainers_and_a_confirmation() -> None:
    card = _section()
    assert "hidden" in card.split(">", 1)[0]                       # shown only for a joined member
    for button in ("leave-smooth", "leave-instant", "leave-confirm"):
        assert f'id="{button}"' in card
    text = re.sub(r"<[^>]+>", " ", card)
    assert "Leave smoothly" in text and "Instant leave" in text
    assert "finish" in text.lower() and "30 seconds" in text       # smooth: lets work finish, 30 s bound
    assert "now" in text.lower()                                   # instant: leaves now
    assert 'id="leave-state"' in card


def test_leave_script_is_isolated_and_posts_the_two_modes() -> None:
    assert "function renderLeaveSwarm(" in JS and "function leaveSwarm(" in JS
    assert re.search(r"'/api/leave'\s*,\s*\{\s*mode\s*,\s*confirm:\s*true", JS)
    assert "leaveSwarm('smooth')" in JS and "leaveSwarm('instant')" in JS
    for code in ("leave_confirmation_required", "leave_mode_invalid", "target_role_required"):
        assert code in JS                                          # readable errors


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_app_js_parses() -> None:
    subprocess.run(["node", "--check", str(WEB / "app.js")], check=True)
