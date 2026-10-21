# SPDX-License-Identifier: AGPL-3.0-only
"""A10 admission refusals: every guard, proven load-bearing by REMOVAL.

Frozen sources (read-only):
* ``tests/a10_acceptance/scenarios.v1.json`` -> ``runtime_batch_requirements``
* ``docs/superpowers/specs/2026-09-21-mycelium-a10-implementation-contract.md``
  (resolutions 1-3: the observation record, one real backend invocation, dispatch-mode
  honesty)

What this file proves
---------------------
The runtime-batch gate asks whether a claimed batch was computed **together in one
backend invocation** or silently looped.  A test that merely asserts "the refusal
happened" is weak: it cannot tell whether the guard is doing any work.  So for every
deterministic refusal path of ``mycelium_router/a10_batch_admission.py`` this suite:

1. asserts the guard text is still present and unique in the frozen module source
   (a renamed or deleted guard fails loudly here rather than silently passing);
2. drives the real path and asserts the exact coded refusal (the refusal itself);
3. **deletes that guard from an in-memory copy of the module** and asserts the
   pinned post-removal behaviour.  Either the coded refusal disappears (the guard was
   the only thing standing between the defect and a published observation), or a
   *different* independent guard refuses instead (defence in depth, pinned by code).
   A case whose pinned post-removal behaviour regresses fails this suite.

That third step is the mutation check: it is what makes each assertion able to fail.

Claim boundary
--------------
Deterministic, in-process, no hardware, no socket, no model artifact, no child
process.  Nothing here is physical, benchmark, browser, product or qualification
evidence.  ``EARNED_CLOSURE_REQUIREMENTS`` must stay empty and this file asserts it;
no closure requirement is claimed, and A10 stays ``design_only`` -- the physical
gates of specification section 10 are what can close it.
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

# The lane worktree must win the import race even when another test module already
# bound ``mycelium_router`` to the standing checkout (see tests/a10_deterministic).
ensure_worktree_imports()

import contextlib
import dataclasses
import json
import pathlib
import sys
import types

import pytest

import mycelium_router.a10_batch_admission as admission_module
import mycelium_router.a10_runtime_batch as scaffold
from mycelium_router.a10_batch_admission import (
   CLAIM_SOURCES,
   DISPATCH_MODES,
   A10AdmissionError,
   RuntimeBatchAdmission,
   evaluate_batch_claim,
)
from mycelium_router.a10_runtime_batch import (
   A10ContractError,
   BACKEND_INVOCATIONS_PER_OBSERVED_BATCH,
   MEMBERSHIP_AUTHORITY_STAGE_RUNTIME,
   MINIMUM_PHYSICAL_BATCH_MEMBERS,
   RuntimeBatchObservation,
)
from mycelium_router.contracts import RuntimeResult
from tests.a10_deterministic.doubles import BatchPort, member, policy

COLLECT_AT = 0.01
DISPATCH_AT = 0.011
RECORD_PROTOCOL = "mycelium.runtime_batch_observation.v1"
INVENTORY = pathlib.Path(__file__).resolve().parents[1] / "a10_acceptance" / "scenarios.v1.json"

ADMISSION_SOURCE = pathlib.Path(admission_module.__file__)
SCAFFOLD_SOURCE = pathlib.Path(scaffold.__file__)

_MUTANT_BODY = "pass  # MUTANT: guard removed by the mutation check"


# ===========================================================================
# Frozen inventory and the observation record
# ===========================================================================


def test_the_frozen_inventory_requirements_are_the_module_constants():
   """The module constants are the frozen inventory's numbers, read from the file."""

   requirements = json.loads(INVENTORY.read_text())["runtime_batch_requirements"]

   assert requirements["final_membership_authority"] == MEMBERSHIP_AUTHORITY_STAGE_RUNTIME
   assert requirements["final_membership_authority"] == "stage_runtime"
   assert requirements["minimum_physical_batch_members"] == MINIMUM_PHYSICAL_BATCH_MEMBERS
   assert requirements["minimum_physical_batch_members"] == 2
   assert (
      requirements["backend_invocations_per_observed_batch"]
      == BACKEND_INVOCATIONS_PER_OBSERVED_BATCH
   )
   assert requirements["backend_invocations_per_observed_batch"] == 1
   for disqualified in (
      "candidate_membership_alone_qualifies",
      "concurrent_admission_alone_qualifies",
      "sequential_loop_alone_qualifies",
   ):
      assert requirements[disqualified] is False
   assert scaffold.EARNED_CLOSURE_REQUIREMENTS == frozenset()
   assert RECORD_PROTOCOL in scaffold.DECLARED_CONTRACT_PROTOCOLS


