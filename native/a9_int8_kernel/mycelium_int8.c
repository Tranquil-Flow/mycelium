/* SPDX-License-Identifier: AGPL-3.0-only
 *
 * Mycelium A9 int8 weight-only linear kernel (W8A32).
 *
 *   y[r, o] = (sum_i x[r, i] * (float)w[o, i]) * scale[o]
 *
 * The int8 weight matrix is the persistent packed state.  Each int8 value is
 * widened to float inside registers; no float copy of a weight row or of the
 * whole matrix is ever written to memory, so decode performs zero full-weight
 * materializations by construction (A9 frozen decision persistent_int8_execution).
 *
 * Dispatch: AVX2+FMA when the build targets it (/arch:AVX2 on MSVC,
 * -mavx2 -mfma on clang/gcc), otherwise a portable scalar loop.  Threads: OpenMP
 * over output features when built with OpenMP (/openmp on MSVC), else serial.
 * Build: build_windows.cmd (MSVC x64 Native Tools prompt) or build_posix.sh.
 */
#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#define MYC_EXPORT __declspec(dllexport)
#else
#define MYC_EXPORT __attribute__((visibility("default")))
#endif

#if defined(__AVX2__)
#include <immintrin.h>
#define MYC_DISPATCH "avx2_fma"
#else
#define MYC_DISPATCH "scalar"
#endif

#if defined(_OPENMP)
#include <omp.h>
#define MYC_THREADING "openmp"
#else
#define MYC_THREADING "serial"
#endif

#define MYC_ABI_VERSION 1
#define MYC_ROW_BLOCK 4

MYC_EXPORT int mycelium_int8_abi_version(void) { return MYC_ABI_VERSION; }
MYC_EXPORT const char *mycelium_int8_dispatch(void) { return MYC_DISPATCH; }
MYC_EXPORT const char *mycelium_int8_threading(void) { return MYC_THREADING; }

#if defined(__AVX2__)
static float myc_hsum256(__m256 v) {
    __m128 lo = _mm256_castps256_ps128(v);
    __m128 hi = _mm256_extractf128_ps(v, 1);
    lo = _mm_add_ps(lo, hi);
    __m128 shuf = _mm_movehdup_ps(lo);
    __m128 sums = _mm_add_ps(lo, shuf);
    shuf = _mm_movehl_ps(shuf, sums);
    sums = _mm_add_ss(sums, shuf);
    return _mm_cvtss_f32(sums);
}
#endif

/* One output feature for a block of up to MYC_ROW_BLOCK activation rows. */
static void myc_dot_block(const float *x, int64_t rows, int64_t in_features,
                          const int8_t *w_row, float scale, float *y,
                          int64_t out_features, int64_t o) {
    float acc[MYC_ROW_BLOCK] = {0.0f, 0.0f, 0.0f, 0.0f};
    int64_t i = 0;
#if defined(__AVX2__)
    __m256 vacc[MYC_ROW_BLOCK];
    for (int64_t r = 0; r < rows; ++r) vacc[r] = _mm256_setzero_ps();
    for (; i + 16 <= in_features; i += 16) {
        __m128i wb = _mm_loadu_si128((const __m128i *)(w_row + i));
        __m256 w_lo = _mm256_cvtepi32_ps(_mm256_cvtepi8_epi32(wb));
        __m256 w_hi = _mm256_cvtepi32_ps(_mm256_cvtepi8_epi32(_mm_srli_si128(wb, 8)));
        for (int64_t r = 0; r < rows; ++r) {
            const float *xr = x + r * in_features + i;
            vacc[r] = _mm256_fmadd_ps(_mm256_loadu_ps(xr), w_lo, vacc[r]);
            vacc[r] = _mm256_fmadd_ps(_mm256_loadu_ps(xr + 8), w_hi, vacc[r]);
        }
    }
    for (int64_t r = 0; r < rows; ++r) acc[r] = myc_hsum256(vacc[r]);
#endif
    for (; i < in_features; ++i) {
        float wv = (float)w_row[i];
        for (int64_t r = 0; r < rows; ++r) acc[r] += x[r * in_features + i] * wv;
    }
    for (int64_t r = 0; r < rows; ++r) y[r * out_features + o] = acc[r] * scale;
}

/* Returns 0 on success, a negative code on invalid arguments. */
MYC_EXPORT int mycelium_w8a32_linear(const float *x, int64_t rows,
                                     const int8_t *w, const float *scales,
                                     int64_t out_features, int64_t in_features,
                                     float *y, int32_t threads) {
    if (!x || !w || !scales || !y) return -1;
    if (rows < 1 || out_features < 1 || in_features < 1) return -2;
    if (out_features > 0x7fffffff || threads < 1) return -3;
    int out = (int)out_features;
    for (int64_t r0 = 0; r0 < rows; r0 += MYC_ROW_BLOCK) {
        int64_t block = rows - r0 < MYC_ROW_BLOCK ? rows - r0 : MYC_ROW_BLOCK;
        const float *xb = x + r0 * in_features;
        float *yb = y + r0 * out_features;
        int o;
#if defined(_OPENMP)
#pragma omp parallel for num_threads(threads) schedule(static)
#endif
        for (o = 0; o < out; ++o) {
            myc_dot_block(xb, block, in_features, w + (int64_t)o * in_features,
                          scales[o], yb, out_features, o);
        }
    }
    return 0;
}
