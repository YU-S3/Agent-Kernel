"""Adversarial lifecycle/ABI cases, independent of any agent algorithm."""
import asyncio
import base64
from copy import deepcopy
import json
from pathlib import Path
import pytest
from agentlab.kernel import Registry, PluginHost, PluginDefinition, KernelError, Handle
from test_kernel_v1 import setup, package, ROOT


def test_K06_unused_invalid_instance_and_schema_fail_before_apply(tmp_path):
    async def run():
        host,data=package(tmp_path,"async def apply(ctx,c):\n raise AssertionError('must not apply')\n")
        data['instances']['b']['config']=[]
        with pytest.raises(KernelError,match='CONFIG_INVALID'):await host.load(data)
        assert not host.instances
        host,data=package(tmp_path,"async def apply(ctx,c):\n raise AssertionError('must not apply')\n")
        manifest=json.loads((tmp_path/'plugin.json').read_text());manifest['config_schema']={'$ref':'https://invalid.example/schema'}
        (tmp_path/'plugin.json').write_text(json.dumps(manifest))
        d=PluginDefinition.read(tmp_path);r=Registry();r.register(d);host=PluginHost(r);data['packages'][0]['digest']=d.digest
        with pytest.raises(KernelError,match='CONFIG_INVALID'):await host.load(data)
        assert not host.instances
    asyncio.run(run())


@pytest.mark.parametrize('remote',[False,True])
def test_K05_startup_is_deferred_and_shared_cancellation(tmp_path,remote):
    async def run():
        host,data=package(tmp_path,"""import asyncio
async def apply(ctx,c):
 state=[]
 async def run(p,call):
  if p=='background': state.append('started');return None
  if p=='slow': await asyncio.sleep(2)
  return {'state':state,'scope':call.scope_id,'caller':call.caller_id}
 async def ready(startup):
  assert startup.scope_id==ctx.scope_id and startup.execution_id.startswith('startup:')
  await ctx.start_call(ctx.self_handle,'run','background',parent=startup)
  assert not state
 ctx.expose('run',run);ctx.on_ready(ready)
""")
        for inst in data['instances'].values():
            inst['execution']={'max_inflight':3}
            if remote:inst['execution'].update(isolation='process',backend='local');inst['control']={'network':'inherit'}
        await host.load(data)
        try:
            await asyncio.sleep(.03)
            a=host.invoke('a','run','slow');b=host.invoke('b','run','slow')
            a.cancel()
            with pytest.raises(KernelError,match='CANCELLED'):await a.wait()
            assert (await host.invoke('a','run',None).wait())['state']==['started']
            assert b.state in ('created','running')
            result=await host.invoke('b','run',None).wait();assert result['scope'] and result['caller'] is None
            b.cancel()
            with pytest.raises(KernelError,match='CANCELLED'):await b.wait()
        finally:await host.unload()
    asyncio.run(run())


def test_K08_K15_unforgeable_handles_and_scope_grant_shrink():
    async def run():
        host,data=setup();data['control']={'env':[],'permissions':[]}
        data['instances']['b']['control']={'env':['PRIVATE_KEY']}
        with pytest.raises(KernelError,match='DENIED'):await host.load(data)
        host,data=setup();await host.load(data)
        try:
            ctx=host.instances['b'].context
            with pytest.raises(KernelError,match='DENIED'):ctx.target(Handle('b',ctx.self_handle.token))
            with pytest.raises(KernelError,match='DENIED'):ctx.parent(type('Fake',(),{'execution_id':'unknown'})())
            with pytest.raises(KernelError,match='DENIED'):ctx.target('b')
        finally:await host.unload()
    asyncio.run(run())


