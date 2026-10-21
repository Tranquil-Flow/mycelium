# SPDX-License-Identifier: AGPL-3.0-only
"""Build-host convenience must not select a newer-OS GPU runtime."""
import importlib.metadata
import pytest
from scripts import build_a13_macos as build

@pytest.mark.parametrize('platform,version,valid',[
    ('macosx_14_0_arm64','0.31.1',True),
    ('macosx_26_0_arm64','0.31.1',False),
    ('macosx_14_0_arm64','0.30.0',False),
    ('manylinux_2_35_aarch64','0.31.1',False)])
def test_runtime_distribution_contract(monkeypatch,platform,version,valid):
    class Distribution:
        def __init__(self):self.version=version
        def read_text(self,name):return 'Wheel-Version: 1.0\nTag: py3-none-'+platform+'\n' if name=='WHEEL' else None
    monkeypatch.setattr(importlib.metadata,'distribution',lambda name:Distribution())
    if valid:
        record=build.require_compatible_mlx_wheels()
        assert set(record)=={'mlx','mlx-metal'}
        assert all(row['version']=='0.31.1' for row in record.values())
    else:
        with pytest.raises(SystemExit,match='compatible_mlx_wheel_required'):
            build.require_compatible_mlx_wheels()
