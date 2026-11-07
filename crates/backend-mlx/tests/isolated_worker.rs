// SPDX-License-Identifier: AGPL-3.0-only
//! Custom Rust subprocess harness. Child modes exist only in this test binary.
use backend_mlx::{
    Context, Worker,
    admission::qwen2_descriptor,
    payload_id,
    process::{OwnedWorker, ProcessError, ProcessLimits, Terminal},
    resources::ResourcePool,
};
use budgets::{BudgetId, Bytes, Entries, Reservation, Resources, Span};
use stage_contract::*;
use std::{
    fs,
    io::{Read, Write},
    os::unix::net::{UnixListener, UnixStream},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    rc::Rc,
    time::{Duration, Instant},
};
fn span(id: BudgetId) -> Span {
    id.span(Entries::ZERO).unwrap()
}
fn duration(s: Span) -> Duration {
    Duration::from_millis(s.millis())
}
fn limits() -> ProcessLimits {
    ProcessLimits {
        deadline: Instant::now() + duration(span(BudgetId::WorkerStartup)),
        close: span(BudgetId::RouterClose),
        reap: span(BudgetId::WorkerCleanupReserve),
        poll: span(BudgetId::RegistryPoll),
    }
}
fn amount() -> Reservation {
    Reservation {
        staged: Bytes::ZERO,
        staging_overhead: Bytes::ZERO,
        resident: Bytes::new(128 * 1024 * 1024),
        scratch: Bytes::new(128 * 1024 * 1024),
        kv: Bytes::new(1024 * 1024),
    }
}
fn pool() -> ResourcePool {
    ResourcePool::new(amount().total().unwrap())
}
fn quiet() {
    let root = PathBuf::from(std::env::var("HOME").unwrap()).join("mycelium-demo-run");
    if root.exists() {
        for entry in fs::read_dir(root).unwrap() {
            assert!(
                !entry
                    .unwrap()
                    .file_name()
                    .to_string_lossy()
                    .ends_with(".busy"),
                "host hold"
            );
        }
    }
    assert_eq!(
        Command::new("pgrep")
            .args(["-f", "[P]ARENT-cert-.*-when-quiet"])
            .status()
            .unwrap()
            .code(),
        Some(1)
    );
}
struct Scratch(PathBuf);
impl Scratch {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!("v02-l8-isolation-{}", std::process::id()));
        fs::create_dir(&root).unwrap();
        Self(root)
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        if let Err(error) = fs::remove_dir_all(&self.0) {
            eprintln!("scratch cleanup failed: {error}");
        }
    }
}
fn command(mode: &str, socket: &Path) -> Command {
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
        .args(["--fixture", mode])
        .arg(socket)
        .stdout(Stdio::null())
        .stderr(Stdio::inherit());
    command
}
fn ready(listener: &UnixListener, deadline: Instant) -> UnixStream {
    listener.set_nonblocking(true).unwrap();
    loop {
        match listener.accept() {
            Ok((mut stream, _)) => {
                // Darwin may inherit the listener's nonblocking flag.
                stream.set_nonblocking(false).unwrap();
                stream
                    .set_read_timeout(Some(deadline.saturating_duration_since(Instant::now())))
                    .unwrap();
                let mut byte = [0];
                stream.read_exact(&mut byte).unwrap();
                assert_eq!(byte, [b'R']);
                return stream;
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                assert!(Instant::now() < deadline, "fixture readiness deadline");
                std::thread::sleep(duration(span(BudgetId::RegistryPoll)));
            }
            Err(error) => panic!("fixture accept: {error}"),
        }
    }
}
fn assert_gone(pid: u32) {
    // Read-only OS confirmation, no rediscovery or raw-PID signalling.
    let status = Command::new("ps")
        .args(["-p", &pid.to_string(), "-o", "pid="])
        .stdout(Stdio::null())
        .status()
        .unwrap();
    assert_eq!(status.code(), Some(1), "owned child remains");
}
fn child(mode: &str, socket: &Path) {
    let native = mode.starts_with("native-");
    let mut state = if native {
        quiet();
        let ctx = Context::new(amount().total().unwrap().memory.get() as usize, 0).unwrap();
        let d = qwen2_descriptor(0..1, false, false).unwrap();
        let cap = Limits {
            weight_bytes: 100_000_000,
            state_bytes: 1_000_000,
            scratch_bytes: 100_000_000,
            max_tokens: 128,
        };
        let model = std::env::var("MYCELIUM_QWEN2_CHECKPOINT").expect("BLOCKED: cached checkpoint");
        let mut worker =
            Worker::prepare(Rc::clone(&ctx), d.clone(), Path::new(&model), cap, 1).unwrap();
        let binding = Binding {
            descriptor: d,
            request: [1; 32],
            assignment: [2; 32],
            load_generation: 1,
        };
        let mut session = worker.new_session(binding.clone(), cap).unwrap();
        run_step(&mut worker, &mut session, &binding);
        Some((ctx, worker, session, binding))
    } else {
        None
    };
    let mut signal = UnixStream::connect(socket).unwrap();
    signal.write_all(b"R").unwrap();
    let mut go = [0];
    signal.read_exact(&mut go).unwrap();
    assert_eq!(go, [b'G']);
    match mode {
        "exit" => (),
        "crash" => std::process::exit(23),
        "hang" | "native-hang" => loop {
            std::thread::park();
        },
        "native-abort" => std::process::abort(),
        "native-busy" => {
            let (_, worker, session, binding) = state.as_mut().unwrap();
            signal.write_all(b"B").unwrap();
            // Bounded work even if the parent/monitor is broken; no burner.
            for _ in 0..128 {
                worker.reset(session).unwrap();
                run_step(worker, session, binding);
            }
            panic!("cancellation failed to stop bounded compute loop");
        }
        "native-fence" => {
            let (ctx, worker, session, _) = state.take().unwrap();
            drop(session);
            drop(worker);
            assert_eq!(ctx.healthy().unwrap_err().code, ErrorCode::Cleanup);
            assert_eq!(ctx.close().unwrap_err().code, ErrorCode::Cleanup);
            assert_eq!(
                Context::new(1024 * 1024, 0).err().unwrap().code,
                ErrorCode::ResourceCap
            );
        }
        "close" | "native-close" => {
            let mut sink = Vec::new();
            std::io::stdin().read_to_end(&mut sink).unwrap();
            assert!(sink.is_empty());
            if let Some((ctx, mut worker, session, _)) = state.take() {
                worker.release_session(session).unwrap();
                worker.close().unwrap();
                drop(worker);
                assert_eq!(ctx.memory_stats().unwrap().0, 0);
                assert_eq!(ctx.reserved_resources().unwrap().memory, Bytes::ZERO);
                ctx.close().unwrap();
            }
        }
        other => panic!("unknown fixture mode {other}"),
    }
}
fn run_step(worker: &mut Worker, session: &mut backend_mlx::Session, binding: &Binding) {
    let input = Input::Hidden(Activation {
        shape: [1, 128, 896],
        dtype: DType::F32,
        order: ByteOrder::Little,
        layout: Layout::BatchSequenceHidden,
        bytes: vec![0; 128 * 896 * 4],
    });
    let step = Step {
        binding: binding.clone(),
        state_generation: session.state_generation(),
        position: session.position(),
        payload: payload_id(&input),
        input,
    };
    let output = worker.prefill(session, step).unwrap();
    assert_eq!(output.committed_position, 128);
}
fn main() {
    let args: Vec<_> = std::env::args().collect();
    if args.get(1).is_some_and(|s| s == "--fixture") {
        child(&args[2], Path::new(&args[3]));
        return;
    }
    quiet();
    let scratch = Scratch::new();
    let mut passed = Vec::new();
    let p = pool();
    let mut expired = limits();
    expired.deadline = Instant::now();
    assert_eq!(
        OwnedWorker::spawn(
            Command::new("/missing-l8-no-spawn"),
            p.clone(),
            amount(),
            expired
        )
        .err()
        .unwrap(),
        ProcessError::Policy("worker-deadline-expired")
    );
    assert_eq!(p.used().unwrap(), Resources::default());
    passed.push("expired-before-spawn");
    let failure = OwnedWorker::spawn(
        Command::new("/missing-l8-launch"),
        p.clone(),
        amount(),
        limits(),
    )
    .err()
    .unwrap();
    assert!(matches!(failure, ProcessError::Io { phase: "spawn", .. }));
    assert_eq!(p.used().unwrap(), Resources::default());
    passed.push("spawn-failure-rollback");
    let mut disk = amount();
    disk.staged = Bytes::new(1);
    assert_eq!(
        OwnedWorker::spawn(
            Command::new("/missing-l8-no-spawn"),
            p.clone(),
            disk,
            limits()
        )
        .err()
        .unwrap(),
        ProcessError::Policy("process-exit-is-not-storage-cleanup")
    );
    passed.push("storage-cleanup-not-forged");
    for (index, (mode, action, expected)) in [
        ("exit", "wait", Terminal::Exited),
        ("crash", "wait", Terminal::Crashed),
        ("close", "close", Terminal::Closed),
        ("hang", "cancel", Terminal::Cancelled),
        ("hang", "deadline", Terminal::Deadline),
        ("hang", "close", Terminal::CloseTimeout),
        ("native-close", "close", Terminal::Closed),
        ("native-abort", "wait", Terminal::Crashed),
        ("native-busy", "cancel", Terminal::Cancelled),
        ("native-hang", "close", Terminal::CloseTimeout),
        ("native-fence", "wait", Terminal::Exited),
        ("native-close", "close", Terminal::Closed),
    ]
    .into_iter()
    .enumerate()
    {
        quiet();
        let socket = scratch.0.join(format!("s{index}"));
        let listener = UnixListener::bind(&socket).unwrap();
        let mut l = limits();
        if action == "deadline" {
            l.deadline = Instant::now() + duration(span(BudgetId::RouterClose));
        }
        let start = Instant::now();
        let mut worker =
            OwnedWorker::spawn(command(mode, &socket), p.clone(), amount(), l).unwrap();
        let pid = worker.pid();
        let mut signal = ready(&listener, l.deadline);
        assert_eq!(p.used().unwrap(), amount().total().unwrap());
        let duplicate = OwnedWorker::spawn(
            Command::new("/missing-l8-overbook"),
            p.clone(),
            amount(),
            limits(),
        );
        assert!(matches!(
            duplicate.err().unwrap(),
            ProcessError::Admission(Error {
                code: ErrorCode::ResourceCap,
                ..
            })
        ));
        signal.write_all(b"G").unwrap();
        if mode == "native-busy" {
            let mut byte = [0];
            signal.read_exact(&mut byte).unwrap();
            assert_eq!(byte, [b'B']);
        }
        match action {
            "cancel" => worker.cancel().unwrap(),
            "close" => {
                worker.request_close().unwrap();
                worker.request_close().unwrap();
            }
            _ => (),
        }
        let report = worker.wait().unwrap().clone();
        assert_eq!(report.pid, pid);
        assert_eq!(report.terminal, expected, "{mode}: {report:?}");
        assert!(report.cleanup_proven, "{report:?}");
        assert!(report.errors.is_empty(), "{report:?}");
        if mode == "crash" {
            assert_eq!(report.status.unwrap().code(), Some(23));
        }
        worker.cancel().unwrap();
        assert_eq!(worker.wait().unwrap().terminal, expected);
        assert_eq!(p.used().unwrap(), Resources::default());
        p.healthy().unwrap();
        assert_gone(pid);
        println!(
            "isolation case={index} mode={mode} action={action} terminal={expected:?} elapsed_ms={:.3} pid={pid} cleanup=true",
            start.elapsed().as_secs_f64() * 1000.
        );
        passed.push(mode);
    }
    // Drop must synchronously cancel/reap its own leaf, not detach it.
    let socket = scratch.0.join("drop");
    let listener = UnixListener::bind(&socket).unwrap();
    let l = limits();
    let worker = OwnedWorker::spawn(command("hang", &socket), p.clone(), amount(), l).unwrap();
    let pid = worker.pid();
    let mut signal = ready(&listener, l.deadline);
    signal.write_all(b"G").unwrap();
    drop(worker);
    assert_gone(pid);
    assert_eq!(p.used().unwrap(), Resources::default());
    passed.push("drop-cancel-reap");
    println!(
        "isolated_worker: {} checks passed; zero ignored; native child fixtures, not L5 transport/L6 signed authority",
        passed.len()
    );
}
