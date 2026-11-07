// SPDX-License-Identifier: AGPL-3.0-only
use protocol::{artifact_grant as ag, identity::Identity};
use runtime::iroh_artifact::{ArtifactWireLimits,ChunkSession,RESPONSE_PROTOCOL};
fn artifact_fixture()->serde_json::Value{serde_json::from_str(include_str!("../../../protocol/fixtures/artifact_grant.json")).unwrap()}
fn artifact_now()->u64{artifact_fixture()["now_unix_ms"].as_u64().unwrap()}
fn grant_limits()->ag::GrantLimits {
    ag::GrantLimits{
        max_grant_ttl_ms:B::PyMyceliumMobileArtifactGrantMaxGrantTtlMsL38.span(Entries::ZERO).unwrap().millis(),
        max_request_ttl_ms:B::PyMyceliumMobileArtifactGrantMaxRequestTtlMsL39.span(Entries::ZERO).unwrap().millis(),
        request_ttl_ms:B::PyFinalMyceliumMobileArtifactGrantTtlMsL309C18.span(Entries::ZERO).unwrap().millis(),
        clock_skew_ms:B::PyMyceliumMobileArtifactGrantClockSkewMsL40.span(Entries::ZERO).unwrap().millis(),
    }
}
fn artifact_limits()->ArtifactWireLimits {
    // Reduced, input-derived test cap. Shipped native defaults require L1's
    // separate registry bindings, not an unrelated equal-valued Python budget.
    let fixture_bytes=include_str!("../../../protocol/fixtures/artifact_grant.json").len() as u64;
    ArtifactWireLimits{request_bytes:budgets::Bytes::new(fixture_bytes),header_bytes:budgets::Bytes::new(fixture_bytes),io:B::RouterClose.span(Entries::ZERO).unwrap()}
}
fn artifact_data()->&'static [u8]{b"real artifact bytes: \0\xff\x01, not a Router frame"}
fn artifact_seed()->Identity{Identity::from_secret("seed-endpoint-1",&[0x22;32]).unwrap()}
fn artifact_grant(member:&Identity,source:&Identity)->ag::Grant {
    let seed=artifact_seed();let mut message=artifact_fixture()["grant"]["message"].clone();
    message["recipient_endpoint_id"]=member.endpoint_id().into();
    message["recipient_key_digest"]=member.verification_key_digest().into();
    message["authorized_sources"]=serde_json::json!([{"endpoint_id":source.endpoint_id(),"verification_key_digest":source.verification_key_digest(),"direct_addrs":[]}]);
    message["allowed_object_digests"]=serde_json::json!([ag::range_digest(artifact_data())]);
    let envelope=serde_json::json!({"protocol":protocol::membership::SIGNED_MESSAGE_PROTOCOL,"signature":seed.sign(&message).unwrap(),"message":message,"verification_key":seed.public_key_record()});
    ag::verify_grant(&envelope,&seed.public_key_record(),artifact_now(),grant_limits()).unwrap()
}
fn chunk_envelope(member:&Identity,grant:&ag::Grant,id:&str)->(serde_json::Value,ag::ChunkRequest) {
    let source=grant.sources.keys().next().unwrap();let object=ag::range_digest(artifact_data());
    let request=ag::ChunkRequest{request_id:id.into(),source_endpoint_id:source.clone(),object_digest:object.clone(),offset_bytes:0,length_bytes:artifact_data().len() as u64};
    let envelope=ag::sign_chunk_request(member,grant,id,id,source,&object,0,request.length_bytes,artifact_now(),grant_limits()).unwrap();
    (envelope,request)
}
fn artifact_child(expected:EndpointId,mode:&str) {
    let rt=runtime();let clock=Time::new();
    let mut host=bind_host_protocols(&rt,&clock,clock.deadline(),iroh::SecretKey::from_bytes(&[94;32]),&[Channel::ArtifactChunk]).unwrap();
    let address=host.address().unwrap();let socket=*address.ip_addrs().next().unwrap();
    let source=Identity::from_secret(&address.id.to_string(),&[0x33;32]).unwrap();
    println!("L9_READY {}",serde_json::json!({"id":address.id.to_string(),"socket":socket.to_string(),"pid":std::process::id()}));std::io::stdout().flush().unwrap();
    let conn=host.accept(&clock,clock.deadline()).unwrap();assert_eq!(conn.remote_id(),expected);assert_eq!(conn.alpn(),Channel::ArtifactChunk.alpn());
    if ["artifact-good","artifact-auth","artifact-backpressure"].contains(&mode) {
        use runtime::iroh_artifact_source::{ArtifactService,ArtifactServeLimits};
        let objects=std::sync::Arc::new(FixtureObjects::new());
        let span=B::RouterClose.span(Entries::ZERO).unwrap();
        let service=ArtifactService::new(source,artifact_seed().public_key_record(),grant_limits(),objects.clone(),ArtifactServeLimits{wire:artifact_limits(),connections:Entries::new(1),per_peer:Entries::new(1),blocking:Entries::new(1),read:span,idle:span,maximum_pace:span},std::sync::Arc::new(artifact_now)).unwrap();
        rt.block_on(service.serve_connection(conn)).unwrap();
        assert_eq!(objects.read_count(),if mode=="artifact-auth"{1}else{2},"invalid or replayed request must not read bytes");
        objects.cleanup();
        host.close(&clock,clock.deadline()).unwrap();drop(host);drop(rt);assert!(UdpSocket::bind(socket).is_ok());println!("L9_CLOSED true");
        return;
    }
    let mut seen=std::collections::BTreeSet::new();
    rt.block_on(async {
        loop {
            let streams=tokio::time::timeout(allowance(),conn.accept_bi()).await.unwrap();
            let Ok((mut send,mut recv))=streams else{break;};
            let raw=recv.read_to_end(artifact_limits().request_bytes.get() as usize).await.unwrap();
            let envelope:serde_json::Value=serde_json::from_slice(&raw).expect("unwrapped signed canonical JSON");
            let (request,grant)=ag::verify_chunk_request(&envelope,&artifact_seed().public_key_record(),source.endpoint_id(),&source.verification_key_digest(),&|_|Some(artifact_data().len() as u64),artifact_now(),grant_limits()).unwrap();
            assert_eq!(grant.recipient_endpoint_id,conn.remote_id().to_string());
            let mut data=artifact_data().to_vec();
            let header=if !seen.insert(request.request_id.clone()){
                serde_json::json!({"protocol":RESPONSE_PROTOCOL,"status":"refused","code":"artifact_chunk_request_replay"})
            } else {
                let receipt=ag::sign_chunk_receipt(&source,&request,&grant,&data,artifact_now()).unwrap();
                serde_json::json!({"protocol":RESPONSE_PROTOCOL,"status":"ok","receipt":receipt})
            };
            let mut header=header;
            if header["status"]!="ok"{data.clear();}
            if mode=="artifact-tamper"{data[0]^=1;}
            if mode=="artifact-protocol"{header["protocol"]="wrong".into();}
            let bytes=protocol::canonical::to_bytes(&header).unwrap();
            let mut framed=(bytes.len() as u32).to_be_bytes().to_vec();framed.extend_from_slice(&bytes);framed.extend_from_slice(&data);
            if mode=="artifact-short"{framed.truncate(3);}
            if mode=="artifact-trailing"{framed.push(0);}
            send.write_all(&framed).await.unwrap();send.finish().unwrap();
        }
    });
    drop(conn);host.close(&clock,clock.deadline()).unwrap();drop(host);drop(rt);assert!(UdpSocket::bind(socket).is_ok());println!("L9_CLOSED true");
}
#[test]
fn real_quic_artifact_signed_grant_request_receipt_reuse_and_replay_refusal() {
    let rt=runtime();let clock=Time::new();let mut host=bind_host(&rt,&clock,clock.deadline()).unwrap();
    let local=host.address().unwrap().id;let mut peer=Peer::spawn(local,"artifact-good");
    let member=Identity::from_secret(&local.to_string(),&[0x11;32]).unwrap();let source=Identity::from_secret(&peer.address.id.to_string(),&[0x33;32]).unwrap();
    let grant=artifact_grant(&member,&source);
    let conn=host.connect_channel(&peer.address,Channel::ArtifactChunk,&clock,clock.deadline()).unwrap();
    let mut session=ChunkSession::new(conn,&rt,&clock,local,artifact_limits()).unwrap();
    for id in ["chunk-0","chunk-1"] {
        let (env,request)=chunk_envelope(&member,&grant,id);
        assert_eq!(session.fetch(&env,&request,&grant,clock.deadline()).expect("signed artifact chunk must cross real QUIC"),artifact_data());
    }
    let (env,request)=chunk_envelope(&member,&grant,"chunk-1");
    assert_eq!(session.fetch(&env,&request,&grant,clock.deadline()).unwrap_err().0,"artifact_source_refused:artifact_chunk_request_replay");
    assert_eq!(session.fetch(&env,&request,&grant,clock.deadline()).unwrap_err().0,"artifact_no_source_available","failed source stays excluded for acquisition");
    drop(session);host.close(&clock,clock.deadline()).unwrap();peer.finish();
}
#[test]
fn real_quic_artifact_receipt_and_framing_faults_exclude_source() {
    for (mode,code) in [("artifact-tamper","artifact_chunk_receipt_mismatch"),("artifact-trailing","artifact_chunk_receipt_mismatch"),("artifact-short","artifact_source_response_invalid"),("artifact-protocol","artifact_source_response_invalid")] {
        let rt=runtime();let clock=Time::new();let mut host=bind_host(&rt,&clock,clock.deadline()).unwrap();let local=host.address().unwrap().id;
        let mut peer=Peer::spawn(local,mode);let member=Identity::from_secret(&local.to_string(),&[0x11;32]).unwrap();let source=Identity::from_secret(&peer.address.id.to_string(),&[0x33;32]).unwrap();let grant=artifact_grant(&member,&source);
        let conn=host.connect_channel(&peer.address,Channel::ArtifactChunk,&clock,clock.deadline()).unwrap();let mut session=ChunkSession::new(conn,&rt,&clock,local,artifact_limits()).unwrap();let (env,request)=chunk_envelope(&member,&grant,"chunk-0");
        assert_eq!(session.fetch(&env,&request,&grant,clock.deadline()).unwrap_err().0,code,"{mode}");
        assert_eq!(session.fetch(&env,&request,&grant,clock.deadline()).unwrap_err().0,"artifact_no_source_available");
        drop(session);host.close(&clock,clock.deadline()).unwrap();peer.finish();
    }
}
