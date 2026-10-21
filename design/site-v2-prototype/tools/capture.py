#!/usr/bin/env python3
"""Screenshot every prototype page at desktop (1440x900) and mobile (390x844) widths and report layout shift (CLS).

    python3 -m http.server 8765 --bind 127.0.0.1   # from design/site-v2-prototype/, in another terminal
    python3 tools/capture.py                        # needs: pip install playwright && playwright install chromium

Writes screenshots/<name>-<desktop|mobile>.png (and -light variants).  Exit 1 when any page shifts more than 0.02.
"""
import sys, time, pathlib
from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent.parent
OUT = HERE / "screenshots"; OUT.mkdir(exist_ok=True)
BASE = "http://127.0.0.1:8765/"
PAGES = [  # name, path, full page, scheme
    ("index", "index.html", True, "dark"), ("swarms-browse", "swarms.html", True, "dark"),
    ("swarms-chat-tab", "swarms.html#chat", False, "dark"), ("swarms-contributing", "swarms.html#contributing", True, "dark"),
    ("swarms-owned", "swarms.html#owned", True, "dark"), ("swarm-detail", "swarm.html?id=reading", True, "dark"),
    ("swarm-detail-warming", "swarm.html?id=weekend", True, "dark"), ("swarm-detail-offline", "swarm.html?id=studio", True, "dark"),
    ("owner", "owner.html", True, "dark"), ("create", "create.html", True, "dark"),
    ("chat-first", "chat.html?id=demo", False, "dark"), ("chat-ready", "chat.html?id=reading", False, "dark"),
    ("chat-needs-request", "chat.html?id=garage&state=needs-request", False, "dark"),
    ("chat-pending", "chat.html?id=garage&state=pending", False, "dark"), ("chat-approved", "chat.html?id=garage&state=approved", False, "dark"),
    ("chat-declined", "chat.html?id=garage&state=declined", False, "dark"), ("chat-busy", "chat.html?id=reading&state=busy", False, "dark"),
    ("chat-offline", "chat.html?id=studio&state=offline", False, "dark"), ("chat-invite", "chat.html?id=lab&state=invite", False, "dark"),
    ("index", "index.html", True, "light"), ("swarms-browse", "swarms.html", True, "light"), ("chat-ready", "chat.html?id=reading", False, "light"),
]
CLS = "window.__cls=[];new PerformanceObserver(l=>{for(const e of l.getEntries()){if(!e.hadRecentInput)window.__cls.push(e.value)}}).observe({type:'layout-shift',buffered:true});"
worst = 0.0
with sync_playwright() as p:
    b = p.chromium.launch()
    for name, path, full, scheme in PAGES:
        for label, vp in (("desktop", {"width": 1440, "height": 900}), ("mobile", {"width": 390, "height": 844})):
            c = b.new_context(viewport=vp, is_mobile=label == "mobile", has_touch=label == "mobile", color_scheme=scheme)
            pg = c.new_page(); pg.add_init_script(CLS)
            pg.goto(BASE + path, wait_until="load"); time.sleep(1.6)
            cls = sum(pg.evaluate("window.__cls")); worst = max(worst, cls)  # read before the screenshot helper touches the page
            if full and label == "mobile": pg.add_style_tag(content=".tabbar{position:static!important}")  # so a full-page shot is not covered by the fixed bar
            suffix = "" if scheme == "dark" else "-light"
            pg.screenshot(path=str(OUT / f"{name}{suffix}-{label}.png"), full_page=full)
            print(f"{name}{suffix:6s} {label:8s} CLS {cls:.4f}")
            c.close()
    b.close()
print("worst CLS", round(worst, 4))
sys.exit(1 if worst > 0.02 else 0)