def test_the_record_binds_the_frozen_contract_record_name():
   """A record name is the contract, so the record itself carries it."""

   assert RuntimeBatchObservation.PROTOCOL == RECORD_PROTOCOL
   assert RuntimeBatchObservation.PROTOCOL in scaffold.DECLARED_CONTRACT_PROTOCOLS


def _record(**overrides) -> RuntimeBatchObservation:
   values = {
      "batch_ref": "batch:1",
      "phase": "DECODE",
      "stage": "stage-000",
      "backend_invocation_id": "invocation:1",
      "backend_invocations": 1,
      "membership_authority": "stage_runtime",
      "proposed_member_count": 2,
      "actual_member_count": 2,
      "member_outcomes": (("member:a", "completed"), ("member:b", "completed")),
      "collected_at": 0.001,
      "executed_at": 0.002,
      "cleanup": "released",
   }
   values.update(overrides)
   return RuntimeBatchObservation(**values)


def test_the_record_is_produced_by_the_stage_runtime_with_one_outcome_per_actual_member():
   """The admission path publishes ``runtime_batch_observation.v1``, not a proposal."""

   port = BatchPort()
   admission = _two_member_admission(port)
   collection = admission.collect(now=COLLECT_AT)

   outcome = admission.dispatch(collection, now=DISPATCH_AT)
   observation = outcome.observation

   assert isinstance(observation, RuntimeBatchObservation)
   assert observation.PROTOCOL == RECORD_PROTOCOL
   assert observation.membership_authority == "stage_runtime"
   assert observation.backend_invocations == 1
   assert observation.proposed_member_count == 2
   assert observation.actual_member_count == 2
   assert len(observation.member_outcomes) == observation.actual_member_count
   assert observation.member_outcomes == (
      ("member:a", "completed"),
      ("member:b", "completed"),
   )
   assert observation.stage == "stage-000"
   assert observation.backend_invocation_id
   assert observation.cleanup == "released"
   assert observation.collected_at == collection.collected_at
   assert observation.executed_at == DISPATCH_AT
   assert observation.claims_runtime_batch() is True
   # The runtime's own record is the only evidence that counts.
   assert admission.observations() == (observation,)
   assert evaluate_batch_claim(
      source="stage_runtime_observation", observations=(observation,)
   ).qualifies is True


def test_the_observation_is_not_synthesized_when_less_than_a_physical_batch_ran():
   """Candidate membership is a proposal; fewer than two actual members never claims."""

   port = BatchPort()
   admission = _two_member_admission(port)
   admission.cancel("member:b", at="pre_launch", now=0.005)
   collection = admission.collect(now=COLLECT_AT)

   observation = admission.dispatch(collection, now=DISPATCH_AT).observation

   assert observation.proposed_member_count == 2
   assert observation.actual_member_count == 1
   assert observation.member_outcomes == (("member:a", "completed"),)
   assert observation.claims_runtime_batch() is False
   assert evaluate_batch_claim(
      source="stage_runtime_observation", observations=(observation,)
   ).qualifies is False


@pytest.mark.parametrize(
   "overrides,code",
   [
      ({"membership_authority": "relay_proposal"}, "invalid_membership_authority"),
      ({"membership_authority": "queue"}, "invalid_membership_authority"),
      ({"actual_member_count": 3}, "invalid_actual_member_count"),
      ({"backend_invocations": 0}, "invalid_backend_invocation_count"),
      (
         {"member_outcomes": (("member:a", "completed"),)},
         "invalid_member_outcome_count",
      ),
      (
         {
            "member_outcomes": (
               ("member:a", "completed"),
               ("member:a", "failed"),
            )
         },
         "duplicate_member_outcome",
      ),
      (
         {
            "member_outcomes": (
               ("member:a", "completed"),
               ("member:b", "batched"),
            )
         },
         "invalid_member_outcome",
      ),
      ({"executed_at": 0.0}, "invalid_observation_intervals"),
      ({"proposed_member_count": 0}, "invalid_proposed_member_count"),
      ({"cleanup": ""}, "invalid_observation_cleanup"),
   ],
   ids=[
      "relay_owns_membership",
      "queue_owns_membership",
      "more_actual_than_proposed",
      "zero_invocations",
      "missing_member_outcome",
      "duplicate_member_outcome",
      "unknown_member_outcome",
      "executed_before_collected",
      "zero_proposed",
      "unreported_cleanup",
   ],
)
def test_the_record_refuses_every_shape_that_cannot_be_a_stage_runtime_batch(overrides, code):
   """The record is the membership authority, so its own invariants fail closed."""

   with pytest.raises(A10ContractError) as raised:
      _record(**overrides)
   assert raised.value.code == code


