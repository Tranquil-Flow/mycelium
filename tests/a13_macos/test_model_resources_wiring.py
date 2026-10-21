# SPDX-License-Identifier: AGPL-3.0-only
"""The per-model, per-split resource proposal is what the app enforces and shows.

Wiring of model_resources into: storage consent bounds, the pairing quota cap,
per-member worker limits (disk + memory/load-peak guard), the standing-consent
caps of a small joining Mac, the slice-fetch D-1 rule, the member-side refusal
and the owner's run error message.
"""
import pytest

from mycelium_a13_macos import model_resources as R
from mycelium_a13_macos.serving import (CANDIDATE_MODEL, QWEN3_0_6B_MODEL, QWEN3_8B_MODEL, QWEN3_14B_MODEL,
                                        RESOURCE_PROPOSAL)

GiB = 1 << 30
POLICY = {'peer_visibility': True, 'power': 'ac_only', 'network': 'unmetered_only',
          'background': False, 'diagnostics': False, 'thermal': 'stop_on_serious'}


# -- model contract ----------------------------------------------------------

def test_each_contract_carries_its_own_proposal():
    assert CANDIDATE_MODEL.resource_proposal == RESOURCE_PROPOSAL
    big = QWEN3_14B_MODEL.resource_proposal
    assert big.additional_disk_bytes >= 2 * 29_536_614_400  # a whole-model member, runner halves it
    assert big.max_combined_rss_bytes > RESOURCE_PROPOSAL.max_combined_rss_bytes
    assert QWEN3_0_6B_MODEL.resource_proposal.max_combined_rss_bytes < RESOURCE_PROPOSAL.max_combined_rss_bytes


# -- storage consent and pairing quota ----------------------------------------

def test_storage_consent_can_express_the_14b_owner_requirement():
    from mycelium_a13_macos.app import consent_valid
    need = R.owner_storage_requirement(QWEN3_14B_MODEL)
    assert need == 32_507_875_154
    assert consent_valid({**POLICY, 'storage_bytes': need})
    assert not consent_valid({**POLICY, 'storage_bytes': R.storage_consent_ceiling() + 1})
    assert consent_valid({**POLICY, 'storage_bytes': 23_052_975_062})  # the 7B seeded path


def test_pairing_quota_cap_follows_the_largest_member_share():
    from mycelium_a13_macos.app import pairing_quota_valid
    cap = R.pairing_quota_ceiling()
    assert pairing_quota_valid({'storage_bytes': cap, 'network_bytes_per_day': cap})
    assert not pairing_quota_valid({'storage_bytes': cap + 1, 'network_bytes_per_day': 1})
    assert not pairing_quota_valid({'storage_bytes': 1, 'network_bytes_per_day': cap + 1})
    assert pairing_quota_valid({'storage_bytes': 8_000_000_000, 'network_bytes_per_day': 8_000_000_000})


def test_model_picker_defaults_to_14b_and_shows_the_enforced_requirement(tmp_path):
    from mycelium_a13_macos import run_model
    assert run_model.storage_ceiling() == R.storage_consent_ceiling()
    assert run_model.demo_default_choice() == 'qwen3-14b'
    root = tmp_path / 'state'; root.mkdir(mode=0o700)
    doc = run_model.document(root, environ={})
    rows = {c['name']: c for c in doc['choices']}
    assert rows['qwen3-14b']['owner_storage_bytes'] == 32_507_875_154
    assert rows['qwen3-14b']['fits_storage_ceiling'] is True
    assert doc['resources']['storage_consent_ceiling_bytes'] == R.storage_consent_ceiling()


# -- D-1 slice rule ------------------------------------------------------------

def test_slice_fetch_needs_a_registered_resource_derivation():
    from mycelium_a13_macos.serving import slice_fetch_permitted
    assert slice_fetch_permitted(29_536_665_640, architecture='qwen3', model=QWEN3_14B_MODEL)

    class Unknown:
        model_id, resolved_commit = 'Qwen/Qwen3-4B', '1' * 40
    assert not slice_fetch_permitted(8_000_000_000, architecture='qwen3', model=Unknown())
    assert not slice_fetch_permitted(15_231_271_888, architecture='qwen2', model=CANDIDATE_MODEL)


# -- per-member limits on the owner's run --------------------------------------

def _plan():
    return R.plan_for_members(QWEN3_14B_MODEL, [{'node_id': 'node-a', 'memory_bytes': 48 * GiB},
                                                {'node_id': 'node-b', 'memory_bytes': 8 * GiB}],
                              owner_node_id='node-a')


