"""Production plugins over intercepted HTTP, without paid model requests."""
import asyncio
import hashlib
import json
from copy import deepcopy

import pytest
from aiohttp import web

from agentlab.abi import KernelError
from agentlab_app.benchmarks import DatasetCatalog, normalize_searchqa, NORMALIZATION
from agentlab_app.distribution import load
from agentlab_plugins.content import task_content
from agentlab_plugins.context_projection import project, compact_text
from test_benchmark_live_modes import provider, response, settings_at


def test_category_media_and_versioned_catalog(tmp_path):
    raw=json.dumps([{'question':'The two paintings seen here','category':'BEGINNING & END',
        'answer':'PRIVATE_TARGET','search_results':[{'snippet':'public evidence'}]}]).encode()
    old=tmp_path/hashlib.sha256(raw).hexdigest()[:24];old.mkdir();(old/'original').write_bytes(raw)
    c=DatasetCatalog(tmp_path);d=c.import_bytes(raw,'source')
    assert d['normalization']==NORMALIZATION and d['id']!=old.name
    public=json.loads((tmp_path/d['id']/'public.json').read_text())[0]
    assert public['metadata']['category']=='BEGINNING & END'
    assert public['media']['missing_referenced_media'] and public['images']==[]
    assert 'PRIVATE_TARGET' not in json.dumps(public) and (old/'original').read_bytes()==raw


def test_projection_preserves_task_reasoning_pairs_and_originals():
    facts=[{'role':'system','content':'system'},{'role':'user','content':'OLD TASK'},
        {'role':'assistant','content':'old answer'}, {'role':'user','content':'REQUIRED TASK AND OPTIONS'},
        {'role':'assistant','content':'plan'},{'role':'user','content':'execute plan'}]
    for i in range(20):
        facts.extend([{'role':'assistant','content':'','reasoning_content':'reasoning'+str(i),
            'tool_calls':[{'id':str(i),'function':{'name':'search','arguments':'{}'}}]},
            {'role':'tool','tool_call_id':str(i),'content':'evidence '*2000}])
    original=deepcopy(facts)
    out=project(facts,{'max_chars':18000,'turns':1},protected_from=3)
    assert out['fits'] and out['input_chars']<18000 and facts==original
    assert any(m.get('content')=='REQUIRED TASK AND OPTIONS' for m in out['messages'])
    assert [m.get('reasoning_content') for m in out['messages'] if m.get('tool_calls')]==['reasoning'+str(i) for i in range(20)]
    assert [m['tool_call_id'] for m in out['messages'] if m['role']=='tool']==[str(i) for i in range(20)]
    final=project(facts,{'max_chars':18000},True,3)
    assert not any('reasoning_content' in m for m in final['messages'])
    assert any(m.get('content')=='REQUIRED TASK AND OPTIONS' for m in final['messages'])
    for n in (256,1024,8192):
        clipped=compact_text('"\\\n'*20000,n)
        assert len(clipped)<=n and json.loads(clipped)['original_chars']==60000


def test_required_oversize_not_silently_removed():
    task=[{'type':'text','text':'x'*220000},{'type':'image_url','image_url':{'url':'data:image/png;base64,'+'x'*10000}}]
    facts=[{'role':'user','content':task},{'role':'user','content':'final answer now'}]
    out=project(facts,{'max_chars':200000},True,0)
    assert not out['fits'] and out['messages'][0]['content']==task


@pytest.mark.parametrize('profile',['live','live-plan-execute','live-memory'])
def test_repeated_search_finalizes_once_without_tools(tmp_path,profile):
    async def run():
        sent=[]
        async def chat(r):
            p=await r.json();sent.append(p)
            if not p.get('tools'):
                if 'concise plan' in str(p['messages'][-1]):return response('Inspect supplied evidence')
                assert p['max_tokens']==2048
                assert 'Question CATEGORY' in json.dumps(p)
                assert not any('reasoning_content' in m for m in p['messages'])
                return response('INSUFFICIENT_EVIDENCE')
            return web.json_response({'model':'contract','choices':[{'message':{'role':'assistant','content':'',
                'reasoning_content':'keep exact reasoning',
                'tool_calls':[{'id':str(len(sent)),'type':'function','function':{'name':'search_snippets','arguments':'{"query":"evidence"}'}}]},
                'finish_reason':'tool_calls'}],'usage':{'total_tokens':30}})
        runner,url=await provider(chat);s=settings_at(tmp_path,url);h=None
        cat=DatasetCatalog(tmp_path/'catalog');d=cat.import_bytes(json.dumps([{'id':'one','question':'Question','category':'Question CATEGORY',
            'answer':'PRIVATE GOLD','snippets':['evidence']}]).encode(),'fixture');spec={'dataset_id':d['id'],'count':1};root=tmp_path/'run';cat.materialize(spec,root)
        try:
            h,_,_=await load(root,profile,connection={**s.config(),'credential_file':str(s.secret)},options={'benchmark':spec})
            out=await h.run(payload={});task=out['tasks'][0]
            assert task['status']=='abstained' and task['terminal_reason']=='no_new_evidence'
            assert task['finalization_attempted'] and out['errors']==0 and out['scored']==1 and task['em']==0
            assert len(sent)==(6 if profile=='live-plan-execute' else 5)
            assert out['requests']==len(sent) and out['tokens']==30*len(sent) and not out['unknown_requests']
            assert 'PRIVATE GOLD' not in json.dumps(sent)
            tools=[m for m in sent[-1]['messages'] if m['role']=='tool']
            first=json.loads(tools[0]['content']);second=json.loads(tools[1]['content'])
            assert first['new_count']==1 and second['new_count']==0 and 'text' not in second['results'][0]
        finally:
            if h:await h.unload()
            await runner.cleanup()
    asyncio.run(run())


