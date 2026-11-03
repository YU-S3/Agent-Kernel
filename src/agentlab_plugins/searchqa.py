"""SearchQA adapter: per-episode public retrieval and private local scoring."""
import json
import re
import string
from collections import Counter
from pathlib import Path
from .common import tool, result, fail


def normalized(text):
    text = ''.join(c for c in text.lower() if c not in string.punctuation)
    return ' '.join(re.sub(r'\b(a|an|the)\b', ' ', text).split())


def score_answer(answer, gold):
    prediction = normalized(answer); pt = prediction.split(); scores = []
    for target in gold:
        truth = normalized(target); tt = truth.split(); shared = sum((Counter(pt)&Counter(tt)).values())
        f1 = 2*shared/(len(pt)+len(tt)) if pt and tt else float(pt==tt)
        scores.append((float(prediction==truth), f1))
    return {'em': max(x[0] for x in scores), 'f1': max(x[1] for x in scores)}


async def apply(ctx, config):
    public = json.loads(Path(config['public_path']).read_text(encoding='utf-8'))
    gold = json.loads(Path(config['gold_path']).read_text(encoding='utf-8'))
    rows = {r['id']: r for r in public}; active = {}
    def owner(c): return ctx.authority(c)['chain'][-1]['execution']
    def current(c):
        if owner(c) not in active: fail('BENCHMARK.NO_ACTIVE_EPISODE')
        return rows[active[owner(c)]]
    async def open_task(p, c):
        if p['id'] not in rows: fail('BENCHMARK.UNKNOWN_TASK')
        key = owner(c); active[key] = p['id']
        ctx.scope_cleanup(ctx.authority(c)['chain'][-1]['scope'], lambda: active.pop(key, None), parent=c)
        row = current(c)
        return {'id': row['id'], 'task': row['question'] + '\n\nUse the provided search_snippets and read_snippet tools to inspect the supplied evidence. Return only the short answer, without explanation.', 'snippet_count': len(row['snippets'])}
    async def search(p, c):
        row = current(c); tokens = set(re.findall(r'\w+', p.get('query', '').lower()))
        scores = [(len(tokens & set(re.findall(r'\w+', (s['title']+' '+s['text']).lower()))), i, s) for i,s in enumerate(row['snippets'])]
        ranked = sorted(scores, key=lambda x: (-x[0], x[1]))
        return {'task_id': row['id'], 'total': len(ranked), 'results': [x[2] for x in ranked[:p.get('limit', 5)]]}
    async def read(p, c):
        row = current(c)
        found = next((s for s in row['snippets'] if s['id']==p['id']), None)
        if found is None: fail('BENCHMARK.UNKNOWN_SNIPPET')
        return found
    async def score(p, c):
        row = current(c)
        return score_answer(p.get('answer', ''), gold[row['id']])
    async def close(p, c): active.pop(owner(c), None); return {'closed': True}
    for name, fn in [('open', open_task), ('search', search), ('read', read), ('score', score), ('close', close)]: ctx.expose(name, fn)


async def tools_apply(ctx, config):
    async def describe(p, c):
        return [tool('search_snippets', 'Search the supplied evidence for the current question; stable lexical ranking.',
            {'query': {'type':'string'}, 'limit': {'type':'integer','minimum':1,'maximum':10}}, ['query']),
            tool('read_snippet', 'Read one complete snippet from the current question by its local ID.', {'id':{'type':'string'}}, ['id'])]
    async def execute(p, c):
        method = {'search_snippets':'search','read_snippet':'read'}[p['name']]
        return result(json.dumps(await ctx.call('adapter', method, p['arguments'], parent=c), ensure_ascii=False))
    ctx.expose('describe', describe); ctx.expose('execute', execute)