def test_run_exchange_carries_per_member_limits_and_resources():
    from mycelium_a13_macos.run_exchange import RunExchange

    class Mailbox:
        route_extensions = None
    exchange = RunExchange(Mailbox(), clock=lambda: 1.0)
    rows = {r['node_id']: r for r in _plan()['members']}
    per_node = {n: {'work_limits': R.work_limits_for(r), 'resources': R.public_member_resources(r)}
                for n, r in rows.items()}
    exchange.announce(phase='work', run_id='r', deployment_id='d', work_limits={'max_rss_bytes': 1},
                      per_node=per_node)
    view = exchange.announcement('node-b')
    assert view['work_limits']['max_rss_bytes'] == rows['node-b']['max_rss_bytes'] == 5_427_065_085
    assert view['member_resources']['max_disk_bytes'] == 3_969_910_631
    assert exchange.announcement('node-z')['work_limits'] == {'max_rss_bytes': 1}
    with pytest.raises(ValueError, match='demo_announcement_invalid'):
        exchange.announce(phase='work', per_node={'node-b': {'resources': {'surprise': 1}}})


def test_owner_member_work_limits_come_from_the_compiled_plan(tmp_path):
    from mycelium_a13_macos.demo_chain import member_work_limits
    plan = _plan()
    state = {'resource_plan': {'members': plan['members']}}
    assert member_work_limits(state, 'node-a', storage_bytes=40_000_000_000) == {
        'max_rss_bytes': 32_621_085_758, 'max_disk_bytes': 28_677_232_180,
        'max_wall_seconds': 1800, 'max_commands': 5000}
    assert member_work_limits(state, 'node-a', storage_bytes=10_000_000_000)['max_disk_bytes'] == 10_000_000_000
    assert member_work_limits({}, 'node-a', storage_bytes=1) is None


def test_7b_work_limits_are_unchanged():
    from mycelium_a13_macos.demo_chain import member_work_limits
    split = {'stages': [{'node_id': 'laptop', 'start': 0, 'end': 14, 'stage_bytes': 7_615_612_928},
                        {'node_id': 'm4pro', 'start': 14, 'end': 28, 'stage_bytes': 7_615_620_096}],
             'members': []}
    rows = R.member_resources(CANDIDATE_MODEL, split, slice_mode=False)
    limits = member_work_limits({'resource_plan': {'members': rows}}, 'm4pro', storage_bytes=24_854_140_006)
    assert limits['max_rss_bytes'] == RESOURCE_PROPOSAL.max_combined_rss_bytes // 2
    assert limits['max_disk_bytes'] == RESOURCE_PROPOSAL.additional_disk_bytes // 2


def test_compile_refuses_a_member_that_cannot_hold_its_share_with_a_message():
    from mycelium_a13_macos.chain_compile import member_resource_plan
    split = {'stages': [{'node_id': 'node-a', 'start': 0, 'end': 30, 'stage_bytes': 21_000_000_000},
                        {'node_id': 'node-b', 'start': 30, 'end': 40, 'stage_bytes': 8_536_614_400}],
             'members': [{'node_id': 'node-a', 'memory_bytes': 48 * GiB},
                         {'node_id': 'node-b', 'memory_bytes': 8 * GiB}]}
    with pytest.raises(R.ResourceRefused) as error:
        member_resource_plan(QWEN3_14B_MODEL, split, reports={}, owner_node_id='node-a')
    assert (error.value.code, error.value.node_id) == ('member_memory_insufficient_for_share', 'node-b')
    plan = member_resource_plan(QWEN3_14B_MODEL, _plan()['split'], reports={}, owner_node_id='node-a')
    assert plan['proposal'] == _plan()['proposal']
    assert [m['node_id'] for m in plan['members']] == ['node-a', 'node-b']


def test_owner_run_error_carries_a_human_message():
    from mycelium_a13_macos.owner_run import OwnerRunError
    error = OwnerRunError('member_memory_cap_below_share', message='The Mac needs 5.4 GB.')
    assert error.code == 'member_memory_cap_below_share' and error.message == 'The Mac needs 5.4 GB.'
    assert OwnerRunError('x_y').message is None


# -- joining Mac: standing caps and refusal ------------------------------------

def test_standing_memory_cap_is_bounded_by_the_devices_ram(tmp_path):
    from mycelium_a13_macos.standing_consent import (DISCLOSURE_DIGEST, StandingConsentError,
                                                     grant_standing_consent)
    policy = {**POLICY, 'storage_bytes': 10_000_000_000}

    def body(memory):
        return {'acknowledged': True, 'disclosure_digest': DISCLOSURE_DIGEST,
                'caps': {'storage_bytes': 8_000_000_000, 'max_memory_bytes': memory,
                         'max_wall_seconds': 1800, 'max_commands': 5000}}
    with pytest.raises(StandingConsentError, match='standing_consent_memory_exceeds_device'):
        grant_standing_consent(tmp_path, body(8 * GiB), policy=policy, now=1.0, device_memory_bytes=8 * GiB)
    grant = grant_standing_consent(tmp_path, body(6 * GiB), policy=policy, now=1.0,
                                   device_memory_bytes=8 * GiB)
    assert grant['caps']['max_memory_bytes'] == 6 * GiB


