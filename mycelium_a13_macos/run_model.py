# SPDX-License-Identifier: AGPL-3.0-only
"""The demo model picker: one pinned Qwen3 size, persisted in app state.

A Finder launch cannot set ``MYCELIUM_A13_RUN_MODEL``, so the coworking demo
chooses its model in the owner UI and the choice is stored privately as
``<state root>/run-model.json``.  The environment variable still wins when it
is set (operator scripts and tests), and then the picker is shown as locked.

Without a saved choice the demo default is Qwen3-14B when the owner's storage
requirement for it (its pinned snapshot plus 10%, model_resources.py) fits the
consent storage ceiling, else the largest Qwen3 that does.  The ceiling is
derived per model (``model_resources.storage_consent_ceiling``: 32,568,709,325 B,
set by Qwen3-14B; its owner need is 32,507,875,154 B), so the default is
Qwen3-14B; joined Macs fetch only their
slice, bounded by their share of the per-split proposal.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .serving import (QWEN3_0_6B_MODEL, QWEN3_8B_MODEL, QWEN3_14B_MODEL,
                      RUN_MODEL_ENVIRONMENT, ModelContract, selected_run_model)

PROTOCOL = 'mycelium.a13_run_model_choice.v1'
_FILE = 'run-model.json'


@dataclass(frozen=True)
class Choice:
    name: str
    label: str
    model: ModelContract
    #: Sum of every file in the pinned HF revision (HF tree API, 2026-10-13).
    snapshot_bytes: int
    #: int8-weight-only resident weights across the whole route (approximate).
    int8_weight_bytes: int


CHOICES: tuple[Choice, ...] = (
    Choice('qwen3-0.6b', 'Qwen3-0.6B (tests, quick bring-up)', QWEN3_0_6B_MODEL,
           1_519_209_243, 750_000_000),
    Choice('qwen3-8b', 'Qwen3-8B (safe fallback)', QWEN3_8B_MODEL,
           16_397_461_266, 8_200_000_000),
    Choice('qwen3-14b', 'Qwen3-14B (target demo model)', QWEN3_14B_MODEL,
           29_552_613_776, 14_800_000_000),
)
_BY_NAME = {c.name: c for c in CHOICES}


def choice(name: object) -> Choice:
    if not isinstance(name, str) or name not in _BY_NAME:
        raise ValueError('run_model_choice_invalid')
    return _BY_NAME[name]


def choice_for_model(model: object) -> Choice | None:
    return next((c for c in CHOICES if c.model == model), None)


def storage_ceiling() -> int:
    """Largest storage a person can consent to (app.consent_valid's bound)."""
    from .model_resources import storage_consent_ceiling
    return storage_consent_ceiling()


def owner_storage_bytes(c: Choice) -> int:
    """Storage the owner must consent to for this choice (model_resources)."""
    from .model_resources import owner_storage_requirement
    return owner_storage_requirement(c.model)


def demo_default_choice(*, storage_ceiling: int | None = None) -> str | None:
    ceiling = globals()['storage_ceiling']() if storage_ceiling is None else storage_ceiling
    fitting = [c for c in CHOICES if owner_storage_bytes(c) <= ceiling]
    return max(fitting, key=lambda c: c.snapshot_bytes).name if fitting else None


def load_choice(root: Path | str) -> str | None:
    from .authority import _load_private_json
    try:
        value = _load_private_json(Path(root) / _FILE, max_bytes=4096)
    except Exception:
        raise ValueError('run_model_state_invalid') from None
    if value is None:
        return None
    if set(value) != {'protocol', 'choice'} or value['protocol'] != PROTOCOL or value['choice'] not in _BY_NAME:
        raise ValueError('run_model_state_invalid')
    return value['choice']


def save_choice(root: Path | str, name: str) -> str:
    from .authority import _atomic_private_json
    choice(name)
    _atomic_private_json(Path(root) / _FILE, {'protocol': PROTOCOL, 'choice': name}, max_bytes=4096)
    return name


def _environ(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    import os
    return os.environ if environ is None else environ


def app_run_model(root: Path | str, *, environ: Mapping[str, str] | None = None
                  ) -> tuple[str, str, ModelContract]:
    """``(name, source, contract)``; source is environment | saved | demo_default."""
    env = _environ(environ)
    if RUN_MODEL_ENVIRONMENT in env:
        model = selected_run_model(env)
        return env[RUN_MODEL_ENVIRONMENT], 'environment', model
    saved = load_choice(root)
    if saved is not None:
        return saved, 'saved', _BY_NAME[saved].model
    default = demo_default_choice()
    if default is None:  # pragma: no cover - the 0.6B always fits
        raise ValueError('run_model_unavailable')
    return default, 'demo_default', _BY_NAME[default].model


def document(root: Path | str, *, environ: Mapping[str, str] | None = None) -> dict:
    """The picker state shown by the UI (owner and target)."""
    from .app import host_memory_bytes
    from .model_resources import consent_document
    name, source, model = app_run_model(root, environ=environ)
    ceiling = storage_ceiling()
    return {'selected': name, 'source': source, 'locked': source == 'environment',
            'model_id': model.model_id, 'resolved_commit': model.resolved_commit,
            'demo_default': demo_default_choice(), 'storage_ceiling_bytes': ceiling,
            'resources': consent_document(model, device_memory_bytes=host_memory_bytes()),
            'choices': [{'name': c.name, 'label': c.label, 'model_id': c.model.model_id,
                         'resolved_commit': c.model.resolved_commit,
                         'snapshot_bytes': c.snapshot_bytes,
                         'int8_weight_bytes': c.int8_weight_bytes,
                         'owner_storage_bytes': owner_storage_bytes(c),
                         'fits_storage_ceiling': owner_storage_bytes(c) <= ceiling} for c in CHOICES]}
