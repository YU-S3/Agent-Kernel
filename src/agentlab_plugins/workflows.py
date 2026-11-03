"""Application orchestrators: dynamic tools, direct delegation, and spawn demos."""
import asyncio
from agentlab.abi import KernelError
from .common import emit


async def apply(ctx,config):
    async def run(p,call):
        mode=config.get('mode','dynamic')
        if mode=='concurrent':
            first=await ctx.start_call('manager','delegate',{'task':'First child','delay':p.get('first_delay',.08)},parent=call)
            second=await ctx.start_call('manager','delegate',{'task':'Second child','delay':p.get('second_delay',.04)},parent=call)
            if p.get('cancel_first'):await asyncio.sleep(.02);first.cancel()
            outputs=await asyncio.gather(first.wait(),second.wait(),return_exceptions=True)
            return {'children':[{'error':r.envelope} if isinstance(r,KernelError) else r for r in outputs]}
        if mode=='spawn':
            handle=await ctx.spawn_instance('service',{}, {},parent=call)
            ex=await ctx.start_call(handle,'echo',p,parent=call)
            return {'instance':handle.instance_id,'result':await ex.wait()}
        registration=await ctx.call('tools','register',{'provider':'metadata','name':'inspect_metadata','scope_ref':call.scope_id},parent=call)
        before=await ctx.call('tools','list',{},parent=call)
        try:
            outcome=await ctx.call('driver','run',dict(p,scenario='dynamic'),parent=call)
            await ctx.call('tools','unregister',registration,parent=call)
            after=await ctx.call('tools','list',{},parent=call)
            try:await ctx.call('tools','execute',{'name':'inspect_metadata','arguments':{},'tool_call_id':'after-revoke'},parent=call)
            except KernelError as exc:denied=exc.envelope
            else:raise AssertionError('Revoked tool executed')
            return {'before':[t['name'] for t in before],'after':[t['name'] for t in after],'revoked':denied,'result':outcome}
        finally:
            emit(ctx,'workflow.completed',{'registration':registration['id']},call)
    ctx.expose('run',run)
