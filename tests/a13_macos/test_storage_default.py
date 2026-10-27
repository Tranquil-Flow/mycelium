# SPDX-License-Identifier: AGPL-3.0-only
"""The fresh-install HTML default covers the starter's whole-snapshot share."""
from html.parser import HTMLParser
from pathlib import Path

from mycelium_a13_macos import model_resources as R
from mycelium_a13_macos.serving import A9_ROUTE_MODEL, slice_fetch_permitted


class StorageSelect(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.options = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'select':
            self.inside = attrs.get('id') == 'storage'
        if self.inside and tag == 'option':
            self.options.append((int(attrs['value']), 'selected' in attrs))
    def handle_endtag(self, tag):
        if tag == 'select':
            self.inside = False


def test_default_storage_covers_starter_even_with_the_whole_model_share():
    parser = StorageSelect()
    parser.feed((Path(__file__).resolve().parents[2] / 'mycelium_a13_macos/web/index.html').read_text())
    assert {v for v, _ in parser.options} == {1_000_000_000, 2_000_000_000, 4_000_000_000, 23_052_975_062}
    selected = [v for v, selected in parser.options if selected]
    assert len(selected) <= 1
    default = selected[0] if selected else parser.options[0][0]
    shape = R.require_shape(A9_ROUTE_MODEL)
    assert not slice_fetch_permitted(shape.weight_source_bytes, architecture=shape.model_type, model=A9_ROUTE_MODEL)
    profile = shape.layer_profile()
    # A conservative one-member share, including the tied head retained by
    # the current resource derivation; this does not add a one-host route.
    split = {'members': [], 'stages': [{'node_id': 'one-mac', 'start': 0, 'end': profile.num_layers,
        'stage_bytes': sum(profile.layer_bytes) + profile.embedding_bytes + profile.output_bytes
            + (profile.embedding_bytes if profile.tie_word_embeddings else 0)}]}
    need = R.member_resources(A9_ROUTE_MODEL, split, slice_mode=False)[0]['max_disk_bytes']
    assert need == 2_559_752_622 and need > 2_000_000_000
    assert default >= need, f'Default {default} cannot hold starter share requiring {need}'
    assert default == 4_000_000_000
