// SPDX-License-Identifier: AGPL-3.0-only
//! Authenticated loopback channel for hosts without Unix-domain sockets.
//!
//! On Windows the local Router reaches the sidecar over a `127.0.0.1:0` TCP
//! listener instead of a mode-0600 Unix socket. Two independent gates guard
//! it, and neither replaces the mandatory HMAC bootstrap handshake that every
//! session still has to pass:
//!
//! * the bootstrap secret arrives on a stdin **pipe** (exactly 32 bytes, then
//!   EOF), so it never appears on a command line or in the environment;
//! * each accepted connection's client end must be owned by the one process
//!   id named with `--allowed-client-pid`, as reported by the host's TCP
//!   connection table (`GetExtendedTcpTable` on Windows).
//!
//! The listener, the bootstrap parser, the ownership policy and the ready
//! record are compiled on every host so they are covered by the ordinary test
//! suite; the CLI flags that select this channel exist only on Windows and
//! under the `loopback-test` feature.

use std::fmt;
use std::io::{self, Read};
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4};

use iroh::EndpointAddr;
use serde::Serialize;
use tokio::net::{TcpListener, TcpSocket, TcpStream};
use zeroize::{Zeroize, Zeroizing};

use crate::sidecar::SidecarError;

/// Length of the bootstrap secret shared with the local Router.
pub const BOOTSTRAP_SECRET_BYTES: usize = 32;
const LOOPBACK_BACKLOG: u32 = 16;

/// Accepts exactly [`BOOTSTRAP_SECRET_BYTES`] bytes followed by EOF.
///
/// Short input, long input and trailing bytes are all refused; the rejected
/// bytes are wiped before returning.
pub fn parse_bootstrap_secret<R: Read>(
    reader: R,
) -> Result<Zeroizing<[u8; BOOTSTRAP_SECRET_BYTES]>, SidecarError> {
    let mut bytes = Vec::with_capacity(BOOTSTRAP_SECRET_BYTES + 1);
    let read = reader
        .take(BOOTSTRAP_SECRET_BYTES as u64 + 1)
        .read_to_end(&mut bytes);
    if read.is_err() || bytes.len() != BOOTSTRAP_SECRET_BYTES {
        bytes.zeroize();
        return Err(SidecarError::InvalidBootstrapLength);
    }
    let mut secret = Zeroizing::new([0_u8; BOOTSTRAP_SECRET_BYTES]);
    secret.copy_from_slice(&bytes);
    bytes.zeroize();
    Ok(secret)
}

/// Reads the bootstrap secret from this process's stdin, which must be a pipe.
pub fn read_bootstrap_stdin() -> Result<Zeroizing<[u8; BOOTSTRAP_SECRET_BYTES]>, SidecarError> {
    if !stdin_is_pipe()? {
        return Err(SidecarError::BootstrapNotPipe);
    }
    parse_bootstrap_secret(io::stdin().lock())
}

#[cfg(windows)]
fn stdin_is_pipe() -> Result<bool, SidecarError> {
    use std::os::windows::io::AsRawHandle as _;
    use windows_sys::Win32::Storage::FileSystem::{FILE_TYPE_PIPE, GetFileType};

    let handle = io::stdin().as_raw_handle();
    if handle.is_null() {
        return Err(SidecarError::InvalidBootstrapFd);
    }
    // SAFETY: GetFileType only inspects the handle; an invalid handle yields
    // FILE_TYPE_UNKNOWN rather than undefined behaviour.
    Ok(unsafe { GetFileType(handle) } == FILE_TYPE_PIPE)
}

#[cfg(unix)]
fn stdin_is_pipe() -> Result<bool, SidecarError> {
    use std::os::fd::AsFd as _;
    use std::os::unix::fs::FileTypeExt as _;

    let descriptor = io::stdin()
        .as_fd()
        .try_clone_to_owned()
        .map_err(|_| SidecarError::InvalidBootstrapFd)?;
    let metadata = std::fs::File::from(descriptor)
        .metadata()
        .map_err(|_| SidecarError::InvalidBootstrapFd)?;
    Ok(metadata.file_type().is_fifo())
}

/// A bound `127.0.0.1` listener and the address clients must dial.
pub struct LoopbackListener {
    listener: TcpListener,
    local: SocketAddrV4,
}

