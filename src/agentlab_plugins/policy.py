import asyncio
import uuid
from copy import deepcopy
from .common import fail, emit


async def apply(ctx, config):
    pending = {}
    async def authorize(p, call):
        if p['name'] in config.get('deny',[]): return {'allowed':False,'reason':'policy_denied'}
        if not p['side_effect'] and p['name'] not in config.get('require_approval',[]): return {'allowed':True}
        mode = config.get('mode','deny')
        ident = uuid.uuid4().hex
        row = {'id':ident,'tool':p['name'],'arguments':deepcopy(p.get('arguments',{})),'execution':call.execution_id,'status':'pending'}
        emit(ctx,'approval.request',row,call)
        if mode in ('allow','deny'):
            allow = mode == 'allow'
        else:
            future = asyncio.get_running_loop().create_future()
            pending[ident] = (row,future)
            try:
                allow = await asyncio.wait_for(future,config.get('timeout_seconds',60))
            except asyncio.TimeoutError: allow = False
            except asyncio.CancelledError:
                emit(ctx,'approval.decision',dict(row,status='cancelled'),call)
                raise
            finally: pending.pop(ident,None)
        emit(ctx,'approval.decision',dict(row,status='allowed' if allow else 'denied'),call)
        return {'allowed':allow,'approval_id':ident,'reason':'approved' if allow else 'approval_denied'}
    async def list_pending(p,call): return [deepcopy(r) for r,f in pending.values()]
    async def decide(p,call):
        if call.caller_id is not None or type(p.get('allow')) is not bool: fail('APPROVAL.NOT_AUTHORIZED')
        row = pending.get(p['id'])
        if not row or row[1].done(): fail('APPROVAL.NOT_PENDING')
        row[1].set_result(p['allow']); return {'accepted':True}
    ctx.expose('authorize',authorize);ctx.expose('pending',list_pending);ctx.expose('decide',decide)
