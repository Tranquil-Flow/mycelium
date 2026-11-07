// SPDX-License-Identifier: AGPL-3.0-only
fn configure(bytes: u64, timeout: u64, deadline: f64, cache_ttl: u64) {}
impl Worker { fn wait(&self, timeout: u64) {} }
fn run(worker: Worker) {
    configure(2048, 30, remaining, budget); // expect: TM002
    configure(2048, budget, -1.0, budget); // expect: TM002
    configure(2048, budget, remaining, (30_u64)); // expect: TM002
    worker.wait(30); // expect: TM002
    Worker::wait(&worker, 30); // expect: TM002
    foreign::set_read_timeout(30); // expect: TM002
}
