import asyncio
import json
from pathlib import Path
import aiohttp
from aiohttp import web
import pytest
from agentlab_app.distribution import LIVE_PROFILES, build_live, load
from agentlab_app.benchmarks import DatasetCatalog
from agentlab_app.server import start_server
from agentlab_app.settings import ModelSettings
from agentlab_plugins.searchqa import score_answer


async def provider(handler):
    app=web.Application();app.router.add_post('/chat/completions',handler)
    runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    return runner, 'http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])


def response(content='alpha', calls=None):
    return web.json_response({'model':'contract-provider','choices':[{'message':{'role':'assistant','content':content,
        **({'tool_calls':calls} if calls else {})},'finish_reason':'tool_calls' if calls else 'stop'}],
        'usage':{'prompt_tokens':20,'completion_tokens':10,'total_tokens':30}})


def settings_at(path, url):
    s=ModelSettings(path/'settings',path/'.env')
    s.save({'base_url':url,'api_key':'fake-contract-key','stream':False})
    return s


@pytest.mark.parametrize('profile',list(LIVE_PROFILES))
def test_every_live_mode_uses_bound_http_provider(tmp_path,profile):
    async def run():
        requests=[]
        async def chat(r):requests.append(await r.json());return response('HTTP provider result')
        runner,url=await provider(chat);s=settings_at(tmp_path,url);h=None
        try:
            h,p,n=await load(tmp_path/'run',profile,connection={**s.config(),'credential_file':str(s.secret)},options={'tools':[]})
            out=await h.run(payload={'task':'Arbitrary real-mode task','session':'main'})
            assert out['answer']=='HTTP provider result' and requests
            assert any('Arbitrary real-mode task' in m.get('content','') for m in requests[0]['messages'])
            assert all(row['plugin']!='model.mock' for row in h.data['instances'].values())
            assert len(requests)==({'live-multi-agent':3,'live-dynamic-spawn':2,'live-plan-execute':2}.get(profile,1))
            if profile=='live-plan-execute':
                assert not requests[0].get('tools') and 'concise plan' in requests[0]['messages'][-1]['content']
                assert 'execute the plan' in requests[1]['messages'][-1]['content']
            if profile=='live-dynamic-tools':
                assert any(t['function']['name']=='read_file' for t in requests[0]['tools'])
                assert not await h.invoke('tools','list',{}).wait()
            if profile=='live-mcp':assert any(t['function']['name']=='mcp_read_file' for t in requests[0]['tools'])
        finally:
            if h:await h.unload()
            await runner.cleanup()
    asyncio.run(run())


def source_rows():
    return [{'id':'question-'+str(i),'question':'Find label '+str(i),'answer':'alpha',
             'snippets':[{'text':'The public label is alpha.','title':'Label reference'}],
             'private_note':'SHOULD_NEVER_BE_VISIBLE'} for i in range(3)]


def test_searchqa_scoring_and_dataset_validation(tmp_path):
    assert score_answer('The ALPHA!', ['alpha'])=={'em':1.0,'f1':1.0}
    assert score_answer('alpha beta',['alpha'])['f1']==pytest.approx(2/3)
    catalog=DatasetCatalog(tmp_path/'datasets');rows=source_rows()
    d=catalog.import_bytes(json.dumps(rows).encode(),'fixture','validation')
    selection=catalog.materialize({'dataset_id':d['id'],'count':2,'start':2},tmp_path/'run')
    assert selection['ids']==['question-1','question-2']
    assert 'SHOULD_NEVER' not in (tmp_path/'run/benchmark/public.json').read_text()
    assert 'answer' not in (tmp_path/'run/benchmark/public.json').read_text()
    with pytest.raises(ValueError):catalog.materialize({'dataset_id':d['id'],'count':3,'start':2},tmp_path/'bad')
    with pytest.raises(ValueError):catalog.import_bytes(json.dumps(rows+rows).encode(),'duplicate')
    with pytest.raises(ValueError):catalog.import_bytes(b'[{"question":"x","answer":"a"}]','missing evidence')
    nullable=catalog.import_bytes(b'[{"question":"x","answer":"a","search_results":[{"snippet":null,"title":"evidence","url":"https://example.com"}]}]','upstream-null')
    assert nullable['count']==1


