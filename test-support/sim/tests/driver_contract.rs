// SPDX-License-Identifier: AGPL-3.0-only
use sim::*;
#[test]
fn wall_jumps_do_not_move_monotonic_time() {
    let mut clock = Clock::default();
    clock.advance_to(500).unwrap();
    clock.jump_wall(-1000).unwrap();
    assert_eq!((clock.mono_ms, clock.wall_ms), (500, -500));
    assert!(clock.advance_to(499).is_err());
    assert_eq!((clock.mono_ms, clock.wall_ms), (500, -500));
}
#[test]
fn task_order_tie_break_and_midflight_snapshot_are_exact() {
    let mut q = Scheduler::default();
    q.at(10, 2, "second").unwrap();
    q.at(10, 1, "first").unwrap();
    q.at(10, 2, "third").unwrap();
    assert_eq!(q.pop().unwrap().unwrap().event, "first");
    assert_eq!(q.pop().unwrap().unwrap().event, "second");
    assert_eq!(q.pop().unwrap().unwrap().event, "third");
    let mut w = World::default();
    w.driver.at(0, 0, Io::Add { node: 1 }).unwrap();
    w.driver
        .at(
            0,
            1,
            Io::Put {
                node: 1,
                key: 7,
                value: 9,
            },
        )
        .unwrap();
    w.step().unwrap();
    w.step().unwrap();
    let bytes = serde_json::to_vec(&w).unwrap();
    let mut restored: World = serde_json::from_slice(&bytes).unwrap();
    restored.drain(100).unwrap();
    w.drain(100).unwrap();
    assert_eq!(restored, w);
    assert_eq!(w.nodes[&1].durable[&7], 9);
}
fn send(id: u64, fault: DeliveryFault) -> Io {
    Io::Send {
        from: 1,
        to: 2,
        message: Message { id, value: id },
        fault,
    }
}
#[test]
fn typed_transport_faults_reach_drop_duplicate_reorder_partition_and_heal() {
    let mut w = World::default();
    for n in [1, 2] {
        w.driver.at(0, 0, Io::Add { node: n }).unwrap();
    }
    w.driver.at(0, 1, send(1, DeliveryFault::Reorder)).unwrap();
    w.driver
        .at(0, 1, send(2, DeliveryFault::Duplicate))
        .unwrap();
    w.driver.at(0, 1, send(3, DeliveryFault::Drop)).unwrap();
    w.drain(100).unwrap();
    assert_eq!(
        w.nodes[&2].inbox.iter().map(|m| m.id).collect::<Vec<_>>(),
        [2, 2, 1]
    );
    let now = w.driver.clock.mono_ms;
    w.driver
        .at(
            now,
            0,
            Io::Partition {
                a: 1,
                b: 2,
                enabled: true,
            },
        )
        .unwrap();
    w.driver.at(now, 1, send(4, DeliveryFault::None)).unwrap();
    w.drain(100).unwrap();
    assert_eq!(w.nodes[&2].inbox.len(), 3);
    assert!(w.log.contains(&"delivery:partition".into()));
    let now = w.driver.clock.mono_ms;
    w.driver
        .at(
            now,
            0,
            Io::Partition {
                a: 2,
                b: 1,
                enabled: false,
            },
        )
        .unwrap();
    w.driver.at(now, 1, send(5, DeliveryFault::None)).unwrap();
    w.drain(100).unwrap();
    assert_eq!(w.nodes[&2].inbox.last().unwrap().id, 5);
    assert!(w.log.contains(&"send:dropped".into()));
}
#[test]
fn disk_failure_and_restart_preserve_only_completed_writes() {
    let mut w = World::default();
    w.driver.at(0, 0, Io::Add { node: 1 }).unwrap();
    w.driver
        .at(
            0,
            1,
            Io::Put {
                node: 1,
                key: 7,
                value: 9,
            },
        )
        .unwrap();
    w.drain(100).unwrap();
    let now = w.driver.clock.mono_ms;
    w.driver
        .at(
            now,
            0,
            Io::DiskFailure {
                node: 1,
                enabled: true,
            },
        )
        .unwrap();
    w.driver
        .at(
            now,
            1,
            Io::Put {
                node: 1,
                key: 7,
                value: 10,
            },
        )
        .unwrap();
    w.drain(100).unwrap();
    assert_eq!(w.nodes[&1].durable[&7], 9);
    assert!(w.log.contains(&"write:disk_failure".into()));
    let now = w.driver.clock.mono_ms;
    w.driver
        .at(
            now,
            0,
            Io::DiskFailure {
                node: 1,
                enabled: false,
            },
        )
        .unwrap();
    w.driver
        .at(
            now,
            1,
            Io::Put {
                node: 1,
                key: 7,
                value: 11,
            },
        )
        .unwrap();
    w.driver.at(now, 2, Io::Crash { node: 1 }).unwrap();
    w.driver.at(now, 3, Io::Restart { node: 1 }).unwrap();
    w.drain(100).unwrap();
    assert_eq!(w.nodes[&1].memory[&7], 9);
    assert!(w.log.contains(&"write:stale_generation".into()));
}
#[test]
fn keyed_prng_is_stable_and_seed_exploration_is_not_vacuous() {
    assert_eq!(random(0, 0, 0, 0), 0);
    let mut reached = std::collections::BTreeSet::new();
    for seed in 0..100 {
        let before = random(seed, 1, 2, 3);
        let other = random(seed, 9, 2, 3);
        assert_eq!(random(seed, 1, 2, 3), before);
        reached.insert(other % 4);
        let mut w = World::default();
        for n in [1, 2] {
            w.driver.at(0, 0, Io::Add { node: n }).unwrap();
        }
        let fault = match other % 4 {
            0 => DeliveryFault::None,
            1 => DeliveryFault::Drop,
            2 => DeliveryFault::Duplicate,
            _ => DeliveryFault::Reorder,
        };
        w.driver.at(0, 1, send(seed, fault)).unwrap();
        let mut replay = w.clone();
        w.drain(100).unwrap();
        replay.drain(100).unwrap();
        assert_eq!(w, replay);
    }
    assert_eq!(reached.len(), 4);
}
