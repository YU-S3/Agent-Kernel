"""Bounded JSON-RPC 2.0 / Content-Length duplex transport."""
import asyncio
import json
import uuid
import re
from .abi import KernelError


class Peer:
    def __init__(self, reader, writer, handler, max_frame=1048576, max_pending=64):
        self.reader, self.writer, self.handler = reader, writer, handler
        self.max_frame, self.max_pending = max_frame, max_pending
        self.pending, self.active = {}, {}
        self.metadata, self.progress_tasks, self.stream_ids = {}, {}, {}
        self.write_lock = asyncio.Lock()
        self.closed = False
        self.task = asyncio.create_task(self.read_loop())

    async def send(self, message):
        if self.closed:
            raise KernelError('EXECUTION.RESULT_UNKNOWN', outcome_known=False)
        data = json.dumps(message, allow_nan=False, separators=(',', ':')).encode()
        if len(data) > self.max_frame:
            raise KernelError('EXECUTION.PROTOCOL_ERROR', 'Frame exceeds configured bound')
        async with self.write_lock:
            self.writer.write(('Content-Length: %d\r\n\r\n' % len(data)).encode() + data)
            await self.writer.drain()

    async def request(self, method, params):
        if len(self.pending) >= self.max_pending:
            raise KernelError('EXECUTION.CAPACITY_EXCEEDED', 'RPC pending capacity')
        ident = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        self.metadata[ident] = params
        try:
            await self.send({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params})
            return await future
        finally:
            self.pending.pop(ident, None)
            self.metadata.pop(ident, None)

    async def notify(self, method, params):
        await self.send({'jsonrpc': '2.0', 'method': method, 'params': params})

    async def dispatch(self, message):
        ident = message.get('id')
        try:
            result = await self.handler(message['method'], message.get('params', {}))
            if ident is not None:
                await self.send({'jsonrpc': '2.0', 'id': ident, 'result': result})
        except BaseException as exc:
            error = exc if isinstance(exc, KernelError) else KernelError(
                'EXECUTION.CANCELLED' if isinstance(exc, asyncio.CancelledError) else 'PLUGIN.UNHANDLED_EXCEPTION',
                type(exc).__name__, outcome_known=False)
            if ident is not None and not self.closed:
                await self.send({'jsonrpc': '2.0', 'id': ident,
                                 'error': {'code': -32000, 'message': error.envelope['code'], 'data': error.envelope}})
            elif ident is None:
                await self.close(error)
        finally:
            self.active.pop(ident if ident is not None else id(message), None)

    async def read_loop(self):
        error = KernelError('EXECUTION.RESULT_UNKNOWN', 'Worker channel closed', outcome_known=False)
        try:
            while True:
                header = b''
                header = await self.reader.readuntil(b'\r\n\r\n')
                if len(header) > 128 or not re.fullmatch(rb'Content-Length: [0-9]+\r\n\r\n', header):
                    raise ValueError('Invalid frame header')
                length = int(header[16:-4])
                if length < 1 or length > self.max_frame:
                    raise ValueError('Invalid frame length')
                message = json.loads((await self.reader.readexactly(length)).decode('utf-8'),
                                     parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
                if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                    raise ValueError('Invalid JSON-RPC message')
                if 'method' in message:
                    ident = message.get('id', id(message))
                    if ident in self.active or len(self.active) >= self.max_pending:
                        raise ValueError('Duplicate ID or inbound capacity exceeded')
                    if message['method'] == 'kernel.stream_open':
                        self.stream_ids[message['params']['request_token']] = message['params']['execution_id']
                    task = asyncio.create_task(self.dispatch(message))
                    self.active[ident] = task
                    if message['method'] == 'kernel.progress':
                        eid = message['params']['execution_id']
                        tasks = self.progress_tasks.setdefault(eid, set())
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                else:
                    future = self.pending.get(message.get('id'))
                    if future is None or future.done():
                        continue  # late response after cancellation
                    if ('result' in message) == ('error' in message):
                        raise ValueError('Invalid response')
                    params = self.metadata.get(message.get('id'), {})
                    eid = params.get('execution_id') or self.stream_ids.get(params.get('request_token'))
                    progress = list(self.progress_tasks.get(eid, ()))
                    async def settle(future=future, message=message, progress=progress, eid=eid, params=params):
                        if progress:
                            await asyncio.gather(*progress)
                        if not future.done():
                            if 'error' in message:
                                try:
                                    error = KernelError.from_wire(message['error']['data'])
                                except Exception:
                                    await self.close(KernelError('EXECUTION.PROTOCOL_ERROR', 'Malformed error envelope', outcome_known=False))
                                    return
                                future.set_exception(error)
                            else:
                                future.set_result(message['result'])
                        self.progress_tasks.pop(eid, None)
                        self.stream_ids.pop(params.get('request_token'), None)
                    task = asyncio.create_task(settle())
                    self.active[('settle', message['id'])] = task
                    task.add_done_callback(lambda _, key=('settle',message['id']): self.active.pop(key,None))
        except asyncio.IncompleteReadError as exc:
            if exc.partial and not exc.partial.startswith(b'Content-Length:') and not header.startswith(b'Content-Length:'):
                error = KernelError('EXECUTION.PROTOCOL_ERROR', 'Non-protocol stdout', outcome_known=False)
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception:
            error = KernelError('EXECUTION.PROTOCOL_ERROR', 'Malformed worker frame', outcome_known=False)
        finally:
            await self.close(error)

    async def close(self, error=None):
        if self.closed:
            return
        self.closed = True
        error = error or KernelError('EXECUTION.RESULT_UNKNOWN', outcome_known=False)
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(error)
        for task in list(self.active.values()) + [self.task]:
            if task is not asyncio.current_task():
                task.cancel()
        self.writer.close()