impl LoopbackListener {
    /// Binds a fresh ephemeral port on the IPv4 loopback interface only.
    ///
    /// On Windows the socket is bound with `SO_EXCLUSIVEADDRUSE` so no other
    /// process can bind the same port and intercept connections.
    pub fn bind() -> Result<Self, SidecarError> {
        let socket = TcpSocket::new_v4().map_err(|_| SidecarError::SocketBind)?;
        #[cfg(windows)]
        exclusive_address_use(&socket)?;
        socket
            .bind(SocketAddr::V4(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0)))
            .map_err(|_| SidecarError::SocketBind)?;
        let listener = socket
            .listen(LOOPBACK_BACKLOG)
            .map_err(|_| SidecarError::SocketBind)?;
        let local = match listener.local_addr() {
            Ok(SocketAddr::V4(local)) if *local.ip() == Ipv4Addr::LOCALHOST => local,
            _ => return Err(SidecarError::SocketSecurity),
        };
        Ok(Self { listener, local })
    }

    pub const fn local_addr(&self) -> SocketAddrV4 {
        self.local
    }

    pub const fn local_port(&self) -> u16 {
        self.local.port()
    }

    /// Accepts one connection and returns it with its client address.
    pub async fn accept(&self) -> io::Result<(TcpStream, SocketAddr)> {
        let (stream, peer) = self.listener.accept().await?;
        // Every record is written as a length prefix followed by its body;
        // Nagle would hold the body back for a delayed ACK on each record.
        stream.set_nodelay(true)?;
        Ok((stream, peer))
    }
}

#[cfg(windows)]
fn exclusive_address_use(socket: &TcpSocket) -> Result<(), SidecarError> {
    use std::os::windows::io::AsRawSocket as _;
    use windows_sys::Win32::Networking::WinSock::{SO_EXCLUSIVEADDRUSE, SOL_SOCKET, setsockopt};

    let enabled: i32 = 1;
    // SAFETY: the socket handle is owned by `socket` for the whole call and the
    // option value points at a live i32 of the length passed.
    let status = unsafe {
        setsockopt(
            socket.as_raw_socket() as usize,
            SOL_SOCKET,
            SO_EXCLUSIVEADDRUSE,
            (&raw const enabled).cast(),
            size_of::<i32>() as i32,
        )
    };
    if status != 0 {
        return Err(SidecarError::SocketSecurity);
    }
    Ok(())
}

/// One IPv4 TCP connection row and the process that owns its local end.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct TcpOwnerRow {
    pub local: SocketAddrV4,
    pub remote: SocketAddrV4,
    pub owning_pid: u32,
}

/// Host view of which process owns each established IPv4 TCP connection.
///
/// Production uses the operating system table; tests substitute a fixed one.
pub trait ConnectionOwnerTable: Send + Sync {
    fn rows(&self) -> io::Result<Vec<TcpOwnerRow>>;
}

/// Why a loopback connection was refused before its handshake.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LoopbackRejection {
    NotLoopback,
    OwnerLookupFailed,
    OwnerUnknown,
    ForeignOwner,
    AmbiguousOwner,
    InvalidPolicy,
}

impl LoopbackRejection {
    pub const fn code(self) -> &'static str {
        match self {
            Self::NotLoopback => "loopback_peer_not_loopback",
            Self::OwnerLookupFailed => "loopback_owner_lookup_failed",
            Self::OwnerUnknown => "loopback_owner_unknown",
            Self::ForeignOwner => "loopback_owner_foreign",
            Self::AmbiguousOwner => "loopback_owner_ambiguous",
            Self::InvalidPolicy => "loopback_policy_invalid",
        }
    }
}

impl fmt::Display for LoopbackRejection {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.code())
    }
}

impl std::error::Error for LoopbackRejection {}

/// Admits a connection only when its client end is owned by `allowed_pid`
/// and by no other process.
///
/// `client` is the accepted peer address and `server` the listener address,
/// so the matching row is the one whose *local* end is `client` and whose
/// remote end is `server` (the sidecar's own row is the mirror image). Any
/// lookup failure refuses the connection.
pub fn authorize_loopback_client(
    table: &dyn ConnectionOwnerTable,
    client: SocketAddr,
    server: SocketAddr,
    allowed_pid: u32,
) -> Result<(), LoopbackRejection> {
    if allowed_pid == 0 {
        return Err(LoopbackRejection::InvalidPolicy);
    }
    let (SocketAddr::V4(client), SocketAddr::V4(server)) = (client, server) else {
        return Err(LoopbackRejection::NotLoopback);
    };
    if !client.ip().is_loopback() || *server.ip() != Ipv4Addr::LOCALHOST {
        return Err(LoopbackRejection::NotLoopback);
    }
    let rows = table
        .rows()
        .map_err(|_| LoopbackRejection::OwnerLookupFailed)?;
    let mut owners = rows
        .iter()
        .filter(|row| row.local == client && row.remote == server)
        .map(|row| row.owning_pid)
        .collect::<Vec<_>>();
    owners.sort_unstable();
    owners.dedup();
    match owners.as_slice() {
        [] => Err(LoopbackRejection::OwnerUnknown),
        [owner] if *owner == allowed_pid => Ok(()),
        [_] => Err(LoopbackRejection::ForeignOwner),
        _ => Err(LoopbackRejection::AmbiguousOwner),
    }
}