@pytest.mark.parametrize('remote',[False,True])
def test_K09_authorized_resource_ranges_and_revocation(tmp_path,remote):
    async def run():
        host,data=package(tmp_path,"""async def apply(ctx,c):
 async def run(p,call): return await ctx.read_resource(p,parent=call,offset=2,length=4)
 ctx.expose('run',run)
""")
        if remote:
            for row in data['instances'].values():row.update(execution={'isolation':'process','backend':'local'},control={'network':'inherit'})
        await host.load(data)
        ref=host.publish_resource('a',b'0123456789')
        assert base64.b64decode(await host.invoke('a','run',ref).wait())==b'2345'
        with pytest.raises(KernelError,match='DENIED'):await host.invoke('b','run',ref).wait()
        with pytest.raises(KernelError,match='DENIED'):await host.invoke('a','run',{**ref,'size':99}).wait()
        short=host.publish_resource('a',b'x',ttl=.001);await asyncio.sleep(.01)
        with pytest.raises(KernelError,match='DENIED'):await host.invoke('a','run',short).wait()
        await host.unload()
        assert not host.control.resources
    asyncio.run(run())


def test_K09_explicit_local_only_never_crosses_process(tmp_path):
    async def run():
        _,data=package(tmp_path,"async def apply(ctx,c):\n async def run(p,call): return p\n ctx.expose('run',run)\n")
        manifest=json.loads((tmp_path/'plugin.json').read_text());manifest['local_only']=['run']
        (tmp_path/'plugin.json').write_text(json.dumps(manifest))
        d=PluginDefinition.read(tmp_path);r=Registry();r.register(d);host=PluginHost(r);data['packages'][0]['digest']=d.digest
        await host.load(data)
        obj=object();assert await host.invoke('a','run',obj).wait() is obj
        assert host.records[-2]['input_digest'] is None
        await host.unload()
        data['instances']['b'].update(execution={'isolation':'process','backend':'local'},control={'network':'inherit'})
        with pytest.raises(KernelError,match='CONFIG_INVALID'):await PluginHost(r).load(data)
    asyncio.run(run())


def test_K14_cleanup_retains_primary_and_redacts_audit(tmp_path,monkeypatch):
    monkeypatch.setenv('AK_SECRET','private-test-value')
    async def run():
        host,data=package(tmp_path,"""from agentlab.abi import PluginOperationError
async def apply(ctx,c):
 async def cleanup(): raise RuntimeError('cleanup')
 ctx.on_cleanup(cleanup)
 raise PluginOperationError('business.failure','private-test-value')
""")
        data['control']={'env':['AK_SECRET']}
        with pytest.raises(KernelError,match='REPORTED_ERROR') as error:await host.load(data)
        assert error.value.diagnostics and error.value.envelope['cause']['code']=='business.failure'
        assert 'private-test-value' not in json.dumps(host.records)
        assert 'private-test-value' not in str(error.value) and 'private-test-value' not in repr(error.value)
    asyncio.run(run())


def test_K16_per_instance_timeout_cannot_be_relaxed_by_invocation():
    async def run():
        host,data=setup();data['instances']['b']['control']={'max_wall_seconds':.01}
        await host.load(data)
        try:
            with pytest.raises(KernelError,match='TIMEOUT'):await host.invoke('b','slow',1,timeout=20).wait()
        finally:await host.unload()
    asyncio.run(run())


def test_K17_source_mutation_rejected_before_load(tmp_path):
    async def run():
        host,data=package(tmp_path,"async def apply(ctx,c): pass\n")
        (tmp_path/'plugin.py').write_text("raise AssertionError('modified')\n")
        with pytest.raises(KernelError,match='VERSION_MISMATCH'):await host.load(data)
        assert not host.instances
    asyncio.run(run())


def test_K17_dependency_bytes_changed_without_version_change(tmp_path,monkeypatch):
    from agentlab import identity
    path=tmp_path/'dependency.py';path.write_text('value=1\n')
    class Entry(str):hash=True
    class Dist:
        metadata={'Name':'fixture-dependency'};version='1.0';files=[Entry('dependency.py')]
        def read_text(self,name):return 'unchanged-record' if name=='RECORD' else None
        def locate_file(self,name):return tmp_path/name
    monkeypatch.setattr(identity,'distributions',lambda:[Dist()])
    monkeypatch.setattr(identity,'_dependency_snapshots',{})
    async def run():
        host,data=setup();await host.load(data);lock=host.lock;await host.unload()
        path.write_text('value=22\n')
        new,_=setup()
        with pytest.raises(KernelError,match='VERSION_MISMATCH'):await new.load(data,expected_lock=lock)
        assert not new.instances
    asyncio.run(run())