def test_claims_runtime_batch_accepts_only_one_invocation_over_multiple_members():
   """The single predicate for ``one_real_backend_invocation_has_multiple_members``."""

   assert _record().claims_runtime_batch() is True
   assert (
      _record(
         actual_member_count=3,
         proposed_member_count=3,
         member_outcomes=(
            ("member:a", "completed"),
            ("member:b", "completed"),
            ("member:c", "completed"),
         ),
      ).claims_runtime_batch()
      is True
   )
   # One member is not a physical batch.
   assert (
      _record(actual_member_count=1, member_outcomes=(("member:a", "completed"),))
      .claims_runtime_batch()
      is False
   )
   # A split is not one invocation.
   assert _record(backend_invocations=2).claims_runtime_batch() is False
   # A relay-owned membership is unconstructible, let alone a claim.
   with pytest.raises(A10ContractError) as raised:
      _record(membership_authority="relay_proposal")
   assert raised.value.code == "invalid_membership_authority"


def test_a_zero_member_execution_is_refused_by_the_entry_point_and_never_claims():
   """Zero actual members is not a batch: refused where it matters, never qualifying.

   A degenerate port can report that it executed nothing.  The entry point refuses
   that outright (``empty_observed_batch``), and the record can represent it only as
   a non-claim, so a missing batch is never mistaken for a successful one.
   """

   from mycelium_router.a10_runtime_execution import execute_observed_batch

   port = BatchPort()
   admission = _two_member_admission(port)
   proposal = admission.collect(now=COLLECT_AT).proposal()

   with pytest.raises(A10ContractError) as raised:
      execute_observed_batch(ExecutionPort(executed=()), proposal)
   assert raised.value.code == "empty_observed_batch"

   zero = _record(actual_member_count=0, member_outcomes=())
   assert zero.claims_runtime_batch() is False
   verdict = evaluate_batch_claim(
      source="stage_runtime_observation", observations=(zero,)
   )
   assert verdict.qualifies is False
   assert verdict.reason == "observation_does_not_claim_runtime_batch"


# ===========================================================================
# The state-runtime dispatch double used by this file
# ===========================================================================


def _make_port(
   module,
   *,
   mode="runtime_batch",
   port_mode=None,
   invocations=1,
   refs=None,
   kv_ref_fn=None,
   incarnation="incarnation:1",
   authority="stage_runtime",
   arena=False,
   partial=(),
   nan_for=(),
   committed=None,
):
   """A dispatch double that reports exactly what the case asks it to report.

   ``module`` supplies the record classes so the same builder works for the real
   module and for a mutated in-memory copy of it.
   """

   kv_ref = kv_ref_fn or (lambda ref: f"kv:{ref}")

   class _Port:
      def dispatch_mode(self):
         return mode if port_mode is None else port_mode

      def execution_interval(self):
         return (0.0, 0.001)

      def execute_batch(self, request):
         names = (
            tuple(request.member_refs) if refs is None else tuple(refs(request.member_refs))
         )
         executed = tuple(
            module.MemberExecution(
               member_ref=name,
               kv_ref=kv_ref(name),
               result=RuntimeResult(
                  success=True,
                  payload=(
                     float("nan") if name in nan_for else f"out:{name}".encode()
                  ),
                  token_id=1000 + index,
               ),
               committed_prefix=(committed or {}).get(name, 0),
               event_bytes=(4,),
               partial_commit=name in partial,
            )
            for index, name in enumerate(names)
         )
         return module.BatchExecutionReport(
            dispatch_mode=mode,
            backend_invocations=invocations,
            membership_authority=authority,
            incarnation=incarnation,
            executed=executed,
            arena_open=arena,
         )

   return _Port()


def _module_policy(module):
   real = policy()
   return module.RuntimeBatchAdmissionPolicy(
      **{field.name: getattr(real, field.name) for field in dataclasses.fields(real)}
   )


