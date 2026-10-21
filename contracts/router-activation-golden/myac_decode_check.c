/* SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Reference MYAC v1 activation-frame decoder and golden-vector checker.
 *
 * Dependency-free C99.  It reads <dir>/vectors.tsv (written by
 * scripts/generate_router_wire_golden.py), decodes every <dir>/<file>.bin and
 * checks it against the vector: valid frames must decode to the recorded dtype
 * code, rank, dimensions and element bytes; reject frames must fail with the
 * recorded error code.  Check order mirrors mycelium_router/payloads.py.
 *
 * Frame layout (all header integers big-endian, element bytes little-endian):
 *   magic "MYAC" | u8 version=1 | u8 dtype (1=f16, 2=bf16, 3=f32) | u8 rank (1..8)
 *   | u8 byte_order=1 (LE) | u64 data_length | rank x u32 dims | data_length bytes
 *
 * usage: myac_decode_check <golden-directory>
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MYAC_HEADER_BYTES 16u
#define MYAC_MAX_RANK 8u
#define MYAC_MAX_DATA_BYTES 268435456u

typedef struct {
   unsigned dtype_code;
   unsigned rank;
   uint32_t dims[MYAC_MAX_RANK];
   uint64_t data_length;
   const unsigned char *data;
} myac_frame;

static uint32_t be32(const unsigned char *p) {
   return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

static uint64_t be64(const unsigned char *p) {
   return ((uint64_t)be32(p) << 32) | (uint64_t)be32(p + 4);
}

static unsigned element_bytes(unsigned dtype_code) {
   switch (dtype_code) {
      case 1: return 2u;
      case 2: return 2u;
      case 3: return 4u;
      default: return 0u;
   }
}

/* Returns NULL on success, otherwise the stable error code string. */
static const char *myac_decode(const unsigned char *buf, size_t len, myac_frame *out) {
   unsigned i;
   size_t metadata;
   uint64_t count = 1;
   unsigned size;

   if (len < MYAC_HEADER_BYTES) return "truncated_activation_payload";
   if (memcmp(buf, "MYAC", 4) != 0) return "invalid_activation_payload_magic";
   if (buf[4] != 1) return "unknown_activation_payload_version";
   out->dtype_code = buf[5];
   size = element_bytes(out->dtype_code);
   if (size == 0u) return "unknown_activation_dtype_code";
   if (buf[7] != 1) return "unsupported_activation_byte_order";
   out->rank = buf[6];
   if (out->rank == 0u || out->rank > MYAC_MAX_RANK) return "invalid_activation_rank";
   metadata = MYAC_HEADER_BYTES + (size_t)out->rank * 4u;
   if (len < metadata) return "truncated_activation_shape";
   out->data_length = be64(buf + 8);
   if (out->data_length > MYAC_MAX_DATA_BYTES) return "activation_payload_too_large";
   if ((uint64_t)len != (uint64_t)metadata + out->data_length) return "activation_payload_length_mismatch";
   for (i = 0; i < out->rank; i++) {
      out->dims[i] = be32(buf + MYAC_HEADER_BYTES + (size_t)i * 4u);
      if (out->dims[i] == 0u) return "invalid_activation_dimension";
      count *= (uint64_t)out->dims[i];
      if (count * size > MYAC_MAX_DATA_BYTES) return "activation_payload_too_large";
   }
   if (out->data_length != count * size) return "activation_data_length_mismatch";
   out->data = buf + metadata;
   return NULL;
}

static int hex_value(int c) {
   if (c >= '0' && c <= '9') return c - '0';
   if (c >= 'a' && c <= 'f') return c - 'a' + 10;
   return -1;
}

static unsigned char *read_file(const char *path, size_t *length) {
   FILE *file = fopen(path, "rb");
   unsigned char *data;
   long size;
   if (!file) return NULL;
   if (fseek(file, 0, SEEK_END) != 0 || (size = ftell(file)) < 0 || fseek(file, 0, SEEK_SET) != 0) {
      fclose(file);
      return NULL;
   }
   data = (unsigned char *)malloc(size > 0 ? (size_t)size : 1u);
   if (!data || fread(data, 1, (size_t)size, file) != (size_t)size) {
      free(data);
      fclose(file);
      return NULL;
   }
   fclose(file);
   *length = (size_t)size;
   return data;
}

