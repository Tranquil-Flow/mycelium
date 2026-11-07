// SPDX-License-Identifier: AGPL-3.0-only
async fn run() -> Result<(), E> {
    let worker = thread::spawn(work);
    worker.join().map_err(Error::Join)?;
    let task = tokio::spawn(work_async());
    task.await??;
    Ok(())
}
