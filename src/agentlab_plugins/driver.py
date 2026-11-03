"""Driver plugins own Agent loops; Kernel only schedules exported calls."""
import json
import uuid
from agentlab.abi import KernelError
from .common import fail, emit, text_of


async def apply(ctx,config):
    async def run(p,call):
        session=p.get('session') or uuid.uuid4().hex
        await ctx.call('history','open',{'session':session},parent=call)
        async def append(message):return await ctx.call('history','append',{'session':session,'message':message},parent=call)
        existing=await ctx.call('history','read',{'session':session},parent=call)
        if not existing['messages'] and config.get('system_prompt'):await append({'role':'system','content':config['system_prompt']})
        await append({'role':'user','content':str(p.get('task','Read data.txt'))})
        mode=config.get('mode','basic')
        scenario=p.get('scenario',config.get('scenario','read'))
        async def infer(scenario):
            tools=await ctx.call('tools','list',{},parent=call)
            facts=await ctx.call('history','read',{'session':session},parent=call)
            projection=await ctx.call('context','build',facts,parent=call)
            reservation=await ctx.call('budget','reserve',{'tokens':config.get('reservation_tokens',128)},parent=call)
            response=None
            try:
                async for chunk in ctx.stream('model','infer',{'session':session,'messages':projection['messages'],'tools':tools,
                        'scenario':scenario,'delay':p.get('delay',config.get('delay',.01))},parent=call):
                    await call.progress(chunk)
                    if chunk.get('kind')=='complete':
                        if response is not None:fail('PROVIDER.DUPLICATE_COMPLETE')
                        response=chunk['response']
            except KernelError as exc:
                if (exc.envelope.get('cause') or {}).get('http_request_sent') is False:
                    await ctx.call('budget','settle',{'id':reservation['id'],'tokens':0},parent=call)
                raise
            if not response:fail('PROVIDER.INCOMPLETE_RESPONSE')
            await ctx.call('budget','settle',{'id':reservation['id'],'tokens':response.get('usage',{}).get('total_tokens')},parent=call)
            return response
        if mode=='plan_execute':
            planning=await infer('plan');await append(planning['message'])
            emit(ctx,'agent.plan',{'session':session,'plan':planning['message']['content']},call)
        for step in range(config.get('max_steps',6)):
            response=await infer(scenario)
            message=response.get('message',{})
            if message.get('role')!='assistant':fail('PROVIDER.INVALID_ROLE')
            if response.get('finish_reason')=='length':
                # Never dispatch incomplete function arguments. The exact wire
                # response remains in the request audit; history stays valid.
                await append({k:v for k,v in message.items() if k!='tool_calls'})
                emit(ctx,'agent.truncated',{'session':session,'step':step,'tool_calls_dispatched':0},call)
                return {'session':session,'answer':message.get('content') or '', 'steps':step+1,'status':'truncated','finish_reason':'length'}
            calls=message.get('tool_calls') or []
            ids=set();parsed=[]
            for tc in calls:
                try:
                    ident=tc['id'];name=tc['function']['name'];args=tc['function']['arguments']
                    if not isinstance(ident,str) or not ident or ident in ids:raise ValueError()
                    args=json.loads(args) if isinstance(args,str) else args
                    if not isinstance(args,dict):raise ValueError()
                    ids.add(ident);parsed.append({'name':name,'arguments':args,'tool_call_id':ident})
                except (KeyError,TypeError,ValueError):fail('PROVIDER.INVALID_TOOL_CALL')
            await append(message)  # facts precede effects, including mixed text + calls
            emit(ctx,'agent.step',{'session':session,'step':step,'tool_calls':len(calls)},call)
            if not calls:return {'session':session,'answer':message.get('content') or '', 'steps':step+1,'status':'completed'}
            for request in parsed:
                out=await ctx.call('tools','execute',request,parent=call)
                await append({'role':'tool','tool_call_id':request['tool_call_id'],'content':text_of(out),
                    'is_error':out['is_error'],'execution_id':out['execution_id'],'status':out['status']})
            if mode=='single_step':return {'session':session,'answer':message.get('content',''),'steps':1,'status':'step_completed'}
        fail('AGENT.STEP_LIMIT')
    ctx.expose('run',run)
