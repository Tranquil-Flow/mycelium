import unittest

from mycelium_layer_planner.calibration import ingest_calibration


class CalibrationTests(unittest.TestCase):
    def valid(self, evidence="measured"):
        return {
            "model_id": "org/model",
            "revision": "immutable-revision",
            "backend": "test-backend",
            "device_id": "device-1",
            "layer_start": 0,
            "layer_end": 2,
            "precision": "fp16",
            "prefill_samples_ms": [1.0, 1.1],
            "decode_samples_ms": [0.2, 0.3],
            "payload_bytes": [256, 1024],
            "batch_context_points": [[1, 70], [4, 285]],
            "measured_at": "2026-07-16T00:00:00Z",
            "environment": {"os": "test"},
            "evidence": evidence,
            "measurement_command": "python benchmark.py" if evidence == "measured" else None,
        }

    def test_measured_artifact_requires_real_evidence_handle(self):
        artifact = ingest_calibration(self.valid())
        self.assertTrue(artifact.is_measured)
        bad = self.valid()
        bad["measurement_command"] = None
        with self.assertRaises(ValueError):
            ingest_calibration(bad)

    def test_heuristic_cannot_be_labeled_measured(self):
        data = self.valid("heuristic")
        data["measurement_command"] = "pretend"
        artifact = ingest_calibration(data)
        self.assertFalse(artifact.is_measured)

    def test_ranges_and_distributions_are_validated(self):
        data = self.valid()
        data["layer_end"] = data["layer_start"]
        with self.assertRaises(ValueError):
            ingest_calibration(data)
        data = self.valid()
        data["decode_samples_ms"] = []
        with self.assertRaises(ValueError):
            ingest_calibration(data)


if __name__ == "__main__":
    unittest.main()
