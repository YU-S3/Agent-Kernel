"""AG01–AG30: production Kernel + reference plugins, no paid model transport."""
import asyncio
import json
import os
from pathlib import Path
import socket
import uuid
import pytest
from agentlab.abi import KernelError,PluginDefinition,digest
from agentlab.host import PluginHost
from agentlab.observation import snapshot,ObservationLog
from agentlab_app.distribution import build,load,ROOT


@pytest.fixture
def case(request,tmp_path,monkeypatch):
    ident=request.node.name.split('_')[1]
    root=Path(os.environ.get('AGENTLAB_REPORT_DIR',tmp_path))
    if not root.is_absolute():root=ROOT/root
    root=root/ident
    if root.exists():root=root/uuid.uuid4().hex[:8]
    root.mkdir(parents=True)
    original=socket.socket.connect
    def offline(sock,address):
        if isinstance(address,tuple) and address[0] not in ('127.0.0.1','::1','localhost'):raise AssertionError('Offline suite forbids external network')
        return original(sock,address)
    monkeypatch.setattr(socket.socket,'connect',offline)
    hosts=[];proof={}
    class Case:
        path=root
        evidence=proof
        def adopt(self,h):
            h.audit_path=root/f'run-{len(hosts)}'/'kernel_events.jsonl'
            hosts.append(h)
            return h
        async def make(self,profile='viz-basic',**kwargs):
            folder=root/f'run-{len(hosts)}'
            h,p,n=await load(folder,profile,**kwargs);hosts.append(h)
            return h,p,n
        async def finish(self):
            for h in hosts:
                await h.unload()
                assert all(i.scope.closed for i in h.instances.values())
                assert all(e.done.is_set() for e in h.supervisor.executions.values())
            rows=[json.loads(line) for p in root.glob('run-*/kernel_events.jsonl') for line in p.read_text().splitlines()]
            (root/'kernel_events.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
            for name,topic in [('business_trace.jsonl',None),('tool_receipts.jsonl','tool.receipt')]:
                selected=[r for r in rows if r['event']=='plugin.detail' and (topic is None or r.get('topic')==topic)]
                (root/name).write_text(''.join(json.dumps(r)+'\n' for r in selected))
            (root/'composition.lock').write_text(json.dumps([h.lock for h in hosts],indent=2))
            (root/'run_manifest.json').write_text(json.dumps({'case':ident,'seed':42,'model_http_requests':0,
                'mode':'offline','locks':[h.lock['digest'] for h in hosts]},indent=2))
            (root/'cleanup_report.json').write_text(json.dumps({'hosts':len(hosts),'all_scopes_closed':all(h.scope.closed for h in hosts),
                'all_executions_terminal':all(e.done.is_set() for h in hosts for e in h.supervisor.executions.values())},indent=2))
    c=Case();yield c
    (root/'result.json').write_text(json.dumps({'status':'passed' if getattr(request.node,'rep_call',None) and request.node.rep_call.passed else 'failed','evidence':proof},indent=2))


def run_case(case,fn):
    async def run():
        try:await fn()
        finally:await case.finish()
    asyncio.run(run())


def details(h,topic):return [r['detail'] for r in h.records if r.get('topic')==topic]
async def invoke(h,i,m,p):return await h.invoke(i,m,p).wait()
async def wait_until(fn,seconds=5):
    for _ in range(int(seconds/.01)):
        if fn():return
        await asyncio.sleep(.01)
    raise AssertionError('Condition not reached')


def test_AG01_entry_is_application_choice(case):
    async def test():
        r,data,p,n=build(case.path/'bare');data.pop('entry');h=case.adopt(PluginHost(r))
        await h.load(data)
        try:
            with pytest.raises(KernelError,match='ENTRY_MISSING'):await h.run(payload=p)
        finally:await h.unload()
        case.evidence.update(load_without_entry=True,run_rejected=True)
    run_case(case,test)


def test_AG02_two_step_real_tool_loop(case):
    async def test():
        h,p,n=await case.make();before=(case.path/'run-0/workspace/data.txt').read_bytes()
        out=await h.run(payload=p);assert out['answer']=='3 行'
        assert len(details(h,'provider.executed'))==1
        requests=details(h,'model.request');assert len(requests)==2
        assert requests[1]['messages'][-1]['role']=='tool'
        assert requests[1]['messages'][-1]['tool_call_id']=='call-1'
        assert (case.path/'run-0/workspace/data.txt').read_bytes()==before
        case.evidence.update(answer=out,tool_executions=1,observation_in_second_request=True)
    run_case(case,test)


def test_AG03_dynamic_registration_lifecycle(case):
    async def test():
        h,p,n=await case.make('viz-dynamic-tools');out=await h.run(payload=p)
        assert 'inspect_metadata' in out['before'] and 'inspect_metadata' not in out['after']
        assert out['revoked']['cause']['code']=='TOOL.NOT_VISIBLE'
        assert any(t['name']=='inspect_metadata' for t in details(h,'model.request')[0]['tools'])
        case.evidence.update(out)
    run_case(case,test)


def test_AG04_schema_duplicate_and_no_effect(case):
    async def test():
        h,p,n=await case.make()
        for request,code in [({'provider':'file','name':'read_file','global':True},'TOOL.DUPLICATE'),
            ({'provider':'file','definition':{'name':'x','input_schema':{'type':'not-a-type'}}},'TOOL.INVALID_SCHEMA')]:
            with pytest.raises(KernelError,match=code):await invoke(h,'tools','register',request)
        with pytest.raises(KernelError,match='INVALID_ARGUMENTS'):await invoke(h,'tools','execute',{'name':'read_file','arguments':{'path':42},'tool_call_id':'bad'})
        assert not details(h,'provider.executed');case.evidence['executions']=0
    run_case(case,test)


def test_AG05_scoped_visibility_and_dispatch(case):
    async def test():
        h,p,n=await case.make(overrides={'agent':{'control':{'permissions':['tool:fixture_http']}}})
        with pytest.raises(KernelError,match='NOT_VISIBLE'):await h.run(payload=p)
        assert [t['name'] for t in details(h,'model.request')[0]['tools']]==['fixture_http']
        assert not details(h,'provider.executed');case.evidence['execute_bypass_blocked']=True
    run_case(case,test)


def test_AG06_two_calls_keep_text_and_order(case):
    async def test():
        h,p,n=await case.make();await h.run(payload={**p,'scenario':'multi'})
        facts=(await invoke(h,'history','read',{'session':'main'}))['messages']
        assert facts[1]['content'] and len(facts[1]['tool_calls'])==2
        assert [m['tool_call_id'] for m in facts if m['role']=='tool']==['call-1','call-2']
        case.evidence['facts']=facts
    run_case(case,test)


def test_AG07_error_layers_no_retry(case):
    async def test():
        h,p,n=await case.make();await h.run(payload={**p,'scenario':'business_error'})
        assert details(h,'tool.receipt')[-1]['is_error']
        for scenario,code in [('429','PLUGIN.REPORTED_ERROR'),('crash','PLUGIN.UNHANDLED_EXCEPTION')]:
            with pytest.raises(KernelError,match=code):await h.run(payload={**p,'scenario':scenario,'session':scenario})
        assert len(details(h,'model.request'))==4
        case.evidence.update(requests=4,automatic_retries=0)
    run_case(case,test)


def test_AG08_real_shell_and_python_containers(case):
    async def test():
        h,p,n=await case.make()
        for scenario in ('shell','python'):assert (await h.run(payload={**p,'scenario':scenario,'session':scenario}))['status']=='completed'
        receipts=details(h,'sandbox.receipt');assert len(receipts)==2
        assert all(r['exit_code']==0 and r['cleanup'] and r['actual']['NetworkMode']=='none' for r in receipts)
        assert '3 data.txt' in receipts[0]['stdout'];assert receipts[1]['stdout'].strip()=='3'
        case.evidence['enforcement']=receipts
    run_case(case,test)


def test_AG09_os_enforcement_escape_network_timeout_process_tree(case):
    async def test():
        h,p,n=await case.make(overrides={'sandbox':{'config':{'root':str(case.path/'run-0/workspace'),'timeout_seconds':1.2}}})
        workspace=case.path/'run-0/workspace';(workspace/'escape').symlink_to('/etc/passwd')
        for path in ('../outside','/etc/passwd','escape'):
            with pytest.raises(KernelError,match='WORKSPACE.'):await invoke(h,'workspace','read',{'path':path})
        marker=case.path/'host-only';marker.write_text('not mounted')
        code=f"import pathlib,socket\np=pathlib.Path({str(marker)!r}); print('host_visible',p.exists())\ntry:\n pathlib.Path('/workspace/data.txt').write_text('changed')\nexcept OSError: print('read_only')\ns=socket.socket();s.settimeout(.2)\ntry:s.connect(('1.1.1.1',443));print('network_open')\nexcept OSError:print('network_denied')"
        out=await invoke(h,'sandbox','run',{'kind':'python','code':code})
        assert 'host_visible False' in out['stdout'] and 'read_only' in out['stdout'] and 'network_denied' in out['stdout']
        with pytest.raises(KernelError,match='TIMEOUT'):await invoke(h,'sandbox','run',{'kind':'python','code':'import subprocess,time; subprocess.Popen(["sleep","30"]);time.sleep(30)'})
        assert details(h,'sandbox.receipt')[-1]['cleanup']
        limits=await invoke(h,'sandbox','run',{'kind':'python','code':'import subprocess\nchildren=[]\ntry:\n for i in range(45):children.append(subprocess.Popen(["sleep","10"]))\nexcept OSError:print("pid_limit_enforced")\nfinally:\n for child in children:child.kill()'})
        assert 'pid_limit_enforced' in limits['stdout']
        memory=await invoke(h,'sandbox','run',{'kind':'python','code':'x=bytearray(300*1024*1024);print(len(x))'})
        assert memory['oom_killed'] and memory['exit_code']!=0
        output=await invoke(h,'sandbox','run',{'kind':'python','code':'from pathlib import Path;Path("/output/report.txt").write_text("artifact");print("x"*50000)'})
        assert output['stdout_bytes']>8192 and len(output['stdout'])==8192 and output['artifacts']==[{'path':'report.txt','text':'artifact'}]
        case.evidence['enforcement']=details(h,'sandbox.receipt')
    run_case(case,test)


def test_AG10_cancel_settles_pending_without_replay(case):
    async def test():
        h,p,n=await case.make(overrides={'fixture':{'config':{'delay':5}}})
        ex=h.invoke('agent','run',{**p,'scenario':'multi'})
        await wait_until(lambda:len(details(h,'provider.executed'))==2);ex.cancel()
        with pytest.raises(KernelError,match='CANCELLED'):await ex.wait()
        facts=(await invoke(h,'history','read',{'session':'main'}))['messages']
        assert facts[-1]['status']=='cancelled_or_unknown'
        assert len(details(h,'provider.executed'))==2
        await h.run(payload={**p,'scenario':'final'})
        assert len(details(h,'provider.executed'))==2
        case.evidence.update(facts=facts,no_replay=True)
    run_case(case,test)


def test_AG11_import_transaction_rollback(case):
    async def test():
        r,data,p,n=build(case.path/'bad');data['instances']['tools']['config']['static']=['file','file']
        h=case.adopt(PluginHost(r));await h.load(data)
        try:
            with pytest.raises(KernelError,match='DUPLICATE'):await invoke(h,'tools','bootstrap',{})
            assert await invoke(h,'tools','list',{})==[]
        finally:await h.unload()
        assert h.scope.closed;case.evidence.update(registry_empty=True,cleanup=True,primary_error='TOOL.DUPLICATE')
    run_case(case,test)


def test_AG12_arbitrary_names_generate_topology(case):
    async def test():
        h,p,n=await case.make('viz-random-names');out=await h.run(payload=p)
        view=snapshot(h);assert all(k.startswith('node_') for k in view['instances'])
        assert out['answer']=='3 行' and view['entry']['instance'] in view['instances']
        case.evidence.update(nodes=list(view['instances']),entry=view['entry'])
    run_case(case,test)


def test_AG13_actual_browser_interactions(case):
    async def test():
        evidence=Path(os.environ.get('AGENTLAB_BROWSER_EVIDENCE',ROOT/'reports/browser/browser_validation.json'))
        assert evidence.is_file(),'Run the real browser acceptance and save its evidence first'
        proof=json.loads(evidence.read_text())
        for name in ('run','pan','node_drag','zoom','fit','manual_camera_preserved','dynamic_spawn','replay','approval','mobile','console_clean'):
            assert proof['checks'][name] is True,name
        assert proof['frontend_digest']==digest({p.name:p.read_text() for p in (ROOT/'src/agentlab_app/static').iterdir() if p.is_file()})
        case.evidence.update(proof)
    run_case(case,test)


def test_AG14_slow_observer_bounded_gap_no_effect(case):
    async def test():
        h,p,n=await case.make();h.observation.rows=__import__('collections').deque(maxlen=5);h.records=h.observation.rows
        out=await h.run(payload=p)
        read=h.observation.read(0);assert read['gap'] and len(read['events'])<=5 and out['answer']=='3 行'
        case.evidence.update(observer_gap=True,answer=out['answer'])
    run_case(case,test)


def test_AG15_model_selected_delegation(case):
    async def test():
        h,p,n=await case.make('viz-subagent');out=await h.run(payload=p)
        children=details(h,'subagent.finished');assert len(children)==2
        assert len({r['session'] for r in children})==2 and all(r['status']=='completed' for r in children)
        receipts=[r for r in details(h,'tool.receipt') if r['name']=='delegate_agent'];assert len(receipts)==2
        assert out['answer']=='3 行';case.evidence['children']=children
    run_case(case,test)


def test_AG16_direct_parallel_children_cancel_one(case):
    async def test():
        h,p,n=await case.make('viz-multi-agent');out=await h.run(payload={**p,'cancel_first':True,'first_delay':.3})
        assert out['children'][0]['error']['code']=='EXECUTION.CANCELLED'
        assert out['children'][1]['answer']=='3 行'
        assert h.instances['model'].state=='READY';case.evidence.update(out)
    run_case(case,test)


def test_AG17_child_permissions_cannot_expand(case):
    async def test():
        h,p,n=await case.make(overrides={'manager':{'control':{'permissions':['tool:read_file']}}})
        with pytest.raises(KernelError,match='PERMISSION_EXPANSION'):await invoke(h,'manager','delegate',{'task':'bad','tools':['shell']})
        assert not details(h,'subagent.started');case.evidence['escalation_blocked']=True
    run_case(case,test)


def test_AG18_child_crash_parent_cancel_cleanup(case):
    async def test():
        h,p,n=await case.make()
        with pytest.raises(KernelError,match='UNHANDLED_EXCEPTION'):await invoke(h,'manager','delegate',{'task':'crash','scenario':'crash'})
        ex=h.invoke('manager','delegate',{'task':'slow','delay':2});await wait_until(lambda:len(details(h,'subagent.started'))==2);ex.cancel()
        with pytest.raises(KernelError,match='CANCELLED'):await ex.wait()
        assert all(i.scope.closed for key,i in h.instances.items() if key.startswith('manager-'))
        assert h.instances['model'].state=='READY';case.evidence['shared_provider_retained']=True
    run_case(case,test)


def test_AG19_history_sqlite_survives_reopen(case):
    async def test():
        db=case.path/'persist.sqlite'
        h,p,n=await case.make(overrides={'history':{'plugin':'history.sqlite','config':{'backend':'sqlite','path':str(db)}}})
        await h.run(payload=p);before=(await invoke(h,'history','read',{'session':'main'}))['messages'];await h.unload()
        h2,p,n=await case.make(overrides={'history':{'plugin':'history.sqlite','config':{'backend':'sqlite','path':str(db)}}})
        assert (await invoke(h2,'history','read',{'session':'main'}))['messages']==before
        await h2.run(payload={**p,'scenario':'final'})
        assert len((await invoke(h2,'history','read',{'session':'main'}))['messages'])>len(before)
        case.evidence.update(persisted_messages=len(before),reopened=True)
    run_case(case,test)


def test_AG20_projection_does_not_delete_facts(case):
    async def test():
        h,p,n=await case.make(context='window');await h.run(payload=p);await h.run(payload={**p,'scenario':'final'})
        facts=(await invoke(h,'history','read',{'session':'main'}))['messages'];visible=details(h,'model.request')[-1]['messages']
        assert len(visible)<len(facts) and len(facts)==6
        case.evidence.update(facts=len(facts),visible=len(visible))
    run_case(case,test)


def test_AG21_driver_swap_only_composition(case):
    async def test():
        counts=[]
        for profile in ('viz-basic','viz-single-step','viz-plan-execute'):
            h,p,n=await case.make(profile);out=await h.run(payload=p);counts.append(len(details(h,'model.request')))
            assert out['status'] in ('completed','step_completed')
        assert counts==[2,1,3];case.evidence['model_calls']=counts
    run_case(case,test)


def test_AG22_spawn_call_and_dispose_observable(case):
    async def test():
        h,p,n=await case.make('viz-dynamic-spawn');out=await h.run(payload=p)
        assert h.instances[out['instance']].scope.closed
        rows=[r for r in h.records if r.get('instance')==out['instance']]
        assert any(r['event']=='instance.spawn' for r in rows) and any(r['event']=='instance.dispose' for r in rows)
        case.evidence.update(instance=out['instance'],events=[r['event'] for r in rows])
    run_case(case,test)


def test_AG23_shared_model_capacity_sessions(case):
    async def test():
        h,p,n=await case.make('viz-multi-agent',overrides={'model':{'execution':{'max_inflight':1,'max_queue':16}}})
        out=await h.run(payload=p);assert len(out['children'])==2
        requests=details(h,'model.request');assert len({r['session'] for r in requests})==2
        active=0;peak=0
        for row in h.records:
            if row.get('instance')!='model':continue
            if row['event']=='execution.started':active+=1;peak=max(peak,active)
            if row['event'] in ('execution.succeeded','execution.failed','execution.cancelled'):active-=1
        assert peak==1;case.evidence['maximum_model_concurrency']=peak
    run_case(case,test)


def test_AG24_external_native_harness_stream_cancel_crash(case):
    async def test():
        h,p,n=await case.make('viz-external');assert (await h.run(payload=p))['value']==p
        ex=h.invoke('external','emit',3,streaming=True);chunks=[x async for x in ex.stream()];assert chunks==[0,1,2]
        ex=h.invoke('external','slow',2);await asyncio.sleep(.03);ex.cancel()
        with pytest.raises(KernelError,match='CANCELLED'):await ex.wait()
        with pytest.raises(KernelError,match='RESULT_UNKNOWN|TRANSPORT_LOST'):await invoke(h,'external','fail',{})
        case.evidence.update(progress=chunks,internal_steps_inferred=False,automatic_retry=False)
    run_case(case,test)


def test_AG25_partial_tool_json_never_executed(case):
    async def test():
        h,p,n=await case.make()
        with pytest.raises(KernelError,match='INCOMPLETE_TOOL_CALL'):await h.run(payload={**p,'scenario':'malformed'})
        assert not details(h,'provider.executed')
        facts=(await invoke(h,'history','read',{'session':'main'}))['messages'];assert len(facts)==1
        await h.run(payload=p);assert len(details(h,'provider.executed'))==1
        case.evidence.update(partial_executions=0,complete_executions=1)
    run_case(case,test)


def test_AG26_mcp_uses_same_registry_validation(case):
    async def test():
        h,p,n=await case.make('viz-mcp');await h.run(payload=p)
        receipts=details(h,'tool.receipt');assert len(receipts)==1 and receipts[0]['name']=='mcp_echo'
        with pytest.raises(KernelError,match='INVALID_ARGUMENTS'):await invoke(h,'tools','execute',{'name':'mcp_echo','arguments':{'text':7}})
        assert len(details(h,'provider.executed'))==1;case.evidence['receipt']=receipts[0]
    run_case(case,test)


def test_AG27_persisted_replay_matches_live_ids(case):
    async def test():
        h,p,n=await case.make('viz-subagent');await h.run(payload=p)
        live=snapshot(h);disk=[json.loads(line) for line in (case.path/'run-0/kernel_events.jsonl').read_text().splitlines()]
        created={r['execution']:r for r in disk if r['event']=='execution.created'}
        assert set(created)==set(live['executions'])
        assert all(created[k]['scope']==v['scope'] and created[k]['caller']==v['caller'] for k,v in live['executions'].items())
        assert [r['seq'] for r in disk]==list(range(1,len(disk)+1))
        replay=await invoke(h,'trace','replay',{});assert not replay['gap']
        case.evidence.update(executions=len(created),scope_and_edges_equal=True)
    run_case(case,test)


def test_AG28_shared_budget_refuses_without_double_spend(case):
    async def test():
        h,p,n=await case.make('viz-multi-agent',overrides={'budget':{'config':{'tokens':1000,'requests':2}}})
        out=await h.run(payload=p)
        assert all(r['error']['cause']['code']=='BUDGET.EXHAUSTED' for r in out['children'])
        budget=await invoke(h,'budget','remaining',{});assert budget['requests']==2 and budget['used']==40 and budget['reserved']==0
        case.evidence.update(budget)
    run_case(case,test)


def test_AG29_manual_approval_deny_then_allow(case):
    async def test():
        h,p,n=await case.make('viz-approval')
        for allow in (False,True):
            ex=h.invoke('agent','run',{**p,'session':str(allow)})
            pending=[]
            for _ in range(200):
                pending=await invoke(h,'policy','pending',{})
                if pending:break
                await asyncio.sleep(.01)
            assert pending
            assert len(details(h,'provider.executed'))==0
            await invoke(h,'policy','decide',{'id':pending[0]['id'],'allow':allow})
            await ex.wait()
            assert len(details(h,'provider.executed'))==int(allow)
        case.evidence['decisions']=details(h,'approval.decision')
        ex=h.invoke('agent','run',{**p,'session':'cancel-approval'})
        await wait_until(lambda:len(details(h,'approval.request'))==3)
        ex.cancel()
        with pytest.raises(KernelError,match='CANCELLED'):await ex.wait()
        assert details(h,'approval.decision')[-1]['status']=='cancelled'
        assert await invoke(h,'policy','pending',{})==[]
    run_case(case,test)


def test_AG30_frozen_repeat_causal_equivalence(case):
    async def test():
        h,p,n=await case.make();lock=h.lock;outputs=[];causes=[]
        for i in range(2):
            start=h.observation.sequence;outputs.append(await h.run(payload={**p,'session':f'repeat-{i}'}))
            causes.append([(r['instance'],r['method']) for r in h.records if r['seq']>start and r['event']=='execution.created'])
        assert causes[0]==causes[1] and outputs[0]['answer']==outputs[1]['answer'] and h.lock==lock
        assert len(details(h,'provider.executed'))==2
        case.evidence.update(causal_equal=True,lock_digest=lock['digest'],tool_calls_per_run=1)
    run_case(case,test)