def _module_member(module, member_ref, **overrides):
   real = member(member_ref, **overrides)
   return module.BatchMember(
      **{field.name: getattr(real, field.name) for field in dataclasses.fields(real)}
   )


def _two_member_admission(port, **policy_overrides) -> RuntimeBatchAdmission:
   admission = RuntimeBatchAdmission(
      policy=policy(**policy_overrides),
      port=port,
      incarnation="incarnation:1",
      stage_ref="stage-000",
   )
   receipt = admission.submit((member("member:a"), member("member:b")), now=0.0)
   assert receipt.accepted == ("member:a", "member:b")
   return admission


def _coded_refusal(exc: BaseException) -> str | None:
   """The coded refusal an A10 surface raises, from any of its modules."""

   code = getattr(exc, "code", None)
   return code if isinstance(code, str) else None


def _dispatch(module, port, *, members=("member:a", "member:b"), member_overrides=None):
   """Drive one collect+dispatch; return ``(code, outcome)`` with at most one set."""

   admission = module.RuntimeBatchAdmission(
      policy=_module_policy(module),
      port=port,
      incarnation="incarnation:1",
      stage_ref="stage-000",
   )
   overrides = member_overrides or {}
   admission.submit(
      tuple(_module_member(module, ref, **overrides.get(ref, {})) for ref in members),
      now=0.0,
   )
   collection = admission.collect(now=COLLECT_AT)
   try:
      outcome = admission.dispatch(collection, now=DISPATCH_AT)
   except BaseException as exc:  # noqa: BLE001 - classified below
      code = _coded_refusal(exc)
      if code is None:
         raise
      return code, None
   return None, outcome


def _summarize(outcome) -> str:
   observation = outcome.observation
   return (
      f"ACCEPTED actual={observation.actual_member_count} "
      f"invocations={observation.backend_invocations} "
      f"claims={observation.claims_runtime_batch()}"
   )


# ===========================================================================
# The mutation check: each guard is deleted and the outcome is pinned
# ===========================================================================


def test_a_member_the_runtime_never_saw_is_refused():
   """The port may not add a member to the batch it was offered."""

   code, outcome = _dispatch(
      admission_module,
      _make_port(admission_module, refs=lambda refs: tuple(refs) + ("member:ghost",)),
   )
   assert (code, outcome) == ("member_identity_mismatch_fails_closed", None)


def test_a_registered_member_that_was_not_offered_is_still_refused():
   """Being registered is not membership: the port may not smuggle a peer in.

   The premise is asserted, not assumed: ``member:c`` is a real, registered member
   that the collection excluded from this batch, so the runtime reporting it as an
   actual member is a defect the identity guard must refuse.
   """

   port = _make_port(admission_module, refs=lambda refs: tuple(refs) + ("member:c",))
   admission = RuntimeBatchAdmission(
      policy=policy(), port=port, incarnation="incarnation:1", stage_ref="stage-000"
   )
   admission.submit(
      (
         member("member:a"),
         member("member:b"),
         member("member:c", phase="PREFILL"),
      ),
      now=0.0,
   )
   assert admission.queue_depth() == 3
   collection = admission.collect(now=COLLECT_AT)
   assert collection.candidate_member_refs == ("member:a", "member:b")
   assert ("member:c", "incompatible_compatibility_key") in collection.excluded

   with pytest.raises(A10AdmissionError) as raised:
      admission.dispatch(collection, now=DISPATCH_AT)
   assert raised.value.code == "member_identity_mismatch_fails_closed"
   assert admission.observations() == ()


def _mutant_module(source_path: pathlib.Path, name: str, pairs) -> types.ModuleType:
   """An in-memory copy of a frozen module with ``pairs`` surgically removed."""

   source = source_path.read_text()
   for old, new in pairs:
      assert source.count(old) == 1, f"guard text is not unique in {source_path}:\n{old!r}"
      indent = old[: len(old) - len(old.lstrip())]
      replacement = new if new[:1] in (" ", "\t") else indent + new
      source = source.replace(old, replacement, 1)
   module = types.ModuleType(name)
   module.__file__ = str(source_path)
   exec(compile(source, str(source_path), "exec"), module.__dict__)
   return module


@contextlib.contextmanager
def _runtime_batch_swapped(mutated):
   """Let a mutated ``a10_runtime_batch`` be imported by a mutated admission copy."""

   key = "mycelium_router.a10_runtime_batch"
   saved = sys.modules.get(key)
   sys.modules[key] = mutated
   try:
      yield
   finally:
      if saved is None:
         sys.modules.pop(key, None)
      else:
         sys.modules[key] = saved


