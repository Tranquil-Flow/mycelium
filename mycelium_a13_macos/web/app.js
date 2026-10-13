'use strict';
const $ = (id) => document.getElementById(id);
let busy = false;
let connected = false;
let latest = null;
let reviewedRequest = null;
let stageOffer = null;
async function api(path, body) {
  const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST',
    cache:'no-store',credentials:'same-origin',redirect:'error',referrerPolicy:'no-referrer',
    headers:body === undefined ? {} : {'Content-Type':'application/json','X-Mycelium-Action':'1'},
    body:body === undefined ? undefined : JSON.stringify(body)});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'request_failed');
  return value;
}
function notice(text) { $('notice').textContent=text; }
function rung(label, value) {
  const dt=document.createElement('dt'); dt.textContent=label;
  const dd=document.createElement('dd'); dd.textContent=String(value ?? 'Unknown');
  $('ladder').append(dt,dd);
}
function render(value) {
  latest=value; connected=true; $('private').hidden=false;
  const a=value.authority;
  const m=value.model_contract;
  $('model-contract').textContent=m ? `${m.model_id}\nRevision ${m.resolved_commit}\n${m.runtime_backend} · ${m.runtime_dtype} · ${m.runtime_quantization}\n${m.decoding} · seed ${m.sampling_seed}\nSelected contract only — no model loaded` : 'Model contract unavailable';
  const target=value.installation_role==='target';
  $('onboarding-title').textContent=target?(a.membership_state==='current'?'This Mac joined a swarm':'Join this Mac to a swarm'):'Create or join a swarm';
  $('installation-role').textContent=target?'Target role · this Mac owns its identity, not the swarm seed. Stop locally to disconnect; the owner manages revocation.':
    (a.swarm_created?'Owner role · your installation owns this swarm authority.':'Choose one role for this installation.');
  $('staging').hidden=!target;
  $('cleanup').hidden=!target;
  $('check-cleanup').disabled=busy || !value.running || a.membership_state!=='current';
  $('accept-cleanup').disabled=busy || !cleanupOffer || !$('cleanup-consent').checked || !value.running || a.membership_state!=='current' || a.native_control?.state==='running';
  const control=a.native_control;
  $('control-state').textContent=control ? `Native control: ${control.state} · model load ${control.model_load_authorized ? 'authorized' : 'not authorized'}` : 'Native control: stopped';
  $('start-control').disabled=busy || !stageOffer || !$('control-consent').checked || !value.running || a.membership_state!=='current' || control?.state==='running';
  $('stop-control').disabled=busy || !control || control.state==='stopped';
  $('check-stage').disabled=busy || !value.running || a.membership_state!=='current';
  $('accept-stage').disabled=busy || !stageOffer || !$('stage-consent').checked || !value.running || a.membership_state!=='current';
  $('create').hidden=target;
  $('join').hidden=target && a.membership_state==='current';
  $('revoke-consent').hidden=target;
  if(value.resource_policy){
    for(const key of ['power','network','storage']){
      $(key).value=String(value.resource_policy[key==='storage'?'storage_bytes':key]);
      $(key).disabled=true;
    }
  }
  $('create').disabled=target || !!a.swarm_created;
  $('join').disabled=!!a.swarm_created || a.membership_state==='current';
  if(a.swarm_created || a.resource_use_acknowledged)$('visibility').checked=true;
  $('pair-target').hidden=!target;
  $('pair-owner').hidden=target || !a.swarm_created;
  $('pair-approve').disabled=busy || !reviewedRequest || !$('pair-confirm').checked;
  $('revoke').disabled=target || a.membership_state!=='current';
  $('capability-report').disabled=!value.running || a.membership_state!=='current';
  $('ladder').replaceChildren();
  rung('Application',value.running ? 'Started · foreground only' : 'Stopped · private state retained');
  rung('Membership',a.membership_state ?? a.membership ?? a.state ?? 'Not joined');
  rung('Generation',a.membership_generation ?? a.generation);
  rung('Lease',a.lease_state ?? a.lease_expires_at ?? 'Unknown');
  const capabilityStates={self_report_accepted:'Self-report accepted · not independently qualified',
    not_reported:'Not reported',stale:'Stale · report again'};
  rung('Capability',capabilityStates[a.capability_state] ?? 'Not independently qualified');
  if(a.capability)rung('Observed runtime',`${a.capability.backends.join(', ')} · ${a.capability.precisions.join(', ')}`);
  rung('Assignment',a.assignment_state ?? 'Unassigned');
  rung('Load & challenge','Not yet exercised');
  rung('Qualification & selection',value.serving.state);
  $('serving-state').textContent=value.role.replaceAll('_',' ');
  $('serving-action').textContent=value.serving.next_action;
  const acquisition=value.acquisition;
  const downloading=acquisition && !['idle','prepared','failed','cancelled','closed'].includes(acquisition.state);
  const canDownload=value.running && a.membership_state==='current' && !!value.resource_policy;
  $('download-weights').disabled=busy || !canDownload || downloading || acquisition?.state==='prepared' || !$('download-consent').checked;
  $('cancel-download').disabled=busy || !downloading;
  $('download-consent').disabled=busy || downloading;
  $('download-limit').disabled=busy || downloading;
  if(acquisition){
    $('acquisition-state').textContent=`Source: ${acquisition.state.replaceAll('_',' ')}${acquisition.error_code ? ' · '+acquisition.error_code.replaceAll('_',' ') : ''}${value.acquisition_policy_error ? ' · '+value.acquisition_policy_error.replaceAll('_',' ') : ''}`;
    $('acquisition-bytes').textContent=`${acquisition.downloaded_bytes.toLocaleString()} / ${acquisition.total_bytes.toLocaleString()} bytes · ${acquisition.state==='prepared' ? 'source verified; no model loaded' : 'not source-ready'}`;
    $('acquisition-progress').max=Math.max(1,acquisition.total_bytes);
    $('acquisition-progress').value=acquisition.downloaded_bytes;
  }
  const observation=value.resource_observation;
  $('resource-observation').textContent=observation ? `Observed power: ${observation.power} · thermal: ${observation.thermal} · network: ${observation.network}` : 'Resource observations unavailable';
  renderJoinFlow(value,target,a);
  $('members').replaceChildren();
  for (const member of a.members ?? []) {
    const row=document.createElement('div'); row.className='member';
    const label=document.createElement('span');
    label.textContent=`${member.label ?? member.node_id ?? member.member_id} · generation ${member.generation ?? member.membership_generation} · ${member.lifecycle_state ?? member.state ?? 'unknown'}`;
    const button=document.createElement('button'); button.type='button';button.className='secondary';button.textContent='Revoke member';
    button.addEventListener('click',()=>{
      if (!window.confirm('Revoke this membership? It cannot accept future work. Local identity and evidence are retained.')) return;
      void perform('/api/revoke',{node_id:member.node_id ?? member.member_id,generation:member.generation ?? member.membership_generation},'Membership revoked.');
    });
    row.append(label,button);$('members').append(row);
  }
}
async function refresh(announce=true) {
  try {
    render(await api('/api/status'));
    if (announce) notice('Connected to your private local installation. Serving remains separately qualified.');
  } catch(error) {
    if (error.message==='open_from_native_launcher') {
      connected=false;$('private').hidden=true;clearPairing();$('output').textContent='';
      notice('This browser is not authorized. Open the application’s private launch file to begin. No pairing or inference data is available here.');
    } else {
      connected=false;latest=null;$('private').hidden=true;
      clearPairing();$('output').textContent='';
      notice('Connection unavailable. No state is assumed current. Reconnect from the launcher; private identity remains on your Mac.');
    }
  }
}
async function perform(path, body, success) {
  if(busy)return;
  busy=true;
  for(const button of document.querySelectorAll('button'))button.disabled=true;
  try {const result=await api(path,body);await refresh(false);notice(success);return result;}
  catch(error){notice(`Action not completed: ${error.message.replaceAll('_',' ')}. Existing identity and evidence are retained.`);}
  finally{
    busy=false;
    for(const button of document.querySelectorAll('button'))button.disabled=false;
    $('submit').disabled=true;
    if(latest&&connected)render(latest);
  }
}
$('create-form').addEventListener('submit',async(event)=>{
  event.preventDefault();
  const join=event.submitter?.id==='join';
  const result=await perform(join?'/api/join/request':'/api/create',{consent:{peer_visibility:$('visibility').checked,power:$('power').value,
    storage_bytes:Number($('storage').value),network:$('network').value,background:false,diagnostics:false,
    thermal:'stop_on_serious'}},join?'Target identity created. Send the public request to the owner. No seed was created on this Mac.':
    'Owner authority created and this Mac enrolled. Membership alone does not enable serving.');
  if(join && result && connected)showRequest(result);
});
$('control-consent').addEventListener('change',()=>{if(latest&&connected)render(latest);});
$('start-control').addEventListener('click',()=>{
  if(!stageOffer||!$('control-consent').checked)return;
  void perform('/api/control/start',{offer_id:stageOffer.offer_id,
    archive_digest:stageOffer.archive_digest,control_acknowledged:true},'Bounded native control started. Model loading remains denied.');
});
$('stop-control').addEventListener('click',()=>void perform('/api/control/stop',{},'Native control stopped; identity and staged evidence retained.'));
let cleanupOffer=null;
$('check-cleanup').addEventListener('click',async()=>{
  cleanupOffer=null;$('cleanup-consent').checked=false;$('cleanup-offer').textContent='';
  const result=await perform('/api/staging/cleanup/check',{},'Cleanup request checked. No files removed.');
  if(result&&connected){
    cleanupOffer=result.offer;
    $('cleanup-offer').textContent=cleanupOffer ? `Run ${cleanupOffer.run_id}\n${cleanupOffer.archive_digest}` : 'No current cleanup request.';
    if(latest)render(latest);
  }
});
$('cleanup-consent').addEventListener('change',()=>{if(latest&&connected)render(latest);});
$('accept-cleanup').addEventListener('click',async()=>{
  if(!cleanupOffer||!$('cleanup-consent').checked)return;
  const result=await perform('/api/staging/cleanup/accept',{offer_id:cleanupOffer.offer_id,archive_digest:cleanupOffer.archive_digest,cleanup_acknowledged:true},'Staging removed; identity and recovery evidence retained.');
  if(result&&connected){
    cleanupOffer=null;stageOffer=null;$('cleanup-consent').checked=false;$('cleanup-offer').textContent='';
    $('cleanup-state').textContent='Staging removed · identity and recovery evidence retained';
    $('stage-state').textContent='Staged run removed';
    if(latest)render(latest);
  }
});
$('check-stage').addEventListener('click',async()=>{
  stageOffer=null;$('stage-consent').checked=false;$('stage-offer').textContent='';$('stage-state').textContent='';
  const result=await perform('/api/staging/check',{},'Staging offers checked. No execution authorized.');
  if(result&&connected){
    stageOffer=result.offer;
    $('stage-offer').textContent=stageOffer ? `${stageOffer.archive_bytes.toLocaleString()} bytes\n${stageOffer.archive_digest}\nRun ${stageOffer.run_id}\nExpires ${new Date(stageOffer.expires_at*1000).toISOString()}` : 'No current staging offer.';
    if(latest)render(latest);
  }
});
$('stage-consent').addEventListener('change',()=>{if(latest&&connected)render(latest);});
$('accept-stage').addEventListener('click',async()=>{
  if(!stageOffer||!$('stage-consent').checked)return;
  const result=await perform('/api/staging/accept',{offer_id:stageOffer.offer_id,
    archive_digest:stageOffer.archive_digest,stage_acknowledged:true},'Staging verified. Model execution remains separately authorized.');
  if(result&&connected){$('stage-state').textContent='Staged and verified · no model loaded';$('stage-consent').checked=false;if(latest)render(latest);}
});
$('download-consent').addEventListener('change',()=>{if(latest&&connected)render(latest);});
$('download-weights').addEventListener('click',()=>{
  if(!$('download-consent').checked || !latest?.resource_policy)return;
  const max=Number($('download-limit').value);
  if(!Number.isSafeInteger(max) || max<=0){notice('Choose a positive whole-byte download limit.');return;}
  void perform('/api/model/download',{consent:{download_acknowledged:true,
    max_download_bytes:max,storage_limit_bytes:latest.resource_policy.storage_bytes}},
    'Source acquisition requested. Progress comes from the worker; model execution remains separate.');
});
$('cancel-download').addEventListener('click',()=>void perform('/api/model/cancel',{},'Download cancellation requested; partial files are retained for explicit retry.'));
$('refresh').addEventListener('click',()=>void refresh());
$('capability-report').addEventListener('click',()=>void perform('/api/capability/report',{},
  'Capability and relay link re-reported through signed HTTPS. Reports are also sent automatically on join and before each run. This is not model or physical qualification.'));
