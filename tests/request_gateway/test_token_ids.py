# SPDX-License-Identifier: AGPL-3.0-only
"""Router token IDs survive the real local gateway; not inference evidence."""
from dataclasses import replace

import pytest
from mycelium_request_gateway.backend import _GatewayTokenSink
from mycelium_request_gateway.contracts import (
    AdmissionError, StreamEvent, REQUEST_EVENT_PROTOCOL, REQUEST_EVENT_PROTOCOL_V2,
    REQUEST_GATEWAY_PROTOCOL_V2,
)
from mycelium_request_gateway.service import RequestGatewayService
from test_stream import _submission, _drain
from test_core import MutableQualificationSource, _synthetic_qualification


class Codec:
    def encode(self, prompt): return (1,)
    def decode_token(self, token_id):
        return ' unchanged text '


class TokenBackend:
    def __init__(self, replay_id=11): self.replay_id=replay_id
    def run(self, request_id, submission, emit_token, is_cancelled):
        sink=_GatewayTokenSink(Codec(),emit_token)
        sink.emit(0,11)
        sink.emit(0,self.replay_id)
        sink.emit(1,12)
        return 'completed'
    def cancel(self, request_id): pass


def run_gateway(v2=True,replay_id=11):
    qualification=_synthetic_qualification()
    service=RequestGatewayService(qualification_source=MutableQualificationSource(qualification),
        backend=TokenBackend(replay_id),max_buffered_events=16,request_id_source=lambda:"token-ids")
    submission=_submission(qualification,max_new_tokens=2)
    if v2:
        submission=replace(submission,protocol=REQUEST_GATEWAY_PROTOCOL_V2,
            workload_profile_id='interactive_chat_v1',qos_class='interactive')
    try:
        request_id=service.submit(submission)
        return _drain(service.subscribe(request_id,last_event_id=None))
    finally: service.close()


def test_v2_carries_ids_without_changing_text_and_roundtrips():
    events=run_gateway()
    assert events[-1].kind=='completed'
    tokens=[e for e in events if e.kind=='token']
    assert [e.to_dict()['token_id'] for e in tokens]==[11,12]
    assert [e.text for e in tokens]==[' unchanged text ']*2
    assert [StreamEvent.from_dict(e.to_dict()) for e in tokens]==tokens


def test_v1_wire_is_unchanged_with_real_token_sink():
    events=run_gateway(v2=False)
    assert events[-1].kind=='completed'
    assert all('token_id' not in e.to_dict() for e in events)
    assert len([e for e in events if e.kind=='token'])==2


def test_different_id_with_identical_decoded_text_is_not_an_idempotent_replay():
    events=run_gateway(replay_id=12)
    assert events[-1].kind=='failed'
    assert events[-1].code=='token_order_violation'


@pytest.mark.parametrize('value',[True,-1,1.2,'12',None])
def test_v2_rejects_invalid_present_token_id(value):
    document=StreamEvent(request_id='r',sequence=1,kind='token',token_index=0,
        text='same',protocol=REQUEST_EVENT_PROTOCOL_V2).to_dict()
    document['token_id']=value
    with pytest.raises(AdmissionError): StreamEvent.from_dict(document)


def test_old_v2_without_id_remains_readable():
    event=StreamEvent(request_id='r',sequence=1,kind='token',token_index=0,
        text='',protocol=REQUEST_EVENT_PROTOCOL_V2)
    assert StreamEvent.from_dict(event.to_dict())==event


def test_ids_not_permitted_on_v1_or_nontoken_events():
    for event in (
        StreamEvent(request_id='r',sequence=1,kind='token',token_index=0,text='',protocol=REQUEST_EVENT_PROTOCOL),
        StreamEvent(request_id='r',sequence=0,kind='accepted',protocol=REQUEST_EVENT_PROTOCOL_V2),
    ):
        document=event.to_dict();document['token_id']=11
        with pytest.raises(AdmissionError): StreamEvent.from_dict(document)
