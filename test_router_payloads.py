import unittest

from mycelium_router.payloads import PayloadError, decode_token_ids, encode_token_ids


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


if __name__ == "__main__":
   unittest.main()
