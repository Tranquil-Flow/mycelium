// SPDX-License-Identifier: AGPL-3.0-only
//! Shared real loopback servers. Python is an external checkout, never copied or reimplemented.
use api::{
    directory::Directory as HttpDirectory,
    http::serve_connection,
    swarm::{
        core::{Config, Directory},
        limits::Limits,
        service::{NativeQr, SwarmService, helper_download},
        store::SqliteStore,
        wire::ed25519_verify,
    },
};
use budgets::{BudgetId, Entries};
use std::{
    io::{BufRead, BufReader},
    net::{TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    thread::JoinHandle,
};

static SERIAL: AtomicU64 = AtomicU64::new(0);
pub struct Scratch(pub PathBuf);
impl Scratch {
    pub fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "swarm-publisher-{}-{}",
            std::process::id(),
            SERIAL.fetch_add(1, Ordering::SeqCst)
        ));
        std::fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}
pub fn set_clock(root: &Path, now: i64) {
    std::fs::write(root.join("clock.next"), now.to_string()).unwrap();
    std::fs::rename(root.join("clock.next"), root.join("clock")).unwrap();
}
fn deadline() -> std::time::Duration {
    std::time::Duration::from_millis(
        BudgetId::PyMyceliumA13MacosSwarmPublishDirectoryclientInitTimeoutL54
            .span(Entries::ZERO)
            .unwrap()
            .millis(),
    )
}
// Only constructor/clock/process wiring: all routes, crypto, storage and refusal behavior
// run in the unchanged oracle modules. No Python file or alternate server is created.
const PYTHON_DRIVER: &str = r#"
import sys
from pathlib import Path
from mycelium_swarm_directory.server import DirectoryServer
from mycelium_swarm_directory.directory import Directory, Limits
from mycelium_swarm_directory.crypto import verify
from mycelium_swarm_directory.store import SqliteStore
root = Path(sys.argv[1])
directory = Directory(SqliteStore(root / 'directory.sqlite3'), verify,
    clock=lambda: int((root / 'clock').read_text()), limits=Limits(), allow_loopback_entries=True)
server = DirectoryServer(directory, port=int(sys.argv[2])).start()
print(server.origin, flush=True)
try:
    sys.stdin.read()
finally:
    server.close()
"#;
enum Running {
    Python(Child),
    Rust {
        stop: Arc<AtomicBool>,
        join: JoinHandle<()>,
    },
}
pub struct Server {
    pub origin: String,
    root: PathBuf,
    python: bool,
    running: Option<Running>,
}
impl Server {
    pub fn start(root: &Path, python: bool) -> Self {
        let mut server = Self {
            origin: String::new(),
            root: root.into(),
            python,
            running: None,
        };
        server.resume(0);
        server
    }
    fn resume(&mut self, port: u16) {
        if self.python {
            let checkout = std::env::var_os("MYCELIUM_SWARM_ORACLE")
                .expect("set MYCELIUM_SWARM_ORACLE to the read-only v0.1.4 checkout");
            let python = std::env::var_os("MYCELIUM_SWARM_ORACLE_PYTHON")
                .unwrap_or_else(|| "/usr/bin/python3".into());
            let child = Command::new(python)
                .current_dir(checkout)
                .args(["-B", "-u", "-c", PYTHON_DRIVER])
                .arg(&self.root)
                .arg(port.to_string())
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::inherit())
                .spawn()
                .unwrap();
            self.running = Some(Running::Python(child));
            let Some(Running::Python(child)) = self.running.as_mut() else {
                unreachable!()
            };
            let stdout = child.stdout.take().unwrap();
            let (tx, rx) = std::sync::mpsc::sync_channel(1);
            let reader = std::thread::spawn(move || {
                let mut line = String::new();
                BufReader::new(stdout).read_line(&mut line).unwrap();
                if tx.send(line).is_err() {
                    eprintln!("oracle_readiness_receiver_closed");
                }
            });
            self.origin = rx
                .recv_timeout(deadline())
                .expect("oracle readiness deadline")
                .trim()
                .into();
            reader.join().unwrap();
            assert!(
                self.origin.starts_with("http://127.0.0.1:"),
                "oracle did not start: {}",
                self.origin
            );
        } else {
            let root = self.root.clone();
            let core = Directory::new(
                SqliteStore::open(&root.join("directory.sqlite3")).unwrap(),
                Box::new(ed25519_verify),
                Box::new(move || {
                    std::fs::read_to_string(root.join("clock"))
                        .unwrap()
                        .parse()
                        .unwrap()
                }),
                Config {
                    limits: Limits::source().unwrap(),
                    allow_loopback_entries: true,
                    allow_private_entries: false,
                },
            )
            .unwrap();
            let listener = TcpListener::bind(("127.0.0.1", port)).unwrap();
            self.origin = format!("http://{}", listener.local_addr().unwrap());
            let app = HttpDirectory::new(SwarmService::new(
                core,
                &self.origin,
                helper_download(),
                NativeQr,
            ));
            let stop = Arc::new(AtomicBool::new(false));
            let stopping = stop.clone();
            let join = std::thread::spawn(move || {
                for socket in listener.incoming() {
                    let socket = socket.unwrap();
                    if stopping.load(Ordering::SeqCst) {
                        break;
                    }
                    serve_connection(socket, deadline(), |r| app.preflight(r), |r| app.handle(r))
                        .unwrap();
                }
            });
            self.running = Some(Running::Rust { stop, join });
        }
    }
    pub fn stop(&mut self) {
        match self.running.take() {
            Some(Running::Python(mut child)) => {
                child.kill().unwrap();
                child.wait().unwrap();
            }
            Some(Running::Rust { stop, join }) => {
                stop.store(true, Ordering::SeqCst);
                TcpStream::connect(self.origin.strip_prefix("http://").unwrap()).unwrap();
                join.join().unwrap();
            }
            None => {}
        }
    }
    pub fn restart(&mut self) {
        self.stop();
        self.resume(self.origin.rsplit(':').next().unwrap().parse().unwrap());
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        self.stop();
    }
}
