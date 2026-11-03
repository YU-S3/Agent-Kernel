"""Local Agent workbench. Observation never drives Agent execution."""
import asyncio
import json
import uuid
import yaml
from pathlib import Path
from aiohttp import web
from agentlab.abi import KernelError
from agentlab.observation import snapshot
from .distribution import load,build,build_live,PROFILES,LIVE_PROFILES,ROOT
from .settings import ModelSettings
from .benchmarks import DatasetCatalog

STATIC=Path(__file__).parent/'static'


class Application:
    def __init__(self,root,profiles_dir=None):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.runs={}
        self.datasets=DatasetCatalog(self.root.parent/'datasets')
        self.profiles_dir=Path(profiles_dir).resolve() if profiles_dir else None
        self.models=ModelSettings(Path.home()/'.agentlab',ROOT/'.env')
        for path in self.root.glob('*/result.json'):
            try:
                row=json.loads(path.read_text());ident=row['id']
                if len(ident)!=32:continue
                row.update(path=path.parent,snapshot=json.loads((path.parent/'snapshot.json').read_text()))
                if row['status'] in ('loading','running'):row['status']='interrupted'
                runtime=path.parent/'runtime.json'
                if runtime.exists():row['runtime']=json.loads(runtime.read_text())
                self.runs[ident]=row
            except (ValueError,KeyError,OSError):continue

    def profiles(self):
        profiles={name:name for name in [*LIVE_PROFILES,*PROFILES]}
        if self.profiles_dir and self.profiles_dir.is_dir():
            for path in sorted(self.profiles_dir.glob('*.yaml')):
                descriptor=yaml.safe_load(path.read_text())
                if isinstance(descriptor,dict) and descriptor.get('kind')=='agentlab.profile.v1':
                    profiles['custom:'+path.stem]=str(path.resolve())
        return profiles

    def persist(self,row):
        path=row['path'];host=row.get('host')
        if host:
            row['snapshot']=snapshot(host)
        def write(name,data):
            temp=path/(name+'.tmp');temp.write_text(json.dumps(data,ensure_ascii=False,indent=2));temp.replace(path/name)
        if row.get('snapshot'):write('snapshot.json',row['snapshot'])
        write('result.json',{k:row.get(k) for k in ('id','profile','status','result','error')})

    async def execute(self,row,payload):
        row['status']='loading'
        try:
            if not row.get('host'):
                host,default,names=await load(row['path'],row.get('profile_source',row['profile']),**row.get('runtime',{}))
                row.update(host=host,names=names,default=default)
            host=row['host'];entry=host.data['entry']
            request={**row['default'],**payload}
            ex=host.invoke(entry['instance'],entry['method'],request,streaming=True)
            row.update(execution=ex,status='running')
            self.persist(row)
            async for chunk in ex.stream():pass  # observations are already independently recorded
            result=await ex.wait();status=result.get('status') if isinstance(result,dict) else None
            row.update(result=result,status=status if status in ('truncated','paused','interrupted','step_completed') else 'completed',error=None)
        except asyncio.CancelledError:
            row.update(status='cancelled',error={'code':'EXECUTION.CANCELLED','outcome_known':False})
        except KernelError as exc:
            row.update(status='cancelled' if exc.envelope['code']=='EXECUTION.CANCELLED' else 'failed',error=exc.envelope)
        except Exception as exc:
            row.update(status='failed',error={'code':'APPLICATION.ERROR','message':type(exc).__name__})
        finally:self.persist(row)

    async def close(self):
        for row in self.runs.values():
            if row.get('execution') and not row['execution'].done.is_set():row['execution'].cancel()
        await asyncio.gather(*(r['task'] for r in self.runs.values() if r.get('task')),return_exceptions=True)
        for row in self.runs.values():
            if row.get('host'):await row['host'].unload();self.persist(row)

    def routes(self):
        @web.middleware
        async def boundaries(request,handler):
            origin=request.headers.get('Origin')
            if request.method not in ('GET','HEAD') and origin and origin!=f'{request.scheme}://{request.host}':
                raise web.HTTPForbidden(text='Cross-origin mutation denied')
            if request.host.split(':')[0] not in ('localhost','127.0.0.1','[::1]'):
                raise web.HTTPForbidden(text='Loopback host only')
            try:return await handler(request)
            except KernelError as exc:return web.json_response({'error':exc.envelope},status=409)
            except ValueError as exc:raise web.HTTPBadRequest(text=str(exc))
            except (KeyError,TypeError):raise web.HTTPBadRequest(text='Invalid request structure')
        app=web.Application(middlewares=[boundaries],client_max_size=100*1024*1024)
        async def index(request):return web.FileResponse(STATIC/'index.html')
        async def profiles(request):return web.json_response({'profiles':list(self.profiles()),'live_profiles':LIVE_PROFILES,'mode':'mixed'})
        async def datasets(request):return web.json_response(self.datasets.list())
        async def import_dataset(request):
            if request.content_type=='application/json':
                data=await request.json();path=Path(data['path']).expanduser()
                if not path.is_file():raise ValueError('文件不存在；请填写 WSL 路径或使用上传按钮')
                if path.stat().st_size>100*1024*1024:raise ValueError('导入文件超过 100 MiB，请按 split 分文件')
                raw=await asyncio.to_thread(path.read_bytes);name=data.get('name',path.name);split=data.get('split','unspecified')
            else:
                raw=await request.read();name=request.query.get('name','SearchQA');split=request.query.get('split','unspecified')
            record=await asyncio.to_thread(self.datasets.import_bytes,raw,name,split)
            return web.json_response(record,status=201)
        async def model_settings(request):
            if request.method=='GET':return web.json_response(self.models.public())
            return web.json_response(self.models.save(await request.json()))
        def configuration(data,path):
            profile=data.get('profile','live')
            if profile in LIVE_PROFILES:
                c=self.models.config()
                runtime={'connection':{**c,'credential_file':str(self.models.secret),'env_file':str(self.models.env_file)},'options':data.get('options',{})}
                _,composition,_,_=build_live(path,profile=profile,**runtime)
            elif profile in PROFILES:
                runtime={};_,composition,_,_=build(path,profile)
            else:
                runtime={}
                composition=yaml.safe_load(Path(self.profiles()[profile]).read_text())['composition']
            return runtime,composition
        def preview(composition):
            return {'composition':composition['composition'],'entry':composition.get('entry'),'instances':{k:{'plugin':v['plugin'],'bindings':v.get('bindings',{}),'state':'CONFIGURED'} for k,v in composition['instances'].items()},'executions':{},'preview':True}
        async def config_preview(request):
            data=await request.json()
            if data.get('profile','live') not in self.profiles():raise web.HTTPBadRequest(text='Unknown profile')
            _,composition=configuration(data,self.root/'preview')
            return web.json_response(preview(composition) if composition else {'instances':{},'executions':{},'preview':True})
        async def start(request):
            data=await request.json();profile=data.get('profile','viz-basic')
            catalog=self.profiles()
            if profile not in catalog:raise web.HTTPBadRequest(text='Unknown profile')
            if profile in LIVE_PROFILES and not self.models.key():raise web.HTTPBadRequest(text='请先在模型设置中保存 API Key，或填写项目 .env 的 DEEPSEEK_API_KEY。')
            bench=data.get('options',{}).get('benchmark')
            if bench and profile not in LIVE_PROFILES:raise ValueError('Benchmark 必须使用真实模型配置；离线示例不是正式答题')
            if sum(r['status'] in ('loading','running') for r in self.runs.values())>=4:raise web.HTTPTooManyRequests(text='Four active runs maximum')
            # Keep at most eight live Hosts; completed traces remain on disk.
            for row in list(self.runs.values())[:-7]:
                if row.get('host') and row['status'] not in ('loading','running'):
                    await row['host'].unload();self.persist(row);row['host']=None
            ident=uuid.uuid4().hex;path=self.root/ident;path.mkdir()
            row={'id':ident,'path':path,'profile':profile,'profile_source':catalog[profile],'status':'loading'};self.runs[ident]=row
            try:
                runtime,composition=configuration(data,path);row['runtime']=runtime
                if bench:self.datasets.materialize(bench,path)
                if composition:row['snapshot']=preview(composition)
                (path/'runtime.json').write_text(json.dumps(runtime,indent=2))
                self.persist(row)
            except Exception:
                self.runs.pop(ident,None);raise
            payload={k:v for k,v in data.items() if k in ('task','session')}
            row['task']=asyncio.create_task(self.execute(row,payload))
            return web.json_response({'id':ident},status=202)
        def get(request):
            row=self.runs.get(request.match_info['id'])
            if not row:raise web.HTTPNotFound()
            return row
        async def runs(request):return web.json_response([{'id':r['id'],'profile':r['profile'],'status':r['status']} for r in self.runs.values()])
        async def state(request):
            row=get(request);host=row.get('host')
            view=snapshot(host) if host else row.get('snapshot',{'instances':{},'executions':{}})
            progress=row['path']/'benchmark/progress.json';selection=row['path']/'benchmark/selection.json'
            bench=json.loads(progress.read_text()) if progress.exists() else None
            if bench is None and selection.exists():
                sel=json.loads(selection.read_text());bench={'status':'loading','finished':0,'total':sel['count'],'tasks':[]}
            return web.json_response(dict(view,run_id=row['id'],run_status=row['status'],result=row.get('result'),error=row.get('error'),benchmark=bench,
                actions={'history':'history' in row.get('names',{}) or row['profile'] in LIVE_PROFILES,'approval':'policy' in row.get('names',{}),
                    'continue':(bool(host) or row['profile'] in LIVE_PROFILES) and (not bench or row['status'] in ('paused','interrupted','failed','cancelled')),
                    'pause':bool(bench) and row['status']=='running'}))
        async def pause_benchmark(request):
            row=get(request)
            if not row.get('host') or 'benchmark' not in row.get('names',{}):raise ValueError('当前不是运行中的 benchmark')
            return web.json_response(await row['host'].invoke('benchmark','pause',{}).wait())
        async def events(request):
            row=get(request);host=row.get('host');after=int(request.query.get('after','0'))
            if host:return web.json_response(host.observation.read(after))
            path=row['path']/'kernel_events.jsonl'
            rows=[json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
            return web.json_response({'events':[r for r in rows if r['seq']>after],'cursor':rows[-1]['seq'] if rows else 0,'gap':False})
        async def cancel(request):
            row=get(request)
            if row.get('execution') and not row['execution'].done.is_set():row['execution'].cancel()
            elif row['status']=='loading':row['task'].cancel()
            return web.json_response({'accepted':True})
        async def continuation(request):
            row=get(request)
            if row['status'] in ('loading','running') or (not row.get('host') and row['profile'] not in LIVE_PROFILES):raise web.HTTPConflict(text='Host not idle or fixture cannot be restored')
            if (row['path']/'benchmark/selection.json').exists() and row['status']=='completed':raise web.HTTPConflict(text='已完成的 benchmark 不会重复运行；请新建实验')
            body=await request.json();row.update(status='loading',result=None,error=None)
            self.persist(row)
            row['task']=asyncio.create_task(self.execute(row,{k:v for k,v in body.items() if k in ('task','session')}))
            return web.json_response({'accepted':True})
        async def approvals(request):
            row=get(request);host=row.get('host')
            if not host:return web.json_response([])
            pending={}
            for event in host.records:
                if event.get('topic')=='approval.request':pending[event['detail']['id']]=event['detail']
                elif event.get('topic')=='approval.decision':pending.pop(event['detail']['id'],None)
            return web.json_response(list(pending.values()))
        async def decide(request):
            row=get(request);body=await request.json()
            if 'policy' not in row.get('names',{}):raise web.HTTPConflict(text='Profile has no approval binding')
            return web.json_response(await row['host'].invoke(row['names']['policy'],'decide',body).wait())
        async def history(request):
            row=get(request)
            if not row.get('host') and row['profile'] in LIVE_PROFILES:
                import sqlite3
                with sqlite3.connect(row['path']/'history.sqlite') as db:
                    return web.json_response({'messages':[json.loads(r[0]) for r in db.execute('SELECT body FROM facts WHERE session=? ORDER BY seq',(request.query.get('session','main'),))]})
            if not row.get('host') or 'history' not in row.get('names',{}):raise web.HTTPConflict(text='Profile has no active history binding')
            return web.json_response(await row['host'].invoke(row['names']['history'],'read',{'session':request.query.get('session','main')}).wait())
        async def replay(request):
            ident=request.match_info['id']
            if len(ident)!=32 or any(c not in '0123456789abcdef' for c in ident):raise web.HTTPNotFound()
            path=self.root/ident
            row=self.runs.get(ident)
            if row and row.get('host'):self.persist(row)
            if not (path/'snapshot.json').exists():raise web.HTTPConflict(text='Run has no final snapshot yet')
            rows=[json.loads(line) for line in (path/'kernel_events.jsonl').read_text().splitlines()]
            return web.json_response({'snapshot':json.loads((path/'snapshot.json').read_text()),'events':rows,
                'result':json.loads((path/'result.json').read_text()),'gap':any(b['seq']!=a['seq']+1 for a,b in zip(rows,rows[1:]))})
        app.add_routes([web.get('/',index),web.get('/api/model',model_settings),web.post('/api/model',model_settings),web.post('/api/preview',config_preview),web.get('/api/profiles',profiles),web.get('/api/datasets',datasets),web.post('/api/datasets/import',import_dataset),web.get('/api/runs',runs),web.post('/api/runs',start),
            web.get('/api/runs/{id}',state),web.get('/api/runs/{id}/events',events),web.post('/api/runs/{id}/cancel',cancel),
            web.post('/api/runs/{id}/pause',pause_benchmark),
            web.post('/api/runs/{id}/continue',continuation),web.get('/api/runs/{id}/approvals',approvals),
            web.post('/api/runs/{id}/approvals',decide),web.get('/api/runs/{id}/history',history),web.get('/api/replay/{id}',replay)])
        app.router.add_static('/static/',STATIC)
        return app


async def start_server(root,host='127.0.0.1',port=8765,profiles_dir=None):
    if host not in ('127.0.0.1','localhost','::1'):raise ValueError('Reference server binds loopback only')
    application=Application(root,profiles_dir);runner=web.AppRunner(application.routes());await runner.setup()
    site=web.TCPSite(runner,host,port);await site.start()
    actual=site._server.sockets[0].getsockname()[1]
    async def close():await application.close();await runner.cleanup()
    return {'url':f'http://{host}:{actual}','application':application,'close':close}
