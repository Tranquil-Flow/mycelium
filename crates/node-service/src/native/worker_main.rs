// SPDX-License-Identifier: AGPL-3.0-only
//! The NATIVE WORKER process (RU-020). It only computes: it holds the MLX context, the packed
//! weights and the stage session, and speaks to its member AGENT over the private
//! [`worker_link`](crate::worker_link) socketpair end it inherited as descriptor 1. It holds no
//! signing key, no Iroh endpoint and no Router carrier, so its death is the agent's to survive
//! and to report (v0.1.4 `worker.py:1002-1060`, `r9-oracle/`).
use super::*;
use crate::{
    activation,
    worker_link::{Link, LinkError},
};
use stage_contract::Input;
use std::os::{fd::AsFd, unix::net::UnixStream};

/// Entry of `mycelium-node native-worker`. Returns when the agent closes the stage or is gone.
pub fn native_worker() -> Result<()> {
    let stream = UnixStream::from(std::io::stdout().as_fd().try_clone_to_owned()?);
    let mut link = Link::new(stream, service::duration(B::MemberChosenWall))?;
    let mut stage: Option<NativeStage> = None;
    loop {
        let (head, payload) = match link.receive() {
            Ok(frame) => frame,
            // The agent is gone: there is nobody to compute for. Process exit releases the stage.
            Err(LinkError::Closed) => return Ok(()),
            Err(error) => return Err(error.into()),
        };
        let command = text(&head, "command")?.to_owned();
        let outcome = (|| -> Result<(Value, Vec<u8>, bool)> {
            match command.as_str() {
                "prepare" | stage_contract::native_shard::COMMAND => {
                    if stage.is_some() {
                        return Err("worker_already_prepared".into());
                    }
                    let range = u32::try_from(number(&head, "start")?)?
                        ..u32::try_from(number(&head, "end")?)?;
                    let total = u32::try_from(number(&head, "total")?)?;
                    let memory = number(&head, "memory")?;
                    let tokens = u32::try_from(number(&head, "tokens")?)?;
                    // stage_image is PRIVATE metadata, not an external plan extension.
                    let mut legacy = head.clone();
                    if command == stage_contract::native_shard::COMMAND {
                        legacy
                            .as_object_mut()
                            .ok_or("native_shard_binding_mismatch")?
                            .remove("shard");
                    }
                    let image = crate::native_selection::assigned_image(
                        &legacy,
                        range.clone(),
                        total,
                        memory,
                        tokens,
                    )?;
                    let path = Path::new(text(&head, "checkpoint")?);
                    let id = text(&head, "id")?;
                    let generation = number(&head, "generation")?;
                    stage = Some(match image {
                        Some(image) if command == stage_contract::native_shard::COMMAND => {
                            NativeStage::prepare_shard(
                                path,
                                &image,
                                memory,
                                tokens,
                                id,
                                generation,
                                &head["shard"],
                            )?
                        }
                        None if command == stage_contract::native_shard::COMMAND => {
                            return Err("native_shard_binding_mismatch".into());
                        }
                        Some(image) => NativeStage::prepare_assigned(
                            path, &image, memory, tokens, id, generation,
                        )?,
                        None => NativeStage::prepare(
                            path, range, total, memory, tokens, id, generation,
                        )?,
                    });
                    Ok((json!({"ok":true}), Vec::new(), false))
                }
                stage_contract::native_proof::COMMAND => {
                    use stage_contract::native_proof as proof;
                    if !payload.is_empty()
                        || head.as_object().is_none_or(|object| {
                            object.len() != proof::REQUEST_FIELDS.len()
                                || proof::REQUEST_FIELDS
                                    .iter()
                                    .any(|key| !object.contains_key(*key))
                        })
                        || head["protocol"] != proof::REQUEST_PROTOCOL
                    {
                        return Err("native_proof_request_invalid".into());
                    }
                    let nonce = text(&head, "nonce")?;
                    if !proof::valid_nonce(nonce) {
                        return Err("native_proof_request_invalid".into());
                    }
                    let proof = stage
                        .as_ref()
                        .ok_or("worker_not_prepared")?
                        .resident_proof(nonce)?;
                    Ok((json!({"ok":true,"proof":proof}), Vec::new(), false))
                }
                "step" => {
                    let input = match head.get("tokens") {
                        Some(tokens) => Input::Tokens(serde_json::from_value(tokens.clone())?),
                        None => Input::Hidden(activation::decode(&payload)?),
                    };
                    let output = stage.as_mut().ok_or("worker_not_prepared")?.step(input)?;
                    let out = output.logits.unwrap_or(output.hidden);
                    Ok((json!({"ok":true}), activation::encode(&out)?, false))
                }
                "close" => {
                    let stats = stage.take().ok_or("worker_not_prepared")?.close()?;
                    Ok((json!({"ok":true,"stats":stats}), Vec::new(), true))
                }
                _ => Err("worker_command_unknown".into()),
            }
        })();
        match outcome {
            Ok((reply, body, finished)) => {
                link.send(&reply, &body)?;
                if finished {
                    return Ok(());
                }
            }
            Err(error) => {
                // Say why, then die: a worker that failed a command is not reused.
                let mut refusal = json!({"error":error.to_string()});
                if command == stage_contract::native_shard::COMMAND
                    && let Some(error) = error.downcast_ref::<stage_contract::Error>()
                {
                    refusal["code"] = json!(error.phase);
                }
                if let Err(send) = link.send(&refusal, &[]) {
                    eprintln!("native worker could not report {error}: {send}");
                }
                return Err(error);
            }
        }
    }
}
