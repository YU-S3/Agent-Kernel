import asyncio
import json
import aiohttp
from agentlab_app.server import start_server


def test_http_start_observe_disconnect_replay_and_origin(tmp_path):
    async def test():
        server=await start_server(tmp_path,port=0);url=server['url']
        try:
            async with aiohttp.ClientSession() as client:
                async with client.get(url) as r:assert r.status==200 and 'AgentLab' in await r.text()
                async with client.post(url+'/api/runs',json={'profile':'viz-basic'},headers={'Origin':'https://untrusted.example'}) as r:assert r.status==403
                async with client.post(url+'/api/runs',json={'profile':'viz-basic'}) as r:ident=(await r.json())['id']
            # Disconnect the observer entirely while execution continues.
            row=server['application'].runs[ident];await row['task']
            assert row['status']=='completed' and row['result']['answer']=='3 行'
            async with aiohttp.ClientSession() as client:
                async with client.get(url+'/api/runs/'+ident) as r:live=await r.json()
                async with client.get(url+'/api/replay/'+ident) as r:replay=await r.json()
                assert live['instances']==replay['snapshot']['instances']
                assert live['executions']==replay['snapshot']['executions'] and not replay['gap']
                async with client.get(url+'/api/runs/'+ident+'/history') as r:assert len((await r.json())['messages'])==4
                async with client.post(url+'/api/runs/'+ident+'/continue',json={'task':'Again'}) as r:assert r.status==200
                await row['task'];assert row['status']=='completed'
        finally:await server['close']()
    asyncio.run(test())


def test_ui_discovers_custom_composition_without_name_branches(tmp_path):
    from pathlib import Path
    from agentlab_app.distribution import ROOT
    async def test():
        profiles=tmp_path/'profiles';profiles.mkdir()
        (profiles/'custom.yaml').write_text('kind: agentlab.profile.v1\npackage_roots:\n  - '+str(ROOT/'plugins/examples/services')+'\ncomposition:\n  kernel_api: 1\n  composition: arbitrary\n  instances:\n    any_id:\n      plugin: example.services\n  entry:\n    instance: any_id\n    method: echo\npayload:\n  value: custom\n')
        server=await start_server(tmp_path/'runs',port=0,profiles_dir=profiles)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.get(server['url']+'/api/profiles') as r:assert 'custom:custom' in (await r.json())['profiles']
                async with client.post(server['url']+'/api/runs',json={'profile':'custom:custom'}) as r:ident=(await r.json())['id']
                row=server['application'].runs[ident];await row['task']
                assert row['status']=='completed' and row['result']['value']=={'value':'custom'}
                assert list(row['snapshot']['instances'])==['any_id']
        finally:await server['close']()
    asyncio.run(test())


def test_universal_transport_local_fixture_and_paid_disabled(tmp_path,monkeypatch):
    from aiohttp import web
    from agentlab_app.distribution import load
    from agentlab.abi import KernelError
    import pytest
    monkeypatch.setenv('AGENTLAB_FIXTURE_KEY','offline-test-key')
    async def test():
        sent=[]
        async def completion(request):
            assert request.headers['Authorization']=='Bearer offline-test-key'
            payload=await request.json();sent.append(payload)
            has_tool=any(m['role']=='tool' for m in payload['messages'])
            message={'role':'assistant','content':'3 lines' if has_tool else 'Reading',
                'tool_calls':[] if has_tool else [{'id':'wire-call','type':'function','function':{'name':'read_file','arguments':'{"path":"data.txt"}'}}]}
            return web.json_response({'model':'local-protocol-fixture','choices':[{'message':message,'finish_reason':'stop' if has_tool else 'tool_calls'}],
                'usage':{'prompt_tokens':12,'completion_tokens':8,'total_tokens':20}})
        app=web.Application();app.router.add_post('/chat/completions',completion)
        runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        config={'allow_paid':False,'api_key_env':'AGENTLAB_FIXTURE_KEY','base_url':f'http://127.0.0.1:{port}','model':'fixture'}
        h=None
        try:
            h,p,n=await load(tmp_path/'disabled',overrides={'model':{'plugin':'model.universal','config':config}})
            with pytest.raises(KernelError,match='PAID_DISABLED'):await h.run(payload=p)
            assert not sent;await h.unload()
            h,p,n=await load(tmp_path/'enabled',overrides={'model':{'plugin':'model.universal','config':{**config,'allow_paid':True}},'budget':{'config':{'requests':2}}})
            assert (await h.run(payload=p))['answer']=='3 lines' and len(sent)==2
            assert sent[0]['tools'][0]['function']['name']=='read_file'
            assert all('is_error' not in m and 'status' not in m for m in sent[1]['messages'])
            with pytest.raises(KernelError,match='BUDGET.EXHAUSTED'):await h.run(payload={**p,'session':'over-budget'})
            assert len(sent)==2
        finally:
            if h:await h.unload()
            await runner.cleanup()
    asyncio.run(test())
