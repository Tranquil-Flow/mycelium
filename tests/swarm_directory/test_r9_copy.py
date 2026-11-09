# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-directed text-node bytes, excluding the independent hue controls.

apex.html is pinned to the owner-approved positioning copy (9d9289b1: hero
'AI for everyone, controlled by no one.', vision eyebrow 'AI, grown together')
plus the header 'About' and closing 'About Mycelium →' links (renamed from
'Vision' / 'Read our vision →', owner 9 Nov); about.html (was vision.html) is
pinned to the owner-approved vision copy (Technical v2; Simple v3, 9 Nov, with
the detector figure '90–95%') with the title 'About · Mycelium' and the header
'About' link.
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
    expected={'apex.html': 'f6691c436b61aa4e509fa9cf322fbfb6d7d45850746a7b435c32feeb4cca5fb7', 'about.html': '47c0b74dece076d53c27ae2ad47b58dca9e09e16b8997b158b7437af5edf1717', 'index.html': '40cd559de22190164797da49e45d2728a6c94aa8eaee4527720c33214120a9b3'}
    for name, digest in expected.items():
        parser=Copy(); parser.feed((web/name).read_text())
        assert hashlib.sha256(''.join(parser.parts).encode()).hexdigest() == digest, name
