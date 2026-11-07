// SPDX-License-Identifier: AGPL-3.0-only
use super::*;
fn fence() -> CallFence {
    CallFence {
        closed: Arc::new(AtomicBool::new(false)),
        wake: Arc::new(Wake::default()),
        waiter: Some(Arc::new(Waiter::default())),
    }
}
#[test]
fn publication_holds_owner_close_gate_and_refuses_closed_state() {
    let fence = fence();
    let held = fence
        .publish(|| {
            Ok(matches!(
                fence.wake.generation.try_lock(),
                Err(TryLockError::WouldBlock)
            ))
        })
        .unwrap();
    assert!(
        held,
        "owner close must not slip between final local check and publication"
    );
    fence.closed.store(true, Ordering::Release);
    assert_eq!(
        fence
            .publish::<()>(|| panic!("closed publication callback ran"))
            .unwrap_err()
            .0,
        "managed_owner_closed"
    );
}
#[cfg(feature = "iroh-adapter")]
#[test]
fn async_cancellation_is_latched_and_wakes_all_registered_waiters() {
    use std::{
        future::Future,
        task::{Context, Poll, Waker},
    };
    let fence = fence();
    let mut a = Box::pin(fence.cancelled());
    let mut b = Box::pin(fence.cancelled());
    let mut context = Context::from_waker(Waker::noop());
    assert!(matches!(a.as_mut().poll(&mut context), Poll::Pending));
    assert!(matches!(b.as_mut().poll(&mut context), Poll::Pending));
    fence.wake.signal().unwrap();
    assert!(matches!(a.as_mut().poll(&mut context), Poll::Pending));
    fence
        .waiter
        .as_ref()
        .unwrap()
        .interrupt("cancelled_locally")
        .unwrap();
    fence.wake.signal().unwrap();
    for waiter in [&mut a, &mut b] {
        assert_eq!(
            waiter.as_mut().poll(&mut context),
            Poll::Ready(OwnerError("managed_control_command_interrupted"))
        );
    }
    let mut late = Box::pin(fence.cancelled());
    assert_eq!(
        late.as_mut().poll(&mut context),
        Poll::Ready(OwnerError("managed_control_command_interrupted"))
    );
}
