// SPDX-License-Identifier: AGPL-3.0-only
fn dispatch_child(expected: EndpointId) {
    use runtime::iroh_receive::DispatchRefusal;
    let rt = runtime();
    let clock = Time::new();
    let mut host = bind_host(&rt, &clock, clock.deadline()).unwrap();
    let address = host.address().unwrap();
    println!(
        "L9_READY {}",
        serde_json::json!({"id":address.id.to_string(),"socket":address.ip_addrs().next().unwrap().to_string(),"pid":std::process::id()})
    );
    std::io::stdout().flush().unwrap();
    let conn = host.accept(&clock, clock.deadline()).unwrap();
    let fence = ConnectionFence {
        endpoint_id: *expected.as_bytes(),
        local_generation: 3,
        remote_generation: 2,
    };
    let generation = Cell::new(fence);
    let mut receiver =
        RouterReceiver::new(conn.clone(), &rt, &clock, fence, || generation.get()).unwrap();
    let delivery = receiver.receive(clock.deadline()).unwrap();
    assert_eq!(delivery.payload(), payload());
    println!("L9_DISPATCH_PENDING");
    std::io::stdout().flush().unwrap();
    // Keep the single-thread runtime pumping QUIC while a separate reader
    // waits for the dispatch decision. Blocking stdin here inflated PTO and
    // starved transport ACK/close processing in the fixture.
    let (tx, rx) = tokio::sync::oneshot::channel();
    let reader = std::thread::spawn(move || {
        let mut command = String::new();
        std::io::stdin().read_line(&mut command).unwrap();
        tx.send(command).unwrap();
    });
    let command = rt.block_on(async {
        tokio::time::timeout(allowance(), rx)
            .await
            .unwrap()
            .unwrap()
    });
    reader.join().unwrap();
    match command.trim() {
        "ack" => delivery.acknowledge().unwrap(),
        "queue_full" => delivery.reject(DispatchRefusal::QueueFull).unwrap(),
        "rotate" => {
            generation.set(ConnectionFence {
                remote_generation: fence.remote_generation + 1,
                ..fence
            });
            assert_eq!(delivery.acknowledge(), Err(Error::Generation));
        }
        _ => panic!("unknown fixture command"),
    }
    rt.block_on(async {
        tokio::time::timeout(allowance(), conn.closed())
            .await
            .unwrap();
    });
    drop(receiver);
    drop(conn);
    host.close(&clock, clock.deadline()).unwrap();
    drop(host);
    drop(rt);
    for address in address.ip_addrs() {
        assert!(UdpSocket::bind(*address).is_ok());
    }
    println!("L9_CLOSED true");
}
#[test]
fn real_quic_dispatch_ack_is_delayed_and_queue_full_or_rotation_never_acknowledges() {
    for command in ["ack", "queue_full", "rotate"] {
        let rt = runtime();
        let clock = Time::new();
        let mut host = bind_host(&rt, &clock, clock.deadline()).unwrap();
        let peer = Peer::spawn(host.address().unwrap().id, "dispatch");
        let conn = host
            .connect(&peer.address, &clock, clock.deadline())
            .unwrap();
        let (mut send, mut recv) = rt.block_on(conn.open_bi()).unwrap();
        let data = [2u64.to_be_bytes().as_slice(), &payload()].concat();
        let frame = remote::encode_remote_frame(RemoteKind::Transfer, [9; 16], &data).unwrap();
        rt.block_on(send.write_all(&frame)).unwrap();
        send.finish().unwrap();
        // A registry-bound observation interval is not a dispatch deadline or a
        // cleanup grace. It drives QUIC while the receiver awaits an explicit
        // local command; an eager stream-read ACK deterministically fails here.
        let mut first = [0u8; 1];
        let pending = rt.block_on(async {
            tokio::time::timeout(
                Duration::from_millis(
                    B::PyMyceliumRouterTransportsIrohDeliveryCancelAckTimeoutSecondsL75
                        .span(Entries::ZERO)
                        .unwrap()
                        .millis(),
                ),
                recv.read(&mut first),
            )
            .await
        });
        assert!(
            pending.is_err(),
            "stream read must not ACK before dispatch: {pending:?}"
        );
        peer.line("L9_DISPATCH_PENDING");
        let mut peer = peer;
        writeln!(peer.child.stdin.as_mut().unwrap(), "{command}").unwrap();
        let response = rt.block_on(async {
            tokio::time::timeout(
                allowance(),
                recv.read_to_end(remote::REMOTE_MAX_FRAME_BYTES),
            )
            .await
            .unwrap()
        });
        match command {
            "ack" => {
                let bytes = response.unwrap();
                let frame = remote::decode_remote_frame(&bytes).unwrap();
                assert_eq!(frame.kind, RemoteKind::Ack);
                assert_eq!(frame.message_id, [9; 16]);
            }
            "queue_full" => {
                let bytes = response.unwrap();
                let frame = remote::decode_remote_frame(&bytes).unwrap();
                assert_eq!(frame.kind, RemoteKind::Error);
                assert_eq!(frame.payload, b"queue_full");
            }
            "rotate" => assert!(response.is_err(), "rotated identity must close, not ACK"),
            _ => unreachable!(),
        }
        drop(send);
        drop(recv);
        conn.close(0u32.into(), b"dispatch_done");
        drop(conn);
        host.close(&clock, clock.deadline()).unwrap();
        peer.finish();
    }
}
