// SPDX-License-Identifier: AGPL-3.0-only
//! Pinned turmoil 0.7.2 / Tokio 1.53.1 typed-seam comparison, not QUIC/Iroh.
use budgets::BudgetId as B;
use sim::{DeliveryFault, Io, Message, World};

#[derive(Debug, Clone, Copy)]
enum Case {
    Healthy,
    Drop,
    Duplicate,
    Reorder,
    Partition,
}
fn expected(case: Case) -> Vec<u64> {
    match case {
        Case::Healthy => vec![1, 2, 3],
        Case::Drop => vec![2, 3],
        Case::Duplicate => vec![1, 1, 2, 3],
        Case::Reorder => vec![2, 1, 3],
        Case::Partition => vec![3],
    }
}
fn minimal_network(case: Case) -> Vec<u64> {
    let mut world = World::default();
    for node in [1, 2] {
        world.driver.at(0, node, Io::Add { node }).unwrap();
    }
    world.drain(10).unwrap();
    if matches!(case, Case::Partition) {
        world
            .driver
            .at(
                0,
                0,
                Io::Partition {
                    a: 1,
                    b: 2,
                    enabled: true,
                },
            )
            .unwrap();
    }
    for id in [1, 2] {
        let fault = if id == 1 {
            match case {
                Case::Drop => DeliveryFault::Drop,
                Case::Duplicate => DeliveryFault::Duplicate,
                Case::Reorder => DeliveryFault::Reorder,
                _ => DeliveryFault::None,
            }
        } else {
            DeliveryFault::None
        };
        world
            .driver
            .at(
                0,
                1,
                Io::Send {
                    from: 1,
                    to: 2,
                    message: Message { id, value: id },
                    fault,
                },
            )
            .unwrap();
    }
    world.drain(30).unwrap();
    let recovery = sim::span(B::MemberRestartGrace).unwrap().millis() * 3;
    world
        .driver
        .at(
            recovery,
            0,
            Io::Partition {
                a: 1,
                b: 2,
                enabled: false,
            },
        )
        .unwrap();
    world
        .driver
        .at(
            recovery,
            1,
            Io::Send {
                from: 1,
                to: 2,
                message: Message { id: 3, value: 3 },
                fault: DeliveryFault::None,
            },
        )
        .unwrap();
    world.drain(10).unwrap();
    world.nodes[&2].inbox.iter().map(|m| m.id).collect()
}
fn duration(id: B) -> std::time::Duration {
    std::time::Duration::from_millis(sim::span(id).unwrap().millis())
}
fn turmoil_network(case: Case, seed: u64) -> Vec<u64> {
    use std::{cell::RefCell, net::Ipv4Addr, rc::Rc};
    use turmoil::net::UdpSocket;
    let tick = duration(B::OwnerPoll);
    let hold = duration(B::MemberRestartGrace);
    let mut simulation = turmoil::Builder::new()
        .rng_seed(seed)
        .epoch(std::time::SystemTime::UNIX_EPOCH)
        .simulation_duration(duration(B::MemberChosenWall))
        .tick_duration(tick)
        .min_message_latency(tick)
        .max_message_latency(tick)
        .fail_rate(0.0)
        .repair_rate(0.0)
        .build();
    let output = Rc::new(RefCell::new(Vec::new()));
    let received = output.clone();
    let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
    simulation.client("receiver", async move {
        let socket = UdpSocket::bind((Ipv4Addr::UNSPECIFIED, 4100)).await?;
        ready_tx
            .send(())
            .map_err(|_| "receiver_ready_channel_closed")?;
        loop {
            let mut buffer = [0u8; 8];
            let (n, _) = socket.recv_from(&mut buffer).await?;
            assert_eq!(n, 8, "typed message width");
            let id = u64::from_be_bytes(buffer);
            received.borrow_mut().push(id);
            if id == 3 {
                break;
            }
        }
        // Give every queued message a virtual completion opportunity; a delayed
        // duplicate after recovery must not be silently omitted from comparison.
        tokio::time::sleep(hold).await;
        let mut buffer = [0u8; 8];
        loop {
            match socket.try_recv_from(&mut buffer) {
                Ok((n, _)) => {
                    assert_eq!(n, 8);
                    received.borrow_mut().push(u64::from_be_bytes(buffer));
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                Err(e) => return Err(e.into()),
            }
        }
        Ok(())
    });
    simulation.client("sender", async move {
        ready_rx.await?;
        let socket = UdpSocket::bind((Ipv4Addr::UNSPECIFIED, 4101)).await?;
        let destination = (turmoil::lookup("receiver"), 4100);
        match case {
            Case::Partition => {
                turmoil::partition("sender", "receiver");
                socket.send_to(&1u64.to_be_bytes(), destination).await?;
                socket.send_to(&2u64.to_be_bytes(), destination).await?;
                tokio::time::sleep(hold).await;
                turmoil::repair("sender", "receiver");
            }
            Case::Drop => {
                // Drop exactly one real simulated datagram by partitioning only
                // its delivery window. Repair precedes the next message.
                turmoil::partition_oneway("sender", "receiver");
                socket.send_to(&1u64.to_be_bytes(), destination).await?;
                tokio::time::sleep(hold).await;
                turmoil::repair_oneway("sender", "receiver");
                socket.send_to(&2u64.to_be_bytes(), destination).await?;
            }
            Case::Reorder => {
                // Explicit typed-adapter reorder, not a claimed turmoil packet
                // reordering API: defer message 1 until message 2 is delivered.
                socket.send_to(&2u64.to_be_bytes(), destination).await?;
                tokio::time::sleep(hold).await;
                socket.send_to(&1u64.to_be_bytes(), destination).await?;
            }
            Case::Healthy | Case::Duplicate => {
                socket.send_to(&1u64.to_be_bytes(), destination).await?;
                if matches!(case, Case::Duplicate) {
                    socket.send_to(&1u64.to_be_bytes(), destination).await?;
                }
                socket.send_to(&2u64.to_be_bytes(), destination).await?;
            }
        }
        tokio::time::sleep(hold).await;
        socket.send_to(&3u64.to_be_bytes(), destination).await?;
        Ok(())
    });
    simulation.run().unwrap();
    output.borrow().clone()
}
#[test]
fn pinned_turmoil_network_matches_minimal_driver_and_independent_oracle() {
    for case in [
        Case::Healthy,
        Case::Drop,
        Case::Duplicate,
        Case::Reorder,
        Case::Partition,
    ] {
        let oracle = expected(case);
        assert_eq!(minimal_network(case), oracle, "minimal {case:?}");
        for seed in [17, 89] {
            let actual = turmoil_network(case, seed);
            assert_eq!(actual, oracle, "turmoil {case:?} seed={seed}");
            assert_eq!(turmoil_network(case, seed), actual, "repeat drift");
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
struct DiskObservation {
    failed_write: bool,
    old_preserved: bool,
    failed_value_absent: bool,
    recovered: bool,
    cleaned: bool,
}
fn minimal_disk() -> DiskObservation {
    let mut w = World::default();
    w.driver.at(0, 0, Io::Add { node: 1 }).unwrap();
    w.drain(5).unwrap();
    for event in [
        Io::Put {
            node: 1,
            key: 1,
            value: 7,
        },
        Io::DiskFailure {
            node: 1,
            enabled: true,
        },
        Io::Put {
            node: 1,
            key: 2,
            value: 8,
        },
    ] {
        w.driver.at(w.driver.clock.mono_ms, 1, event).unwrap();
        w.drain(5).unwrap();
    }
    let failed_write = w.log.iter().any(|s| s == "write:disk_failure");
    let old_preserved = w.nodes[&1].durable.get(&1) == Some(&7);
    let failed_value_absent = !w.nodes[&1].durable.contains_key(&2);
    for event in [
        Io::DiskFailure {
            node: 1,
            enabled: false,
        },
        Io::Put {
            node: 1,
            key: 3,
            value: 9,
        },
        Io::Crash { node: 1 },
        Io::Restart { node: 1 },
    ] {
        w.driver.at(w.driver.clock.mono_ms, 1, event).unwrap();
        w.drain(5).unwrap();
    }
    let recovered = w.nodes[&1].memory.get(&3) == Some(&9);
    let cleaned = w.driver.pending() == 0;
    DiskObservation {
        failed_write,
        old_preserved,
        failed_value_absent,
        recovered,
        cleaned,
    }
}
fn turmoil_disk(seed: u64) -> DiskObservation {
    use std::{
        cell::{Cell, RefCell},
        os::unix::fs::FileExt,
        rc::Rc,
    };
    use turmoil::fs::shim::std::fs::{OpenOptions, read, read_dir, remove_file, sync_dir};
    let cap = B::ReplayPage.bytes().unwrap().get();
    let mut builder = turmoil::Builder::new();
    builder
        .rng_seed(seed)
        .epoch(std::time::SystemTime::UNIX_EPOCH)
        .simulation_duration(duration(B::MemberChosenWall))
        .tick_duration(duration(B::OwnerPoll));
    builder
        .fs()
        .capacity(cap)
        .sync_probability(0.0)
        .io_error_probability(0.0);
    let mut simulation = builder.build();
    let phase = Rc::new(Cell::new(false));
    let observation = Rc::new(RefCell::new(DiskObservation {
        failed_write: false,
        old_preserved: false,
        failed_value_absent: false,
        recovered: false,
        cleaned: false,
    }));
    let notify = Rc::new(tokio::sync::Notify::new());
    let host_phase = phase.clone();
    let host_observation = observation.clone();
    let host_notify = notify.clone();
    simulation.host("disk", move || {
        let phase = host_phase.clone();
        let observation = host_observation.clone();
        let notify = host_notify.clone();
        async move {
            if !phase.get() {
                let old = OpenOptions::new()
                    .create_new(true)
                    .write(true)
                    .open("/key1")?;
                old.write_all_at(&[7], 0)?;
                old.sync_all()?;
                sync_dir("/")?;
                let failed = OpenOptions::new()
                    .create_new(true)
                    .write(true)
                    .open("/key2")?;
                let error = failed
                    .write_all_at(&vec![8; usize::try_from(cap)?], 0)
                    .unwrap_err();
                // turmoil 0.7.2 fs/shim/std/fs/mod.rs:829 maps the capacity
                // diagnostic through Error::other; it does NOT expose errno 28.
                assert_eq!(error.raw_os_error(), None);
                assert_eq!(error.kind(), std::io::ErrorKind::Other);
                assert_eq!(error.to_string(), "No space left on device");
                {
                    let mut o = observation.borrow_mut();
                    o.failed_write = true;
                    o.old_preserved = read("/key1")? == [7];
                    o.failed_value_absent = read("/key2")?.is_empty();
                }
                drop(failed);
                remove_file("/key2")?;
                let recovered = OpenOptions::new()
                    .create_new(true)
                    .write(true)
                    .open("/key3")?;
                recovered.write_all_at(&[9], 0)?;
                recovered.sync_all()?;
                sync_dir("/")?;
            } else {
                assert_eq!(read("/key1")?, [7], "committed old data survives crash");
                observation.borrow_mut().recovered = read("/key3")? == [9];
                remove_file("/key1")?;
                remove_file("/key3")?;
                sync_dir("/")?;
                observation.borrow_mut().cleaned = read_dir("/")?.next().is_none();
            }
            notify.notify_one();
            std::future::pending::<()>().await;
            Ok(())
        }
    });
    let n = notify.clone();
    simulation.client("before-crash", async move {
        n.notified().await;
        Ok(())
    });
    simulation.run().unwrap();
    simulation.crash("disk");
    phase.set(true);
    simulation.bounce("disk");
    simulation.client("after-crash", async move {
        notify.notified().await;
        Ok(())
    });
    simulation.run().unwrap();
    simulation.crash("disk");
    drop(simulation);
    Rc::try_unwrap(observation)
        .expect("simulation released observation")
        .into_inner()
}
#[test]
fn pinned_turmoil_disk_failure_matches_minimal_driver() {
    let oracle = DiskObservation {
        failed_write: true,
        old_preserved: true,
        failed_value_absent: true,
        recovered: true,
        cleaned: true,
    };
    assert_eq!(minimal_disk(), oracle);
    for seed in [17, 89] {
        assert_eq!(turmoil_disk(seed), oracle, "disk seed={seed}");
    }
}