# Guard text, copied from the frozen module source.  The harness asserts each
# snippet is present and unique, so a renamed or deleted guard fails loudly here.
G_SEQUENTIAL = (
   '      if report.dispatch_mode == "sequential_dispatch":\n'
   '         _reject("sequential_dispatch_does_not_qualify")'
)
G_CONCURRENT = (
   '      if report.dispatch_mode == "member_concurrent_dispatch":\n'
   '         _reject("concurrent_admission_alone_does_not_qualify")'
)
G_NOT_A_BATCH = (
   "      if report.dispatch_mode not in DISPATCH_MODES:\n"
   '         _reject("invalid_dispatch_mode")\n'
   '      if report.dispatch_mode != "runtime_batch":\n'
   '         _reject("invalid_dispatch_mode")'
)
G_MODE_MISMATCH = (
   "      if self._port.dispatch_mode() != report.dispatch_mode:\n"
   '         _reject("dispatch_mode_mismatch_fails_closed")'
)
G_ARENA = '      if report.arena_open:\n         _reject("invocation_arena_escaped")'
G_AUTHORITY = (
   "      if report.membership_authority != MEMBERSHIP_AUTHORITY_STAGE_RUNTIME:\n"
   '         _reject("invalid_membership_authority")'
)
G_INCARNATION = (
   "      if report.incarnation != self._incarnation:\n"
   '         _reject("stale_incarnation_fails_closed")'
)
G_SPLIT = (
   "      if report.backend_invocations != BACKEND_INVOCATIONS_PER_OBSERVED_BATCH:\n"
   '         _reject("split_backend_invocations_fail_closed")'
)
G_UNKNOWN_MEMBER = (
   "      if any(member_ref not in allowed for member_ref in executed_refs):\n"
   '         _reject("member_identity_mismatch_fails_closed")'
)
G_MISORDERED = (
   "      if executed_refs != [ref for ref in allowed if ref in set(executed_refs)]:\n"
   '         _reject("member_identity_mismatch_fails_closed")'
)
G_PEER_KV = (
   "         if identity is None or identity.kv_ref != execution.kv_ref:\n"
   '            _reject("cross_member_kv_isolation_fails_closed")'
)
G_NONFINITE = (
   "         result = execution.result\n"
   "         if isinstance(result.payload, float) and not math.isfinite(result.payload):\n"
   '            _reject("nonfinite_member_output_fails_closed")'
)
G_PARTIAL = (
   "      if any(execution.partial_commit for execution in report.executed):\n"
   '         _reject("partial_kv_commit_fails_closed")'
)
G_CLAIM_CANDIDATE = (
   '   if source == "candidate_membership":\n'
   '      return BatchClaimVerdict(False, "candidate_membership_alone_does_not_qualify")'
)
G_CLAIM_CONCURRENT = (
   '   if source == "concurrent_admission":\n'
   '      return BatchClaimVerdict(False, "concurrent_admission_alone_does_not_qualify")'
)
G_CLAIM_SEQUENTIAL = (
   '   if source == "sequential_loop":\n'
   '      return BatchClaimVerdict(False, "sequential_loop_alone_does_not_qualify")'
)


@dataclasses.dataclass(frozen=True)
class RefusalCase:
   """One refusal path, its guard, and what removing the guard does."""

   name: str
   code: str
   guard: tuple[str, ...]
   port: dict
   members: tuple[str, ...] = ("member:a", "member:b")
   member_overrides: dict = dataclasses.field(default_factory=dict)
   claim_source: str | None = None
   after: str | None = None  # None -> accepted; else the rescuing code
   after_summary: str = ""


