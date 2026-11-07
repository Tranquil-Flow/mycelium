// SPDX-License-Identifier: AGPL-3.0-only
//! v0.1.4 refuses a changed member set or identity before automatic re-planning.
//! This gate does not retry, extend workers, or release cleanup reservations.
use crate::{Result, discovery::MemberIdentity};

pub fn check_members(previous: &[MemberIdentity], current: &[MemberIdentity]) -> Result<()> {
    use std::collections::BTreeMap;
    let before: BTreeMap<_, _> = previous.iter().map(|id| (&id.node_id, id)).collect();
    let after: BTreeMap<_, _> = current.iter().map(|id| (&id.node_id, id)).collect();
    if before.len() != previous.len() || after.len() != current.len() || before != after {
        return Err(crate::Error::new("rollover_members_changed"));
    }
    Ok(())
}
