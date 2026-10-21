# SPDX-License-Identifier: AGPL-3.0-only
"""The join poll shows the advisory clock_skew warning and the member leave hint points at Leave swarm."""
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[2] / "mycelium_a13_macos" / "web" / "app.js"


def test_join_poll_surfaces_clock_skew_warning():
    text = APP_JS.read_text(encoding="utf-8")
    assert "w.code==='clock_skew'" in text
    assert "Set time and date automatically" in text


def test_member_leave_hint_points_at_leave_swarm():
    text = APP_JS.read_text(encoding="utf-8")
    assert "use “Leave swarm” below" in text
    assert "press “Withdraw standing consent” (Invite or join with a code) so no more work is accepted" not in text
