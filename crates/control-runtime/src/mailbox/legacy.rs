// SPDX-License-Identifier: AGPL-3.0-only
//! Compatibility with the actual v0.1.4 managed_commands column layout.
use super::*;
pub(super) fn present(db: &Connection) -> Result<bool> {
    let mut q = db.prepare("PRAGMA table_info(managed_commands)")?;
    let columns = q
        .query_map([], |r| r.get::<_, String>(1))?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    Ok(columns.iter().any(|c| c == "envelope_json"))
}
pub(super) fn enqueue(tx: &Transaction<'_>, c: &Context<'_>, s: &Value, env: &Value) -> Result<()> {
    let encoded = bytes(env)?;
    let json = std::str::from_utf8(&encoded)
        .map_err(|_| Error::Refused("managed_control_json_invalid"))?;
    tx.execute("INSERT INTO managed_commands(command_id,envelope_digest,envelope_json,target_node_id,target_endpoint_id,target_key_digest,membership_generation,member_incarnation,run_id,deployment_id,owner_sequence,owner_nonce,command_expires_at,state,updated_at) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,'pending',?14)",params![text(&s["native_command"],"command_id")?,digest(env)?,json,text(s,"target_node_id")?,text(s,"target_endpoint_id")?,text(s,"target_key_digest")?,s["membership_generation"].as_i64().ok_or(Error::Refused("managed_control_generation_unrepresentable"))?,text(s,"member_incarnation")?,text(s,"run_id")?,text(s,"deployment_id")?,s["sequence"].as_i64(),text(s,"nonce")?,s["expires_at"].as_f64(),c.now])?;
    Ok(())
}
pub(super) fn restore(tx: &Transaction<'_>, owner: &Identity) -> Result<()> {
    if !present(tx)? {
        return Ok(());
    }
    let mut q=tx.prepare("SELECT command_id,envelope_json,envelope_digest,claim_token,terminal_receipt_json,native_response_json,target_node_id,target_endpoint_id,target_key_digest,membership_generation,member_incarnation,run_id,deployment_id,owner_sequence,command_expires_at FROM managed_commands WHERE command_id NOT IN (SELECT command_id FROM control_dispatch)")?;
    let mut rows = q.query([])?;
    while let Some(row) = rows.next()? {
        let id: String = row.get(0)?;
        let raw: String = row.get(1)?;
        let recorded_digest: String = row.get(2)?;
        let env = protocol::strict::object(raw.as_bytes(), protocol::managed::MAX_FRAME_BYTES)
            .map_err(|_| Error::Refused("managed_control_state_corrupt"))?;
        require(
            digest(&env)? == recorded_digest
                && env["verification_key"] == owner.public_key_record()
                && env["signature"]["signer_endpoint_id"] == owner.endpoint_id(),
            "managed_control_state_corrupt",
        )?;
        protocol::identity::verify(
            &env["statement"],
            &env["signature"],
            &owner.public_key_record(),
        )
        .map_err(|_| Error::Refused("managed_control_state_corrupt"))?;
        let s = &env["statement"];
        protocol::managed::validate_request_common(s).map_err(contract)?;
        protocol::native::validate_command(&s["native_command"]).map_err(contract)?;
        require(
            s["native_command"]["command_id"] == id,
            "managed_control_state_corrupt",
        )?;
        let bound = json!({"target_node_id":row.get::<_,String>(6)?,"target_endpoint_id":row.get::<_,String>(7)?,"target_key_digest":row.get::<_,String>(8)?,"membership_generation":row.get::<_,i64>(9)?,"member_incarnation":row.get::<_,String>(10)?,"run_id":row.get::<_,String>(11)?,"deployment_id":row.get::<_,String>(12)?});
        require(bound == binding(s), "managed_control_state_corrupt")?;
        let sequence: i64 = row.get(13)?;
        let expires: f64 = row.get(14)?;
        require(
            s["sequence"].as_i64() == Some(sequence) && s["expires_at"].as_f64() == Some(expires),
            "managed_control_state_corrupt",
        )?;
        let native: Option<String> = row.get(5)?;
        let terminal: Option<String> = row.get(4)?;
        if let Some(receipt) = terminal.as_ref() {
            tx.execute("INSERT INTO control_dispatch_receipts VALUES (?1,?2) ON CONFLICT(command_id) DO NOTHING",params![id,receipt.as_bytes()])?;
        }
        tx.execute("INSERT INTO control_dispatch(command_id,binding,envelope,sequence,expires,claim_token,terminal,native) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)",params![id,bytes(&bound)?,bytes(&env)?,sequence,expires,row.get::<_,Option<String>>(3)?,terminal.is_some(),native.map(String::into_bytes)])?;
    }
    Ok(())
}
