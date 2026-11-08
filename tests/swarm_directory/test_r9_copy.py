# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-directed R9 text-node bytes, excluding the independent hue controls."""
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
    expected={'apex.html': 'e9841530bbf782cd8d88a7109f1cb89ae5c0f3aad5337a0cdd455025d572a4c0', 'index.html': '40cd559de22190164797da49e45d2728a6c94aa8eaee4527720c33214120a9b3'}
    for name, digest in expected.items():
        parser=Copy(); parser.feed((web/name).read_text())
        assert hashlib.sha256(''.join(parser.parts).encode()).hexdigest() == digest, name
