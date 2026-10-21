# SPDX-License-Identifier: AGPL-3.0-only
"""Execute the consented two-Mac bootstrap via actual application APIs.

Relay reachability is the one step here that can fail for a reason that is NOT a
product defect, and both measured modes are absorbed in operator tooling rather
than by weakening the product's signed card check:

1. **A freshly created quick-tunnel hostname is not published immediately.** The
   laptop's system resolver returns `gaierror` for `*.trycloudflare.com` names
   (measured 2026-09-15), so the product's DoH fallback is on the critical path
   for every request; until Cloudflare publishes the new name the DoH answer is
   NXDOMAIN and the peer reports `seed_http_unreachable`. Four attempts over
   ~12 s was not enough. Reach failures are now retried for a bounded time
   *inside the card's signed validity*.

2. **A rotation while a previous reconnect failed leaves the peer behind.** The
   card must describe the transition THAT PEER made, but `relocate_authority`
   mints `previous_origin` from the owner's own record. A peer one rotation
   behind then rejects every card with `endpoint_recovery_invalid`, hidden behind
   a generic `action_not_completed`. When a rotation is required the card is
   re-minted through the existing `/api/network/recovery-card` `previous_origin`
   parameter (already supported by the endpoint-recovery API, no schema change).

By default the already-published origin is reused, which needs no rotation, no
extra publication window, and keeps the peer's stored origin inside the card.

The peer's stored origin is read over the operator SSH channel only for the
rotation path. It is not a swarm credential, the peer's public onboarding
projection deliberately omits the seed origin, and the product path never needs
it -- the open integration item is for the peer to supply this itself (round41
recommendation 1).
"""
from pathlib import Path
import json,time,uuid,sys,os,subprocess
from datetime import datetime,timezone
from round14_rpc import owner,target,PRIVATE,REMOTE
# This module is also loaded by file path: tests/a13_macos/test_reachability_gate.py
# stubs round14_rpc and never adds scripts/a13_operator to sys.path. Make the sibling
# policy importable either way rather than assuming script invocation.
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
# Retry policy lives in round14_retry_policy so it is testable without this
# module's ssh/rpc imports; TRANSIENT_REACH is re-exported for existing callers.
from round14_retry_policy import RETRYABLE,TRANSIENT_REACH,is_retryable,matching_codes
# The card is signed for 300 s; stay well inside it while a new origin publishes.
RECONNECT_BUDGET_SECONDS=180.0

def save(name,value):
    p=PRIVATE/name;p.write_text(json.dumps(value));p.chmod(0o600)

def peer_seed_origin():
    """Operator-side read of the peer's own stored seed origin (rotation path only)."""
    script=('import json;print(json.load(open(%r))["seed_url"])' % (REMOTE+'/target/target/membership.state.json'))
    try:
        out=subprocess.run(['ssh','-o','BatchMode=yes','member-mac','/usr/bin/python3','-c',script],
            capture_output=True,text=True,timeout=30)
    except Exception:
        return None
    value=out.stdout.strip()
    return value if out.returncode==0 and value.startswith('https://') else None

def bootstrap_relay(observations):
    """Reach the owner's seed at a published origin; rotate only when asked."""
    if os.environ.get('A13_ROTATE_RELAY')=='1':
        result=owner('/api/network/restart',{'relay_acknowledged':True,'max_seconds':1800})
        origin=peer_seed_origin()
        if origin is not None:
            minted=owner('/api/network/recovery-card',{'relay_acknowledged':True,'previous_origin':origin})
            result={**result,'recovery_card':minted['recovery_card']}
        # A missing peer origin is not fatal: the owner-minted card still works
        # whenever the peer's stored origin is the owner's previous origin.
        observations.append({'event':'relay_rotated','peer_origin_known':origin is not None})
        return result
    minted=owner('/api/network/recovery-card',{'relay_acknowledged':True})
    observations.append({'event':'published_origin_reused','rotation':False})
    return {'network':owner('/api/status').get('network'),'recovery_card':minted['recovery_card']}

