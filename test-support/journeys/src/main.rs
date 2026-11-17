// SPDX-License-Identifier: AGPL-3.0-only
use budgets::BudgetId;
use journey_tests::{
    Result, compare, duration, io,
    process::{Scratch, Target, sandbox_profile, spellings},
    require,
    scenario::run_record,
};
use serde_json::{Value, json};
use std::{
    env, fs,
    io::Write,
    net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::{Command, ExitCode},
};

fn read_json(path: &Path) -> Result<Value> {
    serde_json::from_slice(&fs::read(path).map_err(|e| io("json:read", e))?)
        .map_err(|_| "json:parse".into())
}
fn write_new(path: &Path, value: &Value) -> Result<()> {
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|e| io("receipt:create_new", e))?;
    let bytes = serde_json::to_vec_pretty(value).map_err(|_| "receipt:encode")?;
    file.write_all(&bytes).map_err(|e| io("receipt:write", e))?;
    file.sync_all().map_err(|e| io("receipt:sync", e))
}
fn probe(root: &Path, blocked: &Path) -> Result<()> {
    let listener =
        TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).map_err(|e| io("sandbox:loopback_bind", e))?;
    let socket = TcpStream::connect_timeout(
        &listener.local_addr().map_err(|e| io("probe:address", e))?,
        duration(BudgetId::ModelSelection)?,
    )
    .map_err(|e| io("sandbox:loopback_connect", e))?;
    drop(socket);
    drop(listener);
    // TEST-NET-1 is documentation-only; require kernel EACCES before any packet can leave.
    let result = TcpStream::connect_timeout(
        &SocketAddr::from(([192, 0, 2, 1], 9)),
        duration(BudgetId::ModelSelection)?,
    );
    require(
        matches!(result,Err(ref e) if e.kind()==std::io::ErrorKind::PermissionDenied),
        "sandbox:outbound_not_kernel_denied",
    )?;
    require(
        matches!(fs::read(blocked),Err(ref e) if e.kind()==std::io::ErrorKind::PermissionDenied),
        "sandbox:protected_read_not_denied",
    )?;
    fs::write(root.join("allowed-write"), b"allowed")
        .map_err(|e| io("sandbox:scratch_write", e))?;
    require(
        matches!(fs::write(blocked,b"forbidden"),Err(ref e) if e.kind()==std::io::ErrorKind::PermissionDenied),
        "sandbox:outside_write_not_denied",
    )?;
    Ok(())
}
fn prove_sandbox(home: &Path) -> Result<Value> {
    let mut allowed = Scratch::new(home, None)?;
    let mut other = Scratch::new(home, None)?;
    let blocked = other.root.join("sentinel");
    fs::write(&blocked, b"unchanged").map_err(|e| io("probe:sentinel", e))?;
    let mut policy = sandbox_profile(home, &allowed.root)?;
    policy.push_str(&format!(
        "(deny file-read* {})\n",
        spellings("literal", &blocked)?
    ));
    let status = Command::new("/usr/bin/sandbox-exec")
        .arg("-p")
        .arg(policy)
        .arg(env::current_exe().map_err(|e| io("probe:exe", e))?)
        .arg("--isolation-probe")
        .arg(&allowed.root)
        .arg(&blocked)
        .env_clear()
        .status()
        .map_err(|e| io("probe:spawn", e))?;
    require(status.success(), "BLOCKED:sandbox_probe_failed")?;
    require(
        fs::read(&blocked).map_err(|e| io("probe:sentinel_readback", e))? == b"unchanged",
        "probe:outside_changed",
    )?;
    allowed.remove()?;
    other.remove()?;
    Ok(
        json!({"loopback_connect":"PASS","nonloopback_connect":"PermissionDenied","protected_file_read":"PermissionDenied","outside_file_write":"PermissionDenied","scratch_write":"PASS","sentinel_unchanged":true}),
    )
}
fn run() -> Result<()> {
    let args = env::args().skip(1).collect::<Vec<_>>();
    if args.first().map(String::as_str) == Some("twohost") {
        return journey_tests::twohost::cli(&args[1..]);
    }
    if args.len() == 3 && args[0] == "--isolation-probe" {
        return probe(Path::new(&args[1]), Path::new(&args[2]));
    }
    if args.len() == 1 && args[0] == "--test-isolation" {
        let home = PathBuf::from(env::var_os("HOME").ok_or("HOME:missing")?);
        println!("{}", prove_sandbox(&home)?);
        return Ok(());
    }
    if args.len() == 3 && args[0] == "compare" {
        let old = read_json(Path::new(&args[1]))?;
        let new = read_json(Path::new(&args[2]))?;
        compare(&old["observations"], &new["observations"])?;
        println!(
            "PASS:observations_match:{}",
            old["observations"].as_array().ok_or("compare:array")?.len()
        );
        return Ok(());
    }
    require(
        (args.len() == 6
            || (args.len() == 8 && args[6] == "--scenario" && args[7] == "loopback-boundaries"))
            && args[0] == "--target"
            && args[2] == "--receipt"
            && matches!(args[4].as_str(), "--golden" | "--capture"),
        "usage:journey-tests --target manifest.json --receipt new-receipt.json (--golden existing.json|--capture new-golden.json) [--scenario loopback-boundaries]; compare old.json new.json; --test-isolation",
    )?;
    require(!Path::new(&args[3]).exists(), "receipt:already_exists")?;
    let target: Target =
        serde_json::from_value(read_json(Path::new(&args[1]))?).map_err(|_| "target:manifest")?;
    target.verify()?;
    let home = PathBuf::from(env::var_os("HOME").ok_or("HOME:missing")?);
    let containment = prove_sandbox(&home)?;
    let mut record = if args.len() == 8 {
        journey_tests::boundaries::run_record(&target, &home)
    } else {
        run_record(&target, &home)
    };
    record["containment_probe"] = containment;
    if record["result"] == "PASS" {
        if args[4] == "--capture" {
            let fixture = json!({"schema":"mycelium.journeys.golden.v1","source_sha":target.source_sha,"executable_sha256":target.executable_sha256,"capture_kind":"real_packaged_process_loopback_http","observations":record["observations"]});
            write_new(Path::new(&args[5]), &fixture)?;
        } else {
            let golden = read_json(Path::new(&args[5]))?;
            let expected = target.golden_observations(&golden["observations"])?;
            if let Err(error) = compare(&expected, &record["observations"]) {
                record["result"] = json!("FAIL");
                record["error"] = json!(error);
            }
        }
    }
    write_new(Path::new(&args[3]), &record)?;
    println!(
        "{}:observations={}:launches={}:elapsed_ms={}",
        record["result"],
        record["observations"]
            .as_array()
            .ok_or("record:observations")?
            .len(),
        record["launches"]
            .as_array()
            .ok_or("record:launches")?
            .len(),
        record["elapsed_ms"]
    );
    if record["result"] != "PASS" {
        return Err(record["error"]
            .as_str()
            .unwrap_or("journeys:failed")
            .to_owned());
    }
    Ok(())
}
fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::FAILURE
        }
    }
}
