#!/usr/bin/env python3
"""Click through the prototype's main flows and assert the behaviours the design doc promises (see SITE-REDESIGN-V2.md).
Needs the same local server as capture.py.  Exit 1 on any FAIL."""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8765/"
CLS = "window.__cls=[];new PerformanceObserver(l=>{for(const e of l.getEntries()){if(!e.hadRecentInput)window.__cls.push(e.value)}}).observe({type:'layout-shift',buffered:true});"
fails = []
def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond: fails.append(msg)

with sync_playwright() as p:
    b = p.chromium.launch(); c = b.new_context(viewport={"width": 1440, "height": 900}); pg = c.new_page(); pg.add_init_script(CLS)
    errors = []; pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(BASE + "chat.html?id=garage&state=needs-request"); pg.wait_for_timeout(400)
    ok(pg.is_disabled("#prompt"), "composer is disabled until access exists")
    pg.fill("#note", "Hi it is Sam"); ok(pg.inner_text("#cnt") == "12 / 140", "note counter")
    pg.click("#ask"); pg.wait_for_timeout(200)
    ok("Request sent" in pg.inner_text(".gate h3"), "request sent -> pending panel"); ok(pg.is_disabled("#prompt"), "still disabled while pending")
    pg.click("#demo-approve"); pg.wait_for_timeout(200)
    ok(not pg.is_disabled("#prompt"), "approval enables the composer in place")
    pg.fill("#prompt", "Hello"); pg.click("#send"); pg.wait_for_timeout(700)
    ok(pg.inner_text("#stop") == "Stop", "Stop is shown while generating"); pg.wait_for_timeout(3500)
    ok("tok/s" in pg.inner_text("#log"), "finished answer carries a speed line")
    ok(sum(pg.evaluate("window.__cls")) < 0.01, "request -> approve -> chat caused no layout shift")
    pg.goto(BASE + "swarms.html"); pg.wait_for_timeout(300); pg.focus("#tab-browse"); pg.keyboard.press("ArrowRight"); pg.wait_for_timeout(100)
    ok(pg.get_attribute("#tab-chat", "aria-selected") == "true" and not pg.is_visible("#panel-browse"), "ArrowRight moves the tab and its panel")
    pg.goto(BASE + "swarms.html#browse"); pg.click("[data-filter=open]"); ok(pg.inner_text("#browse-count").startswith("2"), "Open filter shows 2 swarms")
    pg.fill("#search", "zzz"); ok(pg.is_visible("#browse-empty"), "empty state when nothing matches")
    pg.goto(BASE + "owner.html"); pg.wait_for_timeout(300)
    pg.click(".req[data-i='0'] [data-act=approve]"); pg.wait_for_timeout(200); ok(pg.inner_text("#o-count") == "1", "badge counts down after approve")
    pg.click("#o-reqs .req[data-i='1'] [data-act=decline]"); ok("Nobody is waiting" in pg.inner_text("#o-lede"), "inbox empty message")
    pg.goto(BASE + "create.html"); pg.wait_for_timeout(200)
    ok(pg.eval_on_selector("#c-model", "e=>e.tagName") == "SELECT" and pg.eval_on_selector_all("#c-model option", "o=>o.length") == 6, "model is a dropdown: 5 registered models + placeholder")
    ok(pg.is_disabled("#c-go"), "Create disabled until name and model are set")
    pg.fill("#c-name", "Test"); pg.select_option("#c-model", "3"); ok(not pg.is_disabled("#c-go"), "Create enabled")
    ok(not pg.is_visible("#c-quota"), "quota fields hidden unless Open"); pg.check("input[value=open]"); ok(pg.is_visible("#c-quota"), "quota fields shown for Open")
    pg.click("#c-go"); ok(pg.is_visible("#c-step2"), "created -> next steps shown")
    ok(not errors, "no page errors: %s" % errors)
    b.close()
sys.exit(1 if fails else 0)