/// Size in bytes of one `MIB_TCPROW_OWNER_PID` row.
const OWNER_PID_ROW_BYTES: usize = 6 * 4;
/// Offset of the first row inside `MIB_TCPTABLE_OWNER_PID`.
const OWNER_PID_TABLE_HEADER_BYTES: usize = 4;

/// Decodes the buffer `GetExtendedTcpTable(AF_INET, TCP_TABLE_OWNER_PID_*)`
/// fills: a little-endian row count, then rows of
/// `{state, local_addr, local_port, remote_addr, remote_port, owning_pid}`.
/// Addresses and the low 16 bits of the ports are in network byte order.
pub fn decode_owner_pid_table(buffer: &[u8]) -> io::Result<Vec<TcpOwnerRow>> {
    let invalid = || io::Error::new(io::ErrorKind::InvalidData, "tcp owner table");
    let word = |offset: usize| -> io::Result<[u8; 4]> {
        buffer
            .get(offset..offset + 4)
            .and_then(|bytes| bytes.try_into().ok())
            .ok_or_else(invalid)
    };
    let count = u32::from_le_bytes(word(0)?) as usize;
    let needed = count
        .checked_mul(OWNER_PID_ROW_BYTES)
        .and_then(|rows| rows.checked_add(OWNER_PID_TABLE_HEADER_BYTES))
        .ok_or_else(invalid)?;
    if needed > buffer.len() {
        return Err(invalid());
    }
    let endpoint = |address: [u8; 4], port: [u8; 4]| {
        SocketAddrV4::new(
            Ipv4Addr::from(address),
            u16::from_be_bytes([port[0], port[1]]),
        )
    };
    (0..count)
        .map(|index| {
            let row = OWNER_PID_TABLE_HEADER_BYTES + index * OWNER_PID_ROW_BYTES;
            Ok(TcpOwnerRow {
                local: endpoint(word(row + 4)?, word(row + 8)?),
                remote: endpoint(word(row + 12)?, word(row + 16)?),
                owning_pid: u32::from_le_bytes(word(row + 20)?),
            })
        })
        .collect()
}

/// The host's own connection table.
#[cfg(windows)]
pub struct SystemConnectionTable;

#[cfg(windows)]
impl ConnectionOwnerTable for SystemConnectionTable {
    fn rows(&self) -> io::Result<Vec<TcpOwnerRow>> {
        use windows_sys::Win32::Foundation::{ERROR_INSUFFICIENT_BUFFER, NO_ERROR};
        use windows_sys::Win32::NetworkManagement::IpHelper::{
            GetExtendedTcpTable, TCP_TABLE_OWNER_PID_CONNECTIONS,
        };
        use windows_sys::Win32::Networking::WinSock::AF_INET;

        let mut size = 0_u32;
        for _ in 0..8 {
            // u64 storage keeps the table suitably aligned for the API.
            let mut storage = vec![0_u64; (size as usize).div_ceil(8).max(1)];
            let mut capacity = u32::try_from(storage.len() * 8)
                .map_err(|_| io::Error::other("tcp owner table too large"))?;
            // SAFETY: the buffer is writable for `capacity` bytes and the API
            // writes at most that many, reporting the required size otherwise.
            let status = unsafe {
                GetExtendedTcpTable(
                    storage.as_mut_ptr().cast(),
                    &mut capacity,
                    0,
                    u32::from(AF_INET),
                    TCP_TABLE_OWNER_PID_CONNECTIONS,
                    0,
                )
            };
            if status == NO_ERROR {
                let length = (capacity as usize).min(storage.len() * 8);
                // SAFETY: `storage` owns `storage.len() * 8` initialised bytes.
                let bytes =
                    unsafe { std::slice::from_raw_parts(storage.as_ptr().cast::<u8>(), length) };
                return decode_owner_pid_table(bytes);
            }
            if status != ERROR_INSUFFICIENT_BUFFER {
                return Err(io::Error::from_raw_os_error(status as i32));
            }
            size = capacity.max(size.saturating_add(4096));
        }
        Err(io::Error::other("tcp owner table kept growing"))
    }
}

