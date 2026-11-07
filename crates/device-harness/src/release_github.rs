// SPDX-License-Identifier: AGPL-3.0-only
//! Authenticated release readback adapter. No network implementation or publish
//! authority is supplied; the state machine still owns intent, audit and approval.
use crate::{
    Fault, FaultKind, Result,
    release::{Identity, Receipt, ReleaseIo, Stage, State},
};
use budgets::Span;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Asset {
    pub name: String,
    pub digest: String,
    pub state: String,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Release {
    pub id: u64,
    pub tag_name: String,
    pub draft: bool,
    pub prerelease: bool,
    pub assets: Vec<Asset>,
}
pub struct Page {
    pub releases: Vec<Release>,
    /// None only after the authenticated listing is exhausted. An adapter must
    /// not drop pagination or treat auth/HTTP/decoding errors as an empty page.
    pub next_page: Option<u32>,
}
pub trait Github {
    fn now_ms(&self) -> u64;
    /// GET /repos/{repo}/releases, authenticated, including drafts.
    fn list_authenticated(&mut self, repo: &str, page: u32, bound: Span) -> Result<Page>;
    /// GET /repos/{repo}/releases/tags/{tag}; drafts may return 404 here.
    fn by_tag(&mut self, repo: &str, tag: &str, bound: Span) -> Result<Release>;
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Visibility {
    Draft,
    Published,
}
#[derive(Clone, Debug)]
pub struct Expected {
    pub repository: String,
    pub tag: String,
    pub asset: String,
    pub sidecar_sha256: String,
    pub identity: Identity,
    pub visibility: Visibility,
}
#[derive(Clone, Copy)]
pub struct Limits {
    pub total: Span,
    pub max_pages: u32,
}
fn refuse(code: &'static str) -> Fault {
    Fault::new(code, FaultKind::Observation)
}
fn validate_expected(e: &Expected, limits: Limits) -> Result<()> {
    let components = e.repository.split('/').collect::<Vec<_>>();
    let token = |s: &str| {
        !s.is_empty()
            && s != "."
            && s != ".."
            && s.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
    };
    if components.len() != 2
        || !components.iter().all(|s| token(s))
        || !token(&e.tag)
        || !token(&e.asset)
        || !e.asset.ends_with(".zip")
        || !crate::package::hex(&e.sidecar_sha256, 64)
        || !crate::package::hex(&e.identity.archive, 64)
        || limits.total == Span::ZERO
        || limits.max_pages == 0
    {
        return Err(Fault::new(
            "github_readback_input_invalid",
            FaultKind::Input,
        ));
    }
    Ok(())
}
fn validate_release(r: &Release, e: &Expected) -> Result<()> {
    if r.id == 0
        || r.tag_name != e.tag
        || r.draft != (e.visibility == Visibility::Draft)
        || r.prerelease
        || r.assets.len() != 2
    {
        return Err(refuse("github_release_mismatch"));
    }
    for (name, digest) in [
        (e.asset.clone(), format!("sha256:{}", e.identity.archive)),
        (
            format!("{}.sha256", e.asset),
            format!("sha256:{}", e.sidecar_sha256),
        ),
    ] {
        let hits = r
            .assets
            .iter()
            .filter(|a| a.name == name)
            .collect::<Vec<_>>();
        if hits.len() != 1 || hits[0].digest != digest || hits[0].state != "uploaded" {
            return Err(refuse("github_asset_mismatch"));
        }
    }
    Ok(())
}
pub fn readback<G: Github>(github: &mut G, expected: &Expected, limits: Limits) -> Result<Release> {
    validate_expected(expected, limits)?;
    let mut last = github.now_ms();
    let end = last
        .checked_add(limits.total.millis())
        .ok_or(refuse("github_readback_deadline"))?;
    let mut remaining = |now: u64| -> Result<Span> {
        if now < last || now >= end {
            return Err(refuse("github_readback_deadline"));
        }
        last = now;
        Ok(Span::from_millis(end - now))
    };
    let r = match expected.visibility {
        Visibility::Published => {
            let bound = remaining(github.now_ms())?;
            let r = github.by_tag(&expected.repository, &expected.tag, bound)?;
            remaining(github.now_ms())?;
            r
        }
        Visibility::Draft => {
            let mut page = 1;
            let mut matched = None;
            let mut complete = false;
            for _ in 0..limits.max_pages {
                let bound = remaining(github.now_ms())?;
                let result = github.list_authenticated(&expected.repository, page, bound)?;
                remaining(github.now_ms())?;
                // Match tag before checking draft/assets. Filtering drafts first
                // would conceal an ambiguous published+draft duplicate.
                for r in result.releases {
                    if r.tag_name == expected.tag {
                        if matched.is_some() {
                            return Err(refuse("github_release_match_not_unique"));
                        }
                        matched = Some(r);
                    }
                }
                match result.next_page {
                    None => {
                        complete = true;
                        break;
                    }
                    Some(next) if page.checked_add(1) == Some(next) => page = next,
                    Some(_) => return Err(refuse("github_pagination_invalid")),
                }
            }
            if !complete {
                return Err(refuse("github_listing_incomplete"));
            }
            matched.ok_or(refuse("github_release_match_not_unique"))?
        }
    };
    validate_release(&r, expected)?;
    Ok(r)
}

/// Wrap an effect/store adapter rather than bypassing `release::advance`.
/// A local Publish receipt cannot complete that stage until remote readback
/// matches. Missing readback preserves issued intent and never repeats effects.
pub struct ReadbackIo<I, G> {
    pub inner: I,
    pub github: G,
    pub expected: Expected,
    pub limits: Limits,
}
impl<I: ReleaseIo, G: Github> ReleaseIo for ReadbackIo<I, G> {
    fn load(&mut self) -> Result<Option<State>> {
        self.inner.load()
    }
    fn save(&mut self, expected: Option<u64>, next: &State) -> Result<()> {
        self.inner.save(expected, next)
    }
    fn read_receipt(&mut self, stage: Stage) -> Result<Option<Receipt>> {
        let r = self.inner.read_receipt(stage)?;
        if stage == Stage::Publish
            && let Some(receipt) = &r
        {
            if receipt.identity != self.expected.identity {
                return Err(refuse("github_candidate_mismatch"));
            }
            readback(&mut self.github, &self.expected, self.limits)?;
        }
        Ok(r)
    }
    fn execute(&mut self, stage: Stage, id: &Identity) -> Result<()> {
        if stage == Stage::Publish {
            validate_expected(&self.expected, self.limits)?;
            if *id != self.expected.identity {
                return Err(refuse("github_candidate_mismatch"));
            }
        }
        self.inner.execute(stage, id)
    }
    fn read_audit(&mut self) -> Result<Option<crate::audit::Inventory>> {
        self.inner.read_audit()
    }
    fn authorized(&mut self, stage: Stage, id: &Identity) -> Result<bool> {
        self.inner.authorized(stage, id)
    }
}
