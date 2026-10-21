# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from pathlib import Path

import pytest

from tests.a15_release_decision.synthetic_world import SyntheticWorld, build_template_repo


@pytest.fixture(scope="session")
def template(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, str]]:
    repo = tmp_path_factory.mktemp("a15-synthetic-template") / "checkout"
    return repo, build_template_repo(repo)


@pytest.fixture
def world(tmp_path: Path, template: tuple[Path, dict[str, str]]) -> SyntheticWorld:
    return SyntheticWorld(tmp_path, template)
