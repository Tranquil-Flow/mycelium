from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import time

from mycelium_internet.physical import _verified_browser_observation
from mycelium_qualification.evidence import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://a8.example.test"
DIGEST = "sha256:" + "1" * 64


def test_production_node_browser_signer_verifies_in_python_gate(tmp_path: Path) -> None:
    key_path = tmp_path / "browser.key"
    key_path.write_bytes(b"b" * 32)
    key_path.chmod(0o600)
    module_path = ROOT / "ui" / "web" / "scripts" / "a8-browser-evidence.mjs"
    program = """
const {
  browserEvidenceAuthority,
  loadBrowserEvidenceSigner,
  signBrowserObservation,
} = await import(process.argv[1]);
const signer = await loadBrowserEvidenceSigner(process.argv[2]);
const now = Number(process.argv[5]);
const authority = browserEvidenceAuthority(signer, {
  challenge_id: `sha256:${'2'.repeat(64)}`,
  case_id: 'direct_path_qualified_browser_inference',
  origin: process.argv[3],
  deployment_id: 'deployment-a8',
  spec_digest: process.argv[4],
  source_digest: process.argv[4],
  request_count: 1,
  issued_at_unix_ms: now - 1000,
  expires_at_unix_ms: now + 299000,
});
const observation = {
  protocol: 'mycelium.a8_product_browser_observation.v2',
  challenge_id: authority.challenge_id,
  case_id: authority.case_id,
  origin: process.argv[3],
  deployment_id: authority.deployment_id,
  spec_digest: process.argv[4],
  source_digest: process.argv[4],
  observed_at_unix_ms: now,
  completed_requests: 1,
  request_ids: ['request-a8'],
  transport_report_digests: [`sha256:${'5'.repeat(64)}`],
  numeric_probe: 1e-7,
};
process.stdout.write(JSON.stringify({
  authority,
  envelope: signBrowserObservation(observation, signer),
}));
"""
    now = int(time.time() * 1_000)
    completed = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            program,
            module_path.as_uri(),
            str(key_path),
            ORIGIN,
            DIGEST,
            str(now),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)

    verified = _verified_browser_observation(
        document["envelope"],
        document["authority"],
        expected_case_id="direct_path_qualified_browser_inference",
        expected_origin=ORIGIN,
        expected_deployment_id="deployment-a8",
        expected_spec_digest=DIGEST,
        expected_source_digest=DIGEST,
        expected_transport_report_digests=["sha256:" + "5" * 64],
        now_unix_ms=now,
    )
    assert verified == document["envelope"]["observation"]


def test_node_transport_digest_preserves_python_integral_float_bytes() -> None:
    module_path = ROOT / "ui" / "web" / "scripts" / "a8-browser-evidence.mjs"
    report = {"loss_ratio": 0.0, "sample_count": 5}
    raw_report = canonical_json_bytes(report).decode("utf-8")
    program = """
const { canonicalTransportReportDigest } = await import(process.argv[1]);
process.stdout.write(canonicalTransportReportDigest(process.argv[2]));
"""
    completed = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            program,
            module_path.as_uri(),
            raw_report,
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert completed.returncode == 0, completed.stderr
    expected = "sha256:" + hashlib.sha256(raw_report.encode("utf-8")).hexdigest()
    assert completed.stdout == expected
