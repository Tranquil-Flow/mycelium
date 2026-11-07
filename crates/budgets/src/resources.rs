// SPDX-License-Identifier: AGPL-3.0-only
use crate::*;
use std::collections::BTreeMap;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct Resources {
    pub storage: Bytes,
    pub memory: Bytes,
}
impl Resources {
    pub fn checked_add(self, other: Self) -> Result<Self, Refusal> {
        Ok(Self {
            storage: self.storage.checked_add(other.storage)?,
            memory: self.memory.checked_add(other.memory)?,
        })
    }
    pub fn checked_sub(self, other: Self) -> Result<Self, Refusal> {
        Ok(Self {
            storage: self.storage.checked_sub(other.storage)?,
            memory: self.memory.checked_sub(other.memory)?,
        })
    }
    pub fn require_within(self, cap: Self) -> Result<(), Refusal> {
        if self.storage > cap.storage {
            return Err(Refusal::StorageCap);
        }
        if self.memory > cap.memory {
            return Err(Refusal::MemoryCap);
        }
        Ok(())
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Reservation {
    pub staged: Bytes,
    pub staging_overhead: Bytes,
    pub resident: Bytes,
    pub scratch: Bytes,
    pub kv: Bytes,
}
impl Reservation {
    pub fn total(self) -> Result<Resources, Refusal> {
        Ok(Resources {
            storage: self.staged.checked_add(self.staging_overhead)?,
            memory: self
                .resident
                .checked_add(self.scratch)?
                .checked_add(self.kv)?,
        })
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub struct ReservationId(pub u64);
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Reservations {
    cap: Resources,
    used: Resources,
    held: BTreeMap<ReservationId, Resources>,
}
impl Reservations {
    pub fn new(cap: Resources) -> Self {
        Self {
            cap,
            used: Resources::default(),
            held: BTreeMap::new(),
        }
    }
    pub fn used(&self) -> Resources {
        self.used
    }
    pub fn reserve(&mut self, id: ReservationId, request: Reservation) -> Result<(), Refusal> {
        if self.held.contains_key(&id) {
            return Err(Refusal::DuplicateReservation);
        }
        let amount = request.total()?;
        let next = self.used.checked_add(amount)?;
        next.require_within(self.cap)?;
        self.held.insert(id, amount);
        self.used = next;
        Ok(())
    }
    /// This is a model boundary, not a verifier of signed/native cleanup receipts.
    pub fn release(&mut self, id: ReservationId, proof: CleanupProof) -> Result<(), Refusal> {
        if proof != CleanupProof::Verified {
            return Err(Refusal::CleanupUnproven);
        }
        let amount = *self.held.get(&id).ok_or(Refusal::UnknownReservation)?;
        let next = self.used.checked_sub(amount)?;
        self.held.remove(&id);
        self.used = next;
        Ok(())
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CleanupProof {
    Unproven,
    Verified,
}

/// Account for replay history even after row pruning; include DB pages/indexes/WAL
/// via database_overhead, never pretend logical row bytes equal physical disk use.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RetentionFootprint {
    pub signers: Entries,
    pub max_rows_per_signer: Entries,
    pub retained_row_bytes: Bytes,
    pub retained_history_bytes: Bytes,
    pub active_command_bytes: Bytes,
    pub database_overhead: Bytes,
}
impl RetentionFootprint {
    pub fn required_history(self) -> Result<Bytes, Refusal> {
        BudgetId::ReplayHistoryPerSigner
            .bytes()?
            .checked_mul(self.signers.get())
    }
    pub fn total(self) -> Result<Bytes, Refusal> {
        self.retained_row_bytes
            .checked_add(self.retained_history_bytes)?
            .checked_add(self.active_command_bytes)?
            .checked_add(self.database_overhead)
    }
    pub fn check(self) -> Result<(), Refusal> {
        if self.retained_history_bytes < self.required_history()? {
            return Err(Refusal::ReplayHistoryMissing);
        }
        if self.max_rows_per_signer > BudgetId::ReplayCapacity.count()? {
            return Err(Refusal::ReplayCapacity);
        }
        if self.total()? > BudgetId::MailboxCeiling.bytes()? {
            return Err(Refusal::MailboxCap);
        }
        Ok(())
    }
}