def _dump_failed_attempts(observations, *, name='reconnect-attempts.json'):
    """Persist retry observations BEFORE a budget-exhausted raise.

    Measured 2026-09-18: the soak01 round exhausted the 180 s reconnect budget on
    `local_api_http_409:seed_http_remote_error` from the TARGET's app (the laptop could not reach the seed
    origin), and the observations -- attempts, elapsed_seconds, codes -- were LOST, because they are
    persisted only on the success path. So the one failure whose DURATION matters most, the one that decides
    whether the budget is right, left no record of how long the relay stayed unreachable. A failure that
    cannot be measured cannot size its own budget.
    """
    try:
        root = os.environ.get('A13_EVIDENCE_DIR') or os.environ.get('A13_OWNER_ROOT') or '.'
        path = os.path.join(root, name)
        with open(path, 'w') as handle:
            json.dump({'protocol': 'mycelium.a13.reconnect_attempts.v1',
                       'reconnect_budget_seconds': RECONNECT_BUDGET_SECONDS,
                       'attempts': observations}, handle, indent=1)
        print('reconnect_attempts_written %s attempts=%d' % (path, len(observations)), flush=True)
    except BaseException as exc:
        # never mask the reason the round died: this is evidence, not a control path
        print('reconnect_attempts_dump_failed %r' % (exc,), flush=True)


def reconnect_target(card,observations):
    """Bounded retry while the relay origin publishes, inside the card's validity."""
    started=time.monotonic();attempts=0;delay=2.0
    while True:
        attempts+=1
        try:
            target('/api/network/reconnect',{'card':card,'relay_acknowledged':True})
            observations.append({'attempt':attempts,'outcome':'connected',
                'elapsed_seconds':round(time.monotonic()-started,3)})
            return
        except RuntimeError as error:
            text=str(error)
            codes=matching_codes(text)
            observations.append({'attempt':attempts,'outcome':'failed','transient':bool(codes),
                'codes':codes,'error':text[:400],'elapsed_seconds':round(time.monotonic()-started,3)})
            if not is_retryable(text,budget_exhausted=time.monotonic()-started>=RECONNECT_BUDGET_SECONDS):
                # persist the attempts BEFORE raising, so a budget exhaustion is measurable
                _dump_failed_attempts(observations)
                raise
            time.sleep(delay);delay=min(15.0,delay*1.6)


# Idempotent reads that change nothing (staging status/cleanup reads) are safe to
# repeat, and the relay origin can briefly stop answering for ANY of them -- not
# only during publication.  Measured 2026-09-15: the final cleanup read of an
# otherwise complete run failed with `relay_dns_unavailable` on a tunnel that was
# provably healthy seconds later.  The product now retries a negative DoH answer
# inside the connection; this is the operator-side backstop for a longer window.
IDEMPOTENT_READ_BUDGET_SECONDS=45.0

def idempotent_read(call,observations,*,label):
    started=time.monotonic();attempts=0;delay=2.0
    while True:
        attempts+=1
        try:
            value=call()
            if attempts>1:
                observations.append({'event':'idempotent_read_retried','label':label,
                    'attempts':attempts,'elapsed_seconds':round(time.monotonic()-started,3)})
            return value
        except RuntimeError as error:
            text=str(error)
            transient=any(code in text for code in TRANSIENT_REACH) or 'unreachable' in text
            if not transient or time.monotonic()-started>=IDEMPOTENT_READ_BUDGET_SECONDS:
                raise
            time.sleep(delay);delay=min(10.0,delay*1.6)


