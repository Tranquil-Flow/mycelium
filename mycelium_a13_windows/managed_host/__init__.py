# SPDX-License-Identifier: AGPL-3.0-only
"""Native Windows A13 managed host (the ``managed_https`` target side).

The owner (controller) side is the platform-neutral A13 managed stack:
``ManagedControllerStaging`` reads target-signed stage receipts and
``ManagedSessionFactory`` drives the node through the signed control mailbox.
This package is the Windows target that answers them:

* ``staging`` -- :class:`WindowsTargetStaging`, the target-owned stage/cleanup
  adapter (in-process, owner-only directories, ``LockFileEx``), acknowledging
  a *logical* POSIX staging root because the controller's peer contract only
  admits POSIX staging roots; the physical root never leaves the host.

Nothing here is a physical claim: it is verified on POSIX and on the fake
win32 API until a Windows run day exercises it.
"""
