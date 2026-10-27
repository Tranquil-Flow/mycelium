// SPDX-License-Identifier: AGPL-3.0-only
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
  if (!response.ok) {const error = new Error(value.error || 'request_failed'); error.userMessage = value.message; error.fix = value.fix; throw error;}
  return value;
}
const ADAPTIVE_TEXT={off:'Off. Nothing is reduced or paused automatically; your memory limit, disk floor, power/heat/network limits, Pause sharing and Stop still apply.',
  manual:'Adaptive sharing is off; Pause sharing and Stop are ready. Your memory limit, disk floor and power/heat/network limits still apply.',
  active:'On · normal. New work is accepted within your limits.',reduced:'On · reduced. Taking fewer new requests while this Mac is busy.',
  drain:'No new work: draining, finishing, handing off or ending running work.',withdraw:'Stopped. Model memory released; no work from this Mac.',
  error:'The sharing policy could not be read. New work is refused until you turn it off or on again.'};
const ADAPTIVE_REASON={owner_game_mode:'you paused sharing',owner_stop:'you pressed Stop',host_memory_pressure:'memory is low',
  host_cpu_saturation:'the CPU is busy',resource_thermal_policy_violation:'this Mac is hot',resource_power_policy_violation:'the battery rules apply',none:''};
// Derived Paused -> Rejoining (connecting route) -> Sharing; "Sharing" only once the route is ready.
const SHARING_TEXT={paused:'Paused',rejoining:'Rejoining - connecting route',sharing:'Sharing',idle:'Not sharing yet'};
function renderSharing(value){
  const state=(value.sharing||{}).state||'idle';
  $('sharing-state').textContent=SHARING_TEXT[state]||SHARING_TEXT.idle;
  $('sharing-state').dataset.state=state;
}
function renderAdaptive(value){
  const a=value.adaptive_host||{state:'off'};
  $('adaptive-enable').checked=!!a.adaptive_sharing;$('adaptive-game').checked=!!a.pause_sharing;
  const reason=ADAPTIVE_REASON[a.reason]||'';
  $('adaptive-line').textContent=a.line||'Adaptive sharing: off';
  $('adaptive-status').textContent=(ADAPTIVE_TEXT[a.state]||ADAPTIVE_TEXT.off)+(reason?` Reason: ${reason}.`:'')
    +(a.enabled&&a.expires_in_s!==undefined?` Status refreshes every few seconds (valid ${a.expires_in_s}s).`:'')
    +(a.withdraw_overdue?' Running work has not finished within the expected time.':'');
  $('adaptive-game').disabled=busy;
  $('adaptive-stop').disabled=busy||!a.enabled||!!a.stopped;
  $('adaptive-resume').disabled=busy||!a.enabled||!a.stopped;
}
// Plain-language sentences for the error codes a person can actually meet; anything else
// falls back to the code in words so nothing is hidden.
const HUMAN_ERRORS={
  membership_required:'Create or join a swarm first, then try again.',
  consent_required:'Tick the consent box in step 01 first.',
  download_consent_required:'Tick the authorization box and choose a download limit first.',
  download_consent_invalid:'The download limit must be a positive number of bytes, no larger than your storage limit.',
  acquisition_already_active:'A download is already running. Wait for it to finish or cancel it.',
  two_macs_required:"Invite one other physical Mac with a code or QR, keep Mycelium open on both Macs, and press Start again. This Mac also contributes.",
  member_capability_report_required:"A Mac has not reported its available memory and storage. On that Mac, press Report this Mac capability under Device readiness, then press Start again on the owner.",
  token_path_policy_expired:"Fast-mode permission expired. Keep Mycelium open and connected on both Macs, then press Start on the owner to begin a new run.",
  token_path_process_expired:"The fast-mode worker reached the end of its allowed run. Press Start on the owner to begin a new run.",
  managed_control_expired:"A Mac's control session expired. Keep Mycelium open on both Macs, use Start / reconnect on the disconnected Mac, then press Start on the owner.",
  fast_mode_run_active:"Stop the current run and wait for it to finish closing before changing Fast mode. The setting applies to the next run only.",
  fast_mode_locked_by_environment:"Fast mode is fixed by MYCELIUM_TOKEN_PATH. Remove that environment override and reopen Mycelium to use this switch.",
  pairing_window_relay_required:'Start the public relay to make join codes redeemable; codes can be asked for before that.',
  pairing_window_swarm_required:'Create the swarm first, then invite a Mac.',
  network_requires_fresh_owner:'A public relay is already running, or this Mac is not the owner.',
  network_stop_local_contributor_required:'The public relay would move to a new address, which needs this Mac’s own contributor stopped first (local-contributor/stop). Reopen it on the same address of your own instead (no stop needed), or stop the contributor and press “Start public relay” again.',
  relay_required:'Members joined, but the public relay is not running. Press “Start public relay”, then try again.',
  join_acceptance_mismatch:'The invitation that came back is not the one this Mac asked for. Nothing was joined; ask the owner to approve this Mac again.',
  join_code_invalid:'That join code is not right. Check it against the owner’s screen and try again.',
  join_link_expired:'This join link has expired. Ask the owner for a fresh code.',
  join_on_target_device_required:'Joining has to be started on the Mac that wants to join, not on the owner’s Mac.',
  join_request_already_pending:'This Mac already has a join request waiting for the owner. Wait for approval or cancel it first.',
  join_request_protocol_required:'The join request came from a different version of Mycelium. Update both Macs to the same version and try again.',
  join_seed_pin_mismatch:'The swarm answering at this address is not the one in your join link, so nothing was joined. Ask the owner for a new link.',
  join_state_invalid:'The saved join attempt on this Mac is unreadable. Cancel the join and start it again.',
  join_window_missing:'There is no join attempt in progress on this Mac. Enter a join code to start one.',
  pairing_approval_changed:'The request changed after you reviewed it, so it was not approved. Review it again before approving.',
  pairing_approval_ttl_invalid:'That invitation lifetime is not allowed. Choose one of the listed lifetimes.',
  pairing_body_invalid:'The pairing message was malformed and was ignored. Try again.',
  pairing_body_too_large:'The pairing message was too large and was refused. Try again with a fresh request.',
  pairing_channel_closed:'The pairing connection was closed. Try again.',
  pairing_content_length_invalid:'The pairing message was malformed and was ignored. Try again.',
  pairing_content_type_invalid:'The pairing message was malformed and was ignored. Try again.',
  pairing_downgrade_refused:'Pairing was refused because the connection tried to use weaker security. Check the network and try again.',
  pairing_handoff_binding_invalid:'This encrypted invitation was made for a different swarm or Mac. Ask the owner to create a new one for this Mac.',
  pairing_handoff_changed:'The invitation does not match the one already accepted on this Mac. Nothing was changed.',
  pairing_handoff_expired:'The invitation expired before it was accepted. Ask the owner to approve this Mac again.',
  pairing_handoff_invalid:'That invitation could not be read. Paste the whole encrypted invitation exactly as the owner sent it.',
  pairing_handoff_recipient_mismatch:'That invitation was encrypted for a different Mac. Ask the owner to create one for this Mac.',
  pairing_handoff_request_mismatch:'That invitation answers a different request than this Mac sent. Ask the owner to approve this Mac’s own request.',
  pairing_handoff_too_large:'That invitation is too large to be real and was refused.',
  pairing_identity_invalid:'This Mac’s identity in the request could not be verified. Create a fresh join request.',
  pairing_invite_binding_invalid:'The invitation does not belong to this swarm, so it was refused. Ask the owner for a new one.',
  pairing_nonce_reused:'That pairing message was already used and cannot be replayed. Start a fresh request.',
  pairing_peer_class_unsupported:'This kind of device cannot join this swarm yet.',
  pairing_quota_invalid:'The storage and network limits for this Mac are not valid. Choose the listed options.',
  pairing_rate_exhausted:'Too many pairing attempts in a short time. Wait a minute and try again.',
  pairing_request_expired:'This join request has expired. Create a new one and send it to the owner again.',
  pairing_request_expiring:'This join request is about to expire, so it was not approved. Ask the joining Mac to send a new one.',
  pairing_request_invalid:'That pairing request could not be read. Copy the whole request from the joining Mac.',
  pairing_request_signature_invalid:'The request’s signature did not check out, so it was refused. Ask the joining Mac to create it again.',
  pairing_request_ttl_invalid:'The request’s lifetime is not allowed. Ask the joining Mac to create a new one.',
  pairing_submission_invalid:'The join request could not be read by the owner’s Mac. Try joining again.',
  pairing_time_invalid:'The clocks on the two Macs disagree too much to pair safely. Fix the date and time on both Macs and try again.',
  pairing_transfer_encoding_unsupported:'The pairing message used an unsupported format and was refused. Try again.',
  pairing_window_capacity:'There are already as many open join codes as allowed. Close one, or wait for one to expire, then try again.',
  pairing_window_claimed:'Another Mac already used this join code. Ask the owner for a new code.',
  pairing_window_code_invalid:'That join code is not right. Check it against the owner’s screen and try again.',
  pairing_window_locked:'This join code was locked after too many wrong attempts. Ask the owner for a new one.',
  pairing_window_not_claimed:'No Mac has used this join code yet, so there is nothing to approve.',
  pairing_window_origin_invalid:'The address in this join link is not a valid public address. Ask the owner for a new link.',
  pairing_window_origin_stale:'The owner’s relay address changed since this code was made. Close the code and open a new one.',
  pairing_window_pin_invalid:'The swarm identity in this join link is malformed. Ask the owner for a new link.',
  pairing_window_request_mismatch:'The request in this join window is not the one you reviewed. Review it again before approving.',
  pairing_window_response_invalid:'The owner’s answer to the join request could not be understood. Try again, and ask the owner to update if it keeps happening.',
  pairing_window_ttl_invalid:'That code lifetime is not allowed. Choose one of the listed lifetimes.',
  pairing_window_unknown:'That join code does not exist any more. Ask the owner for a new one.',
  pairing_window_unreachable:'The owner’s relay could not be reached. Check your internet connection and that the owner’s relay is still running, then try again.',
  pairing_window_expired:'This join code has expired. Ask the owner for a fresh one.',
  pairing_window_revoked:'The owner closed this join code. Ask for a new one.',
  pairing_window_closed:'This join code is closed. Ask the owner for a new one.',
  relay_approval_required:'Starting the public relay needs your explicit approval. Tick the acknowledgement and try again.',
  relay_backend_unavailable:'The owner’s Mac could not answer through the relay right now. Try again in a moment.',
  relay_bound_no_socket:'The relay is running but has nothing to forward to yet. Try again in a moment.',
  relay_budget_invalid:'The relay time limit is not valid. Choose between 30 seconds and 30 minutes.',
  relay_chat_failed:'The chat could not reach the swarm through the relay. Try again in a moment.',
  relay_chat_invalid:'The chat message sent through the relay was malformed and was refused.',
  relay_client_installation_required:'The relay helper (cloudflared) is not installed on this Mac. Install it, then start the relay again.',
  relay_connect_timeout:'The relay took too long to answer. Check your internet connection and try again.',
  relay_connect_unavailable:'The relay could not be reached. Check your internet connection and try again.',
  relay_dns_host_invalid:'The relay’s name is not a valid internet address. Restart the relay to get a new one.',
  relay_dns_invalid:'The relay’s name did not resolve to a valid address. Restart the relay to get a new one.',
  relay_dns_nonpublic:'The relay’s name points at a private address, so it was refused. Restart the relay to get a new one.',
  relay_dns_unavailable:'The relay’s name could not be looked up yet. Wait a few seconds and try again.',
  relay_frame_invalid:'The relay received a malformed message and ignored it.',
  relay_headers_invalid:'The relay received a malformed request and ignored it.',
  relay_join_failed:'The join request could not be passed through the relay. Check the relay is still running and try again.',
  relay_local_invalid:'The relay’s local connection is not valid. Stop and start the relay again.',
  relay_not_startable:'The relay cannot start right now. Check your internet connection and that no other relay is running.',
  relay_origin_changed:'The relay’s public address changed. Close open join codes and open new ones.',
  relay_origin_invalid:'That address is not valid. Use a plain https address with a real domain name (for example https://demo.mycelium.now): no port, path, login details or IP number.',
  relay_projection_invalid:'The relay status could not be read. Refresh the page.',
  relay_projection_key_invalid:'The relay status contained something unexpected and was ignored. Refresh the page.',
  relay_provider_invalid:'That relay provider is not supported. Use the Cloudflare quick tunnel or your own address.',
  relay_proxy_port_invalid:'The local port is not valid. Use a number between 1024 and 65535.',
  relay_proxy_port_unavailable:'That local port is already in use on this Mac. Pick another port, or stop the program using it, then try again.',
  relay_proxy_closed:'The relay was closed. Start it again to let other Macs reach this one.',
  relay_route_unknown:'That address is not served through the relay.',
  relay_seed_invalid:'The relay could not reach this swarm’s membership service. Stop and start the relay again.',
  relay_renewal_invalid:'The relay renewal settings are not valid. Use true or false for auto-renew and long-lived, and a total of up to 7 days.',
  relay_start_failed:'The public relay did not start. Check your internet connection, make sure cloudflared is installed, and try again.',
  relay_stop_failed:'The relay did not stop cleanly. Quit and reopen Mycelium if it keeps running.',
  relay_swarm_entry_failed:'The swarm’s public chat could not be reached through the relay. Try again in a moment.',
  relay_swarm_entry_invalid:'The swarm’s public chat request was malformed and was refused.',
  relay_tls_verification_required:'The relay’s secure connection could not be verified, so it was refused. Restart the relay to get a new one.',
  owner_role_required:'Only the owner Mac can do this.',
  target_role_required:'Only a Mac that joined a swarm can do this.',
  revoke_confirmation_required:'Tick the revoke confirmation box first.',
  standing_consent_required:'Tick the standing-consent box before joining.',
  download_limit_exceeded:'The model needs more bytes than the limit allows. Raise “Maximum bytes to fetch” (up to your storage limit) and press Download again.',
  storage_limit_exceeded:'The model does not fit in this Mac’s storage limit. Choose a smaller model, or use a fresh installation with a larger storage limit.',
  insufficient_disk:'This Mac does not have enough free disk space for the download. Free some space and press Download again.',
  network_error:'The download source could not be reached. Check your internet connection and press Download again; partial files are kept.',
  resource_power_policy_violation:'Your power limit is not met right now. Plug in to AC power and try again.',
  resource_thermal_policy_violation:'This Mac is too hot right now. Let it cool down and try again.',
  resource_network_policy_violation:'Your network limit is not met right now (for example a metered connection). Switch networks and try again.',
};
function humanError(code){return HUMAN_ERRORS[code]||`${String(code).replaceAll('_',' ')}.`;}
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
  applyResources(value);
  renderAdaptive(value);renderSharing(value);
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
  $('download-hint').textContent=downloading || acquisition?.state==='prepared' ? ''
    : !value.running ? 'This Mac is stopped. Press “Start / reconnect” (Pause, restart, or leave) first.'
    : !canDownload ? 'Create or join a swarm first; the download unlocks once this Mac is enrolled.'
    : !$('download-consent').checked ? 'Tick the authorization box above to enable the download.' : '';
  $('leave-hint').textContent=target && a.membership_state==='current'
    ? 'This Mac joined as a member. To leave the swarm, use “Leave swarm” below: “Leave smoothly” lets work in progress finish first; “Instant leave” stops at once. Only the owner can revoke a membership; after leaving, this Mac pairs again with a new join code once the owner has revoked it.' : '';
  $('download-consent').disabled=busy || downloading;
  $('download-limit').disabled=busy || downloading;
  if(acquisition){
    $('acquisition-state').textContent=`Source: ${acquisition.state.replaceAll('_',' ')}${acquisition.error_code ? ' · '+acquisition.error_code.replaceAll('_',' ') : ''}${value.acquisition_policy_error ? ' · '+value.acquisition_policy_error.replaceAll('_',' ') : ''}${HUMAN_ERRORS[acquisition.error_code]||HUMAN_ERRORS[value.acquisition_policy_error] ? ' — '+(HUMAN_ERRORS[acquisition.error_code]||HUMAN_ERRORS[value.acquisition_policy_error]) : ''}`;
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
  catch(error){notice(error.userMessage ? `Action not completed: ${error.userMessage}${error.fix?.action==='start_relay'?' Use “Start public relay” in the invite section.':''}` : `Action not completed: ${humanError(error.message)} Existing identity and evidence are retained.`);}
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
  const withRelay=!join && $('create-with-relay').checked;
  if(withRelay && !window.confirm('Start a temporary Cloudflare quick tunnel, then create the swarm? The relay terminates HTTPS and can observe control traffic, visitor IP addresses and timing, and web chat prompts and replies; only inference between the Macs stays on Iroh.'))return;
  const result=await perform(join?'/api/join/request':'/api/create',{consent:{peer_visibility:$('visibility').checked,power:$('power').value,
    storage_bytes:Number($('storage').value),network:$('network').value,background:false,diagnostics:false,
    thermal:'stop_on_serious'},...(withRelay?{relay_acknowledged:true,max_seconds:1800}:{}),adaptive_sharing:$('adaptive-sharing-consent').checked},join?'Target identity created. Send the public request to the owner. No seed was created on this Mac.':
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
// ---- Leave swarm (joined member): self-contained block, keep separate from the rest of the file ----
Object.assign(HUMAN_ERRORS,{
  leave_confirmation_required:'Tick “I want this Mac to leave the swarm” first.',
  leave_mode_invalid:'Choose “Leave smoothly” or “Instant leave”.',
  target_role_required:'Only a Mac that joined a swarm can leave it. The owner revokes members from the owner screen.',
  already_joined:'This Mac is still a member of a swarm. Leaving stops it serving, but the owner must revoke it before it can pair again with a new join code.',
  leave_unload_failed:'The model could not be unloaded cleanly; this Mac left anyway.'});
function renderLeaveSwarm(value){
  const card=$('leave-swarm'); if(!card)return;
  const a=value.authority||{}, leave=value.leave||null;
  card.hidden=!(value.installation_role==='target' && (a.membership_state==='current' || leave));
  const inProgress=!!leave && leave.state!=='left', left=!!leave && leave.state==='left';
  const text=!leave ? ''
    : left ? (leave.fallback==='stall' || leave.fallback==='cap'
        ? 'The swarm did not finish in time, so this Mac left immediately. It no longer accepts work and its consent is withdrawn.'
        : 'This Mac has left the swarm: it no longer accepts work and its standing consent is withdrawn. The owner sees it drop out. To take part again, the owner revokes this Mac and it pairs again with a new join code.')
    : leave.state==='draining' ? 'Leaving smoothly: no new work is accepted; waiting for the work already running to finish (up to 30 seconds).'
    : 'Leaving…';
  $('leave-state').textContent=text;
  const off=busy || inProgress || left;
  $('leave-confirm').disabled=off;
  $('leave-smooth').disabled=off || !$('leave-confirm').checked;
  $('leave-instant').disabled=off || !$('leave-confirm').checked;
}
function leaveSwarm(mode){
  const done=mode==='smooth' ? 'Leaving smoothly: no new work is accepted while running work finishes.'
    : 'This Mac left the swarm.';
  return perform('/api/leave',{mode,confirm:true},done);  // buttons are enabled only while the box is ticked
}
{const baseRender=render; render=function(value){baseRender(value); renderLeaveSwarm(value);};}
$('leave-confirm').addEventListener('change',()=>{if(latest)renderLeaveSwarm(latest);});
$('leave-smooth').addEventListener('click',()=>void leaveSwarm('smooth'));
$('leave-instant').addEventListener('click',()=>void leaveSwarm('instant'));
// ---- end Leave swarm ----
$('revoke').addEventListener('click',()=>{
  if (!$('revoke-confirm').checked) { notice('Confirm that you want to revoke this Mac before continuing.'); return; }
  void perform('/api/revoke-self',{confirm:true},'This Mac is revoked. It cannot renew or resume this membership. Private identity and evidence are retained.');
});
$('pair-request').addEventListener('click',async()=>{
  if(busy)return;
  try {
    const value=await api('/api/pair/request',{});
    showRequest(value);
  }catch(error){notice(`Pairing request not completed: ${humanError(error.message)}`);}
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
// Consent choices derived per model (model_resources.py, via /api/status
// run_model.resources): the same numbers the app enforces.
let resourceKey='';
function setOptions(select,options,keep){
  const current=keep ?? select.value;
  select.replaceChildren(...options.map(([v,label])=>{const o=document.createElement('option');o.value=String(v);o.textContent=label;return o;}));
  if(current && options.some(([v])=>String(v)===String(current)))select.value=String(current);
}
function applyResources(value){
  const r=value.run_model?.resources; if(!r)return;
  const key=JSON.stringify([r.selected?.model_id,r.storage_consent_ceiling_bytes,r.device_memory_bytes,value.resource_policy?.storage_bytes]);
  if(key===resourceKey)return; resourceKey=key;
  const ceiling=r.storage_consent_ceiling_bytes, quota=r.pairing_quota_ceiling_bytes;
  const storage=[[2e9,'2 GB'],[1e9,'1 GB'],[4e9,'4 GB'],[8e9,'8 GB'],[16e9,'16 GB']];
  for(const m of r.models){
    if(m.owner_storage_bytes<=ceiling)storage.push([m.owner_storage_bytes,`${gb(m.owner_storage_bytes)} (${m.label} on the owner Mac: pinned download + headroom)`]);
  }
  const saved=value.resource_policy?.storage_bytes;
  if(saved && !storage.some(([v])=>v===saved))storage.push([saved,`${gb(saved)} (saved)`]);
  setOptions($('storage'),storage.filter(([v])=>v<=ceiling),saved);
  const sel=r.selected;
  $('storage-detail').textContent=`Largest storage limit any supported model needs here: ${gb(ceiling)}.`+
    (sel?` The selected model ${sel.label} needs ${gb(sel.owner_storage_bytes)} on the owner Mac; a joined Mac needs a whole snapshot for small models or its verified slice in slice mode (at most ${gb(sel.whole_model_member_disk_bytes)}, usually far less; its exact share is shown when the run is compiled).`:'');
  setOptions($('pair-storage'),[[2e9,'2 GB'],[1e9,'1 GB'],[4e9,'4 GB'],[8e9,'8 GB'],[16e9,'16 GB'],[quota,`${gb(quota)} (largest share any model gives one Mac)`]].filter(([v])=>v<=quota));
  const mem=r.memory_cap_options||[];
  if(mem.length){
    setOptions($('standing-memory'),mem.slice().reverse().map((v)=>[v,`${gb(v)} (${Math.round(100*v/r.device_memory_bytes)}% of this Mac's ${gb(r.device_memory_bytes)})`]));
  }
  if(sel){
    const preStaged=sel.label==='Qwen2.5-7B';
    const snapshot=sel.snapshot_bytes.toLocaleString('en-US');
    $('acquisition-detail').textContent=preStaged
      ?`The pinned ${sel.label} snapshot is ${snapshot} bytes. If a verified copy is already on this Mac (a retained or LAN-seeded snapshot) the default limit of 11,535,382 bytes is enough: only the small metadata/text files are fetched from Hugging Face. With no copy here, the default limit makes acquisition stop with “download limit exceeded” before any model byte is fetched. To download the model from Hugging Face instead, raise the limit below to at least ${snapshot} bytes (it needs a storage limit of at least ${gb(sel.owner_storage_bytes)}). Joined Macs fetch a whole snapshot for small models, including the tested starter; in slice mode each fetches only its own verified slice.`
      :`Tick the authorization box, then press “Download / resume source” to download the pinned ${sel.label} snapshot (${snapshot} bytes) from Hugging Face onto this owner Mac; it needs a storage limit of at least ${gb(sel.owner_storage_bytes)}. Joined Macs fetch a whole snapshot for small models, including the tested starter; in slice mode each fetches only its own verified slice.`;
    const limit=$('download-limit');
    limit.max=String(Math.max(sel.snapshot_bytes,11535382));
    // The grant may not exceed the storage limit (app.py download_consent_invalid).
    const room=Math.min(sel.snapshot_bytes,saved||sel.snapshot_bytes);
    if(!preStaged)limit.value=String(room);
  }
}
function renderJoinFlow(value,target,a){
  const flow=value.join_flow ?? {};
  const owner=!target;
  $('invite-owner').hidden=!owner;
  $('join-target').hidden=owner && !!a.swarm_created;
  const relayUp=!!value.network?.public_origin;
  $('relay-start').hidden=!owner || relayUp;
  $('own-address').hidden=!owner || relayUp;
  $('relay-order-note').hidden=!owner || relayUp;
  $('window-open').disabled=busy || !owner || !a.swarm_created;
  const waitingMembers=value.relay_wait?.state==='waiting_for_relay'?value.relay_wait.members_joined:0;  // line: "N members joined, waiting for relay to serve"
  $('relay-wait').textContent=owner&&waitingMembers?`${waitingMembers} member${waitingMembers===1?'':'s'} joined, waiting for relay to serve. Press “Start public relay”.`:'';
  $('standing-text').textContent=flow.standing_disclosure?.text ?? '';
  const joined=a.membership_state==='current';
  const waiting=!!flow.join_window;
  $('join-with-code').disabled=busy || joined || waiting || !$('standing-confirm').checked || !$('visibility').checked;
  $('join-cancel').hidden=!waiting;
  const lapsed=target && ['expired','stale','unknown'].includes(a.membership_state);
  $('join-hint').textContent=joined || waiting || (owner && !!a.swarm_created) ? ''
    : lapsed && $('visibility').checked && $('standing-confirm').checked ? 'Your membership lapsed. Ask the owner for a new join code: this Mac keeps its identity and the downloaded model.'
    : !$('visibility').checked ? 'Tick the cooperative-peer visibility box in step 01 to enable “Join with code”.'
    : !$('standing-confirm').checked ? 'Tick the standing-consent box above to enable “Join with code”.' : '';
  const origin=!!value.network?.public_origin;
  $('invite-hint').textContent=!owner || (a.swarm_created && origin) ? ''
    : !a.swarm_created ? 'Create the swarm (step 01) to unlock “Invite a Mac”.'
    : 'No relay is running yet: a code you ask for now is “ready to share once the relay starts” and becomes redeemable then.';
  $('join-fingerprint').textContent=waiting?`Waiting for the owner. Check that the owner sees this fingerprint: ${flow.join_window.fingerprint}`:'';
  const standing=flow.standing_consent;
  $('standing-withdraw').hidden=!target || !standing || standing.state==='withdrawn';
  $('standing-state').textContent=standing?`Standing consent: ${standing.state.replaceAll('_',' ')}${standing.last?` · last offer ${standing.last.state}${standing.last.reason?' ('+standing.last.reason.replaceAll('_',' ')+')':''}`:''}${flow.last_report?` · last report ${flow.last_report.purpose} ${flow.last_report.state}`:''}`:'';
  $('windows').replaceChildren();
  for(const w of flow.pending_windows ?? []){  // asked for before the relay: no address exists yet
    const row=document.createElement('div');row.className='member';
    const label=document.createElement('pre');
    label.textContent=`Code ${w.pending_id} · ${w.state.replaceAll('_',' ')} · ${w.note}`;row.append(label);
    const cancel=document.createElement('button');cancel.type='button';cancel.className='secondary';cancel.textContent='Cancel code';
    cancel.addEventListener('click',()=>void perform('/api/pair/window/revoke',{window_id:w.pending_id},'Code cancelled.'));
    row.append(cancel);$('windows').append(row);
  }
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
$('own-start').addEventListener('click',()=>{
  const origin=$('own-origin').value.trim(),port=Number($('own-port').value);
  if(!origin||!Number.isInteger(port)){notice('Enter your https address and the local port first.');return;}
  if(!window.confirm('Use '+origin+' for this swarm? Mycelium starts no tunnel: you must point this hostname at 127.0.0.1:'+port+' on this Mac yourself (for example a Cloudflare named tunnel). The front you use terminates HTTPS and can observe control traffic, visitor IP addresses and timing, and web chat prompts and replies; only inference between the Macs stays on Iroh.'))return;
  void perform('/api/network/start',{relay_acknowledged:true,max_seconds:1800,provider:'external',public_origin:origin,proxy_port:port,
    ...($('own-cloudflare').checked?{cloudflare_front:true}:{})},'Relay started on your own address. Make sure your hostname points at this Mac; then open a code and approve each Mac when it appears.');
});
$('relay-start').addEventListener('click',()=>{
  if(!window.confirm('Start a temporary Cloudflare quick tunnel? The relay terminates HTTPS and can observe control traffic, visitor IP addresses and timing, and web chat prompts and replies; only inference between the Macs stays on Iroh.'))return;
  void perform('/api/network/start',{relay_acknowledged:true,max_seconds:1800},'Public relay started. Codes you asked for are now ready to share; approve each Mac when it appears.');
});
$('window-open').addEventListener('click',()=>void perform('/api/pair/window/open',{ttl_seconds:Number($('window-ttl').value)},latest?.network?.public_origin?'Join code opened. Show it to one person; approve their Mac when it appears.':'Code requested. It is ready to share once the relay starts: press “Start public relay”.'));
$('standing-confirm').addEventListener('change',()=>{if(latest&&connected)render(latest);});
$('visibility').addEventListener('change',()=>{if(latest&&connected)render(latest);});
$('join-with-code').addEventListener('click',()=>{
  const disclosure=latest?.join_flow?.standing_disclosure;
  if(!disclosure || !$('standing-confirm').checked)return;
  void perform('/api/join/window',{consent:currentConsent(),adaptive_sharing:$('adaptive-sharing-consent').checked,join:$('join-code').value,standing_consent:{acknowledged:true,
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
  try{const value=await api('/api/join/window/poll',{});if(value.state==='joined'){const skew=(value.warnings||[]).find(w=>w&&w.code==='clock_skew');notice(skew?`Joined. Warning: this Mac's clock differs from the owner's by about ${Math.round(skew.skew_seconds)} s. Turn on “Set time and date automatically” in System Settings, or joining and renewals may fail.`:'Joined. Capability and relay link reported to the owner automatically.');}await refresh(false);}
  catch(error){notice(`Join not completed: ${humanError(error.message)}`);await refresh(false);}
  finally{joinPolling=false;}
},3000);
window.addEventListener('pageshow',()=>void refresh(false));
window.addEventListener('online',()=>void refresh());
void refresh();
setInterval(()=>{if(!busy&&connected)void refresh(false);},5000);
function adaptiveBody(){return {adaptive_sharing:$('adaptive-enable').checked,pause_sharing:$('adaptive-game').checked,acknowledged:$('adaptive-ack').checked};}
$('adaptive-enable').addEventListener('change',()=>void perform('/api/adaptive/policy',adaptiveBody(),$('adaptive-enable').checked?'Adaptive sharing on.':'Adaptive sharing off. Pause sharing and Stop still work.'));
$('adaptive-game').addEventListener('change',()=>void perform('/api/adaptive/policy',adaptiveBody(),$('adaptive-game').checked?'Sharing paused: no new work.':'Pause sharing released. Sharing returns only after the calm period.'));
$('adaptive-stop').addEventListener('click',()=>void perform('/api/adaptive/stop',{},'Stopped taking new work on this Mac.'));
$('adaptive-resume').addEventListener('click',()=>void perform('/api/adaptive/resume',{},'Serving allowed again after the calm period.'));
