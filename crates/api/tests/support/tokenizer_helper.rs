// SPDX-License-Identifier: AGPL-3.0-only
//! REAL owned shell process using the product tokenize JSON-line protocol.
//! Scripted tokens/holds only: NOT MLX/tokenizer-model correctness evidence.
use api::{app_host, joined_owner::codec::NativeCodec};
use budgets::{BudgetId as B, Entries};
use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    process::Command,
    sync::Arc,
    time::{Duration, Instant},
};
fn span(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
pub struct HelperFixture {
    pub codec: Arc<NativeCodec>,
    root: PathBuf,
    release: File,
}
impl HelperFixture {
    pub fn new(mode: &str) -> Self {
        let root = std::env::temp_dir().join(format!(
            "mycelium-ep2h-helper-{}-{}",
            std::process::id(),
            app_host::hex(&app_host::random(8).unwrap())
        ));
        fs::create_dir(&root).unwrap();
        let fifo = root.join("release");
        assert!(
            Command::new("/usr/bin/mkfifo")
                .arg(&fifo)
                .status()
                .unwrap()
                .success()
        );
        let release = OpenOptions::new()
            .read(true)
            .write(true)
            .open(fifo)
            .unwrap();
        fs::write(root.join("tokenizer.json"), b"{}").unwrap();
        // The hold has an independent typed rescue ceiling, not a product deadline.
        // FIFO release is explicit; no sleep or CPU load as synchronization.
        let script = format!(
            r#"#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
set -eu
root="${{2%/*}}"
printf '%s\n' "$$" > "$root/pid"
hold() {{
  printf 'entered\n' > "$root/entered"
  IFS= read -r -t '{wait}' release < "$root/release" || printf 'fixture_rescue_timeout\n' >&2
}}
if test '{mode}' = write; then
  IFS= read -r -n 1 prefix
  hold
fi
while IFS= read -r line; do
  case "$line" in
    *'"encode":'*) printf '{{"ids":[0,1,2,3]}}\n' ;;
    *'"decode":'*)
      if test '{mode}' = read; then hold; fi
      token="${{line#*:}}"; token="${{token%\}}}}"
      printf '{{"text":"<%s>"}}\n' "$token"
      ;;
    *) printf '{{"error":"tokenizer_request_invalid"}}\n' ;;
  esac
done
"#,
            wait = (span(B::WorkerStartup) * 2).as_secs_f64()
        );
        let binary = root.join("helper");
        fs::write(&binary, script).unwrap();
        fs::set_permissions(&binary, fs::Permissions::from_mode(0o700)).unwrap();
        let codec = Arc::new(NativeCodec::start(&binary, &root.join("tokenizer.json")).unwrap());
        Self {
            codec,
            root,
            release,
        }
    }
    pub fn entered(&self) -> bool {
        let end = Instant::now() + span(B::WorkerStartup);
        while !self.root.join("entered").exists() && Instant::now() < end {
            std::thread::yield_now();
        }
        self.root.join("entered").exists()
    }
    pub fn pid(&self) -> u32 {
        fs::read_to_string(self.root.join("pid"))
            .unwrap()
            .trim()
            .parse()
            .unwrap()
    }
    pub fn release(&mut self) {
        self.release.write_all(b"release\n").unwrap();
    }
}
impl Drop for HelperFixture {
    fn drop(&mut self) {
        self.release();
        if let Err(e) = self.codec.close() {
            eprintln!("fixture codec close: {}", e.code);
        }
        if let Err(e) = fs::remove_dir_all(&self.root) {
            eprintln!("fixture cleanup: {e}");
        }
    }
}
