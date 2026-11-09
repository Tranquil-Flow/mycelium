# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-directed text-node bytes, excluding the independent hue controls.

apex.html is pinned to the owner-approved positioning copy (9d9289b1: hero
'AI for everyone, controlled by no one.', vision eyebrow 'AI, grown together')
plus the header 'Vision' and closing 'Read our vision →' links; vision.html is
pinned to the owner-approved vision copy v2 (9 Nov).
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
    expected={'apex.html': '737fa9edf53301744f29ade18acc5b98afbe0f509f740bf6e816ec4088526174', 'vision.html': '913bf7e6ff98129cc3963af63edc03b907e914635e6bd662bb6de6e30b0d06ce', 'index.html': '40cd559de22190164797da49e45d2728a6c94aa8eaee4527720c33214120a9b3'}
    for name, digest in expected.items():
        parser=Copy(); parser.feed((web/name).read_text())
        assert hashlib.sha256(''.join(parser.parts).encode()).hexdigest() == digest, name
