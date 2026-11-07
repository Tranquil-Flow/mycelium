// SPDX-License-Identifier: AGPL-3.0-only
//! Aggregate reservations shared by one runtime/supervisor, not signed authority.
//! No automatic release on Drop: an unproven cleanup fences further admission.
use budgets::{CleanupProof, Refusal, Reservation, ReservationId, Reservations, Resources};
use stage_contract::{Error, ErrorCode, Result};
use std::sync::{Arc, Mutex, MutexGuard};

struct Ledger {
    reservations: Reservations,
    next: u64,
    fenced: bool,
}
#[derive(Clone)]
pub struct ResourcePool(Arc<Mutex<Ledger>>);
impl ResourcePool {
    pub fn new(cap: Resources) -> Self {
        Self(Arc::new(Mutex::new(Ledger {
            reservations: Reservations::new(cap),
            next: 1,
            fenced: false,
        })))
    }
    fn lock(&self) -> Result<MutexGuard<'_, Ledger>> {
        self.0
            .lock()
            .map_err(|_| Error::new(ErrorCode::Cleanup, "reservation-lock-poisoned"))
    }
    pub fn used(&self) -> Result<Resources> {
        Ok(self.lock()?.reservations.used())
    }
    pub fn healthy(&self) -> Result<()> {
        if self.lock()?.fenced {
            Err(Error::new(
                ErrorCode::Cleanup,
                "reservation-cleanup-unproven",
            ))
        } else {
            Ok(())
        }
    }
    pub(crate) fn reserve(&self, amount: Reservation) -> Result<HeldReservation> {
        let mut ledger = self.lock()?;
        if ledger.fenced {
            return Err(Error::new(
                ErrorCode::Cleanup,
                "reservation-cleanup-unproven",
            ));
        }
        let id = ReservationId(ledger.next);
        let next = ledger
            .next
            .checked_add(1)
            .ok_or_else(|| Error::new(ErrorCode::ResourceCap, "reservation-id-overflow"))?;
        ledger.reservations.reserve(id, amount).map_err(refusal)?;
        ledger.next = next;
        Ok(HeldReservation {
            pool: self.clone(),
            id,
            released: false,
        })
    }
}
fn refusal(error: Refusal) -> Error {
    let phase = match error {
        Refusal::StorageCap => "aggregate-storage-cap",
        Refusal::MemoryCap => "aggregate-memory-cap",
        Refusal::ArithmeticOverflow => "aggregate-overflow",
        Refusal::ArithmeticUnderflow => "aggregate-underflow",
        Refusal::DuplicateReservation => "aggregate-duplicate",
        Refusal::UnknownReservation => "aggregate-unknown",
        Refusal::CleanupUnproven => "aggregate-cleanup-unproven",
        _ => "aggregate-budget-refusal",
    };
    Error::new(ErrorCode::ResourceCap, phase)
}
pub(crate) struct HeldReservation {
    pool: ResourcePool,
    id: ReservationId,
    released: bool,
}
impl HeldReservation {
    /// Call only after native destruction or reaped owned-process exit. Kept
    /// crate-private so a public caller cannot forge this cleanup assertion.
    pub(crate) fn release_verified(&mut self) -> Result<()> {
        if !self.released {
            self.pool
                .lock()?
                .reservations
                .release(self.id, CleanupProof::Verified)
                .map_err(refusal)?;
            self.released = true;
        }
        Ok(())
    }
}
impl Drop for HeldReservation {
    fn drop(&mut self) {
        if !self.released {
            match self.pool.lock() {
                Ok(mut ledger) => ledger.fenced = true,
                Err(error) => eprintln!("{error}; reservation remains held"),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use budgets::Bytes;
    fn request(memory: u64, storage: u64) -> Reservation {
        Reservation {
            staged: Bytes::new(storage),
            staging_overhead: Bytes::ZERO,
            resident: Bytes::new(memory),
            scratch: Bytes::ZERO,
            kv: Bytes::ZERO,
        }
    }
    #[test]
    fn aggregate_boundary_overflow_and_no_partial_mutation() {
        let pool = ResourcePool::new(Resources {
            storage: Bytes::new(10),
            memory: Bytes::new(20),
        });
        let mut a = pool.reserve(request(12, 6)).unwrap();
        let original = pool.used().unwrap();
        assert_eq!(
            pool.reserve(request(9, 0)).err().unwrap().phase,
            "aggregate-memory-cap"
        );
        assert_eq!(
            pool.reserve(request(0, 5)).err().unwrap().phase,
            "aggregate-storage-cap"
        );
        assert_eq!(
            pool.reserve(request(u64::MAX, 0)).err().unwrap().phase,
            "aggregate-overflow"
        );
        assert_eq!(pool.used().unwrap(), original);
        let mut b = pool.reserve(request(8, 4)).unwrap();
        assert_eq!(pool.used().unwrap().memory.get(), 20);
        a.release_verified().unwrap();
        b.release_verified().unwrap();
        assert_eq!(pool.used().unwrap(), Resources::default());
    }
    #[test]
    fn abandoned_reservation_never_frees_capacity() {
        let pool = ResourcePool::new(Resources {
            storage: Bytes::new(10),
            memory: Bytes::new(20),
        });
        drop(pool.reserve(request(12, 6)).unwrap());
        assert_eq!(pool.used().unwrap().memory.get(), 12);
        assert_eq!(
            pool.reserve(request(1, 1)).err().unwrap().code,
            ErrorCode::Cleanup
        );
    }
    #[test]
    fn hybrid_siblings_require_an_explicit_aggregate_grant() {
        use crate::admission::{qwen35_descriptor, requirements};
        use stage_contract::{Limits, NumericsProfile};
        let limits = Limits {
            weight_bytes: 4_000_000_000,
            state_bytes: 32_000_000,
            scratch_bytes: 2_048_000_000,
            max_tokens: 128,
        };
        let a = requirements(
            &qwen35_descriptor(0..9, true, false, NumericsProfile::MlxExplicitF32).unwrap(),
            limits,
        )
        .unwrap();
        let b = requirements(
            &qwen35_descriptor(9..24, false, true, NumericsProfile::MlxExplicitF32).unwrap(),
            limits,
        )
        .unwrap();
        let pool = ResourcePool::new(Resources {
            storage: Bytes::ZERO,
            memory: Bytes::new(6 * 1024 * 1024 * 1024),
        });
        let mut first = pool.reserve(a).unwrap();
        assert_eq!(pool.reserve(b).err().unwrap().phase, "aggregate-memory-cap");
        println!(
            "hybrid aggregate logical reservation bytes={}",
            a.total().unwrap().memory.get() + b.total().unwrap().memory.get()
        );
        first.release_verified().unwrap();
    }
    #[test]
    fn concurrent_reservations_cannot_overbook() {
        let pool = ResourcePool::new(Resources {
            storage: Bytes::ZERO,
            memory: Bytes::new(10),
        });
        let barrier = Arc::new(std::sync::Barrier::new(3));
        let threads: Vec<_> = (0..2)
            .map(|_| {
                let pool = pool.clone();
                let barrier = Arc::clone(&barrier);
                std::thread::spawn(move || {
                    barrier.wait();
                    let reservation = pool.reserve(request(6, 0));
                    barrier.wait();
                    reservation
                })
            })
            .collect();
        barrier.wait();
        barrier.wait();
        let mut winners = 0;
        for thread in threads {
            match thread.join().unwrap() {
                Ok(mut held) => {
                    winners += 1;
                    held.release_verified().unwrap();
                }
                Err(error) => assert_eq!(error.phase, "aggregate-memory-cap"),
            }
        }
        assert_eq!(winners, 1);
        assert_eq!(pool.used().unwrap().memory, Bytes::ZERO);
    }
}
