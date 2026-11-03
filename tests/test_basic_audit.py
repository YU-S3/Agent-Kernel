"""Regression contracts for Basic plugins; only loopback model HTTP is allowed."""
import asyncio
import json

import pytest
from aiohttp import web
from agentlab.abi import KernelError
from agentlab.host import PluginHost
from agentlab_app.benchmarks import DatasetCatalog
from agentlab_app.distribution import build_live, load
from agentlab_plugins.evidence_progress import EvidenceProgress
from agentlab_plugins.searchqa_scoring import score_answer, contract, selected_version, LEGACY, SURFACE
from test_benchmark_live_modes import provider, response, settings_at


def tool_call(name, args, ident='call'):
    return {'id':ident,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}


def test_evidence_progress_tracks_content_spans_not_ids_or_offsets():
    e=EvidenceProgress();short={'id':'1','title':'Reference','text':'entire evidence'}
    assert e.observe(short,0,len(short['text']))['new_count']==1
    assert e.observe(short,0,len(short['text']))['new_count']==0
    assert e.observe(dict(short,id='duplicate'),0,len(short['text']))['new_count']==0
    long={'id':'2','title':'Reference','text':'x'*4000}
    assert e.observe(long,0,400)['new_chars']==400
    assert e.observe(long,0,2000)['new_chars']==1600
    assert e.observe(long,1500,3500)['new_chars']==1500
    assert e.observe(long,2000,4000)['new_chars']==500
    assert e.observe(long,3999,4000)['new_count']==0
    e2=EvidenceProgress();e2.observe(long,2000,4000)
    assert e2.observe(long,0,400)['new_chars']==400  # a later page cannot hide the search prefix
    assert EvidenceProgress().observe(short,0,len(short['text']))['new_count']==1


@pytest.mark.parametrize('prediction,gold,expected',[
    ('McDonald’s',"McDonald\\'s",1),('Old Fashioned','an old-fashioned',1),
    ('Sri Lanka','Ceylon (or Sri Lanka)',1),('Chester Arthur','Coolidge (or Chester Arthur)',1),
    ('Hattie McDaniel','Hattie McDaniel (for her role in Gone with the Wind)',1),
    ('Muhammad','Muhammed',0),('Richard Nixon','Nixon',0),('Faille','organza',0),
    ('Via Appia (Appian Way)','the Appian Way',0),('Washington','Washington (state)',0),
    ('1.2','12',0),('-1','1',0),('C++','C',0),('Alpha or Beta','Alpha',0),
    ('To be','To be or not to be',0),('Alpha','Alpha (or Beta (note))',0),('', 'a',0),('A','a',1)])
def test_surface_rules_do_not_guess_semantic_answers(prediction,gold,expected):
    assert score_answer(prediction,[gold],SURFACE)['em']==expected


def test_scoring_version_legacy_and_contract_identity(tmp_path):
    assert score_answer('The ALPHA!',['alpha'])=={'em':1.0,'f1':1.0}
    assert selected_version({'dataset':{'scorer':LEGACY}})==LEGACY
    assert selected_version({'evaluation':contract()})==SURFACE
    bad=contract();bad['official']=True
    with pytest.raises(ValueError):selected_version({'evaluation':bad})
    with pytest.raises(ValueError):contract('unknown')
    cat=DatasetCatalog(tmp_path/'catalog')
    d=cat.import_bytes(b'[{"question":"q","answer":"a","snippets":["public"]}]','fixture')
    new=cat.materialize({'dataset_id':d['id']},tmp_path/'new')
    old=cat.materialize({'dataset_id':d['id'],'scorer':LEGACY},tmp_path/'old')
    assert new['evaluation']['version']==SURFACE and old['evaluation']['version']==LEGACY
    assert new['selection_sha256']!=old['selection_sha256']