def _cases() -> tuple[RefusalCase, ...]:
   return (
      RefusalCase(
         name="split_invocation",
         code="split_backend_invocations_fail_closed",
         guard=(G_SPLIT,),
         port={"invocations": 2},
         after_summary="ACCEPTED actual=2 invocations=2 claims=False",
      ),
      RefusalCase(
         name="stale_incarnation",
         code="stale_incarnation_fails_closed",
         guard=(G_INCARNATION,),
         port={"incarnation": "incarnation:stale"},
         after_summary="ACCEPTED actual=2 invocations=1 claims=True",
      ),
      RefusalCase(
         name="misordered_member",
         code="member_identity_mismatch_fails_closed",
         guard=(G_MISORDERED,),
         port={"refs": lambda refs: tuple(reversed(refs))},
         after_summary="ACCEPTED actual=2 invocations=1 claims=True",
      ),
      RefusalCase(
         name="unknown_member_never_offered",
         code="member_identity_mismatch_fails_closed",
         guard=(G_UNKNOWN_MEMBER, G_MISORDERED, G_PEER_KV),
         port={"refs": lambda refs: tuple(refs) + ("member:ghost",)},
         after="unknown_member_kv_identity",
      ),
      RefusalCase(
         name="unknown_member_registered_but_unoffered",
         code="member_identity_mismatch_fails_closed",
         guard=(G_UNKNOWN_MEMBER, G_MISORDERED),
         port={"refs": lambda refs: tuple(refs) + ("member:c",)},
         members=("member:a", "member:b", "member:c"),
         member_overrides={"member:c": {"phase": "PREFILL"}},
         after="invalid_actual_member_count",
      ),
      RefusalCase(
         name="result_bound_to_a_peers_kv_identity",
         code="cross_member_kv_isolation_fails_closed",
         guard=(G_PEER_KV,),
         port={"kv_ref_fn": lambda ref: "kv:peer-of-" + ref},
         after="cross_member_kv_access_fails_closed",
      ),
      RefusalCase(
         name="escaped_invocation_arena",
         code="invocation_arena_escaped",
         guard=(G_ARENA,),
         port={"arena": True},
         after_summary="ACCEPTED actual=2 invocations=1 claims=True",
      ),
      RefusalCase(
         name="partial_kv_commit",
         code="partial_kv_commit_fails_closed",
         guard=(G_PARTIAL,),
         port={"partial": ("member:b",), "committed": {"member:a": 4, "member:b": 4}},
         after_summary="ACCEPTED actual=2 invocations=1 claims=True",
      ),
      RefusalCase(
         name="nonfinite_member_output",
         code="nonfinite_member_output_fails_closed",
         guard=(G_NONFINITE,),
         port={"nan_for": ("member:b",)},
         after_summary="ACCEPTED actual=2 invocations=1 claims=True",
      ),
      RefusalCase(
         name="sequential_dispatch",
         code="sequential_dispatch_does_not_qualify",
         guard=(G_SEQUENTIAL, G_NOT_A_BATCH, G_SPLIT),
         port={"mode": "sequential_dispatch", "invocations": 2},
         after_summary="ACCEPTED actual=2 invocations=2 claims=False",
      ),
      RefusalCase(
         name="member_concurrent_dispatch",
         code="concurrent_admission_alone_does_not_qualify",
         guard=(G_CONCURRENT, G_NOT_A_BATCH, G_SPLIT),
         port={"mode": "member_concurrent_dispatch", "invocations": 2},
         after_summary="ACCEPTED actual=2 invocations=2 claims=False",
      ),
      RefusalCase(
         name="dispatch_mode_mismatch",
         code="dispatch_mode_mismatch_fails_closed",
         guard=(G_MODE_MISMATCH,),
         port={"mode": "runtime_batch", "port_mode": "member_concurrent_dispatch"},
         after_summary="ACCEPTED actual=2 invocations=1 claims=True",
      ),
      RefusalCase(
         name="candidate_membership_alone",
         code="candidate_membership_alone_does_not_qualify",
         guard=(G_CLAIM_CANDIDATE,),
         port={},
         claim_source="candidate_membership",
         after_summary="qualifies=True",
      ),
      RefusalCase(
         name="concurrent_admission_alone",
         code="concurrent_admission_alone_does_not_qualify",
         guard=(G_CLAIM_CONCURRENT,),
         port={},
         claim_source="concurrent_admission",
         after_summary="qualifies=True",
      ),
      RefusalCase(
         name="sequential_loop_alone",
         code="sequential_loop_alone_does_not_qualify",
         guard=(G_CLAIM_SEQUENTIAL,),
         port={},
         claim_source="sequential_loop",
         after_summary="qualifies=True",
      ),
   )


