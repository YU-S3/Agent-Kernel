"""Atomic shared reservation accounting; no token knowledge in Kernel."""
import uuid
import json
from pathlib import Path
from .common import fail, emit


async def apply(ctx, config):
    if config.get('mode')=='accounting':
        # No artificial reservation or session token cap. Counts are observations,
        # never a claim of a prepaid upper bound. Unknown requests stay explicit.
        path=Path(config['path']) if config.get('path') else None
        saved=json.loads(path.read_text()) if path and path.exists() else {}
        pending={};used=saved.get('used',0);requests=saved.get('requests',0);unknown=saved.get('unknown_requests',0)+saved.get('pending',0)
        def persist():
            if path:
                path.parent.mkdir(parents=True,exist_ok=True)
                temp=path.with_suffix('.tmp');temp.write_text(json.dumps({'used':used,'requests':requests,'unknown_requests':unknown,'pending':len(pending)}));temp.replace(path)
        persist()
        async def reserve(p,call):
            nonlocal requests,unknown
            limit=config.get('requests')
            if limit is not None and requests>=limit:fail('BUDGET.EXHAUSTED')
            requests+=1;ident=uuid.uuid4().hex
            chain=ctx.authority(call)['chain'];owner=chain[1] if len(chain)>1 else chain[0]
            pending[ident]=owner['execution']
            persist()
            def close():
                nonlocal unknown
                if pending.pop(ident,None) is not None:unknown+=1;persist()
            ctx.scope_cleanup(owner['scope'],close,parent=call)
            return {'id':ident}
        async def settle(p,call):
            nonlocal used,unknown
            if p['id'] not in pending:fail('BUDGET.ALREADY_SETTLED')
            if pending[p['id']] not in [c['execution'] for c in ctx.authority(call)['chain']]:fail('BUDGET.NOT_OWNED')
            actual=p.get('tokens')
            if actual is not None and (type(actual) is not int or actual<0):fail('BUDGET.USAGE_INVALID')
            pending.pop(p['id'])
            if actual is None:unknown+=1
            else:used+=actual
            persist()
            emit(ctx,'budget.settle',{'id':p['id'],'actual':actual,'mode':'accounting'},call)
            return {'used':used,'unknown_requests':unknown}
        async def remaining(p,call):return {'used':used,'requests':requests,'unknown_requests':unknown,'pending':len(pending),'token_limit':None,'request_limit':config.get('requests'),'mode':'accounting'}
        ctx.expose('reserve',reserve);ctx.expose('settle',settle);ctx.expose('remaining',remaining)
        return
    used, reservations, requests = 0, {}, 0
    unknown = 0
    async def reserve(p, call):
        nonlocal requests
        tokens = p.get('tokens', 0)
        if type(tokens) is not int or tokens < 0: fail('BUDGET.INVALID')
        if used + sum(r['tokens'] for r in reservations.values()) + tokens > config.get('tokens',10000) or requests >= config.get('requests',100):
            fail('BUDGET.EXHAUSTED')
        ident = uuid.uuid4().hex
        requests += 1
        chain = ctx.authority(call)['chain'];owner = chain[1] if len(chain)>1 else chain[0]
        reservations[ident]={'tokens':tokens,'owner':owner['execution']}
        def close():
            nonlocal used, unknown
            row = reservations.pop(ident,None)
            if row:
                used += row['tokens'];unknown += row['tokens']
        ctx.scope_cleanup(owner['scope'],close,parent=call)
        emit(ctx,'budget.reserve',{'id':ident,'tokens':tokens},call)
        return {'id':ident}

    async def settle(p,call):
        nonlocal used, unknown
        row = reservations.get(p['id'])
        if not row: fail('BUDGET.ALREADY_SETTLED')
        if row['owner'] not in [c['execution'] for c in ctx.authority(call)['chain']]: fail('BUDGET.NOT_OWNED')
        actual = p.get('tokens')
        if actual is not None and (type(actual) is not int or actual<0 or actual>row['tokens']): fail('BUDGET.USAGE_INVALID')
        reservations.pop(p['id'])
        used += row['tokens'] if actual is None else actual
        if actual is None: unknown += row['tokens']
        emit(ctx,'budget.settle',{'id':p['id'],'actual':actual,'reserved':row['tokens']},call)
        return {'used':used}

    async def remaining(p,call):
        return {'used':used,'unknown_reserved':unknown,'reserved':sum(r['tokens'] for r in reservations.values()),
                'requests':requests,'token_limit':config.get('tokens',10000),'request_limit':config.get('requests',100)}
    ctx.expose('reserve',reserve);ctx.expose('settle',settle);ctx.expose('remaining',remaining)