def test_search_read_duplicate_stagnation_and_zero_overlap(tmp_path):
    async def run():
        sent=[]
        async def chat(r):
            p=await r.json();sent.append(p)
            if not p.get('tools'):return response('alpha')
            if len(sent)==1:return response('',[tool_call('search_snippets',{'query':'evidence'})])
            return response('',[tool_call('read_snippet',{'id':'1'},str(len(sent)))])
        runner,url=await provider(chat);s=settings_at(tmp_path,url);h=None
        cat=DatasetCatalog(tmp_path/'catalog');d=cat.import_bytes(b'[{"question":"q","answer":"alpha","snippets":["evidence"]}]','fixture')
        spec={'dataset_id':d['id']};root=tmp_path/'run';cat.materialize(spec,root)
        try:
            h,_,_=await load(root,'live',connection={**s.config(),'credential_file':str(s.secret)},options={'benchmark':spec})
            out=await h.run(payload={});task=out['tasks'][0]
            assert len(sent)==5 and task['terminal_reason']=='no_new_evidence' and task['em']==1
            assert task['finish_reason']=='stop' and task['final_request_id'] and task['response_model']=='contract-provider'
            assert out['evaluation']['version']==SURFACE and out['requests']==5 and not out['unknown_requests']
            receipts=[r['detail'] for r in h.records if r.get('topic')=='tool.receipt']
            assert [r['novelty']['new_count'] for r in receipts]==[1,0,0,0]
        finally:
            if h:await h.unload()
            await runner.cleanup()
    asyncio.run(run())


@pytest.mark.parametrize('kind',['mcp','binary','missing','directory','escape','symlink'])
def test_file_tools_through_real_driver_return_controlled_errors(tmp_path,kind):
    async def run():
        seen=[];work=tmp_path/'work';work.mkdir();(work/'data.txt').write_text('public data')
        (work/'binary').write_bytes(b'\xff\xfe\x00');(work/'link').symlink_to(work/'data.txt')
        name='mcp_read_file' if kind=='mcp' else 'read_file'
        filename={'mcp':'data.txt','binary':'binary','missing':'absent','directory':'.','escape':'../outside','symlink':'link'}[kind]
        async def chat(r):
            p=await r.json();seen.append(p)
            if len(seen)==1:return response('',[tool_call(name,{'path':filename})])
            text=[m['content'] for m in p['messages'] if m['role']=='tool'][-1]
            assert {'mcp':'public data','binary':'WORKSPACE.NOT_UTF8','missing':'WORKSPACE.NOT_FOUND','directory':'WORKSPACE.NOT_FILE','escape':'WORKSPACE.ESCAPE','symlink':'WORKSPACE.SYMLINK'}[kind] in text
            return response('done')
        runner,url=await provider(chat);s=settings_at(tmp_path,url);h=None
        try:
            h,_,_=await load(tmp_path/'run','live-mcp' if kind=='mcp' else 'live',
                connection={**s.config(),'credential_file':str(s.secret)},options={'tools':[] if kind=='mcp' else ['file'],'workspace':str(work)})
            out=await h.run(payload={'task':'read file'})
            assert out['status']=='completed' and out['finish_reason']=='stop' and len(seen)==2
            receipts=[r['detail'] for r in h.records if r.get('topic')=='tool.receipt']
            assert receipts[0]['is_error']==(kind!='mcp')
        finally:
            if h:await h.unload()
            await runner.cleanup()
    asyncio.run(run())


