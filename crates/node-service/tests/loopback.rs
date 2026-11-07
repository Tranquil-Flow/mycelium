// SPDX-License-Identifier: AGPL-3.0-only
use std::{
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    thread,
};
#[test]
fn accepted_nonblocking_listener_is_explicitly_rearmed_for_blocking_io() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let mut sender = TcpStream::connect(listener.local_addr().unwrap()).unwrap();
    let deadline = std::time::Instant::now()
        + node_service::service::duration(budgets::BudgetId::WorkerStartup);
    let (mut accepted, _) = loop {
        match listener.accept() {
            Ok(pair) => break pair,
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                assert!(std::time::Instant::now() < deadline);
                thread::sleep(node_service::service::duration(
                    budgets::BudgetId::RegistryPoll,
                ));
            }
            Err(e) => panic!("accept: {e}"),
        }
    };
    // Force the inherited-BSD state on every platform. This is the exact RED
    // predicate for a read_exact-based receiver with no explicit rearming.
    accepted.set_nonblocking(true).unwrap();
    assert_eq!(
        accepted.read_exact(&mut [0]).unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
    node_service::loopback::configure(&accepted).unwrap();
    let task = thread::spawn(move || {
        let mut byte = [0];
        accepted.read_exact(&mut byte).unwrap();
        byte
    });
    sender.write_all(&[7]).unwrap();
    assert_eq!(task.join().unwrap(), [7]);
}