@pytest.mark.parametrize('case',['step','budget','oversize','final_truncated'])
def test_bounded_terminals_and_usage(tmp_path,case):
    async def run():
        sent=[]
        async def chat(r):
            p=await r.json();sent.append(p)
            if not p.get('tools'):
                if case=='final_truncated':return web.json_response({'model':'fixture','choices':[{'message':{'role':'assistant','content':''},'finish_reason':'length'}],'usage':{'total_tokens':30}})
                return response('done')
            return response('',[{'id':str(len(sent)),'type':'function','function':{'name':'read_file','arguments':'{"path":"data.txt"}'}}])
        runner,url=await provider(chat);s=settings_at(tmp_path,url);h=None;work=tmp_path/'work';work.mkdir();(work/'data.txt').write_text('evidence')
        try:
            h,_,_=await load(tmp_path/'run','live',connection={**s.config(),'credential_file':str(s.secret)},
                options={'tools':['file'],'workspace':str(work),'max_steps':2,'request_limit':1 if case=='budget' else None})
            out=await h.run(payload={'task':'x'*220000 if case=='oversize' else 'Read file','session':'main'})
            assert out['status']=={'step':'completed','budget':'budget_limit','oversize':'context_limit','final_truncated':'truncated'}[case]
            assert len(sent)=={'step':2,'budget':1,'oversize':0,'final_truncated':2}[case]
            stats=await h.invoke('budget','remaining',{}).wait();assert stats['requests']==len(sent) and stats['unknown_requests']==0
            assert stats['used']==30*len(sent)
        finally:
            if h:await h.unload()
            await runner.cleanup()
    asyncio.run(run())


@pytest.mark.parametrize('profile',['live','live-plan-execute','live-multi-agent'])
def test_multimodal_preserved_and_unsupported_is_zero_http(tmp_path,profile):
    async def run():
        sent=[]
        async def chat(r):sent.append(await r.json());return response('image response')
        runner,url=await provider(chat);s=settings_at(tmp_path,url);h=None
        task=[{'type':'text','text':'Explain this image'},{'type':'image_url','image_url':{'url':'https://example.com/image.png'}}]
        try:
            h,_,_=await load(tmp_path/'disabled','live',connection={**s.config(),'credential_file':str(s.secret)},options={'tools':[]})
            with pytest.raises(KernelError,match='图片'):await h.run(payload={'task':task})
            assert not sent
            stats=await h.invoke('budget','remaining',{}).wait();assert stats['requests']==0
            await h.unload();h=None
            h,_,_=await load(tmp_path/'enabled',profile,connection={**s.config(),'credential_file':str(s.secret),'supports_images':True},options={'tools':[]})
            await h.run(payload={'task':task})
            assert sent and all(any(isinstance(m.get('content'),list) and any(p.get('type')=='image_url' for p in m['content']) for m in req['messages']) for req in sent)
        finally:
            if h:await h.unload()
            await runner.cleanup()
    asyncio.run(run())


@pytest.mark.parametrize('part',[{'type':'image_url','image_url':None},{'type':'image_url','image_url':{'url':'file:///secret'}},
    {'type':'image_url','image_url':{'url':'data:image/png;base64,ZmFrZQ=='}}])
def test_invalid_image_rejected(part):
    with pytest.raises(KernelError):task_content([part],True)


def test_parallel_consumers_and_delegation_limits_reset_per_episode(tmp_path):
    async def run():
        seen=[]
        async def chat(r):
            p=await r.json();tools=[m for m in p['messages'] if m['role']=='tool']
            if not tools:
                return response('',[{'id':'query','type':'function','function':{'name':'search_snippets','arguments':'{"query":"evidence"}'}}])
            receipt=json.loads(tools[-1]['content']);seen.append(receipt)
            assert receipt['new_count']==1, 'Sibling consumers must each receive original evidence'
            return response('alpha')
        runner,url=await provider(chat);s=settings_at(tmp_path,url);h=None
        cat=DatasetCatalog(tmp_path/'catalog');rows=[{'id':str(i),'question':'Find evidence '+str(i),'answer':'alpha','snippets':['evidence']} for i in range(3)]
        d=cat.import_bytes(json.dumps(rows).encode(),'fixture');spec={'dataset_id':d['id'],'count':3};root=tmp_path/'run';cat.materialize(spec,root)
        try:
            h,_,_=await load(root,'live-multi-agent',connection={**s.config(),'credential_file':str(s.secret)},options={'benchmark':spec})
            out=await h.run(payload={})
            assert out['finished']==3 and out['errors']==0 and out['em']==1 and len(seen)==9
        finally:
            if h:await h.unload()
            await runner.cleanup()
    asyncio.run(run())