@pytest.mark.parametrize('mode',['preflight','incomplete_known','incomplete_unknown','malformed_known','invalid_usage','filtered'])
def test_failed_transport_preserves_actual_billing_and_terminal(tmp_path,mode):
    async def run():
        sent=[]
        async def chat(r):
            sent.append(await r.json())
            if mode.startswith('incomplete'):
                chunk={'model':'contract','choices':[{'index':0,'delta':{'content':'partial'},'finish_reason':'stop'}]}
                if mode.endswith('known') and mode!='incomplete_unknown':chunk['usage']={'total_tokens':17}
                return web.Response(text='data: '+json.dumps(chunk)+'\n\n',content_type='text/event-stream')
            if mode=='malformed_known':return web.json_response({'choices':[],'usage':{'total_tokens':19}})
            if mode=='invalid_usage':return web.json_response({'choices':[],'usage':{'total_tokens':-1}})
            return web.json_response({'model':'contract','choices':[{'message':{'role':'assistant','content':'should not score'},'finish_reason':'content_filter'}],'usage':{'total_tokens':23}})
        runner,url=await provider(chat);s=settings_at(tmp_path,url);h=None;root=tmp_path/'run'
        try:
            h,_,_=await load(root,'live',connection={**s.config(),'credential_file':str(s.secret),
                'allow_paid':mode!='preflight','stream':mode.startswith('incomplete')},options={'tools':[]})
            if mode=='filtered':
                out=await h.run(payload={'task':'q'})
                assert out['status']=='filtered' and out['answer']=='' and out['finish_reason']=='content_filter'
                assert any(r.get('topic')=='agent.filtered' for r in h.records)
                assert not any(r.get('topic')=='agent.truncated' for r in h.records)
            else:
                with pytest.raises(KernelError):await h.run(payload={'task':'q'})
            stats=await h.invoke('budget','remaining',{}).wait()
            assert stats['used']=={'incomplete_known':17,'malformed_known':19,'filtered':23}.get(mode,0)
            assert stats['unknown_requests']==int(mode in ('incomplete_unknown','invalid_usage'))
            assert stats['pending']==0 and len(sent)==int(mode!='preflight')
            receipts=list((root/'requests').glob('*.json'))
            if mode=='incomplete_known':
                audit=json.loads(receipts[0].read_text());assert audit['receipt']['raw_usage']=={'total_tokens':17}
                assert audit['response']['choices'][0]['message']['content']=='partial'
        finally:
            if h:await h.unload()
            await runner.cleanup()
    asyncio.run(run())


def test_delegate_and_workflow_preserve_abstention(tmp_path):
    async def run():
        sent=[]
        async def chat(r):
            p=await r.json();sent.append(p)
            if len(sent)==1:return response('INSUFFICIENT_EVIDENCE')
            assert '"status": "abstained"' in json.dumps(p,ensure_ascii=False).replace('\\"','"')
            return response('INSUFFICIENT_EVIDENCE')
        runner,url=await provider(chat);s=settings_at(tmp_path,url);h=None
        try:
            h,_,_=await load(tmp_path/'run','live-dynamic-spawn',connection={**s.config(),'credential_file':str(s.secret)},options={'tools':[]})
            out=await h.run(payload={'task':'unknown'})
            assert out['status']=='abstained' and out['children'][0]['status']=='abstained'
            status=await h.invoke('manager','status',{}).wait()
            assert status[0]['status']=='abstained' and status[0]['finish_reason']=='stop'
        finally:
            if h:await h.unload()
            await runner.cleanup()
    asyncio.run(run())


@pytest.mark.parametrize('kind',['python','shell'])
def test_docker_tool_stderr_and_artifacts_are_visible_to_model(tmp_path,kind):
    async def run():
        sent=[]
        code="import sys;from pathlib import Path;Path('/output/proof.txt').write_text('artifact');print('failure detail',file=sys.stderr);sys.exit(2)" if kind=='python' else "echo artifact > /output/proof.txt; echo 'failure detail' >&2; exit 2"
        async def chat(r):
            p=await r.json();sent.append(p)
            if len(sent)==1:return response('',[tool_call(kind,{'code':code})])
            result=json.loads([m['content'] for m in p['messages'] if m['role']=='tool'][-1])
            assert result['exit_code']==2 and 'failure detail' in result['stderr']
            assert result['artifacts'][0]['path']=='proof.txt' and 'artifact' in result['artifacts'][0]['text']
            return response('Reported execution failure')
        runner,url=await provider(chat);s=settings_at(tmp_path,url);h=None
        try:
            registry,data,_,_=build_live(tmp_path/'run',{**s.config(),'credential_file':str(s.secret)},options={'tools':[kind]})
            data['instances']['policy']['config']['mode']='allow'  # isolated test fixture
            h=PluginHost(registry);await h.load(data);await h.invoke('tools','bootstrap',{}).wait()
            out=await h.run(payload={'task':'run bounded local code'})
            assert out['status']=='completed' and len(sent)==2
            receipts=await h.invoke('sandbox','inspect',{}).wait()
            assert receipts[0]['cleanup'] and receipts[0]['network']=='none'
        finally:
            if h:await h.unload()
            await runner.cleanup()
    asyncio.run(run())


