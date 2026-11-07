// SPDX-License-Identifier: AGPL-3.0-only
//! `mycelium_seed.authority.derive_product_pseudonym_salt` (v0.1.4): the one pure function of that module.
//! The salt is SHA-256 of the seed key's own Ed25519 signature over the product-pseudonym statement for one
//! swarm, so only the holder of the seed key can derive it and it is stable for a (key, swarm) pair. The
//! file-mode / ownership loaders (`load_product_pseudonym_salt`, `ensure_product_pseudonym_salt`,
//! `load_bound_seed_signer`) read local state and are not protocol.
use crate::identity::{Identity, IdentityError};
use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::json;
use sha2::{Digest, Sha256};

pub const PRODUCT_PSEUDONYM_SEED_PROTOCOL: &str = "mycelium.product_pseudonym_seed.v1";

/// `derive_product_pseudonym_salt(signer, swarm_id=...)`.
pub fn derive_product_pseudonym_salt(
    signer: &Identity,
    swarm_id: &str,
) -> Result<[u8; 32], IdentityError> {
    let signature = signer.sign(&json!({
        "protocol": PRODUCT_PSEUDONYM_SEED_PROTOCOL,
        "seed_key_digest": signer.verification_key_digest(),
        "swarm_id": swarm_id,
    }))?;
    let raw = signature["signature"]
        .as_str()
        .and_then(|b64| STANDARD.decode(b64).ok())
        .ok_or(IdentityError::Signature)?;
    Ok(Sha256::digest(raw).into())
}
