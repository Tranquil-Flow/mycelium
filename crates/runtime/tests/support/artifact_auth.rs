// SPDX-License-Identifier: AGPL-3.0-only
#[test]
fn real_quic_artifact_holds_bytes_until_admission_completes_and_reuses_connection() {
    let rt=runtime();let clock=Time::new();let mut host=bind_host(&rt,&clock,clock.deadline()).unwrap();let local=host.address().unwrap().id;
    let mut peer=Peer::spawn(local,"artifact-backpressure");let member=Identity::from_secret(&local.to_string(),&[0x11;32]).unwrap();let source=Identity::from_secret(&peer.address.id.to_string(),&[0x33;32]).unwrap();let grant=artifact_grant(&member,&source);
    let conn=host.connect_channel(&peer.address,Channel::ArtifactChunk,&clock,clock.deadline()).unwrap();let (env,request)=chunk_envelope(&member,&grant,"held");
    let (mut send,mut recv)=rt.block_on(conn.open_bi()).unwrap();rt.block_on(send.write_all(&protocol::canonical::to_bytes(&env).unwrap())).unwrap();send.finish().unwrap();
    let mut first=[0u8;1];
    let probe=Duration::from_millis(B::PyMyceliumRouterTransportsIrohDeliveryCancelAckTimeoutSecondsL75.span(Entries::ZERO).unwrap().millis());
    assert!(rt.block_on(async{tokio::time::timeout(probe,recv.read(&mut first)).await}).is_err(),"artifact bytes precede durable admission");
    assert_eq!(peer.line("L9_ARTIFACT_HELD "),"true");
    writeln!(peer.child.stdin.as_mut().unwrap(),"release").unwrap();
    let raw=rt.block_on(async{tokio::time::timeout(allowance(),recv.read_to_end(artifact_limits().header_bytes.get() as usize)).await.unwrap().unwrap()});
    let (header,bytes)=runtime::iroh_artifact::decode_response(&raw,artifact_limits().header_bytes).unwrap();ag::verify_chunk_receipt(&header["receipt"],&request,&grant,bytes).unwrap();assert_eq!(bytes,artifact_data());
    drop(send);drop(recv);
    let mut session=ChunkSession::new(conn,&rt,&clock,local,artifact_limits()).unwrap();let (env,request)=chunk_envelope(&member,&grant,"after-held");assert_eq!(session.fetch(&env,&request,&grant,clock.deadline()).unwrap(),artifact_data());
    drop(session);host.close(&clock,clock.deadline()).unwrap();peer.finish();
}
#[test]
fn real_quic_artifact_authentication_refusals_precede_durable_admission() {
    let rt=runtime();let clock=Time::new();let mut host=bind_host(&rt,&clock,clock.deadline()).unwrap();let local=host.address().unwrap().id;
    let mut peer=Peer::spawn(local,"artifact-auth");let member=Identity::from_secret(&local.to_string(),&[0x11;32]).unwrap();let source=Identity::from_secret(&peer.address.id.to_string(),&[0x33;32]).unwrap();let grant=artifact_grant(&member,&source);
    let conn=host.connect_channel(&peer.address,Channel::ArtifactChunk,&clock,clock.deadline()).unwrap();
    let (good,_)=chunk_envelope(&member,&grant,"chunk-0");
    let mut wrong_transport=good.clone();wrong_transport["message"]["grant"]["message"]["recipient_endpoint_id"]=peer.address.id.to_string().into();
    let mut wrong_signature=good.clone();wrong_signature["message"]["request_id"]="tampered".into();
    for (envelope,code) in [(wrong_transport,"artifact_recipient_transport_mismatch"),(wrong_signature,"artifact_chunk_request_signature_invalid")] {
        let raw=protocol::canonical::to_bytes(&envelope).unwrap();
        let response=rt.block_on(async {
            let (mut send,mut recv)=conn.open_bi().await.unwrap();send.write_all(&raw).await.unwrap();send.finish().unwrap();
            tokio::time::timeout(allowance(),recv.read_to_end(artifact_limits().header_bytes.get() as usize)).await.unwrap().unwrap()
        });
        let length=u32::from_be_bytes(response[..4].try_into().unwrap()) as usize;
        assert_eq!(response.len(),4+length,"refusal has header only");
        let header:serde_json::Value=serde_json::from_slice(&response[4..]).unwrap();
        assert_eq!(header,serde_json::json!({"protocol":RESPONSE_PROTOCOL,"status":"refused","code":code}));
    }
    let mut session=ChunkSession::new(conn,&rt,&clock,local,artifact_limits()).unwrap();let (envelope,request)=chunk_envelope(&member,&grant,"chunk-0");
    assert_eq!(session.fetch(&envelope,&request,&grant,clock.deadline()).unwrap(),artifact_data(),"refusals do not consume replay admission");
    drop(session);host.close(&clock,clock.deadline()).unwrap();peer.finish();
}
