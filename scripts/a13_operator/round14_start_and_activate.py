# SPDX-License-Identifier: AGPL-3.0-only
"""Consume the exact compiled proposal, then explicitly accept both workers."""
import json,time
from concurrent.futures import ThreadPoolExecutor
from round14_rpc import owner,target,PRIVATE

def save(name,value):
    p=PRIVATE/name;p.write_text(json.dumps(value));p.chmod(0o600)

def main():
    deadline=time.monotonic()+1800
    while True:
        state=owner('/api/serving/status',{})
        phase=(state['compilation'] or {}).get('state')
        if phase=='compiled':break
        if phase=='failed' or time.monotonic()>deadline:raise RuntimeError('compiler_not_ready:'+str(state))
        time.sleep(1)
    proposal=state['compilation']['execution_proposal']
    result=owner('/api/serving/start',{'execution_proposal':proposal,'model_execution_acknowledged':True})
    save('registry-start.json',result);print('SERVING',result,flush=True)
    limits={'max_rss_bytes':proposal['max_combined_rss_bytes']//2,
        'max_disk_bytes':proposal['max_additional_disk_bytes']//2,
        'max_wall_seconds':proposal['max_wall_seconds'],'max_commands':5000}
    def preview_one(item):
        label,rpc=item;started=time.monotonic()
        until=started+300
        while rpc('/api/staging/check',{})['offer'] is None:
            if time.monotonic()>=until:raise RuntimeError('stage_offer_not_published')
            time.sleep(.25)
        preview=rpc('/api/work/preview',{'limits':limits,'stage_acknowledged':True})
        save(label+'-execution-preview.json',preview)
        print(label,'EXACT_PREVIEW',preview['consent_digest'],'seconds',round(time.monotonic()-started,3),flush=True)
        return label,rpc,preview
    def accept_one(item):
        label,rpc,preview=item;started=time.monotonic()
        result=rpc('/api/work/accept',{'limits':limits,'preview':preview,'work_acknowledged':True})
        save(label+'-accept-result.json',result)
        print(label,'WORK_ACCEPTED',result,'seconds',round(time.monotonic()-started,3),flush=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        previews=list(pool.map(preview_one,[('owner',owner),('target',target)]))
        list(pool.map(accept_one,previews))
    print('ACTIVATION',owner('/api/serving/activate',{'execution_proposal':proposal,'activation_acknowledged':True}),flush=True)
    while True:
        state=owner('/api/serving/status',{})
        if state['serving']['state'] in ('api_running','failed','stopped'):break
        if time.monotonic()>deadline:raise RuntimeError('serving_not_ready')
        time.sleep(1)
    save('serving-load-result.json',state);print('RESULT',state,flush=True)
    if state['serving']['state']!='api_running':raise RuntimeError('qualification_or_startup_failed')

if __name__=='__main__':main()
