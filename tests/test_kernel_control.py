"""OS enforcement tests: no mocked HTTP clients or sockets."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from agentlab.kernel import Registry, PluginDefinition, PluginHost, KernelError
from test_kernel_v1 import IMAGE


def isolated(tmp_path, source, limits=None):
    tmp_path.chmod(0o755)
    (tmp_path/'plugin.py').write_text(source)
    (tmp_path/'plugin.json').write_text(json.dumps(dict(id='control.probe',version='1',kernel_api=1,
        loader={'adapter':'python','target':'plugin:apply'},requires={},exports=['run'])))
    definition=PluginDefinition.read(tmp_path);registry=Registry();registry.register(definition)
    host=PluginHost(registry)
    profile=dict(kernel_api=1,composition='isolation',packages=[dict(id=definition.id,version='1',digest=definition.digest)],
        instances={'probe':dict(plugin=definition.id,execution={'isolation':'process','image':IMAGE},
            control={'network':'none','pids':12,'memory_mb':128,'cpus':.5,**(limits or {})})})
    return host,profile


def test_K15_requests_curl_socket_and_local_server(tmp_path):
    hits=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path);self.send_response(200);self.end_headers();self.wfile.write(b'ok')
        def log_message(self,*a):pass
    server=ThreadingHTTPServer(('0.0.0.0',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    import requests
    assert requests.get('http://127.0.0.1:'+str(server.server_port),timeout=2).status_code==200
    assert len(hits)==1
    for module in ('requests','urllib3','certifi','idna','charset_normalizer'):
        origin=Path(importlib.import_module(module).__file__).parent
        shutil.copytree(origin,tmp_path/module,ignore=shutil.ignore_patterns('__pycache__'))
    curl=shutil.which('curl');assert curl, 'curl is required for this enforcement test'
    shutil.copy2(curl,tmp_path/'curl')
    shutil.copy2('/lib64/ld-linux-x86-64.so.2', tmp_path/'ld.so')
    libs=tmp_path/'lib';libs.mkdir()
    for line in subprocess.check_output(['ldd',curl],text=True).splitlines():
        match=re.search(r'=> (/[^ ]+)',line)
        if match:shutil.copy2(match[1],libs/Path(match[1]).name)
    source='''import sys,os,socket,subprocess
sys.path.insert(0,'/plugin')
import requests
async def apply(ctx,c):
 async def run(p,call):
  result={}
  try: requests.get(p['url'],timeout=.5);result['requests']='allowed'
  except Exception as e:result['requests']=type(e).__name__
  try:socket.create_connection(('host.docker.internal',p['port']),timeout=.5);result['socket']='allowed'
  except Exception as e:result['socket']=type(e).__name__
  command=subprocess.run(['/plugin/ld.so','--library-path','/plugin/lib','/plugin/curl','--noproxy','*','--max-time','1',p['url']],env={},capture_output=True)
  result['curl']=command.returncode
  return result
 ctx.expose('run',run)
'''
    async def run():
        host,profile=isolated(tmp_path,source)
        await host.load(profile)
        try:
            result=await host.invoke('probe','run',{'url':'http://host.docker.internal:'+str(server.server_port),'port':server.server_port}).wait()
            assert result['requests']!='allowed' and result['socket']!='allowed'
            assert result['curl'] in (5,6,7,28),result  # network errors, not missing binary/library
            assert len(hits)==1
        finally:await host.unload()
    try:asyncio.run(run())
    finally:server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('mode',['memory','pids','deadline'])
def test_K16_hard_limits(tmp_path,mode):
    bodies={'memory':'a=bytearray(512*1024*1024);return len(a)',
            'deadline':'import time;time.sleep(10);return 1',
            'pids':'''import subprocess,sys
  children=[]
  try:
   for _ in range(40):children.append(subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)']))
  except OSError:return {'blocked':True,'children':len(children)}
  finally:
   for child in children:child.kill();child.wait()
  return {'blocked':False}'''}
    source='async def apply(ctx,c):\n async def run(p,call):\n  '+bodies[mode]+"\n ctx.expose('run',run)\n"
    async def run():
        host,profile=isolated(tmp_path,source);await host.load(profile)
        try:
            ex=host.invoke('probe','run',None,timeout=.1 if mode=='deadline' else 10)
            if mode=='pids':
                result=await ex.wait();assert result['blocked'] and result['children']<12
            else:
                with pytest.raises(KernelError,match='RESOURCE_LIMIT' if mode=='memory' else 'TIMEOUT'):await ex.wait()
        finally:await host.unload()
    asyncio.run(run())
