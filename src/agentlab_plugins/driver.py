"""Driver plugins own Agent loops; Kernel only schedules exported calls."""
import json
import uuid
import hashlib
from agentlab.abi import KernelError
from .common import fail, emit, text_of
from .content import task_content,append_text


async def apply(ctx,config):
    async def run(p,call):
        task=task_content(p.get('task','Read data.txt'),config.get('supports_images',False))
        session=p.get('session') or uuid.uuid4().hex
        await ctx.call('history','open',{'session':session},parent=call)
        async def append(message):return await ctx.call('history','append',{'session':session,'message':message},parent=call)
        existing=await ctx.call('history','read',{'session':session},parent=call)
        if not existing['messages'] and config.get('system_prompt'):await append({'role':'system','content':config['system_prompt']})
        episode_start=len(existing['messages'])
        await append({'role':'user','content':task})
        mode=config.get('mode','basic')
        scenario=p.get('scenario',config.get('scenario','read'))
        has_policy='loop_policy' in ctx.instance.spec.get('bindings',{})
        stagnant=0;seen_outputs=set()
        async def infer(scenario, planning=False, final=False, final_tokens=None):
            tools=[] if planning or final else await ctx.call('tools','list',{},parent=call)
            facts=await ctx.call('history','read',{'session':session},parent=call)
            if final:
                facts['messages']=list(facts['messages'])+[{'role':'user','content':'Tool execution is finished. Answer the original task from the available evidence without using tools. If the evidence cannot support an answer, return exactly INSUFFICIENT_EVIDENCE. Do not invent missing observations.'}]
            projection=await ctx.call('context','build',dict(facts,final=final,protected_from=episode_start),parent=call)
            if not projection.get('fits',True):return {'needs_finalization':'context_capacity'}
            if has_policy and projection.get('near_limit') and not final and not planning:return {'needs_finalization':'context_pressure'}
            if planning:
                projection['messages'].append({'role':'user','content':'First write a concise plan for the current task. Do not execute tools or give the final answer yet. Execution follows in the next step.'})
            reservation=await ctx.call('budget','reserve',{'tokens':config.get('reservation_tokens',128)},parent=call)
            response=None
            try:
                async for chunk in ctx.stream('model','infer',{'session':session,'messages':projection['messages'],'tools':tools,
                        **({'max_completion_tokens':final_tokens,'purpose':'finalization'} if final else {}),
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
        async def finish(reason,steps,cap=2048):
            emit(ctx,'agent.finalizing',{'session':session,'reason':reason,'steps':steps,'max_completion_tokens':cap},call)
            try:response=await infer('final',final=True,final_tokens=cap)
            except KernelError as exc:
                if not has_policy or (exc.envelope.get('cause') or {}).get('code')!='BUDGET.EXHAUSTED':raise
                return {'session':session,'answer':'','steps':steps,'status':'budget_limit','terminal_reason':'request_budget','finalization_attempted':False}
            if response.get('needs_finalization'):
                return {'session':session,'answer':'','steps':steps,'status':'context_limit','terminal_reason':'required_input_too_large','finalization_attempted':False}
            message=response['message']
            if message.get('tool_calls'):fail('PROVIDER.UNEXPECTED_FINAL_TOOL_CALL')
            await append(message)
            answer=message.get('content') or ''
            status='truncated' if response.get('finish_reason')=='length' else 'abstained' if not answer.strip() or answer.strip().rstrip('.').upper()=='INSUFFICIENT_EVIDENCE' else 'completed'
            if status=='abstained':answer=''
            return {'session':session,'answer':answer,'steps':steps+1,'status':status,'terminal_reason':reason,
                'finalization_attempted':True,'finish_reason':response.get('finish_reason')}
        planned=mode=='plan_execute' and config.get('max_steps',6)>1
        if planned:
            planning=await infer('plan',planning=True)
            if planning.get('needs_finalization'):return await finish(planning['needs_finalization'],0)
            if planning['message'].get('tool_calls'):fail('PROVIDER.UNEXPECTED_PLANNING_TOOL_CALL')
            await append(planning['message'])
            emit(ctx,'agent.plan',{'session':session,'plan':planning['message']['content']},call)
            if planning.get('finish_reason')=='length':
                return {'session':session,'answer':'','steps':1,'status':'truncated','finish_reason':'length'}
            await append({'role':'user','content':'Now execute the plan using the available tools as needed, then provide the final answer to the original task.'})
        for step in range(int(planned),config.get('max_steps',6)):
            if has_policy and mode!='single_step':
                decision=await ctx.call('loop_policy','decide',{'steps':step,'stagnant_steps':stagnant},parent=call)
                if decision['action']=='finalize':return await finish(decision['reason'],step,decision['max_completion_tokens'])
            try:response=await infer(scenario)
            except KernelError as exc:
                if not has_policy or (exc.envelope.get('cause') or {}).get('code')!='BUDGET.EXHAUSTED':raise
                return {'session':session,'answer':'','steps':step,'status':'budget_limit','terminal_reason':'request_budget','finalization_attempted':False}
            if response.get('needs_finalization'):return await finish(response['needs_finalization'],step)
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
            if not calls:
                answer=message.get('content') or ''
                abstained=not answer.strip() or answer.strip().rstrip('.').upper()=='INSUFFICIENT_EVIDENCE'
                return {'session':session,'answer':'' if abstained else answer,'steps':step+1,'status':'abstained' if abstained else 'completed'}
            new_information=False
            for request in parsed:
                out=await ctx.call('tools','execute',request,parent=call)
                fingerprint=hashlib.sha256(text_of(out).encode()).hexdigest()
                novelty=out.get('novelty')
                fresh=novelty.get('new_count',0)>0 if isinstance(novelty,dict) else fingerprint not in seen_outputs
                new_information=new_information or (fresh and not out.get('is_error'))
                seen_outputs.add(fingerprint)
                await append({'role':'tool','tool_call_id':request['tool_call_id'],'content':text_of(out),
                    'is_error':out['is_error'],'execution_id':out['execution_id'],'status':out['status']})
            stagnant=0 if new_information else stagnant+1
            if mode=='single_step':return {'session':session,'answer':message.get('content',''),'steps':1,'status':'step_completed'}
        fail('AGENT.STEP_LIMIT')
    ctx.expose('run',run)
