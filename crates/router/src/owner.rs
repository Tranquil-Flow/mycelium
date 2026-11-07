// SPDX-License-Identifier: AGPL-3.0-only
//! Process-local registration and cancellation retirement (f4e366e9 L16).
//! No tombstone survives restart; none is an ACK or cleanup proof.
use crate::stream::ReceiptStream;
use protocol::ContractError;
use std::collections::{BTreeMap, BTreeSet};
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Retirement {
    pub grant_digest: String,
    pub target_endpoint_id: String,
}
struct Registration {
    stream: ReceiptStream,
    retirement: Retirement,
}
#[derive(Default)]
pub struct RegistrationBook {
    active: BTreeMap<String, Registration>,
    seen: BTreeSet<String>,
    cancelled: BTreeMap<String, Retirement>,
    closed: bool,
}
impl RegistrationBook {
    pub fn open(
        &mut self,
        command: String,
        stream: ReceiptStream,
        retirement: Retirement,
    ) -> Result<(), ContractError> {
        if self.closed {
            return Err(ContractError("managed_control_closed"));
        }
        if command.is_empty() || retirement.target_endpoint_id.is_empty() {
            return Err(ContractError("token_progress_arguments_invalid"));
        }
        if self.seen.contains(&command) {
            return Err(ContractError("token_progress_stream_replayed"));
        }
        if self.active.len() >= 8 || self.seen.len() >= 4096 {
            return Err(ContractError("token_progress_capacity"));
        }
        self.seen.insert(command.clone());
        self.active
            .insert(command, Registration { stream, retirement });
        Ok(())
    }
    pub fn stream(&mut self, command: &str) -> Result<&mut ReceiptStream, ContractError> {
        self.active
            .get_mut(command)
            .map(|r| &mut r.stream)
            .ok_or(ContractError("token_progress_disabled"))
    }
    /// Only the trusted owner cancellation predicate may supply true.
    pub fn close_stream(&mut self, command: &str, cancelled: bool) {
        if let Some(mut registration) = self.active.remove(command) {
            if cancelled {
                self.cancelled
                    .insert(command.to_owned(), registration.retirement);
            }
            registration.stream.close("token_progress_closed");
        }
    }
    pub fn retirement(&self, command: &str) -> Option<&Retirement> {
        self.cancelled.get(command)
    }
    pub fn close(&mut self) {
        self.closed = true;
        for registration in self.active.values_mut() {
            registration.stream.close("token_progress_closed");
        }
        self.active.clear();
        self.cancelled.clear();
    }
}
/// Single attempt owner. Ordinary completion and forwarding retirement are
/// distinct from router terminality; this object never releases a reservation.
#[derive(Default)]
pub struct OwnerAttempt {
    started: bool,
    observed_cancelled: bool,
}
impl OwnerAttempt {
    pub fn start(&mut self) -> Result<(), ContractError> {
        if self.started {
            return Err(ContractError("token_path_grant_replay"));
        }
        self.started = true;
        Ok(())
    }
    pub fn fence(
        &mut self,
        cancelled: impl FnOnce() -> Result<bool, ContractError>,
    ) -> Result<(), ContractError> {
        if cancelled()? {
            self.observed_cancelled = true;
            return Err(ContractError("request_cancelled"));
        }
        Ok(())
    }
    pub fn finalize(
        &mut self,
        book: &mut RegistrationBook,
        command: &str,
        cancelled: impl FnOnce() -> Result<bool, ContractError>,
    ) -> Result<(), ContractError> {
        let result = if self.observed_cancelled {
            Ok(true)
        } else {
            cancelled()
        };
        if matches!(result, Ok(true)) {
            self.observed_cancelled = true;
        }
        // The finalizer always closes even when authorization cannot be established.
        book.close_stream(command, self.observed_cancelled);
        result.map(|_| ())
    }
}
