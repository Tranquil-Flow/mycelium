// SPDX-License-Identifier: AGPL-3.0-only
//! Source grammar: managed_offers.py:44-83,219-257,287-296 @ f4e366e9.
use super::*;
pub(super) const READ: &str = "mycelium.managed_stage_read.v1";
pub(super) const ACK: &str = "mycelium.managed_stage_ack.v1";
pub(super) const RESPONSE: &str = "mycelium.managed_stage_bytes.v1";
pub(super) const CLEANUP_READ: &str = "mycelium.managed_stage_cleanup_read.v1";
pub(super) const CLEANUP_ACK: &str = "mycelium.managed_stage_cleanup_ack.v1";
pub(super) const CLEANUP_RESPONSE: &str = "mycelium.managed_stage_cleanup_state.v1";
fn fields(v: &Value, names: &str, code: &'static str) -> Result<()> {
    require(
        v.as_object().is_some_and(|o| {
            o.len() == names.split_whitespace().count()
                && names.split_whitespace().all(|k| o.contains_key(k))
        }),
        code,
    )
}
fn digest(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        s.len() == 71
            && s.starts_with("sha256:")
            && s[7..]
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}
fn path(v: &Value, absolute: bool, policy: StagePolicy, no_nul: bool) -> bool {
    let Some(s) = v.as_str() else { return false };
    if s.is_empty()
        || s.chars().count() as u64 > policy.path_characters.get()
        || s.starts_with('/') != absolute
        || (no_nul && s.contains('\0'))
    {
        return false;
    }
    let prefix = if s.starts_with("//") && !s.starts_with("///") {
        "//"
    } else if absolute {
        "/"
    } else {
        ""
    };
    let parts: Vec<_> = s
        .split('/')
        .filter(|p| !p.is_empty() && *p != ".")
        .collect();
    if parts.contains(&"..") {
        return false;
    }
    let normalized = format!("{prefix}{}", parts.join("/"));
    s == if normalized.is_empty() {
        "."
    } else {
        &normalized
    }
}
pub(super) fn newer_generation(new: &Value, old: &Value) -> bool {
    let n = new.to_string();
    let o = old.to_string();
    (n.len(), n.as_str()) > (o.len(), o.as_str())
}
fn validate_binding(v: &Value) -> Result<()> {
    let mut common = binding(v);
    for(k,value) in json!({"protocol":"mycelium.managed_control.target_poll.v1","request_id":"validation","swarm_id":"validation","sequence":1,"nonce":"validation","issued_at":0,"expires_at":1}).as_object().ok_or(Error::Refused("offer_invalid"))? {common[k]=value.clone();}
    protocol::managed::validate_request_common(&common).map_err(contract)
}
pub(super) fn preposition(v: &Value, p: StagePolicy) -> Result<Vec<u8>> {
    let code = "offer_preposition_invalid";
    fields(v, "protocol files", code)?;
    require(
        v["protocol"] == "mycelium.controller_prepositioned_member_artifacts.v1",
        code,
    )?;
    let files = v["files"].as_array().ok_or(Error::Refused(code))?;
    require(
        !files.is_empty() && files.len() as u64 <= B::PyStagePrepositionFileCapacity.count()?.get(),
        code,
    )?;
    let mut previous: Option<&str> = None;
    for row in files {
        fields(
            row,
            "content_digest destination_path size_bytes source_path",
            code,
        )?;
        require(
            digest(&row["content_digest"])
                && row["size_bytes"]
                    .as_u64()
                    .is_some_and(|n| n <= p.preposition_file_bytes.get())
                && path(&row["source_path"], true, p, true)
                && path(&row["destination_path"], false, p, true),
            code,
        )?;
        let dest = text(row, "destination_path")?;
        require(previous.is_none_or(|old| old < dest), code)?;
        previous = Some(dest);
    }
    let mut result = bytes(v)?;
    result.push(b'\n');
    require(
        result.len() as u64 <= B::PyStagePrepositionDocumentCeiling.bytes()?.get(),
        code,
    )?;
    Ok(result)
}
pub(super) fn validate_offer(v: &Value, p: StagePolicy) -> Result<()> {
    let code = "offer_invalid";
    let base = "target_node_id target_endpoint_id target_key_digest membership_generation member_incarnation run_id deployment_id offer_id archive_digest archive_bytes expires_at";
    let v2 = v.get("protocol").is_some();
    fields(
        v,
        &if v2 {
            format!("{base} protocol preposition preposition_digest")
        } else {
            base.into()
        },
        code,
    )?;
    validate_binding(v)?;
    let archive_bound = B::PyMyceliumA13MacosManagedOffersMaxArchiveL32
        .bytes()?
        .get();
    require(
        segment(text(v, "offer_id")?)
            && digest(&v["archive_digest"])
            && v["archive_bytes"]
                .as_u64()
                .is_some_and(|n| n > 0 && n <= archive_bound)
            && v["expires_at"].as_f64().is_some_and(f64::is_finite),
        code,
    )?;
    if v2 {
        require(v["protocol"] == "mycelium.managed_stage_offer.v2", code)?;
        let pre = preposition(&v["preposition"], p)?;
        require(
            v["preposition_digest"] == protocol::artifact_grant::range_digest(&pre),
            "offer_preposition_invalid",
        )?;
    }
    Ok(())
}
pub(super) fn validate_request(s: &Value, cleanup: bool, ack: bool) -> Result<()> {
    let mut common = s.clone();
    let o = common
        .as_object_mut()
        .ok_or(Error::Refused("managed_control_fields_invalid"))?;
    require(
        o.remove("offer_id").is_some(),
        "managed_control_fields_invalid",
    )?;
    if !cleanup {
        require(
            o.remove("offset").is_some(),
            "managed_control_fields_invalid",
        )?;
    }
    if ack {
        require(o.remove("ack").is_some(), "managed_control_fields_invalid")?;
    }
    o.insert(
        "protocol".into(),
        "mycelium.managed_control.target_poll.v1".into(),
    );
    protocol::managed::validate_request_common(&common).map_err(contract)?;
    require(
        s["run_id"] == "stage-discovery"
            && s["deployment_id"] == "stage-discovery"
            && (s["offer_id"].is_null() || s["offer_id"].as_str().is_some_and(segment)),
        "offer_request_invalid",
    )?;
    if !cleanup {
        let offset = s["offset"]
            .as_u64()
            .ok_or(Error::Refused("offer_offset_invalid"))?;
        require(
            offset
                <= B::PyMyceliumA13MacosManagedOffersMaxArchiveL32
                    .bytes()?
                    .get()
                && (!(ack || s["offer_id"].is_null()) || offset == 0),
            "offer_offset_invalid",
        )?;
    }
    Ok(())
}
pub(super) fn validate_ack(offer: &Value, ack: &Value, p: StagePolicy) -> Result<()> {
    let root = &ack["staging_root"];
    require(path(root, true, p, false), "offer_ack_invalid")?;
    let mut expected = json!({"protocol":"mycelium.controller_remote_stage_ack.v1","node_id":offer["target_node_id"],"staging_root":root,"archive_digest":offer["archive_digest"],"archive_size_bytes":offer["archive_bytes"]});
    if let Some(pin) = offer.get("preposition_digest") {
        expected["preposition_digest"] = pin.clone();
        expected["preposition_size_bytes"] = json!(preposition(&offer["preposition"], p)?.len());
    }
    require(*ack == expected, "offer_ack_invalid")
}
pub(super) fn validate_cleanup(offer: &Value, ack: &Value, p: StagePolicy) -> Result<()> {
    fields(
        ack,
        "protocol node_id staging_root removed",
        "offer_cleanup_ack_invalid",
    )?;
    require(
        ack["protocol"] == "mycelium.controller_remote_cleanup_ack.v1"
            && ack["node_id"] == offer["target_node_id"]
            && ack["removed"].is_boolean()
            && path(&ack["staging_root"], true, p, false),
        "offer_cleanup_ack_invalid",
    )
}
/// RFC 4648 standard alphabet/padding; encoding only, no permissive decoder.
/// Kept dependency-free at this boundary; the alphabet/group widths are format
/// semantics, not resource policy. Vectors are independent RFC 4648 examples.
pub(super) fn base64(data: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for c in data.chunks(3) {
        let a = c[0];
        let b = c.get(1).copied().unwrap_or(0);
        let d = c.get(2).copied().unwrap_or(0);
        out.push(ALPHABET[(a >> 2) as usize] as char);
        out.push(ALPHABET[(((a & 3) << 4) | (b >> 4)) as usize] as char);
        out.push(if c.len() > 1 {
            ALPHABET[(((b & 15) << 2) | (d >> 6)) as usize] as char
        } else {
            '='
        });
        out.push(if c.len() > 2 {
            ALPHABET[(d & 63) as usize] as char
        } else {
            '='
        });
    }
    out
}
#[cfg(test)]
mod tests {
    #[test]
    fn rfc4648_vectors() {
        for (raw, encoded) in [
            ("", ""),
            ("f", "Zg=="),
            ("fo", "Zm8="),
            ("foo", "Zm9v"),
            ("foob", "Zm9vYg=="),
            ("fooba", "Zm9vYmE="),
            ("foobar", "Zm9vYmFy"),
        ] {
            assert_eq!(super::base64(raw.as_bytes()), encoded);
        }
        assert_eq!(super::base64(&[0xfb, 0xff, 0xff]), "+///");
    }
}
