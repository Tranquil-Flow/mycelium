// SPDX-License-Identifier: AGPL-3.0-only
/* Self test: the built dispatch path against a double-precision reference. */
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int mycelium_w8a32_linear(const float *, int64_t, const int8_t *, const float *,
                          int64_t, int64_t, float *, int32_t);
const char *mycelium_int8_dispatch(void);

static uint32_t state = 12345u;
static uint32_t next(void) { state = state * 1664525u + 1013904223u; return state; }

int main(void) {
    const int64_t shapes[][3] = {{1, 7, 9}, {3, 33, 47}, {5, 64, 896}, {2, 130, 4864}};
    int failures = 0;
    for (size_t s = 0; s < sizeof shapes / sizeof shapes[0]; ++s) {
        int64_t rows = shapes[s][0], out = shapes[s][1], in = shapes[s][2];
        float *x = malloc(sizeof(float) * rows * in);
        int8_t *w = malloc(out * in);
        float *scale = malloc(sizeof(float) * out);
        float *y = malloc(sizeof(float) * rows * out);
        for (int64_t i = 0; i < rows * in; ++i) x[i] = ((int)(next() % 2001) - 1000) / 500.0f;
        for (int64_t i = 0; i < out * in; ++i) w[i] = (int8_t)((int)(next() % 255) - 127);
        for (int64_t o = 0; o < out; ++o) scale[o] = (1 + next() % 1000) / 50000.0f;
        if (mycelium_w8a32_linear(x, rows, w, scale, out, in, y, 2) != 0) { puts("call failed"); return 2; }
        double worst = 0.0;
        for (int64_t r = 0; r < rows; ++r)
            for (int64_t o = 0; o < out; ++o) {
                double ref = 0.0, mag = 0.0;
                for (int64_t i = 0; i < in; ++i) { ref += (double)x[r * in + i] * w[o * in + i]; mag += fabs((double)x[r * in + i] * w[o * in + i]); }
                ref *= scale[o]; mag *= scale[o];
                double err = fabs(ref - y[r * out + o]) / (mag + 1e-12);
                if (err > worst) worst = err;
            }
        printf("%s rows=%lld out=%lld in=%lld worst_rel=%.3g\n", mycelium_int8_dispatch(),
               (long long)rows, (long long)out, (long long)in, worst);
        if (worst > 1e-5) failures++;
        if (mycelium_w8a32_linear(NULL, rows, w, scale, out, in, y, 1) != -1) failures++;
        if (mycelium_w8a32_linear(x, 0, w, scale, out, in, y, 1) != -2) failures++;
        if (mycelium_w8a32_linear(x, rows, w, scale, out, in, y, 0) != -3) failures++;
        free(x); free(w); free(scale); free(y);
    }
    puts(failures ? "SELFTEST FAIL" : "SELFTEST PASS");
    return failures ? 1 : 0;
}
