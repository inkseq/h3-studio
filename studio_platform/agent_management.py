"""Owner connection controls and attribution on original document/audit authorities."""
import copy
import json
import uuid
from sqlalchemy import select, insert, update, inspect
from .repository import documents, Scope, request_hash, money, spending_settings
from .spending_limits import lock_owner, read_settings, public_usage

DEFAULTS = {'code_ttl_seconds':600,'recovery_ttl_seconds':300,'key_lifetime_days':90,'max_pending_connections':5}
CONTROL_PROJECT='__agent_control'
ATTRIBUTION_PROJECT='__agent_attribution'


def doc_where(tenant, owner, kind, ident, project=CONTROL_PROJECT):
    return (documents.c.tenant_id==tenant,documents.c.owner_id==owner,documents.c.project_id==project,
            documents.c.kind==kind,documents.c.document_id==ident)


def read_doc(conn,tenant,owner,kind,ident,project=CONTROL_PROJECT):
    row=conn.execute(select(documents).where(*doc_where(tenant,owner,kind,ident,project))).mappings().first()
    return dict(row) if row else None


def put_doc(conn,tenant,owner,kind,ident,payload,now,project=CONTROL_PROJECT):
    prior=read_doc(conn,tenant,owner,kind,ident,project)
    values=dict(payload=payload,version=prior['version']+1 if prior else 1,updated_at=now)
    if prior:conn.execute(update(documents).where(*doc_where(tenant,owner,kind,ident,project)).values(**values))
    else:conn.execute(insert(documents).values(tenant_id=tenant,owner_id=owner,project_id=project,kind=kind,document_id=ident,**values))
    return values


def paused(conn,tenant,owner,key_id):
    row=read_doc(conn,tenant,owner,'connection_key_state',key_id)
    return bool(row and row['payload'].get('paused'))


def connection_for_actor(conn,tenant,owner,actor_id):
    if not actor_id.startswith('key:'):return None
    from .agent_connect import connections
    # Ledger-only/offline instances have no credential authority or connections.
    if not inspect(conn).has_table(connections.name):return None
    return conn.execute(select(connections).where(connections.c.tenant==tenant,connections.c.owner==owner,
        connections.c.key_id==actor_id[4:])).mappings().first()


def attribution_identity(kind,ident,project_id=None):
    # Story entity IDs are local to a project; QC resource UUIDs are global.
    return kind+':'+ident if project_id is None else 'project:'+request_hash({'project_id':project_id,'kind':kind,'id':ident})


def record_created(conn,tenant,principal,kind,ident,now,project_id=None):
    if not principal.machine:return
    row=connection_for_actor(conn,tenant,principal.owner,principal.actor_id)
    if row is None:return
    key=attribution_identity(kind,ident,project_id)
    if read_doc(conn,tenant,principal.owner,'created_by',key,ATTRIBUTION_PROJECT):return
    put_doc(conn,tenant,principal.owner,'created_by',key,{'kind':'agent','connection_id':row['id'],
        'name':row['name'],'resource_kind':kind,'resource_id':ident,'project_id':project_id},now,ATTRIBUTION_PROJECT)
    from .agent_connect import audit
    conn.execute(insert(audit).values(tenant=tenant,id='audit-'+uuid.uuid4().hex,connection_id=row['id'],
        owner=principal.owner,event=kind+'.created',created_at=now))


def creator(conn,tenant,owner,kind,ident,project_id=None):
    row=read_doc(conn,tenant,owner,'created_by',attribution_identity(kind,ident,project_id),ATTRIBUTION_PROJECT)
    return {k:row['payload'][k] for k in ('kind','connection_id','name')} if row else None


def record_event(conn,tenant,principal,event,now):
    if not principal.machine:return
    row=connection_for_actor(conn,tenant,principal.owner,principal.actor_id)
    if row is None:return
    from .agent_connect import audit
    conn.execute(insert(audit).values(tenant=tenant,id='audit-'+uuid.uuid4().hex,connection_id=row['id'],
        owner=principal.owner,event=event,created_at=now))


def record_refusal(engine,tenant,principal,code,now):
    if code not in {'connection_daily_limit_reached','user_daily_limit_reached'} or principal is None or not principal.machine:return
    from .agent_connect import audit
    with engine.begin() as conn:
        row=connection_for_actor(conn,tenant,principal.owner,principal.actor_id)
        if row is not None:conn.execute(insert(audit).values(tenant=tenant,id='audit-'+uuid.uuid4().hex,
            connection_id=row['id'],owner=principal.owner,event=code,created_at=now))


