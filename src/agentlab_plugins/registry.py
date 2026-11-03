"""One registry path for native, delegated, dynamic and MCP tools."""
import asyncio
import uuid
import json
from copy import deepcopy
from agentlab.abi import KernelError, PluginOperationError
from .common import fail, schema_check, check, result, emit


async def apply(ctx, config):
    entries = {}
    def visible(row, call):
        auth = ctx.authority(call)
        scopes = {s for c in auth['chain'] for s in (c['scope'],c['instance_scope'])}
        return ('tool:'+row['definition']['name']) in auth['permissions'] and (row['visibility'] is None or row['visibility'] in scopes)

    async def register(p, call):
        auth = ctx.authority(call)
        if 'tools:manage' not in auth['permissions']: fail('TOOL.REGISTRATION_DENIED')
        alias = p['provider']
        if alias not in config['providers']: fail('TOOL.UNBOUND_PROVIDER')
        definitions = await ctx.call(alias,'describe',{},parent=call)
        selected = p.get('definition')
        if selected is None:
            selected = next((d for d in definitions if d['name']==p['name']),None)
        if not isinstance(selected,dict): fail('TOOL.UNKNOWN_DEFINITION')
        for field in ('input_schema','output_schema'): schema_check(selected.get(field,{}))
        if selected not in definitions: fail('TOOL.DEFINITION_MISMATCH')
        definition = deepcopy(selected)
        name = p.get('alias',definition['name']); definition['name'] = name
        if name in entries: fail('TOOL.DUPLICATE')
        owner = p.get('scope_ref') or auth['chain'][0]['instance_scope']
        ident = uuid.uuid4().hex
        row = {'id':ident,'definition':definition,'provider':alias,'provider_name':selected['name'],
               'owner':owner,'visibility':None if p.get('global',False) and call.caller_id is None else owner,
               'lock':asyncio.Lock()}
        ctx.scope_cleanup(owner,lambda: entries.pop(name,None) if entries.get(name) is row else None,parent=call)
        entries[name]=row
        emit(ctx,'tools.register',{'name':name,'id':ident,'scope':owner,'provider':alias},call)
        return {'id':ident,'name':name}

    async def bootstrap(p,call):
        if call.caller_id is not None: fail('TOOL.REGISTRATION_DENIED')
        before = dict(entries)
        try:
            for alias in config.get('static',[]):
                for definition in await ctx.call(alias,'describe',{},parent=call):
                    await register({'provider':alias,'name':definition['name'],'global':True},call)
        except BaseException:
            entries.clear();entries.update(before);raise
        return {'count':len(entries)}

    async def unregister(p,call):
        if 'tools:manage' not in ctx.authority(call)['permissions']: fail('TOOL.REGISTRATION_DENIED')
        row = entries.get(p['name'])
        if row and row['id'] != p.get('id'): fail('TOOL.REGISTRATION_DENIED')
        if row:
            scopes = {s for c in ctx.authority(call)['chain'] for s in (c['scope'],c['instance_scope'])}
            if row['owner'] not in scopes: fail('TOOL.REGISTRATION_DENIED')
            entries.pop(p['name'])
        emit(ctx,'tools.unregister',{'name':p['name']},call)
        return {'removed':row is not None}

    async def list_tools(p,call):
        return [deepcopy(row['definition']) for row in entries.values() if visible(row,call)]

    async def execute(p,call):
        row = entries.get(p['name'])
        if row is None or not visible(row,call): fail('TOOL.NOT_VISIBLE')
        definition = row['definition'];check(definition['input_schema'],p.get('arguments',{}))
        approval = await ctx.call('policy','authorize',dict(name=p['name'],side_effect=definition['side_effect'],arguments=p.get('arguments',{})),parent=call)
        ident = p.get('tool_call_id')
        base = {'tool_call_id':ident,'execution_id':call.execution_id,'name':p['name']}
        if not approval['allowed']:
            out = dict(result(approval['reason'],is_error=True),status='denied',**base)
        else:
            # The admitted definition is immutable even if a concurrent unregister occurs.
            try:
                async def dispatch():
                    return await ctx.call(row['provider'],'execute',{'name':row['provider_name'],'arguments':p.get('arguments',{}),
                        '_consumer':ctx.authority(call)['chain'][1]['execution'] if len(ctx.authority(call)['chain'])>1 else call.execution_id},parent=call)
                async def bounded():
                    try:return await asyncio.wait_for(dispatch(),definition['timeout_ms']/1000)
                    except asyncio.TimeoutError:raise KernelError('EXECUTION.TIMEOUT','Tool deadline',outcome_known=False) from None
                if definition['concurrency_safe']:out=await bounded()
                else:
                    async with row['lock']:out=await bounded()
                check(definition['output_schema'],out,'TOOL.INVALID_OUTPUT')
                check({'type':'array','items':{'type':'object','required':['type','text'],'properties':{'type':{'const':'text'},'text':{'type':'string'}}}},out.get('content'),'TOOL.INVALID_OUTPUT')
                out = deepcopy(out)
                from .context_projection import compact_text
                if not config.get('retain_full_outputs'):
                    for content in out['content']:content['text']=compact_text(content['text'],config.get('max_output_chars',8192))
                out.update(base,status='error' if out.get('is_error') else 'completed')
            except PluginOperationError as exc:
                out = dict(result(exc.envelope['cause']['code'],is_error=True),status='error',**base)
            except BaseException as exc:
                emit(ctx,'tool.receipt',dict(base,status='cancelled' if isinstance(exc,asyncio.CancelledError) else 'unknown',outcome_known=False),call)
                raise
        emit(ctx,'tool.receipt',dict(out,approval=approval),call)
        return out
    for name,fn in [('bootstrap',bootstrap),('register',register),('unregister',unregister),('list',list_tools),('execute',execute)]: ctx.expose(name,fn)
