# SPDX-License-Identifier: AGPL-3.0-only
"""Text-node bytes of the deployed 86758539 HTML, excluding new hue controls."""
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

def test_deployed_copy_is_byte_identical_except_new_hue_controls():
    web=Path(__file__).resolve().parents[2]/'mycelium_swarm_directory/web'
    expected={'apex.html': 'a8a4d0a31850c5d57fd0473cc703fb2b80a5df84905301db28c30fdd132300b5', 'index.html': '3445154e71fa74d9ad7390dd4c858d7ea7e8bd49d94b7e766ddbd4d7ffe062c7'}
    for name, digest in expected.items():
        parser=Copy(); parser.feed((web/name).read_text())
        assert hashlib.sha256(''.join(parser.parts).encode()).hexdigest() == digest, name
