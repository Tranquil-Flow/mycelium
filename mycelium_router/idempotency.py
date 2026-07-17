"""Canonical attempt-scoped Router idempotency keys."""


def hop_idempotency_key(
   *,
   request_id: str,
   path_id: str,
   path_attempt: int,
   phase: str,
   token_index: int,
   hop_index: int,
) -> str:
   return (
      f"{request_id}:{path_id}:{path_attempt}:"
      f"{phase}:{token_index}:{hop_index}"
   )
