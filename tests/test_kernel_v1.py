import asyncio
import json
from pathlib import Path
import pytest
from agentlab.kernel import Registry, PluginHost, PluginDefinition, KernelError

ROOT = Path(__file__).resolve().parents[1]
IMAGE = 'python@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159'


def setup(kinds=('services',), instances=None, entry=None, templates=None):
    registry = Registry()
    definitions = [PluginDefinition.read(ROOT/'plugins/examples'/kind) for kind in kinds]
    for definition in definitions: registry.register(definition)
    data = dict(kernel_api=1, composition='test', packages=[dict(id=d.id, version=d.version, digest=d.digest) for d in definitions],
                instances=instances or {'b': {'plugin': 'example.services'}})
    if entry: data['entry'] = entry
    if templates: data['templates'] = templates
    return PluginHost(registry), data


def test_K01_K02_K03_explicit_entry_and_multiple_instances():
    async def run():
        host, data = setup(instances={'b': {'plugin':'example.services'}, 'c': {'plugin':'example.services'}})
        await host.load(data)
        lock = host.lock
        with pytest.raises(KernelError, match='ENTRY_MISSING'): await host.run(payload=3)
        assert (await host.run(payload=3, entry={'instance':'c','method':'echo'}))['count'] == 1
        assert (await host.invoke('b','echo',4).wait())['count'] == 1
        assert host.lock == lock
        await host.unload()
    asyncio.run(run())


@pytest.mark.parametrize('backend', ['local', 'process'])
def test_K04_K07_K08_workflow_shared_and_spawn(backend):
    async def run():
        execution = {'isolation':'process','backend':'local'} if backend == 'process' else {}
        instances = {'a': {'plugin':'example.workflow', 'bindings':{'helper':'b','next':'c'}, 'execution':execution, 'control':{'network':'inherit'}},
                     'b': {'plugin':'example.services', 'execution':execution, 'control':{'network':'inherit'}, 'config':{'prefix':'B'}},
                     'c': {'plugin':'example.services'}}
        host,data = setup(('services','workflow'), instances, templates={'child': {'plugin':'example.services'}})
        await host.load(data)
        result = await host.invoke('a','run',5).wait()
        assert result['value']['prefix'] == 'B'
        assert (await host.invoke('a','spawn',4).wait())['prefix'] == 'child'
        children = [i for k,i in host.instances.items() if k.startswith('a-')]
        assert children and all(i.scope.closed for i in children)
        assert (await host.invoke('b','echo',4).wait())['count'] == 2
        await host.unload()
    asyncio.run(run())


def package(tmp_path, source, requires=None, exports=('run',)):
    (tmp_path/'plugin.py').write_text(source)
    (tmp_path/'plugin.json').write_text(json.dumps(dict(id='example.custom', version='1',kernel_api=1,
        loader={'adapter':'python','target':'plugin:apply'},requires=requires or {},exports=list(exports))))
    d=PluginDefinition.read(tmp_path); registry=Registry();registry.register(d)
    return PluginHost(registry),dict(kernel_api=1,composition='x', packages=[dict(id=d.id,version=d.version,digest=d.digest)],
        instances={'a':{'plugin':d.id,'bindings':{'peer':'b'} if requires else {}},'b':{'plugin':d.id,'bindings':{'peer':'a'} if requires else {}}})


def test_K05_reference_cycles_and_reentrancy(tmp_path):
    async def run():
        host,data=package(tmp_path,"async def apply(ctx,c):\n async def run(p,call):\n  if p: return await ctx.call('peer','run',p-1,parent=call)\n  return 0\n ctx.expose('run',run)\n",{'peer':['run']})
        await host.load(data)
        assert await host.invoke('a','run',1).wait() == 0
        with pytest.raises(KernelError,match='REENTRANCY_DENIED'): await host.invoke('a','run',2).wait()
        await host.unload()
    asyncio.run(run())


@pytest.mark.parametrize('source,code',[
    ("async def apply(ctx,c):\n await ctx.call('peer','run',{},parent=None)\n",'NOT_READY'),
    ("async def apply(ctx,c):\n pass\n",'EXPORT_MISSING'),
    ("async def apply(ctx,c):\n async def bad(): raise RuntimeError('cleanup')\n ctx.on_cleanup(bad)\n raise RuntimeError('primary')\n",'INIT_FAILED'),
    ("async def apply(ctx,c):\n async def run(p,call): return p\n async def ready(call): raise RuntimeError('ready')\n ctx.expose('run',run)\n ctx.on_ready(ready)\n",'START_FAILED')])
def test_K06_K14_assembly_rollback(tmp_path,source,code):
    async def run():
        host,data=package(tmp_path,source)
        with pytest.raises(KernelError,match=code): await host.load(data)
        assert host.scope.closed and host.state=='FAILED'
        assert all(i.state=='DISPOSED' for i in host.instances.values())
    asyncio.run(run())


def test_K10_bounded_queue_cancel_and_deadline():
    async def run():
        host,data=setup(instances={'b':{'plugin':'example.services','execution':{'max_inflight':1,'max_queue':1}}})
        await host.load(data)
        first=host.invoke('b','slow',.2)
        second=host.invoke('b','slow',.2)
        with pytest.raises(KernelError,match='CAPACITY_EXCEEDED'): host.invoke('b','slow',1)
        await asyncio.sleep(.01)
        second.cancel()
        with pytest.raises(KernelError,match='CANCELLED'): await second.wait()
        assert await first.wait()=='done'
        with pytest.raises(KernelError,match='TIMEOUT'): await host.invoke('b','slow',1,timeout=.01).wait()
        await host.unload()
    asyncio.run(run())


