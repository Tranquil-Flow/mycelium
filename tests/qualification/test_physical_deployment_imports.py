from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[2]


def test_physical_deployment_import_does_not_require_mlx() -> None:
    probe = textwrap.dedent(
        """
        import importlib.abc
        import sys

        class BlockMlx(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "mlx" or fullname.startswith("mlx."):
                    raise ModuleNotFoundError("blocked MLX import on NumPy-only host")
                return None

        sys.meta_path.insert(0, BlockMlx())
        import mycelium_qualification.physical_deployment
        print("physical_deployment_imported_without_mlx")
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "physical_deployment_imported_without_mlx"
