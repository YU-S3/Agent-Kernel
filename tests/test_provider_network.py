import asyncio
import json
import socket

import aiohttp
import pytest
from aiohttp import web

from agentlab.abi import KernelError
from agentlab_app.distribution import load


@pytest.mark.parametrize('failures,errno,attempts,success', [
    (2, socket.EAI_AGAIN, 3, True),
    (99, socket.EAI_AGAIN, 3, False),
    (99, socket.EAI_NONAME, 1, False),
])
def test_dns_recovery_is_presend_bounded_cached_and_accounted(tmp_path, monkeypatch, failures, errno, attempts, success):
    monkeypatch.setenv('NETWORK_FIXTURE_KEY', 'local-only-test-key')

    async def run():
        sent=[]; resolutions=[]

        async def chat(request):
            sent.append(id(request.transport))
            return web.json_response({'model':'local-fixture','choices':[{
                'message':{'role':'assistant','content':'OK'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':5,'completion_tokens':5,'total_tokens':10}})

        async def resolve(self, host, port=0, family=socket.AF_INET):
            resolutions.append(host)
            if len(resolutions)<=failures:
                raise socket.gaierror(errno, 'injected DNS failure')
            return [{'hostname':host,'host':'127.0.0.1','port':port,
                     'family':socket.AF_INET,'proto':0,'flags':socket.AI_NUMERICHOST}]

        monkeypatch.setattr(aiohttp.resolver.DefaultResolver, 'resolve', resolve)
        app=web.Application();app.router.add_post('/chat/completions',chat)
        runner=web.AppRunner(app);await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        h=None
        try:
            h,_,_=await load(tmp_path/'run','live',connection={
                'base_url':f'http://localhost:{port}','model':'fixture',
                'api_key_env':'NETWORK_FIXTURE_KEY','stream':False,'timeout':30},options={'tools':[]})
            if success:
                assert (await h.run(payload={'task':'one','session':'main'}))['answer']=='OK'
                assert (await h.run(payload={'task':'two','session':'main'}))['answer']=='OK'
                assert len(sent)==2 and sent[0]==sent[1]  # Pool survives conversation turns.
            else:
                with pytest.raises(KernelError) as caught:
                    await h.run(payload={'task':'one','session':'main'})
                assert caught.value.envelope['cause']['code']=='PROVIDER.DNS_FAILED'
                assert caught.value.envelope['cause']['http_request_sent'] is False
                assert not sent
            assert len(resolutions)==attempts
            stats=await h.invoke('budget','remaining',{}).wait()
            assert stats['used']==(20 if success else 0)
            assert stats['unknown_requests']==0 and stats['pending']==0
            receipts=[json.loads(p.read_text())['receipt'] for p in (tmp_path/'run/requests').glob('*.json')]
            if not success:
                assert receipts[0]['raw_usage'] is None  # No fake provider usage.
                assert receipts[0]['http_request_sent'] is False
            assert max(len(r['connection_attempts']) for r in receipts)==attempts
            assert all('local-only-test-key' not in json.dumps(r) for r in receipts)
        finally:
            if h:await h.unload()
            await runner.cleanup()

    asyncio.run(run())
