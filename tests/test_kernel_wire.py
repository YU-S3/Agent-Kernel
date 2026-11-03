import asyncio
import json
import pytest
from agentlab.abi import KernelError
from agentlab.wire import Peer


class Writer:
    def __init__(self):self.frames=[];self.closed=False
    def write(self,frame):self.frames.append(frame)
    async def drain(self):pass
    def close(self):self.closed=True


def frame(message):
    body=json.dumps(message).encode()
    return f'Content-Length: {len(body)}\r\n\r\n'.encode()+body


@pytest.mark.parametrize('data',[b'print is not protocol\r\n\r\n',b'Content-Length: 2000000\r\n\r\n',
    frame({'jsonrpc':'1.0','method':'a'}),frame({'jsonrpc':'2.0','id':'a','method':'a'})*2])
def test_K09_K12_malformed_and_duplicate_rpc_close_without_replay(data):
    async def run():
        reader=asyncio.StreamReader();writer=Writer();calls=[]
        async def handler(method,params):calls.append(method);await asyncio.sleep(10)
        peer=Peer(reader,writer,handler)
        pending=asyncio.create_task(peer.request('test',{}));await asyncio.sleep(0)
        reader.feed_data(data)
        with pytest.raises(KernelError,match='PROTOCOL_ERROR'):await asyncio.wait_for(pending,1)
        assert peer.closed and len(calls)<=1
        await peer.close()
    asyncio.run(run())


def test_K12_half_frame_is_unknown_not_retried():
    async def run():
        reader=asyncio.StreamReader();writer=Writer()
        async def handler(*a):raise AssertionError('No inbound request')
        peer=Peer(reader,writer,handler)
        pending=asyncio.create_task(peer.request('side_effect',{}));await asyncio.sleep(0)
        reader.feed_data(b'Content-Length: 80\r\n\r\n{"json');reader.feed_eof()
        with pytest.raises(KernelError,match='RESULT_UNKNOWN'):await pending
        assert len(writer.frames)==1
    asyncio.run(run())


def test_K09_malformed_error_envelope_settles_pending():
    async def run():
        reader=asyncio.StreamReader();writer=Writer()
        async def handler(*a):return None
        peer=Peer(reader,writer,handler)
        pending=asyncio.create_task(peer.request('a',{}));await asyncio.sleep(0)
        ident=next(iter(peer.pending))
        reader.feed_data(frame({'jsonrpc':'2.0','id':ident,'error':{'data':{'unexpected':'bad'}}}))
        with pytest.raises(KernelError,match='PROTOCOL_ERROR'):await asyncio.wait_for(pending,1)
        await peer.close()
    asyncio.run(run())