@pytest.mark.parametrize('remote',[False,True])
def test_K09_K11_K12_K13_worker_stream_cancel_errors(remote):
    async def run():
        spec={'plugin':'example.services','execution':{'max_inflight':3},'control':{'network':'inherit'}}
        if remote:spec['execution'].update(isolation='process',backend='local')
        host,data=setup(instances={'b':spec})
        await host.load(data)
        ex=host.invoke('b','emit',40,streaming=True)
        values=[]
        async for item in ex.stream():
            values.append(item);await asyncio.sleep(.002)
        assert values==list(range(40)) and await ex.wait()==40
        slow=host.invoke('b','slow',2)
        await asyncio.sleep(.02); slow.cancel()
        with pytest.raises(KernelError,match='CANCELLED'): await slow.wait()
        assert (await host.invoke('b','echo',5).wait())['value']==5
        with pytest.raises(KernelError,match='REPORTED_ERROR') as error: await host.invoke('b','fail','business').wait()
        assert error.value.envelope['cause']['code']=='provider.rate_limited'
        with pytest.raises(KernelError,match='UNHANDLED_EXCEPTION'): await host.invoke('b','fail','exception').wait()
        if remote:
            ex=host.invoke('b','fail','crash')
            with pytest.raises(KernelError,match='RESULT_UNKNOWN'): await ex.wait()
            assert ex.state=='unknown'
        await host.unload()
    asyncio.run(run())


def test_K15_K16_fail_closed_and_real_container(tmp_path, monkeypatch):
    async def run():
        host,data=setup()
        data['control']={'network':'none'}
        with pytest.raises(KernelError,match='ENFORCEMENT_UNAVAILABLE'): await host.load(data)
        host,data=setup(instances={'b':{'plugin':'example.services','execution':{'isolation':'process','image':IMAGE},
            'control':{'network':'none','memory_mb':128,'pids':16,'cpus':.5}}})
        secret=tmp_path/'secret';secret.write_text('private')
        monkeypatch.setenv('AK_TEST_SECRET','not-for-worker')
        await host.load(data)
        result=await host.invoke('b','probe',{'host':'1.1.1.1','port':80,'file':str(secret),'secret':'AK_TEST_SECRET'}).wait()
        assert result['socket']!='allowed' and result['file']!='allowed' and result['root_write']!='allowed'
        assert not result['secret_present']
        assert result['memory_max']==str(128*1024*1024) and result['pids_max']=='16'
        assert result['cpu_max'].split()[0]=='50000'
        assert host.control.receipts[0]['hard_isolation']
        await host.unload()
    asyncio.run(run())


def test_K17_identity_changes(tmp_path):
    async def run():
        host,data=setup();await host.load(data);lock=host.lock;await host.unload()
        new,_=setup();await new.load(data,expected_lock=lock);await new.unload()
        new,_=setup();data['instances']['b']['config']={'prefix':'changed'}
        with pytest.raises(KernelError,match='VERSION_MISMATCH'): await new.load(data,expected_lock=lock)
    asyncio.run(run())


def test_K18_K19_basic_loop_shared_provider():
    async def run():
        agent={'plugin':'example.basic_loop','bindings':{'model':'model','tools':'tools'}}
        host,data=setup(('services','basic_loop'), {'a':agent,'b':agent,'model':{'plugin':'example.services'},'tools':{'plugin':'example.services'}})
        await host.load(data)
        assert await asyncio.gather(host.invoke('a','run',3).wait(),host.invoke('b','run',5).wait())==[6,10]
        await host.unload()
    asyncio.run(run())


def test_K09_K11_Node_stdio_and_cancel():
    async def run():
        host,data=setup(('external',), {'node':{'plugin':'example.external','execution':{'isolation':'process','backend':'local','max_inflight':3},'control':{'network':'inherit'}}})
        await host.load(data)
        assert (await host.invoke('node','run',{'x':3}).wait())['value']=={'x':3}
        ex=host.invoke('node','emit',100,streaming=True)
        result=[]
        async for n in ex.stream():
            await asyncio.sleep(.001);result.append(n)
        assert result==list(range(100))
        ex=host.invoke('node','slow',3);await asyncio.sleep(.02);ex.cancel()
        with pytest.raises(KernelError,match='CANCELLED'):await ex.wait()
        assert (await host.invoke('node','run',3).wait())['value']==3
        await host.unload()
    asyncio.run(run())


def test_K11_cross_worker_stream():
    async def run():
        spec={'execution':{'isolation':'process','backend':'local'},'control':{'network':'inherit'}}
        host,data=setup(('services','workflow'), {'a':{**spec,'plugin':'example.workflow','bindings':{'helper':'b','next':'c'}},
            'b':{**spec,'plugin':'example.services'},'c':{'plugin':'example.services'}})
        await host.load(data)
        ex=host.invoke('a','stream',30,streaming=True)
        result=[]
        async for n in ex.stream():
            await asyncio.sleep(.002);result.append(n)
        assert result==list(range(30))
        await host.unload()
    asyncio.run(run())