def test_K13_nested_error_retains_callee_identity(tmp_path):
    async def run():
        host,data=package(tmp_path,"""from agentlab.abi import PluginOperationError
async def apply(ctx,c):
 async def run(p,call):
  if p: return await ctx.call('peer','run',False,parent=call)
  raise PluginOperationError('provider.rate_limited')
 ctx.expose('run',run)
""",{'peer':['run']})
        await host.load(data)
        try:
            with pytest.raises(KernelError,match='REPORTED_ERROR') as error:await host.invoke('a','run',True).wait()
            assert error.value.envelope['instance_id']=='b'
            assert error.value.envelope['cause']['code']=='provider.rate_limited'
            assert len(host.supervisor.executions)==2
        finally:await host.unload()
    asyncio.run(run())


def test_K09_Node_calls_and_streams_through_host_to_Python(tmp_path):
    import shutil
    async def run():
        root=tmp_path/'node';shutil.copytree(ROOT/'plugins/examples/external',root)
        (root/'application.mjs').write_text("""export async function apply(ctx,c){
ctx.expose('run',async(p,call)=>await ctx.call('helper','echo',p,{parent:call}));
ctx.expose('stream',async(p,call)=>{for await(const item of ctx.stream('helper','emit',p,{parent:call}))await call.progress(item);return p;});
}""")
        m=json.loads((root/'plugin.json').read_text());m['exports']=['run','stream'];m['requires']={'helper':['echo','emit']}
        (root/'plugin.json').write_text(json.dumps(m))
        host,data=setup();node=PluginDefinition.read(root);host.registry.register(node)
        data['packages'].append({'id':node.id,'version':node.version,'digest':node.digest})
        data['instances']['a']={'plugin':node.id,'execution':{'isolation':'process','backend':'local'},'control':{'network':'inherit'},'bindings':{'helper':'b'}}
        data['instances']['b'].update(execution={'isolation':'process','backend':'local'},control={'network':'inherit'})
        await host.load(data)
        try:
            assert (await host.invoke('a','run',7).wait())['value']==7
            ex=host.invoke('a','stream',40,streaming=True);items=[]
            async for item in ex.stream():items.append(item);await asyncio.sleep(.002)
            assert items==list(range(40)) and await ex.wait()==40
        finally:await host.unload()
    asyncio.run(run())


@pytest.mark.parametrize('kind',['services','external'])
def test_K11_cancel_backpressured_stream_keeps_shared_worker_alive(kind):
    async def run():
        host,data=setup((kind,),{'b':{'plugin':'example.'+kind,
            'execution':{'isolation':'process','backend':'local','max_inflight':3},'control':{'network':'inherit'}}})
        await host.load(data)
        try:
            stream=host.invoke('b','emit',100000,streaming=True)
            await asyncio.sleep(.05)
            other=host.invoke('b','slow',.15)
            stream.cancel()
            with pytest.raises(KernelError,match='CANCELLED'):await asyncio.wait_for(stream.wait(),2)
            await other.wait()
            method='echo' if kind=='services' else 'run'
            assert (await host.invoke('b',method,3).wait())['value']==3
        finally:await host.unload()
    asyncio.run(run())


@pytest.mark.parametrize('remote',[False,True])
def test_K09_package_relative_imports_are_isolated_per_instance(tmp_path,remote):
    async def run():
        (tmp_path/'helper.py').write_text('values=[]\n')
        host,data=package(tmp_path,"from .helper import values\nasync def apply(ctx,c):\n async def run(p,call):\n  values.append(p);return list(values)\n ctx.expose('run',run)\n")
        if remote:
            for row in data['instances'].values():row.update(execution={'isolation':'process','backend':'local'},control={'network':'inherit'})
        await host.load(data)
        try:
            assert await host.invoke('a','run',1).wait()==[1]
            assert await host.invoke('b','run',2).wait()==[2]
        finally:await host.unload()
    asyncio.run(run())
