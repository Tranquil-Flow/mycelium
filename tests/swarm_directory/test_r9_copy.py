# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-directed text-node bytes, excluding the independent hue controls.

apex.html is pinned to the owner-approved positioning copy (9d9289b1: hero
'AI for everyone, controlled by no one.', vision eyebrow 'AI, grown together')
plus the header 'About' and closing 'About Mycelium →' links (renamed from
'Vision' / 'Read our vision →', owner 9 Nov); about.html (was vision.html) is
pinned to the owner-approved vision copy (Technical v2; Simple v3, 9 Nov, with
the detector figure '90–95%') with the title 'About · Mycelium' and the header
'About' link. The owner added the CCAI Discord invite (11 Nov) to the three
footers and to both 'Part of something bigger' / 'The wider stack (CCAI)'
sections, so those text nodes are part of the pin too.
"""
import hashlib
from html.parser import HTMLParser
from pathlib import Path

class Copy(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts=[]
        self.skip=0
    def handle_starttag(self, tag, attrs):
        if tag == 'button' and 'data-glow-choice' in dict(attrs): self.skip += 1
    def handle_endtag(self, tag):
        if tag == 'button' and self.skip: self.skip -= 1
    def handle_data(self, data):
        if not self.skip: self.parts.append(data)
    def handle_entityref(self, name):
        if not self.skip: self.parts.append('&'+name+';')
    def handle_charref(self, name):
        if not self.skip: self.parts.append('&#'+name+';')

def test_owner_directed_copy_is_pinned_except_hue_controls():
    web=Path(__file__).resolve().parents[2]/'mycelium_swarm_directory/web'
    expected={'apex.html': '93313f20d96b67516fd58d9eb5f6d8ec65e1c6e8a2dd96103c4870fdfeb284a6', 'about.html': '785be13b06709c70000492e98efa98195613088140c2eea4b2c609f12a4c47e9', 'index.html': '6f5876911673c9a72a62a1497c7613ebff67d687cdefdab251c1500781daac00'}
    for name, digest in expected.items():
        parser=Copy(); parser.feed((web/name).read_text())
        assert hashlib.sha256(''.join(parser.parts).encode()).hexdigest() == digest, name
