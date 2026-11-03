import asyncio
import json
import os
from pathlib import Path
import aiohttp
from aiohttp import web
from agentlab_app.settings import ModelSettings
from agentlab_app.server import start_server


def test_connection_secrets_env_and_endpoint_binding(tmp_path,monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    env=tmp_path/'.env';env.write_text("DEEPSEEK_API_KEY='local-fixture-secret'\n")
    settings=ModelSettings(tmp_path/'settings',env)
    assert settings.key()=='local-fixture-secret'
    public=settings.save({'api_key':'saved-fixture-secret','model':'arbitrary-model'})
    assert public['key_configured'] and 'saved-fixture-secret' not in json.dumps(public)
    assert settings.secret.stat().st_mode & 0o077==0
    env.unlink()
    settings.save({'base_url':'https://another.example/v1'})
    assert not settings.key()


def test_live_stream_tool_history_usage_and_server_restart(tmp_path):
    async def run():
        sent=[]
        async def chat(request):
            assert request.headers['Authorization']=='Bearer fake-live-key'
            p=await request.json();sent.append(p)
            assert p['stream'] and p['thinking']=={'type':'enabled'} and p['reasoning_effort']=='high'
            assert 'max_tokens' not in p and 'max_http' not in p
            latest=max(i for i,m in enumerate(p['messages']) if m['role']=='user')
            has_tool=any(m['role']=='tool' for m in p['messages'][latest:])
            stream=web.StreamResponse(headers={'Content-Type':'text/event-stream'});await stream.prepare(request)
            async def chunk(delta=None,finish=None,usage=None):
                obj={'model':'fixture-live','choices':[] if delta is None else [{'index':0,'delta':delta,'finish_reason':finish}]}
                if usage:obj['usage']=usage
                await stream.write(('data: '+json.dumps(obj)+'\n\n').encode())
            await chunk({'reasoning_content':'retained reasoning'})
            if not has_tool:
                await chunk({'tool_calls':[{'index':0,'id':'wire-'+str(len(sent)),'function':{'name':'read_file','arguments':'{"path":'}}]})
                await chunk({'tool_calls':[{'index':0,'function':{'arguments':'"data.txt"}'}}]})
                await chunk({},'tool_calls')
            else:
                assert any(m.get('reasoning_content')=='retained reasoning' for m in p['messages'])
                await chunk({'content':'Fixture answer from real HTTP stream.'});await chunk({},'stop')
            await chunk(usage={'prompt_tokens':10000,'completion_tokens':300,'total_tokens':10300})
            await stream.write(b'data: [DONE]\n\n');return stream
        app=web.Application();app.router.add_post('/chat/completions',chat)
        runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        base='http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])
        work=tmp_path/'work';work.mkdir();(work/'data.txt').write_text('actual tool input')
        settings=ModelSettings(tmp_path/'settings',tmp_path/'.env');settings.save({'base_url':base,'api_key':'fake-live-key'})
        server=await start_server(tmp_path/'runs',port=0);server['application'].models=settings
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(server['url']+'/api/preview',json={'profile':'live','options':{'tools':[]}}) as r:preview=await r.json()
                assert 'file' not in preview['instances'] and preview['preview']
                async with client.post(server['url']+'/api/runs',json={'profile':'live','task':'A genuine arbitrary user task','options':{'tools':['file'],'workspace':str(work)}}) as r:
                    assert r.status==202,await r.text();ident=(await r.json())['id']
                row=server['application'].runs[ident]
                await row['task'];assert row['status']=='completed',row.get('error')
                assert row['result']['answer']=='Fixture answer from real HTTP stream.'
                assert len(sent)==2 and any(m.get('content')=='A genuine arbitrary user task' for m in sent[0]['messages'])
                stats=await row['host'].invoke('budget','remaining',{}).wait()
                assert stats['used']==20600 and stats['token_limit'] is None
                assert len(list((row['path']/'requests').glob('*.json')))==2
                assert 'fake-live-key' not in (row['path']/'composition.lock').read_text()
            await server['close']()
            server=await start_server(tmp_path/'runs',port=0);server['application'].models=settings
            async with aiohttp.ClientSession() as client:
                async with client.get(server['url']+'/api/runs') as r:assert any(x['id']==ident for x in await r.json())
                async with client.post(server['url']+'/api/runs/'+ident+'/continue',json={'task':'Follow-up'}) as r:assert r.status==200
                row=server['application'].runs[ident];await row['task'];assert row['status']=='completed',row.get('error')
                assert len(sent)==4 and any(m.get('content')=='A genuine arbitrary user task' for m in sent[2]['messages'])
                stats=await row['host'].invoke('budget','remaining',{}).wait()
                assert stats['used']==41200 and stats['requests']==4 and stats['unknown_requests']==0
                async with client.get(server['url']+'/api/replay/'+ident) as r:replay=await r.json()
                assert not replay['gap']
                seq=[x['seq'] for x in replay['events']]
                assert seq==list(range(1,len(seq)+1))
        finally:await server['close']();await runner.cleanup()
    asyncio.run(run())


def test_loading_snapshot_and_missing_key_do_not_blank_graph(tmp_path,monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY',raising=False)
    async def run():
        server=await start_server(tmp_path/'runs',port=0)
        server['application'].models=ModelSettings(tmp_path/'settings',tmp_path/'.env')
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(server['url']+'/api/preview',json={'profile':'live','options':{'tools':['file','python','delegate']}}) as r:
                    data=await r.json();assert {'file','sandbox','manager'}<=set(data['instances'])
                async with client.post(server['url']+'/api/runs',json={'profile':'live','task':'Hello'}) as r:assert r.status==400
                async with client.post(server['url']+'/api/runs',json={'profile':'viz-basic'}) as r:ident=(await r.json())['id']
                async with client.get(server['url']+'/api/runs/'+ident) as r:assert (await r.json())['instances']
        finally:await server['close']()
    asyncio.run(run())


def test_truncated_tool_stream_is_not_executed_and_unknown_is_not_retried(tmp_path):
    from agentlab_app.distribution import load
    from agentlab.abi import KernelError
    import pytest
    async def run():
        sent=[]
        async def chat(request):
            sent.append(await request.json())
            stream=web.StreamResponse(headers={'Content-Type':'text/event-stream'});await stream.prepare(request)
            delta={'tool_calls':[{'index':0,'id':'cut','function':{'name':'read_file','arguments':'{"path":'}}]}
            chunks=[{'choices':[{'delta':delta,'finish_reason':None}]},
                    {'choices':[{'delta':{},'finish_reason':'length'}],'usage':{'total_tokens':20000}}]
            for chunk in chunks:await stream.write(('data: '+json.dumps(chunk)+'\n\n').encode())
            if len(sent)==1:await stream.write(b'data: [DONE]\n\n')
            return stream
        app=web.Application();app.router.add_post('/chat/completions',chat)
        runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        settings=ModelSettings(tmp_path/'settings',tmp_path/'.env')
        settings.save({'base_url':'http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1]),'api_key':'fixture'})
        h=None
        try:
            h,p,n=await load(tmp_path/'run','live',connection={**settings.config(),'credential_file':str(settings.secret)},options={'tools':['file']})
            result=await h.run(payload={'task':'Truncated tool test','session':'main'})
            assert result['status']=='truncated'
            assert not any(x.get('topic')=='tool.receipt' for x in h.records)
            with pytest.raises(KernelError,match='INCOMPLETE_RESPONSE'):
                await h.run(payload={'task':'Incomplete transport test','session':'main'})
            assert len(sent)==2
            usage=await h.invoke('budget','remaining',{}).wait()
            assert usage['used']==20000 and usage['unknown_requests']==1
            rows=[json.loads(p.read_text()) for p in (tmp_path/'run/requests').glob('*.json')]
            assert len(rows)==2 and any(r['receipt']['error']=='PROVIDER.INCOMPLETE_RESPONSE' for r in rows)
            assert all(not m.get('tool_calls') for m in sent[1]['messages'])
        finally:
            if h:await h.unload()
            await runner.cleanup()
    asyncio.run(run())
