// SPDX-License-Identifier: AGPL-3.0-only
//! One audited unsafe boundary. All pointers are owned mlx-c handles or bounded
//! borrows valid for the duration of a synchronous call. Rc makes arrays/streams
//! !Send + !Sync. No native pointer is exported through stage-contract.
#![allow(unsafe_code)]
mod ffi {
    #![allow(
        non_camel_case_types,
        non_snake_case,
        non_upper_case_globals,
        clippy::upper_case_acronyms,
        dead_code
    )]
    include!(concat!(env!("OUT_DIR"), "/ffi.rs"));
}
use ffi::*;
use model::Tensor;
use stage_contract::{Error, ErrorCode, Result};
use std::{
    cell::Cell,
    collections::BTreeMap,
    ffi::CString,
    rc::Rc,
    sync::{
        Once,
        atomic::{AtomicBool, Ordering},
    },
};
thread_local! { static CALLBACK: Cell<bool> = const { Cell::new(false) }; }
static HANDLER: Once = Once::new();
static OWNED: AtomicBool = AtomicBool::new(false);
// Do not format, allocate, panic, or unwind in a C callback. Native messages may
// contain artifact paths, so only report that a native diagnostic was received.
unsafe extern "C" fn on_error(_message: *const std::ffi::c_char, _data: *mut std::ffi::c_void) {
    if CALLBACK.try_with(|v| v.set(true)).is_err() {
        std::process::abort();
    }
}
fn checked(phase: &'static str, f: impl FnOnce() -> i32) -> Result<()> {
    CALLBACK.with(|v| v.set(false));
    let rc = f();
    if rc == 0 {
        Ok(())
    } else {
        Err(Error {
            code: ErrorCode::Native,
            phase,
            native_status: Some(rc),
            native_callback: CALLBACK.with(Cell::get),
        })
    }
}
/// An explicitly thread-affine native runtime, never a transport handle.
/// ```compile_fail
/// fn requires_send<T: Send>() {}
/// requires_send::<backend_mlx::Context>();
/// ```
/// ```compile_fail
/// fn requires_sync<T: Sync>() {}
/// requires_sync::<backend_mlx::Context>();
/// ```
pub struct Context {
    pub(crate) resources: crate::resources::ResourcePool,
    stream: mlx_stream,
    cleanup_failed: Cell<bool>,
    stream_released: bool,
}
impl Context {
    pub fn new(memory_bytes: usize, cache_bytes: usize) -> Result<Rc<Self>> {
        if memory_bytes == 0 || cache_bytes > memory_bytes {
            return Err(Error::new(ErrorCode::ResourceCap, "native-limit"));
        }
        if OWNED.swap(true, Ordering::AcqRel) {
            return Err(Error::new(
                ErrorCode::ResourceCap,
                "native-worker-exclusive",
            ));
        }
        HANDLER.call_once(|| unsafe {
            mlx_set_error_handler(Some(on_error), std::ptr::null_mut(), None);
        });
        let ctx = Rc::new(Self {
            resources: crate::resources::ResourcePool::new(budgets::Resources {
                storage: budgets::Bytes::ZERO,
                memory: budgets::Bytes::new(memory_bytes as u64),
            }),
            stream: unsafe { mlx_default_gpu_stream_new() },
            cleanup_failed: Cell::new(false),
            stream_released: false,
        });
        if ctx.stream.ctx.is_null() {
            return Err(Error::new(ErrorCode::Native, "new-gpu-stream"));
        }
        let mut previous = 0;
        checked("set-memory-limit", || unsafe {
            mlx_set_memory_limit(&mut previous, memory_bytes)
        })?;
        checked("set-cache-limit", || unsafe {
            mlx_set_cache_limit(&mut previous, cache_bytes)
        })?;
        Ok(ctx)
    }
    /// Consume the last owner after stages/arrays have been released. A cleanup
    /// failure permanently fences this process; L6 must retire the worker.
    pub fn close(self: Rc<Self>) -> Result<()> {
        let mut ctx = Rc::try_unwrap(self)
            .map_err(|_| Error::new(ErrorCode::Cleanup, "native-live-handles"))?;
        if let Err(error) = ctx.synchronize().and_then(|()| ctx.clear_cache()) {
            ctx.cleanup_failed.set(true);
            return Err(error);
        }
        ctx.cleanup("stream-release", || unsafe { mlx_stream_free(ctx.stream) });
        ctx.stream_released = true;
        ctx.healthy()
    }
    pub fn synchronize(&self) -> Result<()> {
        self.healthy()?;
        checked("synchronize", || unsafe { mlx_synchronize(self.stream) })
    }
    /// Logical aggregate reservation, not physical MLX/RSS accounting.
    pub fn reserved_resources(&self) -> Result<budgets::Resources> {
        self.resources.used()
    }
    /// MLX allocator accounting, distinct from process RSS/footprint.
    pub fn memory_stats(&self) -> Result<(usize, usize)> {
        self.healthy()?;
        let mut active = 0;
        let mut peak = 0;
        checked("active-memory", || unsafe {
            mlx_get_active_memory(&mut active)
        })?;
        checked("peak-memory", || unsafe { mlx_get_peak_memory(&mut peak) })?;
        Ok((active, peak))
    }
    pub fn clear_cache(&self) -> Result<()> {
        checked("clear-cache", || unsafe { mlx_clear_cache() })
    }
    pub fn healthy(&self) -> Result<()> {
        self.resources.healthy()?;
        if self.cleanup_failed.get() {
            Err(Error::new(ErrorCode::Cleanup, "native-handle-release"))
        } else {
            Ok(())
        }
    }
    fn cleanup(&self, phase: &'static str, f: impl FnOnce() -> i32) {
        if let Err(error) = checked(phase, f) {
            self.cleanup_failed.set(true);
            eprintln!("{error}; native cleanup unproven");
        }
    }
    pub fn floats(self: &Rc<Self>, data: &[f32], shape: &[i32]) -> Result<Array> {
        self.data(
            data.as_ptr().cast(),
            data.len(),
            shape,
            mlx_dtype__MLX_FLOAT32,
        )
    }
    pub fn words(self: &Rc<Self>, data: &[u32], shape: &[i32]) -> Result<Array> {
        self.data(
            data.as_ptr().cast(),
            data.len(),
            shape,
            mlx_dtype__MLX_UINT32,
        )
    }
    pub fn normal_probe(self: &Rc<Self>, width: i32) -> Result<Array> {
        self.normal_probe_shape(&[129, width])
    }
    pub(crate) fn normal_expert_probe(self: &Rc<Self>, width: i32) -> Result<Array> {
        self.normal_probe_shape(&[65, 1, width])
    }
    fn normal_probe_shape(self: &Rc<Self>, shape: &[i32]) -> Result<Array> {
        let mut key = Array::empty(Rc::clone(self));
        checked("probe-key", || unsafe {
            mlx_random_key(&mut key.raw, 0x5EED)
        })?;
        key.out("probe-normal", |p| unsafe {
            mlx_random_normal(
                p,
                shape.as_ptr(),
                shape.len(),
                mlx_dtype__MLX_FLOAT32,
                0.,
                1.,
                key.raw,
                self.stream,
            )
        })
    }
    pub fn load_packed(
        self: &Rc<Self>,
        path: &str,
        wanted: &BTreeMap<String, Vec<i32>>,
    ) -> Result<BTreeMap<String, model::Weight<Array>>> {
        crate::loader::load_packed(self, path, wanted)
    }
    pub fn ids(self: &Rc<Self>, data: &[u32]) -> Result<Array> {
        let len =
            i32::try_from(data.len()).map_err(|_| Error::new(ErrorCode::MalformedTensor, "ids"))?;
        self.data(
            data.as_ptr().cast(),
            data.len(),
            &[1, len],
            mlx_dtype__MLX_UINT32,
        )
    }
    fn data(
        self: &Rc<Self>,
        ptr: *const std::ffi::c_void,
        len: usize,
        shape: &[i32],
        dtype: mlx_dtype,
    ) -> Result<Array> {
        self.healthy()?;
        let count = shape.iter().try_fold(1usize, |a, &n| {
            if n <= 0 {
                None
            } else {
                a.checked_mul(n as usize)
            }
        });
        if shape.is_empty() || shape.len() > i32::MAX as usize || count != Some(len) {
            return Err(Error::new(ErrorCode::MalformedTensor, "data-shape"));
        }
        // mlx_array_new_data copies exactly product(shape) elements synchronously.
        let raw = unsafe { mlx_array_new_data(ptr, shape.as_ptr(), shape.len() as i32, dtype) };
        if raw.ctx.is_null() {
            return Err(Error::new(ErrorCode::Native, "new-data"));
        }
        Ok(Array {
            raw,
            ctx: Rc::clone(self),
        })
    }
    pub fn load(
        self: &Rc<Self>,
        path: &str,
        wanted: &BTreeMap<String, Vec<i32>>,
    ) -> Result<BTreeMap<String, Array>> {
        let path = CString::new(path).map_err(|_| Error::new(ErrorCode::Artifact, "path-nul"))?;
        // File I/O is a CPU Load primitive; casts and all arithmetic use GPU.
        let mut maps = LoadMaps::new(Rc::clone(self));
        checked("load-safetensors", || unsafe {
            mlx_load_safetensors(
                &mut maps.arrays,
                &mut maps.metadata,
                path.as_ptr(),
                maps.cpu,
            )
        })?;
        maps.iterator = Some(unsafe { mlx_map_string_to_array_iterator_new(maps.arrays) });
        let mut weights = BTreeMap::new();
        loop {
            let mut name = std::ptr::null();
            let mut a = Array::empty(Rc::clone(self));
            let iterator = maps
                .iterator
                .as_ref()
                .ok_or_else(|| Error::new(ErrorCode::Native, "iterator"))?;
            let mut ended = false;
            checked("safetensors-next", || {
                let rc = unsafe {
                    mlx_map_string_to_array_iterator_next(&mut name, &mut a.raw, *iterator)
                };
                if rc == 2 {
                    ended = true;
                    0
                } else {
                    rc
                }
            })?;
            if ended {
                break;
            }
            if name.is_null() {
                return Err(Error::new(ErrorCode::Native, "tensor-name"));
            }
            // Name is owned by iterator and copied before the next iteration.
            let name = unsafe { std::ffi::CStr::from_ptr(name) }
                .to_str()
                .map_err(|_| Error::new(ErrorCode::Artifact, "tensor-name-utf8"))?;
            if let Some(shape) = wanted.get(name) {
                if &a.shape() != shape {
                    return Err(Error::new(ErrorCode::MalformedTensor, "weight-shape"));
                }
                let cast = a.out("cast-f32", |p| unsafe {
                    mlx_astype(p, a.raw, mlx_dtype__MLX_FLOAT32, self.stream)
                })?;
                cast.eval()?;
                weights.insert(name.to_owned(), cast);
            }
        }
        drop(maps);
        self.healthy()?;
        if weights.len() != wanted.len() {
            return Err(Error::new(ErrorCode::Artifact, "missing-weight"));
        }
        Ok(weights)
    }
}
impl Drop for Context {
    fn drop(&mut self) {
        if !self.stream_released {
            self.cleanup("stream-release", || unsafe { mlx_stream_free(self.stream) });
        }
        match self.resources.healthy() {
            Ok(()) if !self.cleanup_failed.get() => OWNED.store(false, Ordering::Release),
            Ok(()) => eprintln!("native cleanup failed; process remains fenced"),
            Err(error) => eprintln!("{error}; process remains fenced"),
        }
    }
}
struct LoadMaps {
    arrays: mlx_map_string_to_array,
    metadata: mlx_map_string_to_string,
    iterator: Option<mlx_map_string_to_array_iterator>,
    cpu: mlx_stream,
    ctx: Rc<Context>,
}
impl LoadMaps {
    fn new(ctx: Rc<Context>) -> Self {
        unsafe {
            Self {
                arrays: mlx_map_string_to_array_new(),
                metadata: mlx_map_string_to_string_new(),
                iterator: None,
                cpu: mlx_default_cpu_stream_new(),
                ctx,
            }
        }
    }
}
impl Drop for LoadMaps {
    fn drop(&mut self) {
        unsafe {
            if let Some(it) = self.iterator {
                self.ctx.cleanup("iterator-release", || {
                    mlx_map_string_to_array_iterator_free(it)
                });
            }
            self.ctx
                .cleanup("map-release", || mlx_map_string_to_array_free(self.arrays));
            self.ctx.cleanup("metadata-release", || {
                mlx_map_string_to_string_free(self.metadata)
            });
            self.ctx
                .cleanup("load-stream-release", || mlx_stream_free(self.cpu));
        }
    }
}
pub struct Array {
    raw: mlx_array,
    ctx: Rc<Context>,
}
impl Drop for Array {
    fn drop(&mut self) {
        self.ctx
            .cleanup("array-release", || unsafe { mlx_array_free(self.raw) });
    }
}
impl Array {
    fn empty(ctx: Rc<Context>) -> Self {
        Self {
            raw: unsafe { mlx_array_new() },
            ctx,
        }
    }
    fn out(&self, phase: &'static str, f: impl FnOnce(*mut mlx_array) -> i32) -> Result<Self> {
        self.ctx.healthy()?;
        let mut result = Self::empty(Rc::clone(&self.ctx));
        checked(phase, || f(&mut result.raw))?;
        Ok(result)
    }
    pub fn take(&self, ids: &Self) -> Result<Self> {
        self.out("take", |p| unsafe {
            mlx_take_axis(p, self.raw, ids.raw, 0, self.ctx.stream)
        })
    }
    pub fn words(&self) -> Result<Vec<u32>> {
        let contiguous = self.out("export-words-contiguous", |p| unsafe {
            mlx_contiguous(p, self.raw, false, self.ctx.stream)
        })?;
        contiguous.eval()?;
        if unsafe { mlx_array_dtype(contiguous.raw) } != mlx_dtype__MLX_UINT32 {
            return Err(Error::new(ErrorCode::MalformedTensor, "export-words-dtype"));
        }
        let ptr = unsafe { mlx_array_data_uint32(contiguous.raw) };
        let len = unsafe { mlx_array_size(contiguous.raw) };
        if ptr.is_null() || len > isize::MAX as usize / 4 {
            return Err(Error::new(ErrorCode::Native, "export-words-pointer"));
        }
        Ok(unsafe { std::slice::from_raw_parts(ptr, len) }.to_vec())
    }
    pub fn floats(&self) -> Result<Vec<f32>> {
        // Raw data access ignores strides. Materialize row-major storage before
        // constructing a Rust slice; keep its owning handle alive through copy.
        let contiguous = self.out("export-contiguous", |p| unsafe {
            mlx_contiguous(p, self.raw, false, self.ctx.stream)
        })?;
        contiguous.eval()?;
        if unsafe { mlx_array_dtype(contiguous.raw) } != mlx_dtype__MLX_FLOAT32 {
            return Err(Error::new(ErrorCode::MalformedTensor, "export-dtype"));
        }
        let ptr = unsafe { mlx_array_data_float32(contiguous.raw) };
        let len = unsafe { mlx_array_size(contiguous.raw) };
        if ptr.is_null() || len > isize::MAX as usize / std::mem::size_of::<f32>() {
            return Err(Error::new(ErrorCode::Native, "export-pointer"));
        }
        // Evaluation completed; backing allocation lives as long as contiguous.
        Ok(unsafe { std::slice::from_raw_parts(ptr, len) }.to_vec())
    }
}
macro_rules! unary {
    ($name:ident,$op:ident) => {
        fn $name(&self) -> Result<Self> {
            self.out(stringify!($op), |p| unsafe {
                $op(p, self.raw, self.ctx.stream)
            })
        }
    };
}
macro_rules! binary {
    ($name:ident,$op:ident) => {
        fn $name(&self, b: &Self) -> Result<Self> {
            self.out(stringify!($op), |p| unsafe {
                $op(p, self.raw, b.raw, self.ctx.stream)
            })
        }
    };
}
impl Tensor for Array {
    fn take(&self, ids: &Self) -> Result<Self> {
        Array::take(self, ids)
    }
    fn quantized_matmul(&self, codes: &Self, scales: &Self, biases: &Self) -> Result<Self> {
        // MLX 0.32.4 added B==1 split-K dispatch whose reduction changes with M.
        // Preserve the oracle's non-split-K qmm arithmetic by executing two
        // identical batches and selecting the first. Reshapes share packed
        // storage; no full floating/signed weight image is materialized.
        let x_shape = self.shape();
        let w_shape = codes.shape();
        let g_shape = scales.shape();
        if x_shape.len() != 2
            || w_shape.len() != 2
            || g_shape.len() != 2
            || x_shape.iter().any(|d| *d <= 0)
            || w_shape.iter().any(|d| *d <= 0)
            || w_shape[1].checked_mul(4) != Some(x_shape[1])
            || x_shape[1] % 128 != 0
            || g_shape != [w_shape[0], x_shape[1] / 128]
            || biases.shape() != g_shape
        {
            return Err(Error::new(
                ErrorCode::MalformedTensor,
                "quantized-matmul-shape",
            ));
        }
        let x = self.reshape(&[1, x_shape[0], x_shape[1]])?.repeat(2, 0)?;
        let w = codes.reshape(&[1, w_shape[0], w_shape[1]])?;
        let s = scales.reshape(&[1, g_shape[0], g_shape[1]])?;
        let b = biases.reshape(&[1, g_shape[0], g_shape[1]])?;
        let result = self.out("quantized-matmul", |p| unsafe {
            mlx_quantized_matmul(
                p,
                x.raw,
                w.raw,
                s.raw,
                b.raw,
                true,
                mlx_optional_int {
                    value: 128,
                    has_value: true,
                },
                mlx_optional_int {
                    value: 8,
                    has_value: true,
                },
                c"affine".as_ptr(),
                self.ctx.stream,
            )
        })?;
        result
            .slice(&[0, 0, 0], &[1, x_shape[0], w_shape[0]])?
            .reshape(&[x_shape[0], w_shape[0]])
    }
    fn dequantize(&self, scales: &Self, biases: &Self) -> Result<Self> {
        self.out("dequantize-selected-rows", |p| unsafe {
            mlx_dequantize(
                p,
                self.raw,
                scales.raw,
                biases.raw,
                mlx_optional_int {
                    value: 128,
                    has_value: true,
                },
                mlx_optional_int {
                    value: 8,
                    has_value: true,
                },
                c"affine".as_ptr(),
                mlx_array {
                    ctx: std::ptr::null_mut(),
                },
                mlx_optional_dtype {
                    value: mlx_dtype__MLX_FLOAT32,
                    has_value: true,
                },
                self.ctx.stream,
            )
        })
    }
    fn equal(&self, other: &Self) -> Result<bool> {
        let a = self.floats()?;
        let b = other.floats()?;
        let equal = self.shape() == other.shape()
            && a.iter().zip(&b).all(|(x, y)| x.to_bits() == y.to_bits());
        if !equal {
            let different = a
                .iter()
                .zip(&b)
                .filter(|(x, y)| x.to_bits() != y.to_bits())
                .count();
            let numerical = a.iter().zip(&b).filter(|(x, y)| x != y).count();
            let max = a
                .iter()
                .zip(&b)
                .fold(0f32, |m, (x, y)| m.max((x - y).abs()));
            eprintln!(
                "row probe mismatch shape={:?} bit_differences={different} numeric_differences={numerical} max_abs={max}",
                self.shape()
            );
        }
        Ok(equal)
    }
    fn shape(&self) -> Vec<i32> {
        let n = unsafe { mlx_array_ndim(self.raw) };
        if n == 0 {
            Vec::new()
        } else {
            unsafe { std::slice::from_raw_parts(mlx_array_shape(self.raw), n) }.to_vec()
        }
    }
    fn share(&self) -> Result<Self> {
        self.out("array-share", |p| unsafe { mlx_array_set(p, self.raw) })
    }
    fn scalar(&self, v: f32) -> Result<Self> {
        self.ctx.floats(&[v], &[1])?.reshape(&[])
    }
    fn range(&self, start: i32, end: i32, step: i32) -> Result<Self> {
        self.out("arange", |p| unsafe {
            mlx_arange(
                p,
                start as f64,
                end as f64,
                step as f64,
                mlx_dtype__MLX_FLOAT32,
                self.ctx.stream,
            )
        })
    }
    fn reshape(&self, dims: &[i32]) -> Result<Self> {
        self.out("reshape", |p| unsafe {
            mlx_reshape(p, self.raw, dims.as_ptr(), dims.len(), self.ctx.stream)
        })
    }
    fn transpose(&self, axes: &[i32]) -> Result<Self> {
        self.out("transpose", |p| unsafe {
            mlx_transpose_axes(p, self.raw, axes.as_ptr(), axes.len(), self.ctx.stream)
        })
    }
    fn slice(&self, start: &[i32], stop: &[i32]) -> Result<Self> {
        let strides = vec![1; start.len()];
        self.out("slice", |p| unsafe {
            mlx_slice(
                p,
                self.raw,
                start.as_ptr(),
                start.len(),
                stop.as_ptr(),
                stop.len(),
                strides.as_ptr(),
                strides.len(),
                self.ctx.stream,
            )
        })
    }
    fn cat(&self, other: &Self, axis: i32) -> Result<Self> {
        let raw = [self.raw, other.raw];
        let vector = unsafe { mlx_vector_array_new_data(raw.as_ptr(), raw.len()) };
        let result = self.out("concatenate", |p| unsafe {
            mlx_concatenate_axis(p, vector, axis, self.ctx.stream)
        });
        self.ctx.cleanup("vector-release", || unsafe {
            mlx_vector_array_free(vector)
        });
        self.ctx.healthy()?;
        result
    }
    fn repeat(&self, times: i32, axis: i32) -> Result<Self> {
        self.out("repeat", |p| unsafe {
            mlx_repeat_axis(p, self.raw, times, axis, self.ctx.stream)
        })
    }
    fn sum(&self) -> Result<Self> {
        self.out("sum", |p| unsafe {
            mlx_sum_axis(p, self.raw, -1, true, self.ctx.stream)
        })
    }
    fn mean(&self) -> Result<Self> {
        self.out("mean", |p| unsafe {
            mlx_mean_axis(p, self.raw, -1, true, self.ctx.stream)
        })
    }
    fn softmax(&self) -> Result<Self> {
        self.out("softmax", |p| unsafe {
            mlx_softmax_axis(p, self.raw, -1, false, self.ctx.stream)
        })
    }
    fn select(&self, yes: &Self, no: &Self) -> Result<Self> {
        self.out("where", |p| unsafe {
            mlx_where(p, self.raw, yes.raw, no.raw, self.ctx.stream)
        })
    }
    fn eval(&self) -> Result<()> {
        checked("eval", || unsafe { mlx_array_eval(self.raw) })
    }
    unary!(copy, mlx_copy);
    unary!(exp, mlx_exp);
    binary!(logaddexp, mlx_logaddexp);
    unary!(square, mlx_square);
    unary!(rsqrt, mlx_rsqrt);
    unary!(sigmoid, mlx_sigmoid);
    unary!(cos, mlx_cos);
    unary!(sin, mlx_sin);
    unary!(neg, mlx_negative);
    unary!(zeros, mlx_zeros_like);
    binary!(add, mlx_add);
    binary!(mul, mlx_multiply);
    binary!(div, mlx_divide);
    binary!(pow, mlx_power);
    binary!(matmul, mlx_matmul);
    binary!(ge, mlx_greater_equal);
}

