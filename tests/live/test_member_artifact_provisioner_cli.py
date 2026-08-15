from __future__ import annotations

import json

from mycelium_live import member_artifact_provisioner
from mycelium_live.member_artifact_provisioner import MemberArtifactAcquisitionError


def test_cli_emits_canonical_bounded_failure_envelope(monkeypatch, capsys) -> None:
    def fail(_job):
        raise MemberArtifactAcquisitionError("insufficient_disk")

    monkeypatch.setattr(
        member_artifact_provisioner, "acquire_member_stage_pack", fail
    )

    assert member_artifact_provisioner.main(["--job", "/private/job.json"]) == 2

    output = capsys.readouterr()
    assert output.err == ""
    assert output.out == (
        json.dumps(
            {
                "protocol": "mycelium.member_artifact_acquisition_failure.v1",
                "reason_code": "insufficient_disk",
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )
