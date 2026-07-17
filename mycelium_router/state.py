"""Fail-closed request, path, and hop lifecycle state machines."""


class StateTransitionError(ValueError):
   def __init__(self, code: str, detail: str = ""):
      self.code = code
      self.detail = detail
      super().__init__(code if not detail else f"{code}: {detail}")


class _StateMachine:
   transitions: dict[str, frozenset[str]] = {}
   initial_state = ""

   def __init__(
      self,
      *,
      path_attempt: int,
      initial_state: str | None = None,
   ):
      if path_attempt < 0:
         raise StateTransitionError("invalid_path_attempt")
      state = initial_state or self.initial_state
      if state not in self.transitions:
         raise StateTransitionError("unknown_state", state)
      self.state = state
      self.path_attempt = path_attempt

   def transition(self, target: str, *, path_attempt: int) -> bool:
      self._validate_attempt(path_attempt)
      if target not in self.transitions:
         raise StateTransitionError("unknown_state", target)
      if target == self.state:
         return False
      if target not in self.transitions[self.state]:
         raise StateTransitionError(
            "illegal_state_transition",
            f"{self.state}->{target}",
         )
      self.state = target
      return True

   def _validate_attempt(self, path_attempt: int) -> None:
      if path_attempt < self.path_attempt:
         raise StateTransitionError("stale_path_attempt")
      if path_attempt > self.path_attempt:
         raise StateTransitionError("future_path_attempt")


class RequestStateMachine(_StateMachine):
   initial_state = "ADMITTING"
   transitions = {
      "ADMITTING": frozenset({"PREFILL", "FAILED", "CANCELLED"}),
      "PREFILL": frozenset({"LOCKED", "FAILED", "CANCELLED"}),
      "LOCKED": frozenset({"DECODING", "FAILED", "CANCELLED"}),
      "DECODING": frozenset({"COMPLETED", "FAILED", "CANCELLED"}),
      "COMPLETED": frozenset(),
      "FAILED": frozenset(),
      "CANCELLED": frozenset(),
   }

   def begin_recovery(self, *, path_attempt: int) -> bool:
      if self.state not in {"PREFILL", "LOCKED", "DECODING"}:
         raise StateTransitionError(
            "illegal_state_transition",
            f"{self.state}->PREFILL",
         )
      if path_attempt != self.path_attempt + 1:
         code = (
            "stale_path_attempt"
            if path_attempt <= self.path_attempt
            else "future_path_attempt"
         )
         raise StateTransitionError(code)
      self.path_attempt = path_attempt
      self.state = "PREFILL"
      return True


class PathStateMachine(_StateMachine):
   initial_state = "BUILDING"
   transitions = {
      "BUILDING": frozenset({"RESERVED", "FAILED"}),
      "RESERVED": frozenset({"LOCKED", "FAILED"}),
      "LOCKED": frozenset({"RETIRING", "FAILED"}),
      "RETIRING": frozenset({"FAILED"}),
      "FAILED": frozenset(),
   }


class HopStateMachine(_StateMachine):
   initial_state = "RECEIVED"
   transitions = {
      "RECEIVED": frozenset({"QUEUED", "FAILED"}),
      "QUEUED": frozenset({"ACCEPTED", "FAILED"}),
      "ACCEPTED": frozenset({"EXECUTING", "FAILED"}),
      "EXECUTING": frozenset({"FORWARDED", "FAILED"}),
      "FORWARDED": frozenset(),
      "FAILED": frozenset(),
   }
