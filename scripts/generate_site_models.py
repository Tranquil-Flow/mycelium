# SPDX-License-Identifier: AGPL-3.0-only
"""Generate the directory's static picker from the app registry (no HTTP/protocol addition).

Run from the repository root: python -m scripts.generate_site_models [--check]
"""
from __future__ import annotations

import argparse
from html import escape
from pathlib import Path

from mycelium_a13_macos.model_resources import model_shape
from mycelium_a13_macos.run_model import CHOICES

START = '<!-- generated-models:start -->'
END = '<!-- generated-models:end -->'


def options() -> str:
    rows = []
    for choice in CHOICES:
        shape = model_shape(choice.model)
        if shape is None:
            raise ValueError('registered model has no resource shape')
        label = f'{shape.label} · {choice.snapshot_bytes / 1e9:.1f} GB download · {shape.num_layers} layers'
        rows.append(f'<option value="{escape(choice.model.model_id, quote=True)}">{escape(label)}</option>')
    return '\n'.join(rows)


def rendered(source: str) -> str:
    before, rest = source.split(START, 1)
    _, after = rest.split(END, 1)
    return before + START + '\n' + options() + '\n' + END + after


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    target = Path(__file__).resolve().parents[1] / 'mycelium_swarm_directory/web/index.html'
    current = target.read_text()
    updated = rendered(current)
    if args.check:
        if current != updated:
            raise SystemExit('site model picker differs from registry; regenerate it')
        print('site model picker matches the registered models and resource shapes')
    else:
        target.write_text(updated)
        print('generated', target)


if __name__ == '__main__':
    main()
