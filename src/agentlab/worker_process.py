"""Generic Python worker/SDK. stdout is exclusively Content-Length framed RPC."""
import asyncio
import importlib.util
import sys
import uuid
import inspect
import time
from pathlib import Path
from types import SimpleNamespace
from .abi import KernelError, PluginDefinition
from .wire import Peer
from .loader import python_apply


class RemoteExecution:
    def __init__(self, ctx, response):
        self.ctx, self.response = ctx, response

    async def wait(self):
        if self.response.get('deferred'):
            raise KernelError('PLUGIN.NOT_READY')
        return await self.ctx.peer.request('kernel.wait_execution', self.response)

    async def cancel(self):
        return await self.ctx.peer.request('kernel.cancel', self.response)


class RemoteContext:
    def __init__(self, worker, instance_id):
        self.worker, self.peer = worker, worker.peer
        self.instance_id = instance_id
        self.self_handle = '$self'
        self.exports, self.ready, self.cleanup = {}, [], []

    def expose(self, name, fn):
        if self.worker.state != 'initialize' or name in self.exports or not inspect.iscoroutinefunction(fn):
            raise KernelError('PLUGIN.INIT_FAILED')
        self.exports[name] = fn

    def on_ready(self, fn):
        if self.worker.state != 'initialize':
            raise KernelError('PLUGIN.NOT_READY')
        self.ready.append(fn)

    def on_cleanup(self, fn):
        if self.worker.state in ('draining', 'disposed'):
            raise KernelError('PLUGIN.DISPOSED')
        self.cleanup.append(fn)

    def params(self, target, method, payload, parent):
        if self.worker.state != 'active':
            raise KernelError('PLUGIN.NOT_READY')
        return dict(target=target, export=method, payload=payload, parent_execution_id=parent.execution_id)

    async def call(self, target, method, payload, *, parent):
        return await self.peer.request('kernel.call_host', self.params(target, method, payload, parent))

    async def start_call(self, target, method, payload, *, parent):
        if self.worker.state == 'activate' and parent is self.worker.startup:
            params = dict(startup=True, target=target, export=method, payload=payload)
        else:
            params = self.params(target, method, payload, parent)
        return RemoteExecution(self, await self.peer.request('kernel.start_call', params))

    async def spawn_instance(self, template, bindings, config, *, parent, control=None):
        return await self.peer.request('kernel.spawn_instance', dict(template=template, bindings=bindings,
                      config=config, control=control, parent_execution_id=parent.execution_id))

    async def authority(self, call):
        return await self.peer.request('kernel.authority', {'parent_execution_id': call.execution_id})

    async def emit(self, topic, detail, *, parent):
        return await self.peer.request('kernel.emit', dict(topic=topic, detail=detail, parent_execution_id=parent.execution_id))

    async def read_resource(self, ref, *, parent, offset=0, length=65536):
        return await self.peer.request('kernel.read_resource', dict(ref=ref, offset=offset, length=length,
                      parent_execution_id=parent.execution_id))

    async def stream(self, target, method, payload, *, parent):
        params = self.params(target, method, payload, parent)
        token = uuid.uuid4().hex
        row = {'queue': asyncio.Queue(8), 'execution': None, 'seq': 0, 'lock': asyncio.Lock()}
        self.worker.streams[token] = row
        params.update(streaming=True, request_token=token)
        request = asyncio.create_task(self.peer.request('kernel.call_host', params))
        try:
            while not request.done() or not row['queue'].empty():
                item = asyncio.create_task(row['queue'].get())
                ready, _ = await asyncio.wait([item, request], return_when=asyncio.FIRST_COMPLETED)
                if item in ready or item.done():
                    yield item.result()
                    await self.peer.notify('kernel.credit', {'execution_id': row['execution'], 'n': 1})
                else:
                    item.cancel()
                    await asyncio.gather(item, return_exceptions=True)
            await request
        finally:
            if not request.done():
                request.cancel()
                if row['execution']:
                    await self.peer.request('kernel.cancel', {'execution_id': row['execution']})
            self.worker.streams.pop(token, None)