$('start').addEventListener('click',()=>void perform('/api/start',{},'Reconnected using retained identity.'));
$('stop').addEventListener('click',()=>void perform('/api/stop',{},'Stopped. Identity, recovery and evidence retained.'));
$('quit').addEventListener('click',()=>void perform('/api/quit',{},'Launcher stopped; identity and evidence retained.'));
$('revoke').addEventListener('click',()=>{
  if (!$('revoke-confirm').checked) { notice('Confirm that you want to revoke this Mac before continuing.'); return; }
  void perform('/api/revoke-self',{confirm:true},'This Mac is revoked. It cannot renew or resume this membership. Private identity and evidence are retained.');
});
$('pair-request').addEventListener('click',async()=>{
  if(busy)return;
  try {
    const value=await api('/api/pair/request',{});
    showRequest(value);
  }catch(error){notice(`Pairing request not completed: ${error.message.replaceAll('_',' ')}.`);}
});
$('infer-form').addEventListener('submit',(event)=>event.preventDefault());
function showRequest(value){
  $('pairing-details').open=true;
  $('pair-output').textContent=JSON.stringify(value,null,2);$('pair-output').hidden=false;
  $('pair-note').textContent='Public identity-bound request only. No swarm credential. Send this to the owner.';
}
function clearPairing(){
  cleanupOffer=null;$('cleanup-consent').checked=false;$('cleanup-offer').textContent='';$('cleanup-state').textContent='';
  $('control-consent').checked=false;
  stageOffer=null;$('stage-offer').textContent='';$('stage-state').textContent='';$('stage-consent').checked=false;
  reviewedRequest=null;$('pair-confirm').checked=false;
  for(const id of ['pair-output','pair-identity','handoff-output'])$(id).textContent='';
  for(const id of ['pair-request-input','handoff-input'])$(id).value='';
}
$('pair-request-input').addEventListener('input',()=>{
  reviewedRequest=null;$('pair-confirm').checked=false;$('pair-approve').disabled=true;
  $('pair-identity').textContent='';
});
$('pair-confirm').addEventListener('change',()=>{
  $('pair-approve').disabled=busy || !reviewedRequest || !$('pair-confirm').checked;
});
$('pair-preview').addEventListener('click',async()=>{
  if(busy)return;
  reviewedRequest=null;$('pair-confirm').checked=false;$('pair-approve').disabled=true;
  try{
    const candidate=JSON.parse($('pair-request-input').value);
    const value=await perform('/api/pair/preview',{request:candidate},'Verify this identity with the target Mac before approving.');
    if(value && connected){
      reviewedRequest=candidate;
      $('pair-identity').textContent=`Target: ${value.node_id}\nIdentity fingerprint: ${value.identity_key_digest}\nClass: ${value.peer_class}\nRequest expires: ${new Date(value.expires_at*1000).toISOString()}`;
    }
  }catch(_){notice('Invalid public pairing request. No invitation created.');}
});
$('pair-approve').addEventListener('click',async()=>{
  if(!reviewedRequest || !$('pair-confirm').checked)return;
  const result=await perform('/api/pair/approve',{request:reviewedRequest,
    ttl_seconds:Number($('pair-ttl').value),quota:{storage_bytes:Number($('pair-storage').value),
    network_bytes_per_day:Number($('pair-network').value),background_allowed:false}},
    'Return this recipient-encrypted invitation to the target Mac. Never share a plaintext invite.');
  if(result && connected){$('handoff-output').textContent=JSON.stringify(result,null,2);$('handoff-output').hidden=false;}
});
$('accept-handoff').addEventListener('click',async()=>{
  try{
    const handoff=JSON.parse($('handoff-input').value);
    const result=await perform('/api/pair/accept',{handoff},'Joined through signed HTTPS. Membership is not serving qualification.');
    if(result){$('handoff-input').value='';$('pair-output').textContent='';}
  }catch(_){notice('Invalid encrypted invitation. Existing identity is retained.');}
});
function gb(bytes){return `${(bytes/1e9).toFixed(1)} GB`;}
function renderJoinFlow(value,target,a){
  const flow=value.join_flow ?? {};
  const owner=!target;
  $('invite-owner').hidden=!owner;
  $('join-target').hidden=owner && !!a.swarm_created;
  $('relay-start').hidden=!owner || !!a.swarm_created || value.network?.state!=='private';
  $('window-open').disabled=busy || !owner || !a.swarm_created || !value.network?.public_origin;
  $('standing-text').textContent=flow.standing_disclosure?.text ?? '';
  const joined=a.membership_state==='current';
  const waiting=!!flow.join_window;
  $('join-with-code').disabled=busy || joined || waiting || !$('standing-confirm').checked || !$('visibility').checked;
  $('join-cancel').hidden=!waiting;
  $('join-fingerprint').textContent=waiting?`Waiting for the owner. Check that the owner sees this fingerprint: ${flow.join_window.fingerprint}`:'';
  const standing=flow.standing_consent;
  $('standing-withdraw').hidden=!target || !standing || standing.state==='withdrawn';
  $('standing-state').textContent=standing?`Standing consent: ${standing.state.replaceAll('_',' ')}${standing.last?` · last offer ${standing.last.state}${standing.last.reason?' ('+standing.last.reason.replaceAll('_',' ')+')':''}`:''}${flow.last_report?` · last report ${flow.last_report.purpose} ${flow.last_report.state}`:''}`:'';
  $('windows').replaceChildren();
  for(const w of flow.pairing_windows ?? []){
    const row=document.createElement('div');row.className='member';
    const label=document.createElement('pre');
    const expires=new Date(w.expires_at*1000).toLocaleTimeString();
    let text=`Code ${w.window_id} · ${w.state} · expires ${expires}`;
    if(w.short_code)text+=`\nType on the joining Mac: ${w.tunnel_name} ${w.short_code}\nOr paste: ${w.join_link}`;
    const p=w.pending;
    if(p)text+=`\nMac ${p.node_id} · fingerprint ${p.fingerprint}\n${p.device.chip} · ${gb(p.device.memory_bytes)} RAM · ${gb(p.device.available_storage_bytes)} free · MLX ${p.device.mlx.available?'yes':'no'} · power ${p.device.power} · thermal ${p.device.thermal}\nRelay link: ${p.link.rtt_ms_median} ms RTT · ${(p.link.upload_bytes_per_second/1e6).toFixed(1)} MB/s up · ${(p.link.download_bytes_per_second/1e6).toFixed(1)} MB/s down`;
    label.textContent=text;row.append(label);
    if(p && w.state==='claimed'){
      const approve=document.createElement('button');approve.type='button';approve.textContent='Approve this Mac';
      approve.addEventListener('click',()=>void perform('/api/pair/window/approve',{window_id:w.window_id,request_digest:p.request_digest},'Approved. The encrypted invitation returns to that Mac automatically.'));
      row.append(approve);
    }
    if(['open','claimed','approved'].includes(w.state)){
      const revoke=document.createElement('button');revoke.type='button';revoke.className='secondary';revoke.textContent='Close code';
      revoke.addEventListener('click',()=>void perform('/api/pair/window/revoke',{window_id:w.window_id},'Code closed. It can no longer be used.'));
      row.append(revoke);
    }
    $('windows').append(row);
  }
}
function currentConsent(){
  return {peer_visibility:$('visibility').checked,power:$('power').value,storage_bytes:Number($('storage').value),
    network:$('network').value,background:false,diagnostics:false,thermal:'stop_on_serious'};
}
$('relay-start').addEventListener('click',()=>{
  if(!window.confirm('Start a temporary Cloudflare quick tunnel? The relay terminates HTTPS and can observe control traffic; inference stays on Iroh.'))return;
  void perform('/api/network/start',{relay_acknowledged:true,max_seconds:1800},'Public relay started. Now create the swarm, then invite Macs.');
});
$('window-open').addEventListener('click',()=>void perform('/api/pair/window/open',{ttl_seconds:Number($('window-ttl').value)},'Join code opened. Show it to one person; approve their Mac when it appears.'));
$('standing-confirm').addEventListener('change',()=>{if(latest&&connected)render(latest);});
$('join-with-code').addEventListener('click',()=>{
  const disclosure=latest?.join_flow?.standing_disclosure;
  if(!disclosure || !$('standing-confirm').checked)return;
  void perform('/api/join/window',{consent:currentConsent(),join:$('join-code').value,standing_consent:{acknowledged:true,
    disclosure_digest:disclosure.digest,caps:{storage_bytes:Number($('storage').value),max_memory_bytes:Number($('standing-memory').value),
    max_wall_seconds:Number($('standing-wall').value),max_commands:5000}}},
    'Request sent through the owner’s relay. Waiting for one-click approval.');
});
$('join-cancel').addEventListener('click',()=>void perform('/api/join/window/cancel',{},'Join cancelled on this Mac.'));
$('standing-withdraw').addEventListener('click',()=>{
  if(!window.confirm('Withdraw standing consent? Automatic acceptance and running work stop now. Membership is kept.'))return;
  void perform('/api/consent/standing/withdraw',{confirm:true},'Standing consent withdrawn. Running work stopped.');
});
let joinPolling=false;
setInterval(async()=>{
  if(busy||!connected||joinPolling||!latest?.join_flow?.join_window)return;
  joinPolling=true;
  try{const value=await api('/api/join/window/poll',{});if(value.state==='joined'){notice('Joined. Capability and relay link reported to the owner automatically.');}await refresh(false);}
  catch(error){notice(`Join not completed: ${error.message.replaceAll('_',' ')}.`);await refresh(false);}
  finally{joinPolling=false;}
},3000);
window.addEventListener('pageshow',()=>void refresh(false));
window.addEventListener('online',()=>void refresh());
void refresh();
setInterval(()=>{if(!busy&&connected)void refresh(false);},5000);
