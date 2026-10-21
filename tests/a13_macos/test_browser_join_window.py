# SPDX-License-Identifier: AGPL-3.0-only
"""Real Chromium on both apps: owner invites, target joins with the link, one click.

Separate browser contexts (separate cookie jars) for owner and target.  The
public tunnel is a fixture; the relay proxy hop and both apps are real.
NOT physical two-Mac or public-network evidence.
"""
import re

from playwright.sync_api import expect, sync_playwright

from test_join_window_flow import _owner, _target


def _open(browser, app):
    context = browser.new_context(viewport={"width": 1280, "height": 1000}, reduced_motion="reduce")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(type(e).__name__))
    page.goto(app.launch_file.as_uri())
    page.get_by_role("button", name="Open private demo", exact=True).click()
    expect(page.locator("#private")).to_be_visible()
    return page, errors


def test_browser_invite_join_and_one_click_approval(tmp_path, monkeypatch):
    owner = _owner(tmp_path, monkeypatch, "https://relay-fixture.invalid")
    target = None
    try:
        target = _target(tmp_path, owner)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            owner_page, owner_errors = _open(browser, owner)
            owner_page.get_by_role("button", name="Invite a Mac", exact=True).click()
            expect(owner_page.locator("#windows")).to_contain_text("mycelium-join:", timeout=10000)
            link = re.search(r"mycelium-join:\S+", owner_page.locator("#windows").inner_text()).group(0)

            target_page, target_errors = _open(browser, target)
            expect(target_page.locator("#standing-text")).to_contain_text("withdraw this consent at any time")
            assert target_page.locator("#join-with-code").is_disabled()
            target_page.locator("#visibility").check()
            target_page.locator("#join-code").fill(link)
            target_page.locator("#standing-confirm").check()
            target_page.get_by_role("button", name="Join with code", exact=True).click()
            expect(target_page.locator("#join-fingerprint")).to_contain_text("fingerprint", timeout=20000)
            fingerprint = target_page.locator("#join-fingerprint").inner_text().rsplit(" ", 1)[1]

            owner_page.get_by_role("button", name="Refresh", exact=True).click()
            expect(owner_page.locator("#windows")).to_contain_text(fingerprint, timeout=10000)
            expect(owner_page.locator("#windows")).to_contain_text("ms RTT")
            owner_page.get_by_role("button", name="Approve this Mac", exact=True).click()
            expect(owner_page.locator("#notice")).to_contain_text("Approved", timeout=10000)

            # The target collects the handoff by itself (no paste, no click).
            expect(target_page.locator("#ladder")).to_contain_text("current", timeout=30000)
            expect(target_page.locator("#standing-state")).to_contain_text("granted", timeout=10000)
            assert target.status()["authority"]["membership_state"] == "current"
            target_page.once("dialog", lambda dialog: dialog.accept())
            target_page.get_by_role("button", name="Withdraw standing consent", exact=True).click()
            expect(target_page.locator("#standing-state")).to_contain_text("withdrawn", timeout=10000)
            assert owner_errors == [] and target_errors == []
            browser.close()
    finally:
        if target is not None:
            target.close()
        owner.close()