def test_missing_docker_is_controlled_and_cleanup_cannot_mask_error(tmp_path,monkeypatch):
    from agentlab_app.distribution import build
    async def run():
        registry,data,_,_=build(tmp_path/'run');h=PluginHost(registry)
        await h.load(data)
        async def missing(*args,**kwargs):raise FileNotFoundError('docker missing')
        monkeypatch.setattr(asyncio,'create_subprocess_exec',missing)
        try:
            with pytest.raises(KernelError,match='SANDBOX_UNAVAILABLE'):
                await h.invoke('sandbox','run',{'kind':'python','code':'print(1)'}).wait()
            receipts=await h.invoke('sandbox','inspect',{}).wait()
            assert len(receipts)==1 and not receipts[0]['cleanup'] and receipts[0]['cleanup_error']=='SANDBOX_UNAVAILABLE'
        finally:await h.unload()
    asyncio.run(run())


def test_zero_overlap_and_invalid_offset_return_no_fake_evidence(tmp_path):
    async def run():
        sent=[]
        async def chat(r):
            p=await r.json();sent.append(p)
            if len(sent)==1:return response('',[tool_call('search_snippets',{'query':'unrelatedword'})])
            observations=[m['content'] for m in p['messages'] if m['role']=='tool']
            if len(sent)==2:
                out=json.loads(observations[-1]);assert out['new_count']==0 and out['results']==[]
                return response('',[tool_call('read_snippet',{'id':'1','offset':999})])
            assert observations[-1]=='BENCHMARK.INVALID_OFFSET'
            return response('INSUFFICIENT_EVIDENCE')
        runner,url=await provider(chat);s=settings_at(tmp_path,url);h=None
        cat=DatasetCatalog(tmp_path/'catalog');d=cat.import_bytes(b'[{"question":"q","answer":"a","snippets":["evidence"]}]','fixture')
        spec={'dataset_id':d['id']};root=tmp_path/'run';cat.materialize(spec,root)
        try:
            h,_,_=await load(root,'live',connection={**s.config(),'credential_file':str(s.secret)},options={'benchmark':spec})
            out=await h.run(payload={});assert out['tasks'][0]['status']=='abstained' and not out['errors']
        finally:
            if h:await h.unload()
            await runner.cleanup()
    asyncio.run(run())


@pytest.mark.parametrize('tamper',['ids','evaluation'])
def test_selection_tampering_fails_before_http(tmp_path,tamper):
    async def run():
        sent=[]
        async def chat(r):sent.append(await r.json());return response()
        runner,url=await provider(chat);s=settings_at(tmp_path,url)
        cat=DatasetCatalog(tmp_path/'catalog');d=cat.import_bytes(b'[{"question":"q","answer":"a","snippets":["evidence"]}]','fixture')
        spec={'dataset_id':d['id']};root=tmp_path/'run';selected=cat.materialize(spec,root)
        if tamper=='ids':selected['ids']=['fake']
        else:selected['evaluation']['version']=LEGACY
        (root/'benchmark/selection.json').write_text(json.dumps(selected))
        try:
            with pytest.raises(KernelError):
                await load(root,'live',connection={**s.config(),'credential_file':str(s.secret)},options={'benchmark':spec})
            assert not sent
        finally:await runner.cleanup()
    asyncio.run(run())