/// Parses `netstat -anv -p tcp` rows (macOS) into owner rows.
///
/// Only `tcp4` rows whose eleventh column is `name:pid` are kept; anything
/// else is skipped, which can only make an authorization fail, never pass.
#[cfg(feature = "loopback-test")]
pub fn parse_netstat_owner_rows(text: &str) -> Vec<TcpOwnerRow> {
    fn endpoint(token: &str) -> Option<SocketAddrV4> {
        let (address, port) = token.rsplit_once('.')?;
        Some(SocketAddrV4::new(address.parse().ok()?, port.parse().ok()?))
    }
    text.lines()
        .filter_map(|line| {
            let columns = line.split_whitespace().collect::<Vec<_>>();
            if columns.first() != Some(&"tcp4") || columns.len() < 11 {
                return None;
            }
            let (_, pid) = columns[10].rsplit_once(':')?;
            Some(TcpOwnerRow {
                local: endpoint(columns[3])?,
                remote: endpoint(columns[4])?,
                owning_pid: pid.parse().ok()?,
            })
        })
        .collect()
}

/// Test-only host table built from `netstat -anv`, which reports the owning
/// process of each socket on macOS.
#[cfg(all(unix, feature = "loopback-test"))]
pub struct SystemConnectionTable;

#[cfg(all(unix, feature = "loopback-test"))]
impl ConnectionOwnerTable for SystemConnectionTable {
    fn rows(&self) -> io::Result<Vec<TcpOwnerRow>> {
        if !cfg!(target_os = "macos") {
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "loopback owner lookup is only implemented for macOS test hosts",
            ));
        }
        let output = std::process::Command::new("/usr/sbin/netstat")
            .args(["-anv", "-p", "tcp"])
            .stdin(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .output()?;
        if !output.status.success() {
            return Err(io::Error::other("netstat failed"));
        }
        Ok(parse_netstat_owner_rows(&String::from_utf8_lossy(
            &output.stdout,
        )))
    }
}

#[derive(Serialize)]
struct LoopbackReadyEvent<'a> {
    event: &'static str,
    endpoint_id: &'a str,
    endpoint_addr: &'a EndpointAddr,
    alpn: &'a str,
    local_port: u16,
}

/// The ready record for the loopback channel: the POSIX record's four keys in
/// the same order, plus the `local_port` clients must dial.
pub fn loopback_ready_record(
    endpoint_id: &str,
    endpoint_addr: &EndpointAddr,
    alpn: &str,
    local_port: u16,
) -> Result<String, SidecarError> {
    serde_json::to_string(&LoopbackReadyEvent {
        event: "ready",
        endpoint_id,
        endpoint_addr,
        alpn,
        local_port,
    })
    .map_err(|_| SidecarError::ReadyOutput)
}

#[cfg(test)]
mod tests {
    use super::*;
    use iroh::SecretKey;
    use std::io::Cursor;

    struct FakeTable(io::Result<Vec<TcpOwnerRow>>);

    impl ConnectionOwnerTable for FakeTable {
        fn rows(&self) -> io::Result<Vec<TcpOwnerRow>> {
            match &self.0 {
                Ok(rows) => Ok(rows.clone()),
                Err(error) => Err(io::Error::new(error.kind(), "fake failure")),
            }
        }
    }

    const CLIENT: SocketAddrV4 = SocketAddrV4::new(Ipv4Addr::LOCALHOST, 52_000);
    const SERVER: SocketAddrV4 = SocketAddrV4::new(Ipv4Addr::LOCALHOST, 60_000);

    fn row(local: SocketAddrV4, remote: SocketAddrV4, owning_pid: u32) -> TcpOwnerRow {
        TcpOwnerRow {
            local,
            remote,
            owning_pid,
        }
    }

    fn authorize(rows: Vec<TcpOwnerRow>, allowed: u32) -> Result<(), LoopbackRejection> {
        authorize_loopback_client(
            &FakeTable(Ok(rows)),
            SocketAddr::V4(CLIENT),
            SocketAddr::V4(SERVER),
            allowed,
        )
    }