def test_upstream_zip_index_and_checksum(tmp_path):
    import zipfile,hashlib
    archive=tmp_path/'upstream.zip'
    with zipfile.ZipFile(archive,'w') as z:
        for i,row in enumerate(source_rows()):z.writestr(f'{i:04}.json',json.dumps(row))
    sha=hashlib.sha256(archive.read_bytes()).hexdigest();catalog=DatasetCatalog(tmp_path/'catalog')
    with pytest.raises(ValueError,match='SHA256'):catalog.import_archive(archive,'validation','0'*64,'test')
    d=catalog.import_archive(archive,'validation',sha,'test');assert d['count']==3 and d['skipped_count']==0
    selection=catalog.materialize({'dataset_id':d['id'],'start':2,'count':1},tmp_path/'run')
    assert selection['ids']==['searchqa:validation:0001']
    assert len(json.loads((tmp_path/'run/benchmark/public.json').read_text()))==1


def test_searchqa_real_http_pause_restore_isolation_and_scores(tmp_path):
    async def run():
        sent=[];first=asyncio.Event();release=asyncio.Event()
        async def chat(r):
            p=await r.json();sent.append(p)
            assert 'SHOULD_NEVER_BE_VISIBLE' not in json.dumps(p)
            current=[m for m in p['messages'] if m['role']=='user']
            assert len(current)==1  # no previous task or gold in the next task's context
            if len(sent)==1:first.set();await release.wait()
            if not any(m['role']=='tool' for m in p['messages']):
                return response('',[{'id':'search-call','type':'function','function':{'name':'search_snippets','arguments':'{"query":"label"}'}}])
            return response()
        runner,url=await provider(chat);settings=settings_at(tmp_path,url)
        server=await start_server(tmp_path/'runs',port=0);server['application'].models=settings
        try:
            async with aiohttp.ClientSession() as client:
                async with client.post(server['url']+'/api/datasets/import?name=fixture&split=validation',
                    data=json.dumps(source_rows()).encode(),headers={'Content-Type':'application/octet-stream'}) as r:
                    assert r.status==201;d=await r.json()
                options={'tools':['file','shell'],'benchmark':{'dataset_id':d['id'],'count':2,'start':2}}
                async with client.post(server['url']+'/api/preview',json={'profile':'live','options':options}) as r:
                    preview=await r.json();assert {'adapter','evidence','benchmark'}<=set(preview['instances'])
                    assert 'file' not in preview['instances'] and 'sandbox' not in preview['instances']
                async with client.post(server['url']+'/api/runs',json={'profile':'live','options':options}) as r:
                    assert r.status==202,await r.text();ident=(await r.json())['id']
                await asyncio.wait_for(first.wait(),10)
                async with client.post(server['url']+f'/api/runs/{ident}/pause',json={}) as r:assert r.status==200
                release.set();row=server['application'].runs[ident];await row['task']
                assert row['status']=='paused' and row['result']['finished']==1
                assert len(sent)==2
            await server['close']();server=await start_server(tmp_path/'runs',port=0);server['application'].models=settings
            async with aiohttp.ClientSession() as client:
                async with client.post(server['url']+f'/api/runs/{ident}/continue',json={}) as r:assert r.status==200
                row=server['application'].runs[ident];await row['task']
                assert row['status']=='completed',row.get('error')
                assert row['result']['finished']==2 and row['result']['em']==1 and len(sent)==4
                assert row['result']['tokens']==120 and row['result']['requests']==4
                assert [r['id'] for r in row['result']['tasks']]==['question-1','question-2']
                async with client.get(server['url']+f'/api/runs/{ident}') as r:assert (await r.json())['benchmark']['finished']==2
                async with client.get(server['url']+f'/api/runs/{ident}/history?session=task-2') as r:
                    h=await r.json();assert any('Find label 2' in m.get('content','') for m in h['messages'])
                async with client.post(server['url']+f'/api/runs/{ident}/continue',json={}) as r:assert r.status==409
        finally:release.set();await server['close']();await runner.cleanup()
    asyncio.run(run())


def test_benchmark_errors_and_unknown_usage_are_not_resent(tmp_path):
    async def run():
        sent=[]
        async def chat(r):
            sent.append(await r.json())
            if len(sent)==1:return web.Response(status=503,text='fixture unavailable')
            return response()
        runner,url=await provider(chat);s=settings_at(tmp_path,url);h=None
        catalog=DatasetCatalog(tmp_path/'catalog');d=catalog.import_bytes(json.dumps(source_rows()).encode(),'fixture')
        root=tmp_path/'run';spec={'dataset_id':d['id'],'count':2};catalog.materialize(spec,root)
        try:
            h,_,_=await load(root,'live',connection={**s.config(),'credential_file':str(s.secret)},options={'benchmark':spec})
            out=await h.run(payload={})
            assert out['finished']==2 and out['errors']==1 and out['scored']==1 and len(sent)==2
            assert out['unknown_requests']==1 and out['tokens']==30
            await h.run(payload={});assert len(sent)==2
        finally:
            if h:await h.unload()
            await runner.cleanup()
    asyncio.run(run())