mod moe;

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ffi_error_is_result_not_exit_and_worker_recovers() {
        let ctx = Context::new(64 * 1024 * 1024, 0).unwrap();
        let x = ctx.floats(&[1., 2.], &[2]).unwrap();
        let err = x.reshape(&[3]).err().expect("native reshape must refuse");
        assert_eq!(err.code, ErrorCode::Native);
        assert_eq!(err.phase, "reshape");
        assert!(err.native_status.is_some());
        assert!(err.native_callback);
        assert_eq!(x.add(&x).unwrap().floats().unwrap(), vec![2., 4.]);
        assert!(ctx.floats(&[1.], &[2]).is_err());
        let matrix = ctx.floats(&[1., 2., 3., 4., 5., 6.], &[2, 3]).unwrap();
        assert_eq!(
            matrix.transpose(&[1, 0]).unwrap().floats().unwrap(),
            vec![1., 4., 2., 5., 3., 6.]
        );
        ctx.synchronize().unwrap();
        let descriptor = crate::admission::qwen2_descriptor(0..8, true, false).unwrap();
        assert_eq!(
            model::forward(
                &descriptor,
                x.scalar(1.).unwrap(),
                &BTreeMap::new(),
                &Vec::new(),
                0
            )
            .err()
            .unwrap()
            .code,
            ErrorCode::MalformedTensor
        );
        let scalar = x.scalar(1.).unwrap();
        assert_eq!(
            scalar
                .quantized_matmul(&scalar, &scalar, &scalar)
                .err()
                .unwrap()
                .code,
            ErrorCode::MalformedTensor
        );
        let weight = model::Weight::Float(scalar);
        assert_eq!(
            weight.rows_are_independent(&x).unwrap_err().code,
            ErrorCode::MalformedTensor
        );
        drop(weight);
        drop(matrix);
        drop(x);
        ctx.cleanup("injected-release-failure", || 1);
        assert_eq!(ctx.healthy().unwrap_err().code, ErrorCode::Cleanup);
        drop(ctx);
        assert!(
            Context::new(64 * 1024 * 1024, 0).is_err(),
            "unproven cleanup must fence successor"
        );
    }
}