def test_standing_acceptor_runs_under_the_members_derived_limits(tmp_path):
    from mycelium_a13_macos import standing_consent as S
    caps = {'storage_bytes': 8_000_000_000, 'max_memory_bytes': 6 * GiB, 'max_wall_seconds': 1800,
            'max_commands': 5000}
    required = {'max_rss_bytes': 5_427_065_085, 'max_disk_bytes': 3_969_910_631,
                'max_wall_seconds': 1800, 'max_commands': 5000}
    limits = S.standing_limits({'caps': caps}, required=required)
    assert (limits.max_rss_bytes, limits.max_disk_bytes) == (5_427_065_085, 3_969_910_631)
    assert S.standing_limits({'caps': caps}).max_rss_bytes == 6 * GiB
    with pytest.raises(S.StandingConsentError, match='member_memory_cap_below_share'):
        S.standing_limits({'caps': {**caps, 'max_memory_bytes': 5 * GiB}}, required=required)
    with pytest.raises(S.StandingConsentError, match='member_storage_cap_below_share'):
        S.standing_limits({'caps': {**caps, 'storage_bytes': 3_000_000_000}}, required=required)


def test_standing_acceptor_refuses_an_offer_beyond_the_caps_by_name(tmp_path):
    from mycelium_a13_macos import standing_consent as S
    policy = {**POLICY, 'storage_bytes': 10_000_000_000}
    grant = S.grant_standing_consent(tmp_path, {
        'acknowledged': True, 'disclosure_digest': S.DISCLOSURE_DIGEST,
        'caps': {'storage_bytes': 8_000_000_000, 'max_memory_bytes': 5 * GiB, 'max_wall_seconds': 1800,
                 'max_commands': 5000}}, policy=policy, now=1.0)
    binding = {'swarm_id': 's', 'node_id': 'node-b', 'seed_key_digest': 'k'}
    S.bind_standing_consent(tmp_path, binding, now=2.0)

    class Actor:
        def pending_stage(self):
            return {'offer_id': 'o-1', 'target_node_id': 'node-b', 'archive_bytes': 10, 'run_id': 'r'}
    seen = []
    acceptor = S.StandingAcceptor(tmp_path, preview=lambda *a, **k: seen.append(k) or {},
                                  limits_for=lambda offer: {'max_rss_bytes': 5_427_065_085,
                                                            'max_disk_bytes': 3_969_910_631})
    result = acceptor.tick(Actor(), policy=policy, binding=binding, resource_check=lambda: None)
    assert result == {'offer_id': 'o-1', 'state': 'refused', 'reason': 'member_memory_cap_below_share'}
    assert seen == [] and grant['state'] == 'granted_unbound'


def test_joined_mac_refuses_a_share_beyond_its_caps_before_fetching(tmp_path, monkeypatch):
    from mycelium_a13_macos.demo_target import DemoTargetAgent

    class Actor:
        member = type('M', (), {'node_id': 'node-b'})()
        peer = type('P', (), {'state_root': tmp_path / 'target'})()
        def pending_stage(self): return None
        def pending_cleanup(self): return None
        def status(self): return {'native_control': {'state': 'stopped'}}

    class App:
        root = tmp_path; authority = Actor(); local_contributor = None
        def action(self, path, body): raise AssertionError(path)

    class Gate:
        def allows(self, app, action): return None
        def storage_cap(self, app): return 8_000_000_000
        def member_caps(self, app): return {'storage_bytes': 8_000_000_000, 'max_memory_bytes': 5 * GiB}

    (tmp_path / 'target').mkdir()
    monkeypatch.setattr('mycelium_a13_macos.installation_descriptor.installation_descriptor',
                        lambda app, **k: {'statement': {'target_node_id': 'node-b'}})
    row = {r['node_id']: r for r in _plan()['members']}['node-b']
    base = {'protocol': 'mycelium.a13_demo_run_announcement.v1', 'run_id': 'a13-aa-qwen-20261013T150000Z',
            'deployment_id': 'dep-1', 'model': {'model_id': QWEN3_14B_MODEL.model_id,
                                                'resolved_commit': QWEN3_14B_MODEL.resolved_commit},
            'slice_mode': True, 'manifest_digest': 'sha256:' + '1' * 64, 'issued_at': 1.0,
            'bootstrap_offer_id': None}
    state = {'a': {**base, 'phase': 'collect', 'work_limits': None, 'node_slice_plan': None}}
    reports = []
    agent = DemoTargetAgent(App(), consent=Gate(), source=lambda: state['a'], sink=reports.append,
                            report_interval=0)
    agent.tick()
    state['a'] = {**base, 'phase': 'work', 'work_limits': R.work_limits_for(row),
                  'member_resources': R.public_member_resources(row),
                  'node_slice_plan': {'node_id': 'node-b', 'node_plan_digest': 'sha256:' + '4' * 64}}
    status = agent.tick()
    assert (status['state'], status['detail']) == ('error', 'member_memory_cap_below_share')
    assert agent._slice_thread is None  # nothing fetched
    assert reports[-1]['detail'] == 'member_memory_cap_below_share'
