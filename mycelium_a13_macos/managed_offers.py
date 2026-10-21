# SPDX-License-Identifier: AGPL-3.0-only
"""Member-bound target-pulled staging bytes on the existing signed TLS plane.

Publication is owner-local only. Reads are deliberately replay-safe: they do not
claim work, start processes or change readiness. Execution stays at target-owned
staging/load consent boundaries. No arbitrary URL, path or argv is transferred.
"""
from __future__ import annotations
from contextlib import contextmanager, closing
import base64
import hashlib
import json
import math
import uuid
from pathlib import Path
from .managed_control import _digest as _pin

from .managed_control import (ManagedControlMailbox, _BINDING_FIELDS,
    _REQUEST_COMMON_FIELDS, _RESPONSE_COMMON_FIELDS, _binding_from_member,
    _require, _segment, _verify_signed)

ACK='mycelium.managed_stage_ack.v1'
REQUEST='mycelium.managed_stage_read.v1'
RESPONSE='mycelium.managed_stage_bytes.v1'
CLEANUP_READ='mycelium.managed_stage_cleanup_read.v1'
CLEANUP_ACK='mycelium.managed_stage_cleanup_ack.v1'
CLEANUP_RESPONSE='mycelium.managed_stage_cleanup_state.v1'
_CLEANUP_REQUEST_FIELDS=_REQUEST_COMMON_FIELDS|_BINDING_FIELDS|{'offer_id'}
_CLEANUP_RESPONSE_FIELDS=_RESPONSE_COMMON_FIELDS|_BINDING_FIELDS|{'offer','ack'}
# Both preview peers use this bounded transfer profile. Base64 plus offer
# metadata remains below the public relay one-MiB frame limit.
CHUNK=512*1024
MAX_ARCHIVE=64*1024*1024
MAX_TOTAL=128*1024*1024
MAX_OFFERS=16
_DISCOVERY='stage-discovery'
_REQUEST_FIELDS=_REQUEST_COMMON_FIELDS|_BINDING_FIELDS|{'offer_id','offset'}
_RESPONSE_FIELDS=_RESPONSE_COMMON_FIELDS|_BINDING_FIELDS|{'offer','offset','data'}
_OFFER_FIELDS=_BINDING_FIELDS|{'offer_id','archive_digest','archive_bytes','expires_at'}


def _digest(data):return 'sha256:'+hashlib.sha256(data).hexdigest()


def _preposition_bytes(value):
    return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)+'\n').encode()


def _preposition(value):
    if value is None:return None
    _require(isinstance(value,dict) and set(value)=={'protocol','files'} and
        value['protocol']=='mycelium.controller_prepositioned_member_artifacts.v1','offer_preposition_invalid')
    _require(isinstance(value['files'],list) and 0<len(value['files'])<=256,'offer_preposition_invalid')
    _require(len(_preposition_bytes(value))<=65536,'offer_preposition_invalid')
    destinations=[]
    for row in value['files']:
        _require(isinstance(row,dict) and set(row)=={'content_digest','destination_path','size_bytes','source_path'},'offer_preposition_invalid')
        _pin(row['content_digest'])
        _require(type(row['size_bytes']) is int and 0<=row['size_bytes']<=4*1024**3,'offer_preposition_invalid')
        for key in ('source_path','destination_path'):
            text=row[key]
            _require(isinstance(text,str) and 0<len(text)<=4096 and '\x00' not in text,'offer_preposition_invalid')
            path=Path(text)
            _require(str(path)==text and '..' not in path.parts and '.' not in path.parts,'offer_preposition_invalid')
            _require(path.is_absolute()==(key=='source_path'),'offer_preposition_invalid')
        destinations.append(row['destination_path'])
    _require(destinations==sorted(set(destinations)),'offer_preposition_invalid')
    return json.loads(_preposition_bytes(value))


