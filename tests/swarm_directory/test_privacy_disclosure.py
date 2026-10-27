# SPDX-License-Identifier: AGPL-3.0-only
"""M3: the privacy text shown to visitors and owners is truthful about Cloudflare and the direct-to-entry flow.

Pure text pins (no browser): the page footer, the directory's privacy note and demo label, the relay status string
and the owner app's tunnel prompt.  Review: R3 F7 (the page said "no logs" and "straight to the entry Mac" and never
named Cloudflare, which terminates TLS for the apex and every ``*.trycloudflare.com`` entry).
"""
from pathlib import Path

from mycelium_a13_macos.public_relay import TemporaryHttpsRelay
from mycelium_swarm_directory import server
from mycelium_swarm_directory.directory import PRIVACY_NOTE

ROOT = Path(__file__).resolve().parents[2]
PAGE = (ROOT / 'mycelium_swarm_directory' / 'web' / 'index.html').read_text()
FOOTER = PAGE[PAGE.index('<footer>'):PAGE.index('</footer>')]
OWNER_JS = (ROOT / 'mycelium_a13_macos' / 'web' / 'app.js').read_text()


def test_the_page_footer_names_cloudflare_and_what_it_can_see():
    assert 'Cloudflare' in FOOTER
    for seen in ('IP address', 'timing', 'prompt'):
        assert seen in FOOTER, seen
    assert 'trycloudflare.com' in FOOTER


def test_the_page_footer_describes_the_direct_to_entry_flow_and_the_stranger_case():
    assert 'directly to the swarm' in FOOTER and 'never through this directory' in FOOTER
    assert 'whoever runs' in FOOTER  # a listed swarm is run by its owner, not by Mycelium


def test_the_page_footer_no_longer_claims_unqualified_no_logs():
    assert 'No cookies, no analytics, no logs.' not in FOOTER
    assert 'keeps no logs' in FOOTER  # the directory itself, said as such


def test_the_directory_privacy_note_and_demo_label_name_cloudflare():
    assert 'Cloudflare' in PRIVACY_NOTE and 'seen by this swarm' in PRIVACY_NOTE
    assert 'Cloudflare' in server.DEMO_LABEL
    assert 'directory never sees prompts' in PRIVACY_NOTE


def test_the_relay_status_says_web_chat_prompts_cross_the_relay(tmp_path):
    # The real constructor (no side effects) so status() sees every attribute a relay has,
    # including the renewal fields quick tunnels now report; then the closed, never-started state.
    relay = TemporaryHttpsRelay(tmp_path, 'cloudflare')
    relay._closed = True
    text = relay.status()['privacy']
    assert 'inference uses Iroh' not in text
    assert 'web chat prompts' in text and 'IP address' in text and 'Iroh' in text


def test_the_owner_tunnel_prompt_does_not_say_inference_stays_on_iroh_alone():
    assert 'inference stays on Iroh.' not in OWNER_JS
    assert 'web chat prompts' in OWNER_JS
