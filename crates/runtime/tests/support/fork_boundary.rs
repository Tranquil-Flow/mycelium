// SPDX-License-Identifier: AGPL-3.0-only
// S23 root cause: a child process inherits this process's descriptors, and a
// concurrent test's fixture spawn races two windows of another test's UDP
// socket. (1) Creation: macOS has no atomic SOCK_CLOEXEC, so std/mio mark a new
// socket close-on-exec after `socket()`; a spawn inside that gap makes the child
// keep the socket for its whole life. (2) Release: a child forked while the
// socket was open still holds a copy until its exec, so an immediate rebind
// probe after the close sees `AddrInUse` although the endpoint released it.
// Socket creation, fixture spawn and the immediate probe therefore share one
// boundary. Handshakes and every network exchange stay parallel and every
// original close/rebind assertion is unchanged.
include!("fork_lock.rs");
fn spawn_fixture_process(command: &mut Command) -> std::io::Result<Child> {
    let _boundary = fixture_spawn_boundary();
    command.spawn()
}
fn assert_socket_released(socket: SocketAddr, what: &str) {
    let _boundary = fixture_spawn_boundary();
    assert!(UdpSocket::bind(socket).is_ok(), "{what}");
}
fn bind_udp_fixture(address: SocketAddr) -> std::io::Result<UdpSocket> {
    let _boundary = fixture_spawn_boundary();
    UdpSocket::bind(address)
}
fn bind_host<'a>(
    runtime: &'a tokio::runtime::Runtime,
    clock: &impl Clock,
    deadline: MonoInstant,
) -> Result<LoopbackHost<'a>, Error> {
    let _boundary = fixture_spawn_boundary();
    LoopbackHost::bind(runtime, clock, deadline)
}
fn bind_host_with_secret<'a>(
    runtime: &'a tokio::runtime::Runtime,
    clock: &impl Clock,
    deadline: MonoInstant,
    secret: iroh::SecretKey,
) -> Result<LoopbackHost<'a>, Error> {
    let _boundary = fixture_spawn_boundary();
    LoopbackHost::bind_with_secret(runtime, clock, deadline, secret)
}
fn bind_host_protocols<'a>(
    runtime: &'a tokio::runtime::Runtime,
    clock: &impl Clock,
    deadline: MonoInstant,
    secret: iroh::SecretKey,
    channels: &[Channel],
) -> Result<LoopbackHost<'a>, Error> {
    let _boundary = fixture_spawn_boundary();
    LoopbackHost::bind_protocols(runtime, clock, deadline, secret, channels)
}