class WorkerServer:
    def __init__(self):
        self.state = 'new'
        self.calls, self.credits, self.streams = {}, {}, {}
        self.startup = object()

    async def handle(self, method, params):
        if method == 'kernel.initialize':
            if self.state != 'new' or params['kernel_api'] != 1:
                raise KernelError('PLUGIN.INIT_FAILED')
            definition = PluginDefinition.read(params['root'])
            if definition.digest != params['plugin_definition_digest']:
                raise KernelError('COMPOSITION.VERSION_MISMATCH')
            self.state = 'initialize'
            self.ctx = RemoteContext(self, params['instance_id'])
            self.ctx.scope_id = params['scope_id']
            self.startup = SimpleNamespace(execution_id='startup:' + uuid.uuid4().hex,
                scope_id=self.ctx.scope_id, caller_id=None, parent_execution_id=None, deadline=None)
            apply, cleanup = python_apply(definition)
            self.ctx.on_cleanup(cleanup)
            await apply(self.ctx, params['config'])
            self.state = 'registered'
            return list(self.ctx.exports)
        if method == 'kernel.activate':
            if self.state != 'registered':
                raise KernelError('PLUGIN.NOT_READY')
            self.state = 'activate'
            self.startup.deadline = time.monotonic() + 5
            for callback in self.ctx.ready:
                await asyncio.wait_for(callback(self.startup), 5)
            self.state = 'active'
            return {'ready': True}
        if method == 'kernel.credit':
            if type(params['n']) is not int or not 0 < params['n'] <= 8:
                raise KernelError('EXECUTION.PROTOCOL_ERROR')
            credit = self.credits.setdefault(params['execution_id'], asyncio.Semaphore(0))
            for _ in range(params['n']):
                credit.release()
            return
        if method == 'kernel.stream_open':
            self.streams[params['request_token']]['execution'] = params['execution_id']
            return
        if method == 'kernel.progress':
            row = next((r for r in self.streams.values() if r['execution'] == params['execution_id']), None)
            if not row:
                raise KernelError('EXECUTION.PROTOCOL_ERROR')
            async with row['lock']:
                if params['seq'] != row['seq']:
                    raise KernelError('EXECUTION.PROTOCOL_ERROR')
                row['seq'] += 1
                await row['queue'].put(params['value'])
            return
        if method == 'kernel.invoke':
            if self.state != 'active':
                raise KernelError('PLUGIN.NOT_READY')
            ident = params['execution_id']
            if ident in self.calls:
                raise KernelError('EXECUTION.PROTOCOL_ERROR')
            self.calls[ident] = asyncio.current_task()
            sequence = 0
            async def progress(item):
                nonlocal sequence
                if params.get('streaming'):
                    await self.credits.setdefault(ident, asyncio.Semaphore(0)).acquire()
                    await self.peer.notify('kernel.progress', dict(execution_id=ident, seq=sequence, value=item))
                    sequence += 1
            call = SimpleNamespace(execution_id=ident, parent_execution_id=params.get('parent_id'),
                                   scope_id=params['scope_id'], caller_id=params.get('caller_id'),
                                   deadline=params.get('deadline'), progress=progress)
            try:
                return await self.ctx.exports[params['export']](params['payload'], call)
            finally:
                self.calls.pop(ident, None)
                self.credits.pop(ident, None)
        if method == 'kernel.cancel':
            task = self.calls.get(params['execution_id'])
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return {'accepted': True, 'stopped': True}
        if method == 'kernel.shutdown':
            self.state = 'draining'
            for task in list(self.calls.values()):
                task.cancel()
            await asyncio.gather(*list(self.calls.values()), return_exceptions=True)
            errors = []
            for callback in reversed(getattr(self, 'ctx', SimpleNamespace(cleanup=[])).cleanup):
                try:
                    result = callback()
                    if inspect.isawaitable(result): await result
                except Exception as exc:
                    errors.append(type(exc).__name__)
            self.state = 'disposed'
            return {'cleanup_errors': errors}
        raise KernelError('EXECUTION.PROTOCOL_ERROR', 'Unknown method')


async def main():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    transport, protocol = await loop.connect_write_pipe(lambda: asyncio.streams.FlowControlMixin(loop=loop), sys.stdout.buffer)
    writer = asyncio.StreamWriter(transport, protocol, None, loop)
    server = WorkerServer()
    server.peer = Peer(reader, writer, server.handle)
    await server.peer.task


if __name__ == '__main__':
    asyncio.run(main())