CASES = _cases()


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_every_refusal_path_still_refuses_and_its_guard_is_load_bearing(case):
   """Real path refuses with the pinned code; removing the guard changes that."""

   # 1. The guard is present and unique in the frozen source (see _mutant_module).
   source = ADMISSION_SOURCE.read_text()
   for guard in case.guard:
      assert source.count(guard) == 1, f"{case.name}: guard text drifted:\n{guard!r}"

   # 2. The real path refuses, with the exact code.
   if case.claim_source is not None:
      real_verdict = evaluate_batch_claim(source=case.claim_source)
      assert real_verdict.qualifies is False
      assert real_verdict.reason == case.code
      real_code = case.code
      flip = case.guard[0].replace("(False,", "(True,")
      mutant = _mutant_module(ADMISSION_SOURCE, admission_module.__name__, [(case.guard[0], flip)])
      after_verdict = mutant.evaluate_batch_claim(source=case.claim_source).qualifies
      payload = f"{case.name}: real={real_code} -> removed=qualifies={after_verdict}"
      print(f"[mutation] {payload}")
      assert after_verdict is True, "the verdict flip is not observable: the test cannot fail"
      return

   real_code, real_outcome = _dispatch(
      admission_module,
      _make_port(admission_module, **case.port),
      members=case.members,
      member_overrides=case.member_overrides,
   )
   assert real_outcome is None
   assert real_code == case.code

   # 3. Remove the guard(s) and pin what happens: the refusal disappears, or a
   #    different independent guard refuses instead.
   pairs = [(guard, _MUTANT_BODY) for guard in case.guard]
   mutant = _mutant_module(ADMISSION_SOURCE, admission_module.__name__, pairs)
   after_code, after_outcome = _dispatch(
      mutant,
      _make_port(mutant, **case.port),
      members=case.members,
      member_overrides=case.member_overrides,
   )
   if case.after is None:
      assert after_code is None, f"{case.name}: still refused with {after_code}"
      assert after_outcome is not None
      summary = _summarize(after_outcome)
      assert summary == case.after_summary, f"{case.name}: {summary}"
      payload = f"{case.name}: real={real_code} -> removed={case.after_summary}"
   else:
      assert after_code == case.after, f"{case.name}: expected rescue {case.after}, got {after_code}"
      assert after_outcome is None
      payload = f"{case.name}: real={real_code} -> removed={after_code} (defence in depth)"
   print(f"[mutation] {payload}")
   assert payload


def test_the_mutation_harness_cannot_pass_by_mistake():
   """A guard text that is absent, or a mutation that changes nothing, must fail loudly."""

   with pytest.raises(AssertionError) as raised:
      _mutant_module(ADMISSION_SOURCE, admission_module.__name__, [("this guard does not exist", "pass")])
   assert "guard text is not unique" in str(raised.value)

   mutant = _mutant_module(ADMISSION_SOURCE, admission_module.__name__, [(G_SPLIT, _MUTANT_BODY)])
   assert mutant.BACKEND_INVOCATIONS_PER_OBSERVED_BATCH == BACKEND_INVOCATIONS_PER_OBSERVED_BATCH
   assert mutant.DISPATCH_MODES == DISPATCH_MODES
   assert mutant.CLAIM_SOURCES == CLAIM_SOURCES
   assert issubclass(mutant.A10AdmissionError, A10ContractError)
   # The mutant is in memory only: the frozen module on disk is untouched.
   assert _MUTANT_BODY not in ADMISSION_SOURCE.read_text()
   # The mutant shares the frozen base error class, so refusal codes stay comparable.
   assert mutant.A10ContractError is A10ContractError


