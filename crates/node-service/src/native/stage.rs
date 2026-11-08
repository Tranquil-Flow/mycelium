// SPDX-License-Identifier: AGPL-3.0-only
use super::*;
use backend_mlx::{Context, Session, Worker, admission, payload_id};
use stage_contract::{Activation, Binding, Input, StageBackend, Step};
use std::rc::Rc;
pub struct NativeStage {
    context: Rc<Context>,
    worker: Worker,
    session: Session,
    binding: Binding,
}
impl NativeStage {
    pub fn prepare(
        path: &Path,
        range: std::ops::Range<u32>,
        total: u32,
        memory: u64,
        tokens: u32,
        id: &str,
        generation: u64,
    ) -> Result<Self> {
        let selected = crate::native_selection::select(None, range, total, memory, tokens)?;
        Self::prepare_selected(path, selected, memory, id, generation)
    }
    /// Authenticated assignment metadata selects the existing backend. This is
    /// additive: `prepare` remains the legacy Qwen2-only constructor.
    pub fn prepare_assigned(
        path: &Path,
        image: &crate::native_selection::AssignedImage,
        memory: u64,
        tokens: u32,
        id: &str,
        generation: u64,
    ) -> Result<Self> {
        let selected = crate::native_selection::select(
            Some(image),
            image.layers.clone(),
            image.total_layers,
            memory,
            tokens,
        )?;
        Self::prepare_selected(path, selected, memory, id, generation)
    }
    fn prepare_selected(
        path: &Path,
        selected: crate::native_selection::Selection,
        memory: u64,
        id: &str,
        generation: u64,
    ) -> Result<Self> {
        Self::prepare_image(path, selected, memory, id, generation, None)
    }
    pub fn prepare_shard(
        path: &Path,
        image: &crate::native_selection::AssignedImage,
        memory: u64,
        tokens: u32,
        id: &str,
        generation: u64,
        shard: &Value,
    ) -> Result<Self> {
        let selected = crate::native_selection::select(
            Some(image),
            image.layers.clone(),
            image.total_layers,
            memory,
            tokens,
        )?;
        Self::prepare_image(path, selected, memory, id, generation, Some(shard))
    }
    fn prepare_image(
        path: &Path,
        selected: crate::native_selection::Selection,
        memory: u64,
        id: &str,
        generation: u64,
        shard: Option<&Value>,
    ) -> Result<Self> {
        pressure()?;
        let request = admission::digest_hex(id)?;
        match shard {
            Some(shard) => backend_mlx::stage_shard::verify(path, &selected.descriptor, shard)?,
            None => selected.verify_source(path)?,
        }
        // Source verification can take time. Recheck pressure before native allocation.
        pressure()?;
        let descriptor = selected.descriptor;
        let limits = selected.limits;
        let range = descriptor.layers.clone();
        let context = Context::new(usize::try_from(memory)?, 0)?;
        let mut worker = match shard {
            Some(shard) => Worker::prepare_shard(
                context.clone(),
                descriptor.clone(),
                path,
                limits,
                generation,
                shard,
            )?,
            None => Worker::prepare(
                context.clone(),
                descriptor.clone(),
                path,
                limits,
                generation,
            )?,
        };
        let binding = Binding {
            descriptor,
            request,
            assignment: admission::digest_hex(
                &canonical::digest(&json!([generation, range.start, range.end]))?[7..],
            )?,
            load_generation: generation,
        };
        let session = worker.new_session(binding.clone(), limits)?;
        pressure()?;
        Ok(Self {
            context,
            worker,
            session,
            binding,
        })
    }
    /// Export the material actually resident in this worker, after prepare.
    /// Neither source manifests nor caller-provided digests can fill this proof.
    pub fn resident_proof(&self, nonce: &str) -> Result<Value> {
        use stage_contract::{NumericsProfile, native_proof};
        if !native_proof::valid_nonce(nonce) {
            return Err("native_proof_request_invalid".into());
        }
        pressure()?;
        let weights = self.worker.weight_digests()?;
        let digest = canonical::digest(&weights)?;
        let (host, boot) = crate::hello_bootstrap::identity(nonce)?;
        let descriptor = &self.binding.descriptor;
        let hex = |bytes: &[u8]| {
            bytes
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect::<String>()
        };
        let profile = match descriptor.profile {
            NumericsProfile::MlxPackedInt8 => "mlx_packed_int8",
            NumericsProfile::MlxExplicitF32 => "mlx_explicit_f32",
            NumericsProfile::CpuInt8F32 => "cpu_int8_f32",
        };
        pressure()?;
        Ok(
            json!({"protocol":native_proof::PROOF_PROTOCOL,"nonce":nonce,
            "request_id":hex(&self.binding.request),"load_generation":self.binding.load_generation,
            "start":descriptor.layers.start,"end":descriptor.layers.end,
            "checkpoint_sha256":hex(&descriptor.checkpoint),"tensor_manifest_sha256":hex(&descriptor.tensor_manifest),
            "revision":descriptor.revision,"profile":profile,"worker_pid":std::process::id(),
            "host_id":host,"boot_id":boot,"weights":weights,"weights_digest":digest}),
        )
    }
    pub fn step(&mut self, input: Input) -> Result<stage_contract::Output> {
        pressure()?;
        let step = Step {
            binding: self.binding.clone(),
            state_generation: self.session.state_generation(),
            position: self.session.position(),
            payload: payload_id(&input),
            input,
        };
        let result = if self.session.position() == 0 {
            self.worker.prefill(&mut self.session, step)?
        } else {
            self.worker.decode(&mut self.session, step)?
        };
        pressure()?;
        Ok(result)
    }
    pub fn close(mut self) -> Result<Value> {
        self.worker.cancel(&mut self.session)?;
        self.worker.release_session(self.session)?;
        self.worker.close()?;
        drop(self.worker);
        self.context.synchronize()?;
        self.context.clear_cache()?;
        let stats = self.context.memory_stats()?;
        let reserved = self.context.reserved_resources()?;
        if stats.0 != 0 || reserved.memory.get() != 0 || reserved.storage.get() != 0 {
            return Err("native_residue".into());
        }
        self.context.close()?;
        pressure()?;
        Ok(
            json!({"active_bytes":stats.0,"peak_bytes":stats.1,"reserved_memory_bytes":reserved.memory.get(),"context_closed":true}),
        )
    }
}
pub fn argmax(logits: &Activation) -> Result<u32> {
    if logits.shape[0] != 1
        || logits.shape[1] != 1
        || logits.bytes.len() != logits.shape[2] as usize * 4
    {
        return Err("logits_shape".into());
    }
    let mut best = None;
    for (index, bytes) in logits.bytes.chunks_exact(4).enumerate() {
        let value = f32::from_le_bytes(bytes.try_into()?);
        if !value.is_finite() {
            return Err("logits_nonfinite".into());
        }
        if best.is_none_or(|(_, previous)| value > previous) {
            best = Some((u32::try_from(index)?, value));
        }
    }
    Ok(best.ok_or("empty_logits")?.0)
}
pub fn header(id: &str, generation: u64, position: u32, phase: &str) -> Value {
    json!({"request_id":id,"phase":phase,"token_index":position,"hop_index":0,
        "source_placement_id":"owner","destination_placement_id":"member","idempotency_key":format!("{id}-{generation}-{position}"),
        "path_id":format!("path-{generation}"),"path_attempt":0,"topology_version":generation})
}