# The peer's control channel is the thing a C4/C5 run actually needs, and it is
# NOT proven by the owner's own local health.  Measured 2026-09-15: the peer
# resolved its reconnect once, then its post-rotation origin went unreachable and
# it claimed nothing at all for the rest of the run -- 114/114 laptop `health`
# commands and the final `stop` expired unclaimed against 117/117 `completed` on
# the local node.  A run that dies there has spent its one bounded attempt for
# nothing, so the peer's own read of the owner is gated before the chain is
# driven.
#
# The instrument must be the PRODUCT's read over its own DoH path, never `curl`:
# this laptop cannot resolve `*.trycloudflare.com` through system DNS at all
# (20/20 probes returned `000`), and the product uses Cloudflare HTTPS DNS for
# exactly that reason.  `/api/staging/check` is that read: a non-mutating peer
# request that fetches this peer's stage offer from the owner's seed, issued over
# the same relay/DoH path the control pump uses afterwards.
REACHABILITY_GATE_CONSECUTIVE=2
REACHABILITY_GATE_INTERVAL_SECONDS=3.0
REACHABILITY_GATE_BUDGET_SECONDS=120.0

def assert_retained_acquisition(status,*,where):
    """ERRATA-1: a resumed round must PROVE the seeded total, not merely claim 'prepared'.

    The 2026-09-16 lesson (runs/wp8-02-useful-two-mac/ERRATA-1.md): a partial or silently
    re-fetched stage can reach the state name 'prepared' while the total is NOT present, and the
    round then proceeds on an incomplete model -- a failure that costs a physical attempt and looks
    like something else entirely. Assert the two quantities that make 'prepared' mean anything, and
    fail loudly WITH THE NUMBERS so the next occurrence reads off disk.
    """
    acq=((status or {}).get('acquisition') or {})
    state=acq.get('state')
    downloaded=acq.get('downloaded_bytes')
    total=acq.get('total_bytes')
    if state!='prepared':
        raise RuntimeError('retained_acquisition_not_prepared:%s:%s'%(where,state))
    if not isinstance(downloaded,int) or isinstance(downloaded,bool) \
        or not isinstance(total,int) or isinstance(total,bool) or total<=0:
        raise RuntimeError('retained_acquisition_unmeasured:%s:%r/%r'%(where,downloaded,total))
    if downloaded!=total:
        raise RuntimeError('retained_acquisition_incomplete:%s:%d<%d'%(where,downloaded,total))
    return acq


def peer_owner_read(observations,*,label):
    """Require bounded, repeated non-mutating peer->owner reads to succeed."""
    started=time.monotonic();streak=0;attempts=0;delay=REACHABILITY_GATE_INTERVAL_SECONDS
    while True:
        attempts+=1
        try:
            value=target('/api/staging/check',{})
            streak+=1
            observations.append({'event':'reachability_gate_read','label':label,
                'attempt':attempts,'streak':streak,'outcome':'ok',
                'elapsed_seconds':round(time.monotonic()-started,3)})
            if streak>=REACHABILITY_GATE_CONSECUTIVE:
                return value
        except RuntimeError as error:
            text=str(error)
            streak=0
            transient=any(code in text for code in TRANSIENT_REACH) or 'unreachable' in text
            observations.append({'event':'reachability_gate_read','label':label,
                'attempt':attempts,'streak':0,'outcome':'failed','transient':transient,
                'detail':text[-200:],'elapsed_seconds':round(time.monotonic()-started,3)})
            if not transient or time.monotonic()-started>=REACHABILITY_GATE_BUDGET_SECONDS:
                raise RuntimeError('peer_reachability_gate_failed:'+label+':'+text[-200:])
        time.sleep(delay)

