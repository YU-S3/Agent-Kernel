"""Chat Completions wire protocol. Runtime policy and cumulative budgets live elsewhere."""
import asyncio
import json
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import aiohttp
from agentlab.abi import KernelError
from .common import fail, emit


async def apply(ctx,config):
    # The instance owns its pool and DNS cache, including across conversation turns.
    client=aiohttp.ClientSession(connector=aiohttp.TCPConnector(ttl_dns_cache=300))
    ctx.on_cleanup(client.close)

    async def infer(p,call):
        if config.get('strict_token_budget'):fail('PROVIDER.STRICT_ACCOUNTING_UNAVAILABLE')
        if config.get('allow_paid') is False:fail('PROVIDER.PAID_DISABLED')
        from agentlab_app.settings import read_env
        stored={}
        if config.get('credential_file') and Path(config['credential_file']).is_file():
            stored=json.loads(Path(config['credential_file']).read_text())
        key=stored.get(config['base_url'],'') or os.environ.get(config.get('api_key_env','AGENTLAB_MODEL_API_KEY'))
        if not key and config.get('env_file'):key=read_env(config['env_file']).get(config['api_key_env'])
        if not key:fail('PROVIDER.KEY_MISSING')
        messages=[{k:v for k,v in m.items() if k in ('role','content','tool_calls','tool_call_id','name','reasoning_content')} for m in p['messages']]
        payload={'model':config['model'],'messages':messages,'stream':config.get('stream',False)}
        if config.get('max_tokens') is not None:payload['max_tokens']=config['max_tokens']
        if config.get('thinking') in ('enabled','disabled'):payload['thinking']={'type':config['thinking']}
        if config.get('reasoning_effort') in ('low','high','max'):payload['reasoning_effort']=config['reasoning_effort']
        if payload['stream']:payload['stream_options']={'include_usage':True}
        if p['tools']:
            payload['tools']=[{'type':'function','function':{'name':t['name'],'description':t['description'],'parameters':t['input_schema']}} for t in p['tools']]
            payload['tool_choice']='auto'
        ident=uuid.uuid4().hex;start=datetime.now(timezone.utc).isoformat();body=None;out=None;error=None;http_status=None
        attempts=[];network_error=None;request_sent=None
        emit(ctx,'model.request',{'request_id':ident,'session':p['session'],'model':config['model'],'stream':payload['stream']},call)
        try:
            deadline=min(time.monotonic()+config.get('timeout',600),call.deadline)
            for attempt in range(1,4):
                try:
                    timeout=aiohttp.ClientTimeout(total=max(.001,deadline-time.monotonic()))
                    resp=await client.post(config['base_url'].rstrip('/')+'/chat/completions',json=payload,
                        headers={'Authorization':'Bearer '+key},allow_redirects=False,timeout=timeout)
                    attempts.append({'attempt':attempt,'http_status':resp.status})
                    break
                except aiohttp.ClientConnectorDNSError as exc:
                    detail={'attempt':attempt,'stage':'dns','type':type(exc).__name__,'errno':exc.errno,'http_request_sent':False}
                    attempts.append(detail)
                    delay=attempt
                    if exc.errno!=socket.EAI_AGAIN or attempt==3 or time.monotonic()+delay>=deadline:raise
                    emit(ctx,'model.connection_retry',{'request_id':ident,**detail,'delay_seconds':delay},call)
                    await asyncio.sleep(delay)
            request_sent=True
            async with resp:
                http_status=resp.status
                if resp.status!=200:fail('PROVIDER.HTTP_'+str(resp.status))
                if payload['stream']:
                    message={'role':'assistant','content':'','reasoning_content':''};calls={};usage=None;finish=None;model=None;done=False
                    async for raw in resp.content:
                        line=raw.decode('utf-8').strip()
                        if not line.startswith('data:'):continue
                        data=line[5:].strip()
                        if data=='[DONE]':done=True;break
                        chunk=json.loads(data);model=chunk.get('model',model)
                        if chunk.get('error'):fail('PROVIDER.STREAM_ERROR')
                        if chunk.get('usage') is not None:usage=chunk['usage']
                        for choice in chunk.get('choices') or []:
                            if choice.get('index',0)!=0:continue
                            if choice.get('finish_reason'):finish=choice['finish_reason']
                            delta=choice.get('delta') or {}
                            for field,kind in [('content','text_delta'),('reasoning_content','reasoning_delta')]:
                                if delta.get(field):
                                    message[field]+=delta[field];await call.progress({'kind':kind,'text':delta[field]})
                            for item in delta.get('tool_calls') or []:
                                row=calls.setdefault(item['index'],{'id':'','type':'function','function':{'name':'','arguments':''}})
                                if item.get('id'):row['id']+=item['id']
                                for field in ('name','arguments'):row['function'][field]+=item.get('function',{}).get(field,'') or ''
                                await call.progress({'kind':'tool_delta','index':item['index'],'id':row['id'],'name':row['function']['name']})
                    message['tool_calls']=[calls[k] for k in sorted(calls)]
                    body={'model':model,'choices':[{'message':message,'finish_reason':finish}],'usage':usage}
                    if not done or not finish:fail('PROVIDER.INCOMPLETE_RESPONSE')
                else:body=await resp.json()
            choice=body['choices'][0];message=choice['message'];usage=body.get('usage')
            if usage is not None and (type(usage.get('total_tokens')) is not int or usage['total_tokens']<0):fail('PROVIDER.INVALID_USAGE')
            if choice['finish_reason'] not in ('stop','tool_calls','length','content_filter'):fail('PROVIDER.INVALID_FINISH')
            out={'message':message,'usage':usage or {},'finish_reason':choice['finish_reason'],'model':body.get('model'),'request_id':ident}
            emit(ctx,'model.response',{'session':p['session'],**out},call)
            await call.progress({'kind':'complete','response':out});return out
        except aiohttp.ClientConnectorDNSError as exc:
            error='PROVIDER.DNS_FAILED';request_sent=False
            network_error={'type':type(exc).__name__,'errno':exc.errno,'stage':'dns'}
            raise KernelError('PLUGIN.REPORTED_ERROR','本机 DNS 无法解析 API 域名；未发送模型请求。请检查网络后重试。',
                outcome_known=True,cause={'code':error,'http_request_sent':False,**network_error}) from None
        except (asyncio.TimeoutError,aiohttp.ClientError) as exc:
            error='PROVIDER.TRANSPORT_UNKNOWN'
            network_error={'type':type(exc).__name__,'errno':getattr(exc,'errno',None),'stage':'transport'}
            raise KernelError('PLUGIN.REPORTED_ERROR','模型连接中断或超时；是否已处理请求未知，未自动重发。',
                outcome_known=False,cause={'code':error,**network_error}) from None
        except (ValueError,KeyError,IndexError,TypeError):
            error='PROVIDER.MALFORMED_RESPONSE';fail(error)
        except BaseException as exc:
            envelope=getattr(exc,'envelope',{})
            error=(envelope.get('cause') or {}).get('code') or envelope.get('code',type(exc).__name__);raise
        finally:
            receipt={'request_id':ident,'start':start,'end':datetime.now(timezone.utc).isoformat(),'http_status':http_status,
                     'model':(body or {}).get('model'),'raw_usage':(body or {}).get('usage'),'finish_reason':out.get('finish_reason') if out else None,'error':error,
                     'connection_attempts':attempts,'network_error':network_error,'http_request_sent':request_sent}
            emit(ctx,'model.receipt',receipt,call)
            if config.get('audit_dir'):
                path=Path(config['audit_dir']);path.mkdir(parents=True,exist_ok=True)
                (path/(ident+'.json')).write_text(json.dumps({'request':payload,'response':body,'receipt':receipt},ensure_ascii=False))
    ctx.expose('infer',infer)
