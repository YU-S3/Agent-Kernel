"""Append-only facts, scoped session leases, and deterministic context projection."""
import asyncio
import json
import sqlite3
from pathlib import Path
from copy import deepcopy
from .common import fail, emit


async def history_apply(ctx, config):
    rows, leases, locks = {}, {}, {}
    db = None
    if config.get('backend') == 'sqlite':
        path = Path(config['path']); path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        db.execute('CREATE TABLE IF NOT EXISTS facts (seq INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL, body TEXT NOT NULL)')
        db.commit()
        ctx.on_cleanup(db.close)

    def facts(session):
        if db:
            return [json.loads(r[0]) for r in db.execute('SELECT body FROM facts WHERE session=? ORDER BY seq',(session,))]
        return deepcopy(rows.get(session, []))

    def put(session, message):
        message = deepcopy(message)
        if db:
            db.execute('INSERT INTO facts(session,body) VALUES (?,?)',(session,json.dumps(message)))
            db.commit()
        else:
            rows.setdefault(session, []).append(message)

    def settle(session):
        pending = {}
        for row in facts(session):
            for tc in row.get('tool_calls', []): pending[tc['id']] = True
            if row.get('role') == 'tool': pending.pop(row.get('tool_call_id'), None)
        for ident in pending:
            put(session, {'role':'tool','tool_call_id':ident,'content':'Execution cancelled or outcome unknown',
                          'is_error':True,'status':'cancelled_or_unknown'})

    def allowed(session, call):
        owner = leases.get(session)
        if owner not in [c['execution'] for c in ctx.authority(call)['chain']]: fail('HISTORY.SESSION_NOT_OWNED')

    async def open_session(p, call):
        session = str(p['session'])
        lock = locks.setdefault(session, asyncio.Lock())
        await lock.acquire()
        chain = ctx.authority(call)['chain']
        owner = chain[1] if len(chain) > 1 else chain[0]
        leases[session] = owner['execution']
        settle(session)  # persisted orphan from a previous process crash
        def release():
            settle(session)
            leases.pop(session, None)
            if lock.locked(): lock.release()
        ctx.scope_cleanup(owner['scope'], release, parent=call)
        return {'session':session,'size':len(facts(session))}

    async def append(p, call):
        allowed(p['session'], call)
        message = p['message']
        if message.get('role') not in ('system','user','assistant','tool'): fail('HISTORY.INVALID_ROLE')
        put(p['session'], message)
        emit(ctx,'history.append',{'session':p['session'],'role':message['role'],'size':len(facts(p['session']))},call)
        return {'size':len(facts(p['session']))}

    async def read(p, call):
        if call.caller_id is not None: allowed(p['session'], call)
        return {'messages': facts(p['session'])}

    ctx.expose('open',open_session);ctx.expose('append',append);ctx.expose('read',read)


async def context_apply(ctx, config):
    async def build(p, call):
        messages = deepcopy(p['messages'])
        if config.get('compact_tools'):
            from .context_projection import project
            out=project(messages,config,p.get('final',False),p.get('protected_from'))
            if out['projected']:emit(ctx,'context.projected',{k:v for k,v in out.items() if k!='messages'},call)
            return out
        if config.get('mode') == 'window':
            # Project whole turns; never cut assistant/tool pairs in half.
            starts = [i for i,m in enumerate(messages) if m['role'] == 'user']
            count = max(1,int(config.get('turns',1)))
            if len(starts)>count:
                messages = [m for m in messages[:starts[-count]] if m['role']=='system'] + messages[starts[-count]:]
            limit=config.get('max_chars')
            while limit and len(json.dumps(messages,ensure_ascii=False))>limit:
                starts=[i for i,m in enumerate(messages) if m['role']=='user']
                if len(starts)<2:fail('CONTEXT.CURRENT_TURN_TOO_LARGE')
                messages=[m for m in messages[:starts[1]] if m['role']=='system']+messages[starts[1]:]
            if len(messages)<len(p['messages']):
                emit(ctx,'context.projected',{'facts_count':len(p['messages']),'visible_count':len(messages),'strategy':'whole_recent_turns','facts_preserved':True},call)
        return {'messages':messages,'facts_count':len(p['messages']),'visible_count':len(messages)}
    ctx.expose('build',build)
