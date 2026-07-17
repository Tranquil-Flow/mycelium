import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
SCENARIO = ROOT / "scenarios" / "product-v1-tiny-directed.json"


class CliTests(unittest.TestCase):
    def test_snapshot_to_output_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "plan.json"
            completed = subprocess.run(
                [sys.executable, "-m", "mycelium_layer_planner", "--snapshot", str(SCENARIO), "--output", str(output)],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data["protocol"], "mycelium.route_plan.v2")
            self.assertIn("workload", data["diagnostics"])

    def test_invalid_snapshot_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{}", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, "-m", "mycelium_layer_planner", "--snapshot", str(bad)],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertNotEqual(completed.returncode, 0)

    def test_cli_has_no_max_nodes_or_runtime_flags(self):
        completed = subprocess.run(
            [sys.executable, "-m", "mycelium_layer_planner", "--help"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertNotIn("max-nodes", completed.stdout)
        self.assertNotIn("load-weights", completed.stdout)
        self.assertNotIn("router", completed.stdout.lower())


if __name__ == "__main__":
    unittest.main()