def synchronize_cap_versions(conn,tenant,owner,before,after,now):
    """Called within the original owner lock and global settings transaction.

    A removed map entry means zero/unlimited. Only actual effective cap changes
    invalidate a connection editor; pause state and unrelated controls survive.
    """
    changed={actor for actor in set(before)|set(after) if before.get(actor,0)!=after.get(actor,0)}
    for actor in sorted(changed):
        row=connection_for_actor(conn,tenant,owner,actor)
        if row is None:continue
        state=read_doc(conn,tenant,owner,'connection_state',row['id'])
        put_doc(conn,tenant,owner,'connection_state',row['id'],
            copy.deepcopy(state['payload']) if state else {'paused':False},now)
        from .agent_connect import audit
        conn.execute(insert(audit).values(tenant=tenant,id='audit-'+uuid.uuid4().hex,
            connection_id=row['id'],owner=owner,event='cap.updated',created_at=now))


class AgentManagementMixin:
    def _settings(self,conn,owner):
        row=read_doc(conn,self.tenant,owner,'connection_settings','settings')
        return {'version':row['version'] if row else 0,'value':{**DEFAULTS,**(row['payload'] if row else {})},
            'bounds':{k:{'minimum':1,'maximum':2147483647} for k in DEFAULTS}}

    def settings(self,principal):
        with self.engine.begin() as conn:
            self._browser(conn,principal)
            return self._settings(conn,principal.owner)

    def change_settings(self,principal,body,key):
        from .agent_connect import ConnectError
        if not isinstance(body,dict) or set(body)!={'expected_version','value'} or type(body['expected_version']) is not int or body['expected_version']<0 or not isinstance(body['value'],dict) or set(body['value'])!=set(DEFAULTS) or any(type(v) is not int or not 1<=v<=2147483647 for v in body['value'].values()):
            raise ConnectError('connection_settings_invalid',422)
        with self.engine.begin() as conn:
            self._begin(conn);self._browser(conn,principal);lock_owner(conn,self.tenant,principal.owner)
            prior=self._control_replay(conn,principal,'settings',key,body)
            if prior:return prior
            current=self._settings(conn,principal.owner)
            if current['version']!=body['expected_version']:
                result={**current,'no_mutation_committed':True,'error_code':'connection_version_conflict','request':copy.deepcopy(body)}
                self._control_remember(conn,principal,'settings',key,body,result)
                return result
            put_doc(conn,self.tenant,principal.owner,'connection_settings','settings',body['value'],self.clock())
            result=self._settings(conn,principal.owner)
            self._control_remember(conn,principal,'settings',key,body,result)
            return result

    def _control_replay(self,conn,principal,namespace,key,body):
        from .agent_connect import ConnectError
        if not isinstance(key,str) or not 1<=len(key)<=160:raise ConnectError('connection_idempotency_required',422)
        ident=request_hash({'namespace':namespace,'key':key})
        row=read_doc(conn,self.tenant,principal.owner,'connection_control_receipt',ident)
        if row:
            if row['payload']['fingerprint']!=request_hash(body):raise ConnectError('idempotency_conflict',409)
            return row['payload']['response']

    def _control_remember(self,conn,principal,namespace,key,body,result):
        put_doc(conn,self.tenant,principal.owner,'connection_control_receipt',request_hash({'namespace':namespace,'key':key}),
            {'fingerprint':request_hash(body),'response':result},self.clock())

    def _grant_scope(self,conn,principal,scope,permission):
        from .agent_connect import CONNECT_SCOPES,ConnectError
        if not isinstance(scope,dict) or scope.get('kind') not in {'session','story','all'} or set(scope)!=({'kind'} if scope.get('kind')=='all' else {'kind','id'}):
            raise ConnectError('connection_scope_invalid',422)
        if permission not in {'organise','generate'}:raise ConnectError('connection_permission_invalid',422)
        if scope['kind']!='all' and (not isinstance(scope.get('id'),str) or not 1<=len(scope['id'])<=160):raise ConnectError('connection_scope_invalid',422)
        project_id=None
        if scope['kind']=='session':
            from .quick_chat import objects
            row=conn.execute(select(objects.c.payload).where(objects.c.tenant==self.tenant,objects.c.owner==principal.owner,
                objects.c.kind=='session',objects.c.id==scope.get('id'))).scalar_one_or_none()
            if row is None or row.get('history',{}).get('trash_state','active')!='active':raise ConnectError('connection_scope_not_found',404)
            project_id=row['project_id']
        elif scope['kind']=='story':
            row=read_doc(conn,self.tenant,principal.owner,'project',scope.get('id'),'__projects')
            if row is None or row['payload'].get('integration_kind')=='quick_chat':raise ConnectError('connection_scope_not_found',404)
            from .content_lifecycle import assert_project_writable
            assert_project_writable(conn,self.tenant,principal.owner,scope['id'])
            project_id=scope['id']
        scopes=set(CONNECT_SCOPES)
        if permission=='organise':scopes-={'jobs:write','assistant:run'}
        if project_id:scopes.discard('projects:create')
        return {'scope':copy.deepcopy(scope),'permission':permission,'scopes':sorted(scopes),
            'all_projects':project_id is None,'project_ids':[project_id] if project_id else []}

    def _management_public(self,conn,row,value):
        authorization=json.loads(row['authorization'])
        control=read_doc(conn,self.tenant,row['owner'],'connection_state',row['id'])
        state=control['payload'] if control else {}
        key_id=row.get('key_id')
        config=read_settings(conn,self.tenant,row['owner']) if key_id else None
        cap=config['value']['connection_limits'].get('key:'+key_id,0) if key_id else authorization.get('daily_limit_microusd',0)
        usage=public_usage(conn,self.tenant,row['owner'],self.clock(),actor_id='key:'+key_id) if key_id else {
            'projected_microusd':0,'day':None,'timezone':'Australia/Sydney','includes_unresolved_prior_days':True}
        is_paused=bool(state.get('paused'))
        return {**value,'scope':authorization.get('scope',{'kind':'all'}),'permission':authorization.get('permission','generate'),
            'paused':is_paused,'state_version':control['version'] if control else 0,'daily_limit_microusd':cap,'usage':usage,
            'status':'paused' if is_paused and value['status']=='connected' else value['status']}

    def change_connection(self,principal,ident,body,key):
        from .agent_connect import connections,ConnectError
        if not isinstance(body,dict) or set(body)-{'expected_version','paused','daily_limit_microusd'} or type(body.get('expected_version')) is not int or body['expected_version']<0 or ('paused' in body and type(body['paused']) is not bool):raise ConnectError('connection_update_invalid',422)
        if 'daily_limit_microusd' in body:
            try:money(body['daily_limit_microusd'])
            except ValueError:raise ConnectError('connection_daily_limit_invalid',422) from None
        with self.engine.begin() as conn:
            self._begin(conn);self._browser(conn,principal);lock_owner(conn,self.tenant,principal.owner)
            row=conn.execute(select(connections).where(connections.c.tenant==self.tenant,connections.c.owner==principal.owner,
                connections.c.id==ident).with_for_update()).mappings().first()
            if row is None:raise ConnectError('connection_not_found',404)
            prior=self._control_replay(conn,principal,ident,key,body)
            if prior:return prior
            state=read_doc(conn,self.tenant,principal.owner,'connection_state',ident)
            if (state['version'] if state else 0)!=body['expected_version']:
                result={**self._public_current(conn,row),'no_mutation_committed':True,'error_code':'connection_version_conflict','request':copy.deepcopy(body)}
                self._control_remember(conn,principal,ident,key,body,result)
                return result
            if row['revoked_at'] is not None or row['key_id'] is None:raise ConnectError('connection_not_connected',409)
            value={**(state['payload'] if state else {}),'paused':body.get('paused',state['payload'].get('paused',False) if state else False)}
            put_doc(conn,self.tenant,principal.owner,'connection_state',ident,value,self.clock())
            put_doc(conn,self.tenant,principal.owner,'connection_key_state',row['key_id'],value,self.clock())
            if 'daily_limit_microusd' in body:
                config=read_settings(conn,self.tenant,principal.owner)
                updated={**config['value'],'connection_limits':{**config['value']['connection_limits'],'key:'+row['key_id']:body['daily_limit_microusd']}}
                values={'value':updated,'version':config['version']+1,'actor_id':principal.actor_id,'updated_at':self.clock()}
                if config['version']:conn.execute(update(spending_settings).where(spending_settings.c.tenant_id==self.tenant,spending_settings.c.owner_id==principal.owner).values(**values))
                else:conn.execute(insert(spending_settings).values(tenant_id=self.tenant,owner_id=principal.owner,**values))
            self._emit(conn,row,('paused' if value['paused'] else 'resumed') if 'paused' in body else 'cap.updated')
            result=self._public_current(conn,row)
            self._control_remember(conn,principal,ident,key,body,result)
            return result

    def activity(self,principal,ident,limit=20,offset=0):
        from .agent_connect import audit,connections,ConnectError
        if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or offset<0:raise ConnectError('connection_pagination_invalid',422)
        with self.engine.begin() as conn:
            self._browser(conn,principal)
            if not conn.execute(select(connections.c.id).where(connections.c.tenant==self.tenant,connections.c.owner==principal.owner,connections.c.id==ident)).first():raise ConnectError('connection_not_found',404)
            rows=conn.execute(select(audit.c.id,audit.c.event,audit.c.created_at).where(audit.c.tenant==self.tenant,audit.c.owner==principal.owner,audit.c.connection_id==ident).order_by(audit.c.created_at.desc(),audit.c.id).limit(limit+1).offset(offset)).mappings().all()
            return {'events':[dict(r) for r in rows[:limit]],'limit':limit,'offset':offset,'has_more':len(rows)>limit,'next_offset':offset+limit if len(rows)>limit else None}