    #[test]
    fn bootstrap_parser_accepts_exactly_thirty_two_bytes_then_eof() {
        let secret = parse_bootstrap_secret(Cursor::new([5_u8; 32])).unwrap();
        assert_eq!(&*secret, &[5_u8; 32]);
    }

    #[test]
    fn bootstrap_parser_refuses_short_long_and_trailing_input() {
        for input in [
            Vec::new(),
            vec![5_u8; 1],
            vec![5_u8; 31],
            vec![5_u8; 33],
            vec![5_u8; 64],
            [[5_u8; 32].as_slice(), b"\n".as_slice()].concat(),
        ] {
            assert_eq!(
                parse_bootstrap_secret(Cursor::new(input.clone())).map(|_| ()),
                Err(SidecarError::InvalidBootstrapLength),
                "{} bootstrap bytes must be refused",
                input.len()
            );
        }
    }

    #[test]
    fn bootstrap_parser_refuses_a_failing_reader() {
        struct Broken;
        impl Read for Broken {
            fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
                Err(io::Error::other("broken pipe"))
            }
        }
        assert_eq!(
            parse_bootstrap_secret(Broken).map(|_| ()),
            Err(SidecarError::InvalidBootstrapLength)
        );
    }

    #[test]
    fn pid_policy_admits_only_the_allowed_owner_of_the_client_end() {
        let rows = vec![row(CLIENT, SERVER, 4242), row(SERVER, CLIENT, 7)];
        assert_eq!(authorize(rows.clone(), 4242), Ok(()));
        // The sidecar owns the mirror row; naming it must not admit anyone.
        assert_eq!(
            authorize(rows.clone(), 7),
            Err(LoopbackRejection::ForeignOwner)
        );
        assert_eq!(authorize(rows, 99), Err(LoopbackRejection::ForeignOwner));
    }

    #[test]
    fn pid_policy_refuses_unknown_ambiguous_and_failed_lookups() {
        assert_eq!(
            authorize(vec![row(SERVER, CLIENT, 4242)], 4242),
            Err(LoopbackRejection::OwnerUnknown)
        );
        let other_client = SocketAddrV4::new(Ipv4Addr::LOCALHOST, 52_001);
        assert_eq!(
            authorize(vec![row(other_client, SERVER, 4242)], 4242),
            Err(LoopbackRejection::OwnerUnknown)
        );
        assert_eq!(
            authorize(
                vec![row(CLIENT, SERVER, 4242), row(CLIENT, SERVER, 4243)],
                4242
            ),
            Err(LoopbackRejection::AmbiguousOwner)
        );
        // Duplicate rows for the same owner are one owner, not ambiguity.
        assert_eq!(
            authorize(
                vec![row(CLIENT, SERVER, 4242), row(CLIENT, SERVER, 4242)],
                4242
            ),
            Ok(())
        );
        assert_eq!(
            authorize_loopback_client(
                &FakeTable(Err(io::Error::other("denied"))),
                SocketAddr::V4(CLIENT),
                SocketAddr::V4(SERVER),
                4242,
            ),
            Err(LoopbackRejection::OwnerLookupFailed)
        );
        assert_eq!(
            authorize(vec![row(CLIENT, SERVER, 0)], 0),
            Err(LoopbackRejection::InvalidPolicy)
        );
    }

    #[test]
    fn pid_policy_refuses_non_loopback_addresses() {
        let remote = SocketAddrV4::new(Ipv4Addr::new(192, 0, 2, 1), 52_000);
        assert_eq!(
            authorize_loopback_client(
                &FakeTable(Ok(vec![row(remote, SERVER, 4242)])),
                SocketAddr::V4(remote),
                SocketAddr::V4(SERVER),
                4242,
            ),
            Err(LoopbackRejection::NotLoopback)
        );
        let v6 = SocketAddr::from((std::net::Ipv6Addr::LOCALHOST, 52_000));
        assert_eq!(
            authorize_loopback_client(&FakeTable(Ok(Vec::new())), v6, SocketAddr::V4(SERVER), 4242),
            Err(LoopbackRejection::NotLoopback)
        );
    }

    fn encode_owner_row(buffer: &mut Vec<u8>, row: TcpOwnerRow) {
        buffer.extend_from_slice(&5_u32.to_le_bytes()); // MIB_TCP_STATE_ESTAB
        for endpoint in [row.local, row.remote] {
            buffer.extend_from_slice(&endpoint.ip().octets());
            let mut port = [0_u8; 4];
            port[..2].copy_from_slice(&endpoint.port().to_be_bytes());
            buffer.extend_from_slice(&port);
        }
        buffer.extend_from_slice(&row.owning_pid.to_le_bytes());
    }

    #[test]
    fn owner_pid_table_decodes_network_order_endpoints() {
        let rows = vec![row(CLIENT, SERVER, 4242), row(SERVER, CLIENT, 7)];
        let mut buffer = (rows.len() as u32).to_le_bytes().to_vec();
        for entry in &rows {
            encode_owner_row(&mut buffer, *entry);
        }
        assert_eq!(decode_owner_pid_table(&buffer).unwrap(), rows);

        // A row count larger than the buffer is refused, not over-read.
        let mut truncated = buffer.clone();
        truncated.truncate(buffer.len() - 1);
        assert!(decode_owner_pid_table(&truncated).is_err());
        assert!(decode_owner_pid_table(&[1, 0]).is_err());
        let mut lying = buffer;
        lying[..4].copy_from_slice(&u32::MAX.to_le_bytes());
        assert!(decode_owner_pid_table(&lying).is_err());
    }

    #[test]
    fn loopback_ready_record_adds_only_local_port_to_the_posix_shape() {
        let endpoint_id = SecretKey::from_bytes(&[3_u8; 32]).public();
        let address = EndpointAddr::new(endpoint_id);
        let record = loopback_ready_record(
            &endpoint_id.to_string(),
            &address,
            "mycelium.iroh.sidecar.v1",
            61_234,
        )
        .unwrap();
        let document: serde_json::Value = serde_json::from_str(&record).unwrap();
        let mut keys = document
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect::<Vec<_>>();
        keys.sort();
        assert_eq!(
            keys,
            [
                "alpn",
                "endpoint_addr",
                "endpoint_id",
                "event",
                "local_port"
            ]
        );
        assert_eq!(document["event"], "ready");
        assert_eq!(document["local_port"], 61_234);
        assert_eq!(document["endpoint_id"], endpoint_id.to_string());
        assert_eq!(document["endpoint_addr"]["id"], endpoint_id.to_string());
        assert!(!record.contains('\n'), "the ready record is one line");

        let posix = crate::sidecar::posix_ready_record(
            &endpoint_id.to_string(),
            &address,
            "mycelium.iroh.sidecar.v1",
        )
        .unwrap();
        let suffix = ",\"local_port\":61234}";
        assert_eq!(
            record,
            format!("{}{suffix}", posix.strip_suffix('}').unwrap()),
            "the loopback record is the POSIX record with local_port appended"
        );
    }

    #[tokio::test]
    async fn loopback_listener_binds_ipv4_loopback_on_an_ephemeral_port() {
        let listener = LoopbackListener::bind().unwrap();
        assert_eq!(*listener.local_addr().ip(), Ipv4Addr::LOCALHOST);
        assert_ne!(listener.local_port(), 0);
        let client = TcpStream::connect(SocketAddr::V4(listener.local_addr()))
            .await
            .unwrap();
        let (_server, peer) = listener.accept().await.unwrap();
        assert_eq!(peer, client.local_addr().unwrap());
    }

    #[cfg(feature = "loopback-test")]
    #[test]
    fn netstat_rows_parse_tcp4_owner_columns_and_skip_everything_else() {
        let text = "\
Active Internet connections (including servers)
Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)          rxbytes      txbytes  rhiwat  shiwat          process:pid    state  options           gencnt    flags   flags1 usecnt rtncnt fltrs
tcp4       0      0  127.0.0.1.52000        127.0.0.1.60000        ESTABLISHED         1252            0  407744  146988       python3.11:4242   00102 00000004 000000000654b0c7 00000080 01000800      2      0 000000
tcp4       0      0  127.0.0.1.60000        127.0.0.1.52000        ESTABLISHED            0          600  408300  146988  mycelium-iroh:7   00102 00000108 000000000654b0c6 00000081 04000900      2      0 000000
tcp6       0      0  ::1.52000              ::1.60000              ESTABLISHED            0          600  408300  146988           other:9   00102 00000108 000000000654b0c6 00000081 04000900      2      0 000000
tcp4       0      0  127.0.0.1.52001        127.0.0.1.60000        ESTABLISHED            0          600  408300  146988       Some App:11   00102 00000108 000000000654b0c6 00000081 04000900      2      0 000000
";
        assert_eq!(
            parse_netstat_owner_rows(text),
            vec![row(CLIENT, SERVER, 4242), row(SERVER, CLIENT, 7)]
        );
    }
}