def _offer_valid(offer):
    extra={'protocol','preposition','preposition_digest'}
    _require(isinstance(offer,dict) and set(offer) in (_OFFER_FIELDS,_OFFER_FIELDS|extra),'offer_invalid')
    if set(offer)==_OFFER_FIELDS|extra:
        _require(offer['protocol']=='mycelium.managed_stage_offer.v2','offer_invalid')
        preposition=_preposition(offer['preposition'])
        _require(preposition is not None and offer['preposition_digest']==_digest(_preposition_bytes(preposition)),'offer_preposition_invalid')
    _segment(offer['offer_id'])
    _require(type(offer['archive_bytes']) is int and 0<offer['archive_bytes']<=MAX_ARCHIVE,'offer_invalid')
    from .managed_control import _digest as validate_digest, _validate_binding
    _validate_binding(offer)
    validate_digest(offer['archive_digest'])
    _require(type(offer['expires_at']) in (int,float) and math.isfinite(offer['expires_at']),'offer_invalid')
    return offer


class ManagedProvisioningMailbox(ManagedControlMailbox):
    """Add bounded artifact discovery/delivery without changing native commands."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        with self._offer_connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS managed_stage_offers '
                '(node_id TEXT PRIMARY KEY, offer_id TEXT UNIQUE NOT NULL, '
                'expires REAL NOT NULL, metadata TEXT NOT NULL, archive BLOB NOT NULL)')
            columns={row[1] for row in db.execute('PRAGMA table_info(managed_stage_offers)')}
            if 'ack' not in columns:db.execute('ALTER TABLE managed_stage_offers ADD COLUMN ack TEXT')
            if 'cleanup_requested' not in columns:db.execute('ALTER TABLE managed_stage_offers ADD COLUMN cleanup_requested INTEGER NOT NULL DEFAULT 0')
            if 'cleanup_ack' not in columns:db.execute('ALTER TABLE managed_stage_offers ADD COLUMN cleanup_ack TEXT')
            db.execute('CREATE TABLE IF NOT EXISTS managed_stage_history '
                '(offer_id TEXT PRIMARY KEY, receipt TEXT NOT NULL)')

    @contextmanager
    def _offer_connection(self):
        with closing(self._connect()) as connection:
            with connection:
                yield connection

    def publish_offer(self,*,target,run_id,deployment_id,archive,ttl_seconds=300,preposition=None):
        _require(isinstance(archive,bytes) and 0<len(archive)<=MAX_ARCHIVE,'offer_archive_invalid')
        _require(type(ttl_seconds) in (int,float) and math.isfinite(ttl_seconds)
            and 0<ttl_seconds<=300,'offer_ttl_invalid')
        binding=_binding_from_member(target,run_id,deployment_id)
        preposition=_preposition(preposition)
        preposition_digest=None if preposition is None else 'sha256:'+hashlib.sha256(_preposition_bytes(preposition)).hexdigest()
        with self._lock, self._member_guard(binding):
            now=self._clock()
            offer={**binding,'offer_id':uuid.uuid4().hex,'archive_digest':_digest(archive),
                'archive_bytes':len(archive),'expires_at':now+ttl_seconds}
            if preposition is not None:
                offer.update(protocol='mycelium.managed_stage_offer.v2',preposition=preposition,preposition_digest=preposition_digest)
            _offer_valid(offer)
            with self._offer_connection() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('DELETE FROM managed_stage_offers WHERE expires<=?',(now,))
                self._supersede_older_identity(db,binding)
                _require(db.execute('SELECT 1 FROM managed_stage_offers WHERE node_id=?',
                    (target['node_id'],)).fetchone() is None,'offer_already_pending')
                count,total=db.execute('SELECT count(*),coalesce(sum(length(archive)),0) FROM managed_stage_offers').fetchone()
                _require(count<MAX_OFFERS and total+len(archive)<=MAX_TOTAL,'offer_capacity_exhausted')
                db.execute('INSERT INTO managed_stage_offers (node_id,offer_id,expires,metadata,archive) VALUES (?,?,?,?,?)',
                    (target['node_id'],offer['offer_id'],offer['expires_at'],json.dumps(offer,sort_keys=True),archive))
            return dict(offer)

    @staticmethod
    def _supersede_older_identity(db,binding):
        """Replace a live offer that an OLDER identity of the same node published (V1.2 D1).

        A member app restart is a re-admission (newer generation and incarnation).  The offer bound to the old identity
        can no longer be read, cleaned or retired by anyone, so it would block the node until its TTL.  It is recorded
        in the bounded history as ``superseded`` -- never as a cleanup acknowledgement -- and a full history still
        fails closed.  The same identity, or a newer one than the publisher's, keeps ``offer_already_pending``.
        """
        row=db.execute('SELECT metadata,ack FROM managed_stage_offers WHERE node_id=?',(binding['target_node_id'],)).fetchone()
        if row is None:return
        stale=json.loads(row[0])
        if not (type(stale.get('membership_generation')) is int
                and stale['membership_generation']<binding['membership_generation']
                and stale.get('member_incarnation')!=binding['member_incarnation']):return
        _require(db.execute('SELECT count(*) FROM managed_stage_history').fetchone()[0]<256,
            'offer_history_capacity_exhausted')
        receipt={'offer':stale,'stage_ack':None if row[1] is None else json.loads(row[1]),'cleanup_ack':None,
            'superseded':True,'superseded_by':{'membership_generation':binding['membership_generation'],
            'member_incarnation':binding['member_incarnation']}}
        db.execute('INSERT INTO managed_stage_history (offer_id,receipt) VALUES (?,?)',
            (stale['offer_id'],json.dumps(receipt,sort_keys=True)))
        db.execute('DELETE FROM managed_stage_offers WHERE node_id=?',(binding['target_node_id'],))

    def retire_offer(self,offer):
        """Owner-local retirement after exact target-confirmed cleanup only.

        Keep bounded durable receipt history, release the active artifact slot,
        and never use elapsed time as proof of cleanup. History exhaustion fails
        closed; no identity/evidence is silently deleted to admit more work.
        """
        _offer_valid(offer)
        with self._lock,self._member_guard(offer),self._offer_connection() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT receipt FROM managed_stage_history WHERE offer_id=?',
                (offer['offer_id'],)).fetchone()
            if previous is not None:
                receipt=json.loads(previous[0])
                _require(receipt['offer']==offer,'offer_retirement_conflict')
                return receipt
            _require(self._current_offer(db,offer)==offer,'offer_unavailable')
            row=db.execute('SELECT ack,cleanup_requested,cleanup_ack FROM managed_stage_offers WHERE offer_id=?',
                (offer['offer_id'],)).fetchone()
            _require(row is not None and row[1]==1 and row[2] is not None,'offer_cleanup_unconfirmed')
            receipt={'offer':offer,
                'stage_ack':None if row[0] is None else self._stage_ack(offer,json.loads(row[0])),
                'cleanup_ack':self._cleanup_ack(offer,json.loads(row[2]))}
            _require(db.execute('SELECT count(*) FROM managed_stage_history').fetchone()[0]<256,
                'offer_history_capacity_exhausted')
            db.execute('INSERT INTO managed_stage_history (offer_id,receipt) VALUES (?,?)',
                (offer['offer_id'],json.dumps(receipt,sort_keys=True)))
            db.execute('DELETE FROM managed_stage_offers WHERE offer_id=?',(offer['offer_id'],))
            return json.loads(json.dumps(receipt))

    def _current_offer(self,db,binding):
        row=db.execute('SELECT metadata FROM managed_stage_offers WHERE node_id=? AND expires>?',
            (binding['target_node_id'],self._clock())).fetchone()
        if row is None:return None
        offer=_offer_valid(json.loads(row[0]))
        for key in _BINDING_FIELDS-{'run_id','deployment_id'}:
            if offer[key]!=binding[key]:return None
        return offer

    def pending_offer(self,target):
        binding=_binding_from_member(target,_DISCOVERY,_DISCOVERY)
        with self._lock, self._member_guard(binding), self._offer_connection() as db:
            return self._current_offer(db,binding)

    @staticmethod
    def _stage_open(db,offer):
        row=db.execute('SELECT cleanup_requested FROM managed_stage_offers WHERE offer_id=?',(offer['offer_id'],)).fetchone()
        _require(row is not None and row[0]==0,'offer_cleanup_requested')

    def read_offer(self,envelope):
        statement,_=self._verify_target(envelope,protocol=REQUEST,fields=_REQUEST_FIELDS)
        _require(statement['run_id']==_DISCOVERY and statement['deployment_id']==_DISCOVERY,'offer_request_invalid')
        offset=statement['offset'];offer_id=statement['offer_id']
        _require(type(offset) is int and 0<=offset<=MAX_ARCHIVE,'offer_offset_invalid')
        _require(offer_id is None or isinstance(offer_id,str),'offer_request_invalid')
        if offer_id is not None:_segment(offer_id)
        with self._lock, self._member_guard(statement), self._offer_connection() as db:
            offer=self._current_offer(db,statement)
            if offer is not None:self._stage_open(db,offer)
            data=None
            if offer_id is not None:
                _require(offer is not None and offer['offer_id']==offer_id,'offer_unavailable')
                _require(offset<offer['archive_bytes'],'offer_offset_invalid')
                data=bytes(db.execute('SELECT substr(archive,?,?) FROM managed_stage_offers WHERE offer_id=?',
                    (offset+1,CHUNK,offer_id)).fetchone()[0])
                _require(len(data)==min(CHUNK,offer['archive_bytes']-offset),'offer_storage_invalid')
            else:_require(offset==0,'offer_offset_invalid')
            return self._response(RESPONSE,statement['request_id'],{
                **{key:statement[key] for key in _BINDING_FIELDS},'offer':offer,'offset':offset,
                'data':None if data is None else base64.b64encode(data).decode('ascii')})

    @staticmethod
    def _stage_ack(offer,ack):
        from pathlib import PurePosixPath
        _require(isinstance(ack,dict),'offer_ack_invalid')
        root=ack.get('staging_root')
        _require(isinstance(root,str) and len(root)<=4096 and
            PurePosixPath(root).is_absolute() and str(PurePosixPath(root))==root and
            '..' not in PurePosixPath(root).parts,'offer_ack_invalid')
        expected={'protocol':'mycelium.controller_remote_stage_ack.v1',
            'node_id':offer['target_node_id'],'staging_root':root,
            'archive_digest':offer['archive_digest'],
            'archive_size_bytes':offer['archive_bytes']}
        if offer.get('preposition_digest') is not None:
            expected.update(preposition_digest=offer['preposition_digest'],
                preposition_size_bytes=len(_preposition_bytes(offer['preposition'])))
        _require(ack==expected,'offer_ack_invalid')
        return dict(ack)

    def acknowledge_stage(self,envelope):
        statement,_=self._verify_target(envelope,protocol=ACK,fields=_REQUEST_FIELDS|{'ack'})
        _require(statement['run_id']==_DISCOVERY and statement['deployment_id']==_DISCOVERY
            and type(statement['offset']) is int and statement['offset']==0,'offer_request_invalid')
        with self._lock, self._member_guard(statement), self._offer_connection() as db:
            db.execute('BEGIN IMMEDIATE')
            offer=self._current_offer(db,statement)
            _require(offer is not None and offer['offer_id']==statement['offer_id'],'offer_unavailable')
            self._stage_open(db,offer)
            ack=self._stage_ack(offer,statement['ack'])
            prior=db.execute('SELECT ack FROM managed_stage_offers WHERE offer_id=?',(offer['offer_id'],)).fetchone()[0]
            _require(prior is None or json.loads(prior)==ack,'offer_ack_conflict')
            db.execute('UPDATE managed_stage_offers SET ack=? WHERE offer_id=?',
                (json.dumps(ack,sort_keys=True),offer['offer_id']))
            return self._response(RESPONSE,statement['request_id'],{
                **{key:statement[key] for key in _BINDING_FIELDS},'offer':offer,'offset':0,'data':None})

    def stage_receipt(self,offer):
        """Owner-local current stage ack; not physical qualification evidence."""
        _offer_valid(offer)
        with self._lock, self._member_guard(offer), self._offer_connection() as db:
            current=self._current_offer(db,offer)
            _require(current==offer,'offer_unavailable')
            self._stage_open(db,offer)
            row=db.execute('SELECT ack FROM managed_stage_offers WHERE offer_id=?',(offer['offer_id'],)).fetchone()
            _require(row is not None and row[0] is not None,'offer_stage_unconfirmed')
            return self._stage_ack(offer,json.loads(row[0]))

    @staticmethod
    def _cleanup_ack(offer, ack):
        from pathlib import PurePosixPath
        _require(isinstance(ack,dict) and set(ack)=={'protocol','node_id','staging_root','removed'},'offer_cleanup_ack_invalid')
        root=ack.get('staging_root')
        _require(ack['protocol']=='mycelium.controller_remote_cleanup_ack.v1' and
            ack['node_id']==offer['target_node_id'] and type(ack['removed']) is bool and
            isinstance(root,str) and len(root)<=4096 and PurePosixPath(root).is_absolute() and
            str(PurePosixPath(root))==root and '..' not in PurePosixPath(root).parts,'offer_cleanup_ack_invalid')
        return dict(ack)

    def request_stage_cleanup(self,offer):
        """Owner requests digest-bound target cleanup, never removes target files."""
        _offer_valid(offer)
        with self._lock,self._member_guard(offer),self._offer_connection() as db:
            db.execute('BEGIN IMMEDIATE')
            _require(self._current_offer(db,offer)==offer,'offer_unavailable')
            db.execute('UPDATE managed_stage_offers SET cleanup_requested=1 WHERE offer_id=?',(offer['offer_id'],))

    def cleanup_receipt(self,offer):
        _offer_valid(offer)
        with self._lock,self._member_guard(offer),self._offer_connection() as db:
            _require(self._current_offer(db,offer)==offer,'offer_unavailable')
            row=db.execute('SELECT cleanup_ack FROM managed_stage_offers WHERE offer_id=?',(offer['offer_id'],)).fetchone()
            _require(row is not None and row[0] is not None,'offer_cleanup_unconfirmed')
            return self._cleanup_ack(offer,json.loads(row[0]))

    def cleanup_state(self,envelope,*,acknowledge=False):
        statement,_=self._verify_target(envelope,protocol=CLEANUP_ACK if acknowledge else CLEANUP_READ,
            fields=_CLEANUP_REQUEST_FIELDS|({'ack'} if acknowledge else set()))
        _require(statement['run_id']==_DISCOVERY and statement['deployment_id']==_DISCOVERY,'offer_request_invalid')
        offer_id=statement['offer_id']
        if offer_id is not None:_segment(offer_id)
        with self._lock,self._member_guard(statement),self._offer_connection() as db:
            db.execute('BEGIN IMMEDIATE')
            offer=self._current_offer(db,statement);ack=None
            if offer is not None:
                row=db.execute('SELECT cleanup_requested,cleanup_ack,ack FROM managed_stage_offers WHERE offer_id=?',(offer['offer_id'],)).fetchone()
                if not row[0]:offer=None
                else:
                    ack=None if row[1] is None else self._cleanup_ack(offer,json.loads(row[1]))
                    if offer_id is None and ack is not None:offer=None;ack=None
            if offer_id is not None:
                _require(offer is not None and offer['offer_id']==offer_id,'offer_unavailable')
            if acknowledge:
                _require(offer is not None and offer_id is not None,'offer_unavailable')
                supplied=self._cleanup_ack(offer,statement['ack'])
                if row[2] is not None:
                    _require(supplied['staging_root']==json.loads(row[2])['staging_root'],'offer_cleanup_ack_invalid')
                _require(ack is None or ack==supplied,'offer_cleanup_ack_conflict')
                ack=supplied
                db.execute('UPDATE managed_stage_offers SET cleanup_ack=? WHERE offer_id=?',
                    (json.dumps(ack,sort_keys=True),offer_id))
            return self._response(CLEANUP_RESPONSE,statement['request_id'],{
                **{k:statement[k] for k in _BINDING_FIELDS},'offer':offer,'ack':ack})

    def handle_http(self,method,path,body):
        # Owner-local extensions on the same signed plane (run_exchange.RunExchange.install).
        extension=(getattr(self,'route_extensions',None) or {}).get(path)
        if extension is not None:
            _require(method=='POST','managed_control_method_invalid')
            return 200,extension(body)
        if path in {'/managed-control/staging/cleanup/read','/managed-control/staging/cleanup/ack'}:
            _require(method=='POST','managed_control_method_invalid')
            return 200,self.cleanup_state(body,acknowledge=path.endswith('/ack'))
        if path=='/managed-control/staging/ack':
            _require(method=='POST','managed_control_method_invalid')
            return 200,self.acknowledge_stage(body)
        if path=='/managed-control/staging/read':
            _require(method=='POST','managed_control_method_invalid')
            return 200,self.read_offer(body)
        return super().handle_http(method,path,body)


class ManagedOfferClient:
    def __init__(self,client):self.client=client

    def _read(self,target,offer_id=None,offset=0,ack=None):
        client=self.client
        now=client._clock();request_id=uuid.uuid4().hex
        binding=_binding_from_member(target,_DISCOVERY,_DISCOVERY)
        # Read-only, bounded, replay-safe; does not consume durable command sequence.
        statement={**binding,'protocol':REQUEST,'swarm_id':client.swarm_id,
            'request_id':request_id,'sequence':1,'nonce':uuid.uuid4().hex,
            'issued_at':now,'expires_at':now+30,'offer_id':offer_id,'offset':offset}
        path='/managed-control/staging/read'
        if ack is not None:
            statement.update(protocol=ACK,ack=ack);path='/managed-control/staging/ack'
        response=client._request(path,client._signed_outbound(statement))
        result=client._verified_response(response,protocol=RESPONSE,fields=_RESPONSE_FIELDS,
            request_id=request_id,target=target,run_id=_DISCOVERY,deployment_id=_DISCOVERY)
        _require(result['offset']==offset,'offer_response_invalid')
        offer=result['offer']
        if offer is not None:
            _offer_valid(offer)
            _require(offer['expires_at']>client._clock(),'offer_expired')
            _require(all(offer[k]==binding[k] for k in _BINDING_FIELDS-{'run_id','deployment_id'}),'offer_response_invalid')
        if offer_id is None:
            _require(result['data'] is None,'offer_response_invalid')
        else:
            _require(offer is not None and offer['offer_id']==offer_id,'offer_response_invalid')
        return result

    def acknowledge(self,target,offer,ack):
        _offer_valid(offer)
        result=self._read(target,offer['offer_id'],ack=ack)
        _require(result['offer']==offer and result['data'] is None,'offer_changed')

    def cleanup_status(self,target,offer=None,*,ack=None):
        client=self.client;now=client._clock();request_id=uuid.uuid4().hex
        binding=_binding_from_member(target,_DISCOVERY,_DISCOVERY)
        statement={**binding,'protocol':CLEANUP_READ if ack is None else CLEANUP_ACK,
            'swarm_id':client.swarm_id,'request_id':request_id,'sequence':1,
            'nonce':uuid.uuid4().hex,'issued_at':now,'expires_at':now+30,
            'offer_id':None if offer is None else offer['offer_id']}
        if ack is not None:statement['ack']=ack
        response=client._request('/managed-control/staging/cleanup/'+('read' if ack is None else 'ack'),
            client._signed_outbound(statement))
        result=client._verified_response(response,protocol=CLEANUP_RESPONSE,fields=_CLEANUP_RESPONSE_FIELDS,
            request_id=request_id,target=target,run_id=_DISCOVERY,deployment_id=_DISCOVERY)
        actual=result['offer']
        if actual is not None:
            _offer_valid(actual)
            _require(actual['expires_at']>client._clock() and all(actual[k]==binding[k]
                for k in _BINDING_FIELDS-{'run_id','deployment_id'}),'offer_response_invalid')
        if offer is not None:_require(actual==offer,'offer_changed')
        if result['ack'] is not None:
            _require(actual is not None,'offer_response_invalid')
            ManagedProvisioningMailbox._cleanup_ack(actual,result['ack'])
        if ack is not None:_require(result['ack']==ack,'offer_cleanup_ack_invalid')
        return result

    def pending_cleanup(self,target):return self.cleanup_status(target)['offer']

    def pending(self,target):return self._read(target)['offer']

    def fetch(self,target,offer,*,max_bytes,authorize=lambda:True):
        _offer_valid(offer)
        _require(type(max_bytes) is int and 0<offer['archive_bytes']<=max_bytes,'offer_budget_exceeded')
        def admitted():
            try:allowed=authorize() is True
            except Exception:allowed=False
            _require(allowed,'offer_transfer_not_authorized')
        chunks=[];offset=0
        while offset<offer['archive_bytes']:
            admitted()
            result=self._read(target,offer['offer_id'],offset)
            _require(result['offer']==offer,'offer_changed')
            try:data=base64.b64decode(result['data'],validate=True)
            except (TypeError,ValueError):raise ValueError('offer_response_invalid') from None
            _require(len(data)==min(CHUNK,offer['archive_bytes']-offset),'offer_response_invalid')
            chunks.append(data);offset+=len(data)
        admitted()
        archive=b''.join(chunks)
        _require(_digest(archive)==offer['archive_digest'],'offer_archive_digest_mismatch')
        _require(offer['expires_at']>self.client._clock(),'offer_expired')
        return archive
