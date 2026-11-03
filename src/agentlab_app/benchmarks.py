"""Local dataset catalog. Benchmark formats stay outside the Kernel and Driver."""
import hashlib
import json
import zipfile
import re
from pathlib import Path
NORMALIZATION='searchqa.public-input.v2'


def write_json(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def normalize_searchqa(data):
    if isinstance(data, dict):
        if 'question' in data: data = [data]
        else: data = data.get('tasks', data.get('data', data.get('rows')))
    if not isinstance(data, list) or not data: raise ValueError('数据必须为非空 JSON 数组或 JSONL 题目列表')
    public, private, seen = [], {}, set()
    for index, original in enumerate(data):
        if not isinstance(original,dict):raise ValueError(f'第 {index+1} 行必须为 JSON 对象')
        row = original.get('row', original)
        if not isinstance(row,dict):raise ValueError(f'第 {index+1} 行必须为 JSON 对象')
        question = row.get('question')
        answers = row.get('answers', row.get('answer'))
        if isinstance(answers, dict): answers = answers.get('text')
        if isinstance(answers, str): answers = [answers]
        if not isinstance(question, str) or not question.strip(): raise ValueError(f'第 {index+1} 行缺少 question')
        if not isinstance(answers, list) or not answers or not all(isinstance(a, str) and a.strip() for a in answers):
            raise ValueError(f'第 {index+1} 行缺少有效 answer / answers；不能用无答案数据计算准确率')
        ident = str(row.get('id', row.get('task_id', 'searchqa:' + hashlib.sha256(question.encode()).hexdigest()[:20])))
        if ident in seen: raise ValueError('重复题目 ID: ' + ident)
        seen.add(ident)
        snippets = row.get('snippets', row.get('search_results'))
        if isinstance(snippets, dict):
            texts = snippets.get('snippets', snippets.get('snippet', []))
            if not isinstance(texts,list):raise ValueError(f'{ident} snippets 列必须为数组')
            def column(name,i):
                values=snippets.get(name) or []
                if not isinstance(values,list):raise ValueError(f'{ident} {name} 列必须为数组')
                return values[i] if i<len(values) else ''
            snippets = [{'text': text,
                'title': column('titles',i), 'url': column('urls',i)} for i, text in enumerate(texts)]
        if not isinstance(snippets, list): raise ValueError(f'{ident} 缺少公开 snippets / search_results')
        clean = []
        for i, snippet in enumerate(snippets):
            if isinstance(snippet, str): snippet = {'text': snippet}
            if not isinstance(snippet,dict):raise ValueError(f'{ident} snippet {i} 必须为文本或对象')
            text = snippet.get('text', snippet.get('snippet'))
            if text is None:text=''  # upstream contains null snippets; preserve their title/URL and the task
            if not isinstance(text, str): raise ValueError(f'{ident} snippet {i} 不是文本')
            clean.append({'id': str(i+1), 'text': text, 'title': str(snippet.get('title') or ''), 'url': str(snippet.get('url') or ''),
                'related_links':snippet.get('related_links') or []})
        metadata={k:row[k] for k in ('category','air_date','value','round','show_number') if isinstance(row.get(k),(str,int,float))}
        images=row.get('images') or []
        if not isinstance(images,list):raise ValueError(f'{ident} images 必须为图片 URL 或 image_url 内容块的数组')
        if images:
            from agentlab_plugins.content import task_content
            images=task_content([{'type':'image_url','image_url':{'url':u}} if isinstance(u,str) else u for u in images],True)
            if any(i['type']!='image_url' for i in images):raise ValueError(f'{ident} images 只能包含图片')
        referenced=bool(re.search(r'\b(seen here|shown here|pictured|in this (picture|photo|image))\b',question,re.I))
        public.append({'id': ident, 'question': question, 'snippets': clean,'metadata':metadata,'images':images,
            'media':{'referenced':referenced,'provided_count':len(images),'missing_referenced_media':referenced and not images}})
        private[ident] = answers
    return public, private


class DatasetCatalog:
    def __init__(self, root):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)

    def import_bytes(self, raw, name, split='unspecified'):
        if len(raw) > 100 * 1024 * 1024: raise ValueError('单次导入上限 100 MiB，请按 split 分文件')
        text = raw.decode('utf-8-sig')
        try: data = json.loads(text)
        except json.JSONDecodeError:
            try: data = [json.loads(line) for line in text.splitlines() if line.strip()]
            except json.JSONDecodeError as exc: raise ValueError('无效 JSON / JSONL') from exc
        public, private = normalize_searchqa(data)
        digest = hashlib.sha256(raw).hexdigest(); ident = hashlib.sha256((digest+NORMALIZATION).encode()).hexdigest()[:24]
        path = self.root / ident
        if not path.exists():
            path.mkdir()
            write_json(path/'public.json', public); write_json(path/'gold.json', private)
            write_json(path/'manifest.json', {'id': ident, 'adapter': 'searchqa', 'name': str(name)[:160],
                'split': str(split)[:40], 'count': len(public), 'sha256': digest,
                'scorer': 'normalized-em-token-f1.v1', 'origin': 'user-import','normalization':NORMALIZATION})
        return self.get(ident)

    def get(self, ident):
        if not isinstance(ident, str) or len(ident)!=24 or any(c not in '0123456789abcdef' for c in ident):
            raise ValueError('无效数据集 ID')
        path = self.root/ident/'manifest.json'
        if not path.exists(): raise ValueError('找不到数据集，请先导入')
        return json.loads(path.read_text(encoding='utf-8'))

    def list(self):
        return [json.loads(p.read_text(encoding='utf-8')) for p in sorted(self.root.glob('*/manifest.json'))]

    def import_archive(self, archive, split, expected_sha256, source_url):
        """Stream upstream raw JSON members; never extract archive paths or load a whole split."""
        archive=Path(archive)
        checksum=hashlib.sha256()
        with archive.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):checksum.update(block)
        digest=checksum.hexdigest()
        if digest!=expected_sha256:raise ValueError('SearchQA archive SHA256 mismatch')
        ident=hashlib.sha256((digest+NORMALIZATION).encode()).hexdigest()[:24]; path=self.root/ident
        if (path/'manifest.json').exists() and self.get(ident).get('normalization')==NORMALIZATION:return self.get(ident)
        path.mkdir(exist_ok=True);index=[];seen=set();skipped=[]
        with zipfile.ZipFile(archive) as bundle, (path/'public.jsonl').open('wb') as public, (path/'gold.jsonl').open('wb') as private:
            for member in sorted(bundle.namelist()):
                if not member.endswith('.json') or '__MACOSX' in member:continue
                data=json.loads(bundle.read(member));data['id']='searchqa:'+split+':'+Path(member).stem
                try: rows,gold=normalize_searchqa([data])
                except ValueError as exc:
                    skipped.append({'member':member,'reason':str(exc)});continue
                row=rows[0]
                if row['id'] in seen:raise ValueError('Duplicate archive ID')
                seen.add(row['id']);index.append({'id':row['id'],'public_offset':public.tell(),'gold_offset':private.tell()})
                public.write((json.dumps(row,ensure_ascii=False)+'\n').encode())
                private.write((json.dumps(gold[row['id']],ensure_ascii=False)+'\n').encode())
        if not index:raise ValueError('Archive contains no valid SearchQA tasks')
        write_json(path/'index.json',index);write_json(path/'skipped.json',skipped)
        write_json(path/'manifest.json',{'id':ident,'adapter':'searchqa','name':'SearchQA upstream','split':split,
            'count':len(index),'sha256':digest,'scorer':'normalized-em-token-f1.v1','origin':source_url,
            'order':'sorted upstream member paths','skipped_count':len(skipped),'storage':'indexed-jsonl'})
        manifest=self.get(ident);manifest['normalization']=NORMALIZATION
        write_json(path/'manifest.json',manifest)
        return self.get(ident)

    def materialize(self, spec, root):
        manifest = self.get(spec['dataset_id'])
        count, start = spec.get('count', 1), spec.get('start', 1)
        if type(count) is not int or type(start) is not int or count<1 or start<1 or start+count-1>manifest['count']:
            raise ValueError('起始题号和题数超出数据集范围')
        source = self.root/manifest['id']; root = Path(root)
        if manifest.get('storage')=='indexed-jsonl':
            index=json.loads((source/'index.json').read_text())[start-1:start-1+count];public=[];gold={}
            with (source/'public.jsonl').open('rb') as pf,(source/'gold.jsonl').open('rb') as gf:
                for item in index:
                    pf.seek(item['public_offset']);gf.seek(item['gold_offset'])
                    public.append(json.loads(pf.readline()));gold[item['id']]=json.loads(gf.readline())
        else:
            public = json.loads((source/'public.json').read_text(encoding='utf-8'))[start-1:start-1+count]
            gold = json.loads((source/'gold.json').read_text(encoding='utf-8'))
        private = {row['id']: gold[row['id']] for row in public}
        selected = {'dataset': manifest, 'start': start, 'count': count, 'ids': [r['id'] for r in public]}
        from agentlab_plugins.searchqa_scoring import contract, SURFACE
        selected['evaluation'] = contract(spec.get('scorer', SURFACE))
        selected['selection_sha256'] = hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()
        write_json(root/'benchmark/public.json', public); write_json(root/'benchmark/gold.json', private)
        write_json(root/'benchmark/selection.json', selected)
        return selected
