// SPDX-License-Identifier: AGPL-3.0-only
//! An own-process watchdog: never signals or discovers somebody else's PID.
use super::*;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
pub struct PressureWatch {
    stop: Arc<AtomicBool>,
    samples: Arc<AtomicU64>,
    task: Option<std::thread::JoinHandle<()>>,
}
impl PressureWatch {
    pub fn start() -> Result<Self> {
        pressure()?;
        let stop = Arc::new(AtomicBool::new(false));
        let samples = Arc::new(AtomicU64::new(1));
        let ending = stop.clone();
        let count = samples.clone();
        let task = std::thread::Builder::new()
            .name("slice-pressure-watch".into())
            .spawn(move || {
                while !ending.load(Ordering::Acquire) {
                    if let Err(e) = pressure() {
                        eprintln!("own-process pressure guard: {e}");
                        std::process::exit(75);
                    }
                    count.fetch_add(1, Ordering::Relaxed);
                    std::thread::sleep(service::duration(B::RegistryPoll));
                }
            })?;
        Ok(Self {
            stop,
            samples,
            task: Some(task),
        })
    }
    pub fn samples(&self) -> u64 {
        self.samples.load(Ordering::Relaxed)
    }
}
impl Drop for PressureWatch {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(task) = self.task.take()
            && task.join().is_err()
        {
            eprintln!("pressure watcher panic");
            std::process::exit(75);
        }
    }
}
