import asyncio
import json
from .common import tool, result, fail, emit


async def file_apply(ctx,config):
    async def describe(p,c): return [tool('read_file','Read an existing UTF-8 file by its relative path in the authorized workspace. This does not list directories; do not use "." as a filename.',{'path':{'type':'string'}},['path'])]
    async def execute(p,c):
        emit(ctx,'provider.executed',{'name':p['name']},c)
        data=await ctx.call('workspace','read',p['arguments'],parent=c)
        return result(data['text'])
    ctx.expose('describe',describe);ctx.expose('execute',execute)


async def compute_apply(ctx,config):
    kind=config['kind']
    async def describe(p,c): return [dict(tool(kind,'Execute in a restricted container; workspace is read-only, write artifacts to /output',{'code':{'type':'string','maxLength':8192}},['code'],True),timeout_ms=config.get('timeout_ms',10000))]
    async def execute(p,c):
        emit(ctx,'provider.executed',{'name':kind},c)
        data=await ctx.call('sandbox','run',{'kind':kind,'code':p['arguments']['code']},parent=c)
        visible={k:data[k] for k in ('stdout','stderr','exit_code','status','artifacts','stdout_bytes','stderr_bytes','oom_killed') if k in data}
        return result(json.dumps(visible,ensure_ascii=False),is_error=data['exit_code']!=0,**data)
    ctx.expose('describe',describe);ctx.expose('execute',execute)


async def fixture_apply(ctx,config):
    name=config.get('name','fixture_http')
    from aiohttp import web,ClientSession,ClientTimeout
    base_url=None
    async def ready(call):
        nonlocal base_url
        async def data(request):
            return web.json_response({'source':'loopback HTTP fixture','lines':3})
        app=web.Application();app.router.add_get('/data',data)
        runner=web.AppRunner(app);await runner.setup();ctx.on_cleanup(runner.cleanup)
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        base_url='http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])+'/data'
    ctx.on_ready(ready)
    async def describe(p,c): return [tool(name,'Read a deterministic local fixture (no Internet)',{'key':{'type':'string'}},[])]
    async def execute(p,c):
        emit(ctx,'provider.executed',{'name':name},c)
        if p['arguments'].get('key')=='business_error': fail('FIXTURE.NOT_FOUND')
        if p['arguments'].get('key')=='crash': raise RuntimeError('fixture crash')
        await asyncio.sleep(config.get('delay',0))
        async with ClientSession(timeout=ClientTimeout(total=2)) as session:
            async with session.get(base_url) as response:
                response.raise_for_status();data=await response.json()
        return result(json.dumps(data))
    ctx.expose('describe',describe);ctx.expose('execute',execute)


async def delegate_apply(ctx,config):
    async def describe(p,c): return [dict(tool('delegate_agent','Delegate one isolated subtask to an authorized child',
        {'task':{'type':'string'},'tools':{'type':'array','items':{'type':'string'}},'delay':{'type':'number','minimum':0,'maximum':10}},['task']),timeout_ms=config.get('timeout_ms',10000))]
    async def execute(p,c):
        out=await ctx.call('manager','delegate',p['arguments'],parent=c)
        visible={k:out[k] for k in ('answer','status','terminal_reason','finish_reason') if k in out}
        return result(json.dumps(visible,ensure_ascii=False),is_error=out.get('status') not in ('completed','step_completed'),
                      child_session=out['session'],child_instance=out['instance'],child_status=out.get('status'))
    ctx.expose('describe',describe);ctx.expose('execute',execute)


async def mcp_apply(ctx,config):
    # Protocol adapter, not a registry bypass. Fixture server uses MCP method envelopes.
    async def describe(p,c):
        response=await ctx.call('server','request',{'jsonrpc':'2.0','id':'list','method':'tools/list','params':{}},parent=c)
        return [dict(tool(t['name'],t['description'],{},[]), input_schema=t['inputSchema']) for t in response['result']['tools']]
    async def execute(p,c):
        response=await ctx.call('server','request',{'jsonrpc':'2.0','id':c.execution_id,'method':'tools/call','params':{'name':p['name'],'arguments':p['arguments']}},parent=c)
        if 'error' in response: fail('MCP.REPORTED_ERROR')
        return {'content':response['result']['content'],'is_error':response['result'].get('isError',False)}
    ctx.expose('describe',describe);ctx.expose('execute',execute)


async def mcp_fixture_apply(ctx,config):
    async def request(p,c):
        if p['method']=='tools/list': out={'tools':[{'name':'mcp_echo','description':'Local MCP echo','inputSchema':{'type':'object','properties':{'text':{'type':'string'}},'required':['text'],'additionalProperties':False}}]}
        elif p['method']=='tools/call':
            emit(ctx,'provider.executed',{'name':'mcp_echo'},c)
            out={'content':[{'type':'text','text':p['params']['arguments']['text']}],'isError':False}
        else: return {'jsonrpc':'2.0','id':p['id'],'error':{'code':-32601,'message':'Unknown method'}}
        return {'jsonrpc':'2.0','id':p['id'],'result':out}
    ctx.expose('request',request)


async def mcp_workspace_apply(ctx,config):
    """Local MCP method bridge to the authorized workspace (no fixture responses)."""
    async def request(p,c):
        if p['method']=='tools/list':
            out={'tools':[{'name':'mcp_read_file','description':'Read a UTF-8 file from the authorized workspace through MCP.',
                'inputSchema':{'type':'object','properties':{'path':{'type':'string'}},'required':['path'],'additionalProperties':False}}]}
        elif p['method']=='tools/call' and p['params']['name']=='mcp_read_file':
            data=await ctx.call('workspace','read',p['params']['arguments'],parent=c)
            out={'content':[{'type':'text','text':data['text']}],'isError':False}
        else:return {'jsonrpc':'2.0','id':p['id'],'error':{'code':-32601,'message':'Unknown method or tool'}}
        return {'jsonrpc':'2.0','id':p['id'],'result':out}
    ctx.expose('request',request)
