import struct
import unittest

from mycelium_router.payloads import (
   ActivationPayload,
   PayloadError,
   decode_activation,
   decode_token_ids,
   encode_activation,
   encode_token_ids,
)


class TokenPayloadTests(unittest.TestCase):
   def test_token_ids_round_trip_through_versioned_binary_payload(self):
      token_ids = (0, 1, 42, 65_535, 1_000_000)

      payload = encode_token_ids(token_ids)

      self.assertIsInstance(payload, bytes)
      self.assertEqual(decode_token_ids(payload), token_ids)

   def test_truncated_token_payload_fails_closed(self):
      payload = encode_token_ids((1, 2, 3))

      with self.assertRaises(PayloadError) as caught:
         decode_token_ids(payload[:-1])

      self.assertEqual(caught.exception.code, "token_payload_length_mismatch")

   def test_unknown_token_payload_version_fails_closed(self):
      payload = bytearray(encode_token_ids((1,)))
      payload[4] = 2

      with self.assertRaises(PayloadError) as caught:
         decode_token_ids(bytes(payload))

      self.assertEqual(caught.exception.code, "unknown_token_payload_version")


class ActivationPayloadTests(unittest.TestCase):
   def assert_payload_error(self, code, operation):
      with self.assertRaises(PayloadError) as caught:
         operation()
      self.assertEqual(caught.exception.code, code)

   def test_supported_activation_dtypes_round_trip_exact_metadata_and_bytes(self):
      cases = (
         ("float16", bytes.fromhex("003c00c0")),
         ("bfloat16", bytes.fromhex("c03f10c0")),
         ("float32", struct.pack("<ff", 1.5, -2.25)),
      )

      for dtype, data in cases:
         with self.subTest(dtype=dtype):
            encoded = encode_activation(dtype=dtype, shape=(1, 2), data=data)
            decoded = decode_activation(encoded)

            self.assertEqual(
               decoded,
               ActivationPayload(dtype=dtype, shape=(1, 2), data=data),
            )
            self.assertEqual(tuple(decoded), (dtype, (1, 2), data))
            self.assertEqual(encoded[:4], b"MYAC")
            self.assertEqual(encoded[4], 1)

   def test_activation_envelope_and_array_like_inputs_are_supported(self):
      envelope = ActivationPayload(
         dtype="float32",
         shape=(1, 1, 2),
         data=struct.pack("<ff", 0.25, -0.5),
      )

      class ArrayLike:
         dtype = "mlx.core.float32"
         shape = (1, 1, 2)

         def __bytes__(self):
            return envelope.data

      self.assertEqual(decode_activation(encode_activation(envelope)), envelope)
      self.assertEqual(decode_activation(encode_activation(ArrayLike())), envelope)

   def test_malformed_activation_header_fields_fail_closed(self):
      valid = encode_activation(
         dtype="float32",
         shape=(1, 2),
         data=struct.pack("<ff", 1.0, 2.0),
      )
      mutations = (
         ("invalid_activation_payload_magic", 0, ord("X")),
         ("unknown_activation_payload_version", 4, 2),
         ("unknown_activation_dtype_code", 5, 0xFF),
         ("invalid_activation_rank", 6, 0),
         ("invalid_activation_rank", 6, 9),
         ("unsupported_activation_byte_order", 7, 2),
      )

      for code, index, value in mutations:
         with self.subTest(code=code, index=index):
            payload = bytearray(valid)
            payload[index] = value
            self.assert_payload_error(code, lambda payload=payload: decode_activation(bytes(payload)))

   def test_truncated_and_length_mismatched_activations_fail_closed(self):
      valid = encode_activation(
         dtype="float32",
         shape=(1, 2),
         data=struct.pack("<ff", 1.0, 2.0),
      )
      cases = (
         ("truncated_activation_payload", valid[:15]),
         ("truncated_activation_shape", valid[:20]),
         ("activation_payload_length_mismatch", valid[:-1]),
         ("activation_payload_length_mismatch", valid + b"\x00"),
      )

      for code, payload in cases:
         with self.subTest(code=code, length=len(payload)):
            self.assert_payload_error(code, lambda payload=payload: decode_activation(payload))

   def test_declared_oversize_activation_fails_before_body_allocation(self):
      payload = bytearray(
         encode_activation(dtype="float32", shape=(1,), data=struct.pack("<f", 1.0))
      )
      struct.pack_into(">Q", payload, 8, 268_435_457)

      self.assert_payload_error(
         "activation_payload_too_large",
         lambda: decode_activation(bytes(payload)),
      )

   def test_invalid_activation_dtype_shape_and_data_fail_closed_on_encode(self):
      cases = (
         (
            "unsupported_activation_dtype",
            lambda: encode_activation(dtype="int32", shape=(1,), data=b"\x00" * 4),
         ),
         (
            "invalid_activation_rank",
            lambda: encode_activation(dtype="float32", shape=(), data=b""),
         ),
         (
            "invalid_activation_rank",
            lambda: encode_activation(dtype="float32", shape=(1,) * 9, data=b"\x00" * 4),
         ),
         (
            "invalid_activation_dimension",
            lambda: encode_activation(dtype="float32", shape=(0,), data=b""),
         ),
         (
            "activation_data_length_mismatch",
            lambda: encode_activation(dtype="float32", shape=(2,), data=b"\x00" * 4),
         ),
      )

      for code, operation in cases:
         with self.subTest(code=code):
            self.assert_payload_error(code, operation)

   def test_decoded_zero_dimension_is_rejected_after_shape_parsing(self):
      payload = bytearray(
         encode_activation(dtype="float32", shape=(1,), data=struct.pack("<f", 1.0))
      )
      struct.pack_into(">I", payload, 16, 0)

      self.assert_payload_error(
         "invalid_activation_dimension",
         lambda: decode_activation(bytes(payload)),
      )


if __name__ == "__main__":
   unittest.main()
