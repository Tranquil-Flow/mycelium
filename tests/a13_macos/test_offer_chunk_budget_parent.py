# SPDX-License-Identifier: AGPL-3.0-only
"""Real signed TLS chunk round-trip bound; no model/hardware claim."""
import json
from test_control_pump import _world
from test_managed_control import RUN_ID,DEPLOYMENT_ID
from mycelium_a13_macos.managed_offers import ManagedOfferClient
from mycelium_a13_macos.public_relay import _MAX_FRAME as MAX_FRAME_BYTES


def test_multi_megabyte_archive_uses_bounded_large_signed_frames(tmp_path):
    with _world(tmp_path/'world') as w:
        data=b'inert-byte-pattern'*200000
        offer=w['authority']._control_mailbox.publish_offer(target=w['member'],run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID,archive=data)
        client=ManagedOfferClient(w['target_client']);read=client._read;frames=[]
        def measured(*args):
            response=read(*args)
            frames.append(len(json.dumps(response).encode()))
            return response
        client._read=measured
        assert client.fetch(w['member'],offer,max_bytes=len(data))==data
        assert len(frames)<=8, 'small chunks exhaust public-round-trip budget'
        assert max(frames)<MAX_FRAME_BYTES