def test_the_record_itself_refuses_a_relay_owned_membership_without_the_admission_guard():
   """Frozen requirement ``final_membership_authority`` is guarded twice over.

   Removing the admission-level authority guard alone is NOT observable by the code
   (the record refuses with the identical code), so this case removes both the
   admission guard and the record's own invariant to show the frozen requirement is
   what makes the difference.  The rescued-then-accepted observation still cannot
   claim a batch.
   """

   record_guard = (
      "      if self.membership_authority != MEMBERSHIP_AUTHORITY_STAGE_RUNTIME:\n"
      '         _reject("invalid_membership_authority")'
   )
   assert SCAFFOLD_SOURCE.read_text().count(record_guard) == 1

   # The record-level guard alone refuses, with the frozen code.
   with pytest.raises(A10ContractError) as raised:
      _record(membership_authority="relay_proposal")
   assert raised.value.code == "invalid_membership_authority"

   # Admission guard only -> still refused, with the identical code (not observable).
   admission_only = _mutant_module(
      ADMISSION_SOURCE, admission_module.__name__, [(G_AUTHORITY, _MUTANT_BODY)]
   )
   code, outcome = _dispatch(
      admission_only, _make_port(admission_only, authority="relay_proposal")
   )
   assert (code, outcome) == ("invalid_membership_authority", None)

   # Both guards removed -> the observation is published, and it cannot claim a batch.
   mutated_record = _mutant_module(
      SCAFFOLD_SOURCE, scaffold.__name__, [(record_guard, _MUTANT_BODY)]
   )
   with _runtime_batch_swapped(mutated_record):
      both_removed = _mutant_module(
         ADMISSION_SOURCE, admission_module.__name__, [(G_AUTHORITY, _MUTANT_BODY)]
      )
   code, outcome = _dispatch(
      both_removed,
      _make_port(both_removed, authority="relay_proposal"),
   )
   assert code is None
   assert outcome is not None
   observation = outcome.observation
   assert observation.membership_authority == "relay_proposal"
   assert observation.claims_runtime_batch() is False
   print(
      "[mutation] relay_owned_membership_authority: "
      f"real=invalid_membership_authority -> removed="
      f"ACCEPTED actual={observation.actual_member_count} "
      f"invocations={observation.backend_invocations} claims=False"
   )
   # The real module was restored.
   assert sys.modules["mycelium_router.a10_runtime_batch"] is scaffold
   assert RuntimeBatchObservation is scaffold.RuntimeBatchObservation


# ===========================================================================
# Reachability: the frozen contract entry point reaches the admission path
# ===========================================================================


class ExecutionPort:
   """The dispatch surface ``execute_observed_batch`` calls, reporting honestly."""

   def __init__(self, *, executed=None) -> None:
      self.mode = "runtime_batch"
      self.incarnation = "incarnation:1"
      self.requests: list = []
      self._executed = executed

   def dispatch_mode(self) -> str:
      return self.mode

   def execution_interval(self) -> tuple[float, float]:
      return (COLLECT_AT, DISPATCH_AT)

   def execute_batch(self, request):
      from mycelium_router.a10_batch_admission import BatchExecutionReport, MemberExecution

      self.requests.append(request)
      refs = (
         tuple(request.member_refs) if self._executed is None else tuple(self._executed)
      )
      return BatchExecutionReport(
         dispatch_mode=self.mode,
         backend_invocations=1,
         membership_authority="stage_runtime",
         incarnation=self.incarnation,
         executed=tuple(
            MemberExecution(
               member_ref=ref,
               kv_ref=f"kv:{ref}",
               result=RuntimeResult(success=True, payload=f"out:{ref}".encode(), token_id=index),
               committed_prefix=1,
               event_bytes=(4,),
            )
            for index, ref in enumerate(refs)
         ),
      )


def test_the_frozen_entry_point_reaches_the_admission_path_and_publishes_the_record():
   """``execute_observed_batch`` -> the admission records -> the frozen observation.

   Failure mode caught: the contract's declared entry point stops reaching the
   admission path's records (a removed delegation, a renamed record, a record that
   stops being stage-runtime owned, or a claim that no longer needs the observation).
   """

   from mycelium_router.a10_runtime_execution import ObservedBatchExecution, execute_observed_batch

   port = BatchPort()
   admission = _two_member_admission(port)
   proposal = admission.collect(now=COLLECT_AT).proposal()
   assert isinstance(proposal, scaffold.RuntimeBatchProposal)

   execution_port = ExecutionPort()
   result = execute_observed_batch(execution_port, proposal)

   assert isinstance(result, ObservedBatchExecution)
   observation = result.observation
   assert isinstance(observation, RuntimeBatchObservation)
   assert observation.PROTOCOL == RECORD_PROTOCOL
   assert observation.membership_authority == "stage_runtime"
   assert observation.backend_invocations == 1
   assert observation.proposed_member_count == 2
   assert observation.actual_member_count == 2
   assert observation.member_outcomes == (
      ("member:a", "completed"),
      ("member:b", "completed"),
   )
   assert observation.claims_runtime_batch() is True
   assert result.claims_runtime_batch is True
   assert result.claim.qualifies is True
   assert result.claim.reason == "stage_runtime_observation_owns_membership"
   # One invocation, over exactly the offered members, and a mode label that never
   # travels without its evidence.
   assert [request.member_refs for request in execution_port.requests] == [
      proposal.member_refs
   ]
   assert result.member_refs == proposal.member_refs
   assert observation.backend_invocation_id
   assert result.dispatch_mode in DISPATCH_MODES