def main():
    observations=[]
    result=bootstrap_relay(observations)
    save('current-recovery.json',result['recovery_card'])
    try:
        reconnect_target(result['recovery_card'],observations)
    finally:
        save('relay-reconnect-observations.json',{'observations':observations,
            'reconnect_budget_seconds':RECONNECT_BUDGET_SECONDS})
    owner('/api/local-contributor/start',{})
    for rpc in (owner,target):
        s=rpc('/api/status')
        if s['acquisition']['state']!='prepared':
            # D-1 7B small-assets consent: bounds bytes fetched THIS attempt, so a complete
            # seed completes and a missing seed refuses (runs/wp8-02-g1/RECEIPT.md section 3.2).
            rpc('/api/model/download',{'consent':{'download_acknowledged':True,
                'max_download_bytes':11_535_382,'storage_limit_bytes':s['resource_policy']['storage_bytes']}})
    until=time.monotonic()+120
    while True:
        states=[rpc('/api/status')['acquisition']['state'] for rpc in (owner,target)]
        if states==['prepared','prepared']:
            # Retained-state resume must be PROVEN, not assumed (ERRATA-1): a node reporting
            # 'prepared' must also show the seeded total, not merely a state name.
            for label,rpc in (('owner',owner),('target',target)):
                assert_retained_acquisition(rpc('/api/status'),where='resume:'+label)
            break
        if any(s in ('failed','cancelled') for s in states) or time.monotonic()>until:
            raise RuntimeError('source_preparation_not_ready:'+str(states))
        time.sleep(1)
    scope={'run_id':'a13-qwen-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
        'deployment_id':str(uuid.uuid4())}
    save('current-scope.json',scope)
    cards=[rpc('/api/installation/descriptor',{**scope,'disclosure_acknowledged':True}) for rpc in (owner,target)]
    # v2 (2026-09-16): the canonical compile order is lexicographic node id
    # (build_qwen_live_route.py:847-851; placement_order_authority=m14_measured_cycle is the
    # only override and is not used here). The (owner,target) fetch order is not canonical when
    # the node ids reverse-sort, and the app inserts facts in descriptor order
    # (chain_observation.py:43-50), so sort by statement.target_node_id before prepare.
    cards=sorted(cards,key=lambda card:card['statement']['target_node_id'])
    print(owner('/api/serving/observe/prepare',{**scope,'descriptors':cards,'bootstrap_acknowledged':True}),flush=True)
    # The peer's own read of the owner must be proven before the chain is driven:
    # everything below mutates peer state (stage accept, native start, compile),
    # and a peer that cannot reach the rotated origin claims nothing at all.
    peer_owner_read(observations,label='post-prepare:peer-staging-read')
    for label,rpc in [('owner',owner),('target',target)]:
        offer=idempotent_read(lambda rpc=rpc:rpc('/api/staging/check',{}),observations,
            label='staging/check:'+label)['offer']
        intent={'offer_id':offer['offer_id'],'archive_digest':offer['archive_digest']}
        print(label,'stage',rpc('/api/staging/accept',{**intent,'stage_acknowledged':True}),flush=True)
        print(label,'native',rpc('/api/control/start',{**intent,'control_acknowledged':True}),flush=True)
    observation=owner('/api/serving/observe/collect',{'observe_acknowledged':True})
    save('current-observations.json',observation)
    print('OBSERVED',observation,flush=True)
    for label,rpc in [('owner',owner),('target',target)]:
        rpc('/api/control/stop',{})
        offer=idempotent_read(lambda rpc=rpc:rpc('/api/staging/cleanup/check',{}),observations,
            label='cleanup/check:'+label)['offer']
        assert offer is not None
        rpc('/api/staging/cleanup/accept',{'offer_id':offer['offer_id'],'archive_digest':offer['archive_digest'],'cleanup_acknowledged':True})
    save('relay-reconnect-observations.json',{'observations':observations,
        'reconnect_budget_seconds':RECONNECT_BUDGET_SECONDS,
        'idempotent_read_budget_seconds':IDEMPOTENT_READ_BUDGET_SECONDS})
    print('COMPILE',owner('/api/serving/compile',{'model_preparation_acknowledged':True}),flush=True)
    # Second gate immediately before the chain leaves step 1: the peer's read must
    # still succeed here, so a channel that died after prepare cannot be mistaken
    # for a step-2 problem.
    peer_owner_read(observations,label='pre-compile:peer-staging-read')

if __name__=='__main__':main()
