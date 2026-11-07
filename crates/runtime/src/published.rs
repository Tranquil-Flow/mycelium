// SPDX-License-Identifier: AGPL-3.0-only
//! Independent published snapshots; an effect must never own the status lock.
use std::sync::{Mutex, TryLockError};
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot<T> {
    pub generation: u64,
    pub value: T,
}
#[derive(Debug, PartialEq, Eq)]
pub enum PublishError {
    Poisoned,
    Contended,
    StaleGeneration,
}
pub struct Published<T> {
    state: Mutex<Snapshot<T>>,
}
impl<T: Clone> Published<T> {
    pub fn new(generation: u64, value: T) -> Self {
        Self {
            state: Mutex::new(Snapshot { generation, value }),
        }
    }
    pub fn try_snapshot(&self) -> Result<Snapshot<T>, PublishError> {
        self.state
            .try_lock()
            .map(|s| s.clone())
            .map_err(|e| match e {
                TryLockError::WouldBlock => PublishError::Contended,
                TryLockError::Poisoned(_) => PublishError::Poisoned,
            })
    }
    /// Called only by the lifecycle owner after authenticating the successor.
    pub fn replace(&self, generation: u64, value: T) -> Result<(), PublishError> {
        let mut state = self.state.lock().map_err(|_| PublishError::Poisoned)?;
        if generation <= state.generation {
            return Err(PublishError::StaleGeneration);
        }
        *state = Snapshot { generation, value };
        Ok(())
    }
    /// Returns the effect result even when its old-generation publication refuses.
    /// Status publication cannot swallow an effect's own failure or cleanup result.
    pub fn run<R>(
        &self,
        generation: u64,
        working: T,
        effect: impl FnOnce() -> (R, T),
    ) -> Result<(R, Result<(), PublishError>), PublishError> {
        {
            let mut state = self.state.lock().map_err(|_| PublishError::Poisoned)?;
            if state.generation != generation {
                return Err(PublishError::StaleGeneration);
            }
            state.value = working;
        }
        let (result, finished) = effect();
        let publication = match self.state.lock() {
            Err(_) => Err(PublishError::Poisoned),
            Ok(mut state) => {
                if state.generation != generation {
                    Err(PublishError::StaleGeneration)
                } else {
                    state.value = finished;
                    Ok(())
                }
            }
        };
        Ok((result, publication))
    }
}
