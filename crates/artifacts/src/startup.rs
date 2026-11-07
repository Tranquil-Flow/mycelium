// SPDX-License-Identifier: AGPL-3.0-only
//! Offline cached startup: resolve every artifact an assignment needs from the local
//! cache or refuse with the specific paths and causes. Read-only: no download, no
//! repair, no eviction, no index write. A cached object is bytes, not route authority.
use crate::{ArtifactKey, Cache, Result, VerifiedArtifact};
use std::collections::BTreeMap;

/// One required file that the cache cannot serve.
#[derive(Debug, PartialEq, Eq)]
pub struct Unavailable {
    pub path: String,
    /// `artifact_missing`, `artifact_foreign`, `artifact_corrupt`, or an I/O/safety code.
    pub code: &'static str,
}

/// Every unavailable file, in path order. Never empty.
#[derive(Debug, PartialEq, Eq)]
pub struct StartupRefusal {
    pub unavailable: Vec<Unavailable>,
}

impl std::fmt::Display for StartupRefusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("offline_startup_refused:")?;
        for (i, u) in self.unavailable.iter().enumerate() {
            write!(
                f,
                "{}{}={}",
                if i == 0 { " " } else { ", " },
                u.path,
                u.code
            )?;
        }
        Ok(())
    }
}
impl std::error::Error for StartupRefusal {}

impl Cache {
    /// All-or-nothing: either every required file is returned as a verified held handle
    /// (path order), or none is and the refusal names each failing path and cause.
    pub fn resolve_offline(
        &self,
        required: &BTreeMap<String, ArtifactKey>,
    ) -> std::result::Result<Vec<(String, VerifiedArtifact)>, StartupRefusal> {
        let mut held = Vec::with_capacity(required.len());
        let mut unavailable = Vec::new();
        if required.is_empty() {
            unavailable.push(Unavailable {
                path: String::new(),
                code: "artifact_assignment_invalid",
            });
        }
        for (path, key) in required {
            let outcome: Result<VerifiedArtifact> = self.verify(key);
            match outcome {
                Ok(v) => held.push((path.clone(), v)),
                Err(e) => unavailable.push(Unavailable {
                    path: path.clone(),
                    code: e.code,
                }),
            }
        }
        if unavailable.is_empty() {
            Ok(held)
        } else {
            Err(StartupRefusal { unavailable })
        }
    }
}
