"""Generic episode runner. Dataset contents and scoring are bound capabilities."""
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from agentlab.abi import KernelError
from agentlab_app.benchmarks import write_json
from .common import emit


async def apply(ctx, config):
    path = Path(config['directory']); path.mkdir(parents=True, exist_ok=True)
    selection = json.loads(Path(config['selection_path']).read_text(encoding='utf-8'))
    checkpoint = path/'progress.json'
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {
        'total': len(selection['ids']), 'tasks': [], 'status':'ready', 'current':None,
        'selection_sha256': selection['selection_sha256'], 'evaluation':selection.get('evaluation',{'version':selection.get('dataset',{}).get('scorer')})}
    import hashlib
    frozen={k:v for k,v in selection.items() if k!='selection_sha256'}
    if hashlib.sha256(json.dumps(frozen,sort_keys=True).encode()).hexdigest()!=selection['selection_sha256']:
        raise ValueError('Benchmark selection digest mismatch')
    if state['selection_sha256'] != selection['selection_sha256']: raise ValueError('Benchmark selection changed')
    paused = False
    def save():
        done = [r for r in state['tasks'] if r['status'] in ('completed','truncated','step_completed','abstained','context_limit','budget_limit','filtered')]
        state.update(finished=len(state['tasks']), scored=len(done), errors=sum(r['status']=='error' for r in state['tasks']),
            correct=sum(r.get('em',0) for r in done),
            em_all_completed=sum(r.get('em',0) for r in done)/len(state['tasks']) if state['tasks'] else None,
            em=sum(r.get('em',0) for r in done)/len(done) if done else None,
            f1=sum(r.get('f1',0) for r in done)/len(done) if done else None,
            tokens=sum(r.get('tokens',0) for r in state['tasks']),
            requests=sum(r.get('requests',0) for r in state['tasks']),
            unknown_requests=sum(r.get('unknown_requests',0) for r in state['tasks']))
        write_json(checkpoint, state)
    async def pause(p,c):
        nonlocal paused
        paused = True; state['pause_requested']=True; save()
        return {'accepted':True,'at':'task_boundary'}
    async def run(p,c):
        nonlocal paused
        paused=False; state['pause_requested']=False
        # A process restart must never silently resend an uncertain episode.
        if state.get('current'):
            current=state['current'];after=await ctx.call('budget','remaining',{},parent=c);before=current.pop('usage_before')
            state['tasks'].append({**current,'status':'error','error':{'code':'BENCHMARK.INTERRUPTED','outcome_known':False},
                'tokens':after['used']-before['used'],'requests':after['requests']-before['requests'],
                'unknown_requests':after['unknown_requests']-before['unknown_requests']}); state['current']=None
        completed={r['id'] for r in state['tasks']}
        state['status']='running'; save()
        try:
            for index, ident in enumerate(selection['ids']):
                if ident in completed: continue
                if paused: state['status']='paused'; break
                before=await ctx.call('budget','remaining',{},parent=c)
                stamp=datetime.now(timezone.utc).isoformat(); started=time.monotonic()
                row={'id':ident,'index':index+1,'session':'task-'+str(index+1),'started_at':stamp,'usage_before':before}
                state['current']=row; save(); emit(ctx,'benchmark.task_started',row,c)
                try:
                    task=await ctx.call('adapter','open',{'id':ident},parent=c)
                    row.update(metadata=task.get('metadata',{}),media=task.get('media',{}))
                    response=await ctx.call('agent','run',{'task':task['task'],'session':row['session']},parent=c)
                    score=await ctx.call('adapter','score',{'answer':response.get('answer','')},parent=c)
                    row.update(response,**score)
                except KernelError as exc:
                    if exc.envelope['code']=='EXECUTION.CANCELLED': raise
                    row.update(status='error',error=exc.envelope)
                after=await ctx.call('budget','remaining',{},parent=c)
                row.update(tokens=after['used']-before['used'],requests=after['requests']-before['requests'],
                    unknown_requests=after['unknown_requests']-before['unknown_requests'],elapsed_seconds=round(time.monotonic()-started,3),
                    ended_at=datetime.now(timezone.utc).isoformat())
                row.pop('usage_before',None)
                await ctx.call('adapter','close',{},parent=c)
                state['tasks'].append(row); state['current']=None; save(); emit(ctx,'benchmark.task_completed',row,c)
                if row.get('status')=='budget_limit':
                    state['status']='paused';break
                if row.get('error') and (row['error'].get('cause') or {}).get('code','').startswith('BUDGET.'):
                    state['status']='paused'; break
            else: state['status']='completed_with_errors' if state.get('errors') else 'completed'
            save(); emit(ctx,'benchmark.completed',{'status':state['status'],'finished':state['finished'],'total':state['total']},c)
            return dict(state)
        except BaseException:
            state['status']='interrupted'; save(); raise
    ctx.expose('run',run); ctx.expose('pause',pause)
