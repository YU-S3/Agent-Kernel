import asyncio
import uuid
from .common import fail, emit


async def apply(ctx,config):
    active={};completed={};counts={}
    async def delegate(p,call):
        auth=ctx.authority(call)
        depth=sum(c['instance'].startswith(ctx.instance_id+'-') for c in auth['chain'])
        if depth>=config.get('max_depth',2):fail('SUBAGENT.DEPTH_LIMIT')
        root=auth['chain'][-1]['execution']
        if root not in counts:ctx.scope_cleanup(auth['chain'][-1]['scope'],lambda:counts.pop(root,None),parent=call)
        counts[root]=counts.get(root,0)+1
        if counts[root]>config.get('max_children',4):fail('SUBAGENT.COUNT_LIMIT')
        granted=set(auth['permissions'])
        tools=p.get('tools',config.get('default_tools',['read_file']))
        if not isinstance(tools,list) or any('tool:'+t not in granted for t in tools):fail('SUBAGENT.PERMISSION_EXPANSION')
        permissions=sorted((granted-{x for x in granted if x.startswith('tool:')})|{'tool:'+t for t in tools})
        permissions=[x for x in permissions if x!='tools:manage']
        bindings={name:ctx.instance.spec['bindings'][name] for name in ('model','tools','history','context','budget')}
        handle=await ctx.spawn_instance('child',bindings,{'max_steps':config.get('child_max_steps',4),'reservation_tokens':config.get('reservation_tokens',128)},parent=call,control={'permissions':permissions})
        session='child-'+uuid.uuid4().hex
        execution=await ctx.start_call(handle,'run',{'task':p['task'],'session':session,'scenario':p.get('scenario','read'),
                                      'delay':p.get('delay',.01)},parent=call)
        ident=uuid.uuid4().hex
        row={'id':ident,'session':session,'instance':handle.instance_id,'execution':execution.id,'parent':root,'status':'running'}
        active[ident]=(row,execution)
        emit(ctx,'subagent.started',row,call)
        try:
            out=await execution.wait();row['status']='completed'
            return dict(out,instance=handle.instance_id,delegation_id=ident)
        finally:
            if not execution.done.is_set():execution.cancel();await asyncio.gather(execution.wait(),return_exceptions=True)
            if row['status']=='running':row['status']=execution.state
            active.pop(ident,None);completed[ident]=dict(row)
            emit(ctx,'subagent.finished',row,call)
    async def status(p,call):return [dict(r) for r,e in active.values()]+list(completed.values())
    async def cancel(p,call):
        row=active.get(p['id'])
        if not row:return {'accepted':False}
        ancestors={c['execution'] for c in ctx.authority(call)['chain']}
        if call.caller_id is not None and row[0]['parent'] not in ancestors:fail('SUBAGENT.NOT_OWNED')
        row[1].cancel();return {'accepted':True}
    ctx.expose('delegate',delegate);ctx.expose('status',status);ctx.expose('cancel',cancel)
