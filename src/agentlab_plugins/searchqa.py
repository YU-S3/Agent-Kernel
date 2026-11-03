"""SearchQA adapter: per-episode public retrieval and private local scoring."""
import json
import re
import math
from pathlib import Path
from .common import tool, result, fail
from .evidence_progress import EvidenceProgress
from .searchqa_scoring import normalized, score_answer, selected_version, contract, LEGACY


async def apply(ctx, config):
    public = json.loads(Path(config['public_path']).read_text(encoding='utf-8'))
    gold = json.loads(Path(config['gold_path']).read_text(encoding='utf-8'))
    version = selected_version(json.loads(Path(config['selection_path']).read_text(encoding='utf-8'))) if config.get('selection_path') else config.get('scorer',LEGACY)
    contract(version)
    rows = {r['id']: r for r in public}; active = {};seen={}
    def owner(c): return ctx.authority(c)['chain'][-1]['execution']
    def current(c):
        if owner(c) not in active: fail('BENCHMARK.NO_ACTIVE_EPISODE')
        return rows[active[owner(c)]]
    async def open_task(p, c):
        if p['id'] not in rows: fail('BENCHMARK.UNKNOWN_TASK')
        key = owner(c); active[key] = p['id']
        seen[key]={}
        ctx.scope_cleanup(ctx.authority(c)['chain'][-1]['scope'], lambda: (active.pop(key,None),seen.pop(key,None)), parent=c)
        row = current(c)
        category=row.get('metadata',{}).get('category')
        task=('Category: '+str(category)+'\n' if category else '')+row['question']
        if row.get('media',{}).get('missing_referenced_media'):
            task+='\nThe source question refers to media that is not included in this dataset. Do not claim to have seen it. Use the supplied text evidence; if it is insufficient, state that you cannot determine the answer.'
        task+='\n\nUse search_snippets and read_snippet to inspect the supplied evidence. Search is limited to these snippets, not the live web. Return only the short answer, without explanation.'
        if row.get('images'):task=[{'type':'text','text':task},*row['images']]
        return {'id': row['id'], 'task':task,'metadata':row.get('metadata',{}),'media':row.get('media',{}),'snippet_count':len(row['snippets'])}
    async def search(p, c):
        row = current(c); tokens = set(re.findall(r'\w+', p.get('query', '').lower()))
        docs=[set(re.findall(r'\w+',(s['title']+' '+s['text']).lower())) for s in row['snippets']]
        weights={t:math.log(1+len(docs)/(1+sum(t in d for d in docs))) for t in tokens}
        scores = [(sum(weights[t] for t in sorted(tokens&docs[i])), i, s) for i,s in enumerate(row['snippets'])]
        ranked = sorted((r for r in scores if r[0]>0), key=lambda x: (-x[0], x[1]))
        consumer=p.get('_consumer','direct');known=seen[owner(c)].setdefault(consumer,EvidenceProgress());results=[];new=0;chars=0
        for score,_,s in ranked[:p.get('limit',5)]:
            progress=known.observe(s,0,min(400,len(s['text'])));repeated=progress['new_count']==0
            new+=progress['new_count'];chars+=progress['new_chars']+progress['new_title_chars']
            item={'id':s['id'],'title':s['title'][:160],'score':round(score,4),'already_seen':repeated,**progress}
            if not repeated:item.update(text=s['text'][:400],excerpted=len(s['text'])>400)
            results.append(item)
        return {'task_id':row['id'],'total':len(ranked),'new_count':new,'new_chars':chars,'novelty_basis':'visible-text-span.v1','seen_count':len(known.ids),'results':results,
            'notice':'Repeated snippets omit their text; read_snippet can reopen any ID. No external web search is performed.'}
    async def read(p, c):
        row = current(c)
        found = next((s for s in row['snippets'] if s['id']==p['id']), None)
        if found is None: fail('BENCHMARK.UNKNOWN_SNIPPET')
        consumer=p.get('_consumer','direct');known=seen[owner(c)].setdefault(consumer,EvidenceProgress())
        offset=p.get('offset',0);text=found['text']
        if type(offset) is not int or not 0<=offset<=len(text):fail('BENCHMARK.INVALID_OFFSET')
        content=text[offset:offset+2000];progress=known.observe(found,offset,offset+len(content))
        return {'id':found['id'],'title':found['title'][:160],'url':found['url'][:2048],'text':content,'offset':offset,'total_chars':len(text),'next_offset':offset+len(content) if offset+len(content)<len(text) else None,**progress}
    async def score(p, c):
        row = current(c)
        return score_answer(p.get('answer', ''), gold[row['id']], version)
    async def close(p, c): active.pop(owner(c), None); seen.pop(owner(c),None); return {'closed': True}
    for name, fn in [('open', open_task), ('search', search), ('read', read), ('score', score), ('close', close)]: ctx.expose(name, fn)


async def tools_apply(ctx, config):
    async def describe(p, c):
        return [tool('search_snippets', 'Search the supplied evidence for the current question; stable lexical ranking.',
            {'query': {'type':'string'}, 'limit': {'type':'integer','minimum':1,'maximum':10}}, ['query']),
            tool('read_snippet', 'Read a snippet by ID; use next_offset to page through long text.', {'id':{'type':'string'},'offset':{'type':'integer','minimum':0}}, ['id'])]
    async def execute(p, c):
        method = {'search_snippets':'search','read_snippet':'read'}[p['name']]
        data=await ctx.call('adapter', method, {**p['arguments'],'_consumer':p.get('_consumer','direct')}, parent=c)
        return result(json.dumps(data,ensure_ascii=False),novelty={'new_count':data.get('new_count',0)})
    ctx.expose('describe', describe); ctx.expose('execute', execute)
