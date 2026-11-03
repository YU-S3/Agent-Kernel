"""Deterministic offline provider and opt-in OpenAI-compatible HTTP transport."""
import asyncio
import json
import os
from .common import fail, emit


def tc(name,args,ident='call-1'):
    return {'id':ident,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}


async def mock_apply(ctx,config):
    async def infer(p,call):
        await asyncio.sleep(p.get('delay',config.get('delay',.01)))
        messages=p['messages'];scenario=p.get('scenario','read')
        start=max((i for i,m in enumerate(messages) if m['role']=='user'),default=0)
        current=messages[start:];tools=[m for m in current if m['role']=='tool']
        emit(ctx,'model.request',{'session':p['session'],'messages':messages,'tools':p['tools'],'scenario':scenario},call)
        if scenario=='429':fail('PROVIDER.HTTP_429')
        if scenario=='crash':raise RuntimeError('provider crash')
        if scenario=='timeout':await asyncio.sleep(20)
        content='3 行';calls=[]
        if scenario=='refusal':content='I cannot perform this request.'
        elif not tools and scenario not in ('final','plan'):
            mapping={
                'read':[tc('read_file',{'path':'data.txt'})],
                'multi':[tc('read_file',{'path':'data.txt'},'call-1'),tc('fixture_http',{},'call-2')],
                'shell':[tc('shell',{'code':'wc -l data.txt'})],
                'python':[tc('python',{'code':'print(1+2)'})],
                'delegate':[tc('delegate_agent',{'task':'Read the file'},'child-1'),tc('delegate_agent',{'task':'Summarize the local fixture'},'child-2')],
                'mcp':[tc('mcp_echo',{'text':'fixture response'})],
                'business_error':[tc('fixture_http',{'key':'business_error'})],
                'malformed':[tc('read_file',{'path':'data.txt'})],
                'approval':[tc('python',{'code':'print("approved")'})]}
            if scenario=='dynamic':mapping['dynamic']=[tc('inspect_metadata',{})]
            calls=mapping.get(scenario,[]);content='I will use the available tools.'
        if scenario=='plan':content='Read the authorized input, inspect the observation, then answer.'
        message={'role':'assistant','content':content,'tool_calls':calls}
        # Simulate incremental JSON arguments. Driver must not act on these deltas.
        for t in calls:
            args=t['function']['arguments']
            await call.progress({'kind':'tool_delta','id':t['id'],'name':t['function']['name'],'arguments':args[:len(args)//2]})
            await asyncio.sleep(config.get('fragment_delay',.01))
            if scenario=='malformed':fail('PROVIDER.INCOMPLETE_TOOL_CALL')
            await call.progress({'kind':'tool_delta','id':t['id'],'arguments':args[len(args)//2:]})
        await call.progress({'kind':'text_delta','text':content})
        response={'message':message,'usage':{'prompt_tokens':12,'completion_tokens':8,'total_tokens':20},
                  'finish_reason':'tool_calls' if calls else 'stop','model':'offline-fixture'}
        await call.progress({'kind':'complete','response':response})
        emit(ctx,'model.response',{'session':p['session'],**response},call)
        return response
    ctx.expose('infer',infer)


async def universal_apply(ctx,config):
    from .chat_transport import apply
    await apply(ctx,config)