static int fail(const char *file, const char *what) {
   fprintf(stderr, "FAIL %s: %s\n", file, what);
   return 1;
}

static int check_valid(const char *dir, char *fields[], int nfields) {
   char path[4096];
   size_t length = 0, i, hex_length;
   unsigned char *buf;
   myac_frame frame;
   const char *error;
   unsigned long want_code, want_rank;
   char *cursor;
   unsigned d;

   if (nfields != 6) return fail(fields[1], "valid row needs 6 fields");
   snprintf(path, sizeof path, "%s/%s", dir, fields[1]);
   buf = read_file(path, &length);
   if (!buf) return fail(fields[1], "cannot read frame");
   error = myac_decode(buf, length, &frame);
   if (error) {
      free(buf);
      return fail(fields[1], error);
   }
   want_code = strtoul(fields[2], NULL, 10);
   want_rank = strtoul(fields[3], NULL, 10);
   if (frame.dtype_code != want_code || frame.rank != want_rank) {
      free(buf);
      return fail(fields[1], "dtype code or rank mismatch");
   }
   cursor = fields[4];
   for (d = 0; d < frame.rank; d++) {
      char *end;
      unsigned long dim = strtoul(cursor, &end, 10);
      if (dim != frame.dims[d]) {
         free(buf);
         return fail(fields[1], "dimension mismatch");
      }
      cursor = (*end == ',') ? end + 1 : end;
   }
   hex_length = strlen(fields[5]);
   if (hex_length != frame.data_length * 2u) {
      free(buf);
      return fail(fields[1], "element byte count mismatch");
   }
   for (i = 0; i < frame.data_length; i++) {
      int hi = hex_value((unsigned char)fields[5][2 * i]);
      int lo = hex_value((unsigned char)fields[5][2 * i + 1]);
      if (hi < 0 || lo < 0 || (unsigned char)((hi << 4) | lo) != frame.data[i]) {
         free(buf);
         return fail(fields[1], "element bytes mismatch");
      }
   }
   free(buf);
   return 0;
}

static int check_reject(const char *dir, char *fields[], int nfields) {
   char path[4096];
   size_t length = 0;
   unsigned char *buf;
   myac_frame frame;
   const char *error;

   if (nfields != 3) return fail(fields[1], "reject row needs 3 fields");
   snprintf(path, sizeof path, "%s/%s", dir, fields[1]);
   buf = read_file(path, &length);
   if (!buf) return fail(fields[1], "cannot read frame");
   error = myac_decode(buf, length, &frame);
   free(buf);
   if (!error) return fail(fields[1], "decoded but must be rejected");
   if (strcmp(error, fields[2]) != 0) {
      fprintf(stderr, "FAIL %s: got %s, want %s\n", fields[1], error, fields[2]);
      return 1;
   }
   return 0;
}

int main(int argc, char **argv) {
   char path[4096];
   char line[1 << 16];
   FILE *tsv;
   unsigned valid = 0, reject = 0;

   if (argc != 2) {
      fprintf(stderr, "usage: %s <golden-directory>\n", argv[0]);
      return 2;
   }
   snprintf(path, sizeof path, "%s/vectors.tsv", argv[1]);
   tsv = fopen(path, "r");
   if (!tsv) {
      fprintf(stderr, "cannot open %s\n", path);
      return 2;
   }
   while (fgets(line, sizeof line, tsv)) {
      char *fields[8];
      int nfields = 0;
      char *p = line;
      size_t n = strlen(line);
      while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r')) line[--n] = '\0';
      if (n == 0 || line[0] == '#') continue;
      fields[nfields++] = p;
      while (*p && nfields < 8) {
         if (*p == '\t') {
            *p = '\0';
            fields[nfields++] = p + 1;
         }
         p++;
      }
      if (strcmp(fields[0], "valid") == 0) {
         if (check_valid(argv[1], fields, nfields)) return 1;
         valid++;
      } else if (strcmp(fields[0], "reject") == 0) {
         if (check_reject(argv[1], fields, nfields)) return 1;
         reject++;
      } else {
         return fail(fields[0], "unknown row kind");
      }
   }
   fclose(tsv);
   printf("ok valid=%u reject=%u\n", valid, reject);
   return 0;
}
