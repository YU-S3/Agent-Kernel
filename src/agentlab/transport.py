"""Supervisor's worker transport. Channel identity, never payload IDs, grants access."""
import asyncio
import os
import signal
import time
from .abi import KernelError
from .wire import Peer


class Worker:
    def __init__(self, host, instance):
        self.host, self.instance = host, instance
        self.peer, self.process, self.container = None, None, None
        self.streams = {}
        self.finished_streams = []
        self.closed = False
        self.stderr_bytes = 0

    async def initialize(self):
        spec = self.instance.spec
        policy = {**self.host.data.get('control', {}), **spec.get('control', {})}
        command, env, self.container = self.host.control.command(self.instance.definition, spec, policy)
        self.process = await asyncio.create_subprocess_exec(*command, cwd=str(self.instance.definition.root),
                        env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, start_new_session=os.name != 'nt')
        self.peer = Peer(self.process.stdout, self.process.stdin, self.handle)
        self.stderr_task = asyncio.create_task(self.stderr())
        return await asyncio.wait_for(self.peer.request('kernel.initialize', {
            'kernel_api': 1, 'plugin_definition_digest': self.instance.definition.digest,
            'instance_id': self.instance.id, 'config': spec.get('config', {}),
            'scope_id': self.instance.scope.scope_id,
            'target': self.instance.definition.manifest['loader'].get('target'),
            'root': '/plugin' if self.container else str(self.instance.definition.root)}), 15)

    async def stderr(self):
        while True:
            chunk = await self.process.stderr.read(4096)
            if not chunk:
                return
            self.stderr_bytes += len(chunk)
            if self.stderr_bytes > 1048576:
                await self.peer.close(KernelError('CONTROL.RESOURCE_LIMIT', 'stderr bound', outcome_known=False))
                self.process.kill()
                return

    async def activate(self):
        return await asyncio.wait_for(self.peer.request('kernel.activate', {}), 10)

    async def invoke(self, method, payload, call):
        self.streams[call.execution_id] = {'call': call, 'seq': 0, 'credit': 8, 'lock': asyncio.Lock()}
        await self.peer.notify('kernel.credit', {'execution_id': call.execution_id, 'n': 8})
        try:
            return await self.peer.request('kernel.invoke', {'execution_id': call.execution_id,
                 'export': method, 'payload': payload, 'parent_id': call.parent_execution_id,
                 'scope_id': call.scope_id, 'caller_id': call.caller_id,
                 'deadline': call.deadline, 'streaming': call._execution.streaming})
        except KernelError as exc:
            if self.container and exc.envelope['code'] == 'EXECUTION.RESULT_UNKNOWN':
                proc = await asyncio.create_subprocess_exec('docker', 'inspect', '--format', '{{.State.OOMKilled}}', self.container,
                         stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                output, _ = await proc.communicate()
                if output.strip() == b'true':
                    raise KernelError('CONTROL.RESOURCE_LIMIT', 'Worker memory limit', outcome_known=False) from None
            raise
        except asyncio.CancelledError:
            call._execution.cancellation.set()
            try:
                stopped = await asyncio.wait_for(self.peer.request('kernel.cancel', {'execution_id': call.execution_id}), 1)
                if not stopped.get('stopped'):
                    await self.close()
            except BaseException:
                await self.close()
            raise
        finally:
            self.streams.pop(call.execution_id, None)
            self.finished_streams.append(call.execution_id)
            self.finished_streams = self.finished_streams[-64:]

    def parent(self, params):
        ex = self.host.supervisor.executions.get(params.get('parent_execution_id'))
        if not ex or ex.instance is not self.instance or ex.done.is_set():
            raise KernelError('CONTROL.DENIED', 'Worker does not own parent execution')
        return ex.context

    async def handle(self, method, params):
        ctx = self.instance.context
        if method == 'kernel.progress':
            row = self.streams.get(params['execution_id'])
            if not row:
                if params['execution_id'] in self.finished_streams:
                    return  # already-sent chunks can arrive after a cooperative cancellation
                raise KernelError('EXECUTION.PROTOCOL_ERROR', 'Invalid progress sequence or credit')
            async with row['lock']:
                if params['seq'] != row['seq'] or row['credit'] <= 0:
                    raise KernelError('EXECUTION.PROTOCOL_ERROR', 'Invalid progress sequence or credit')
                row['seq'] += 1
                row['credit'] -= 1
                await row['call'].progress(params['value'])
                row['credit'] += 1
                await self.peer.notify('kernel.credit', {'execution_id': params['execution_id'], 'n': 1})
            return
        if method in ('kernel.wait_execution', 'kernel.cancel'):
            execution = ctx.executions.get(params['execution_id'])
            if not execution:
                raise KernelError('CONTROL.DENIED', 'Unowned execution handle')
            if method == 'kernel.cancel':
                execution.cancel()
                return {'accepted': True, 'stopped': execution.done.is_set()}
            return await execution.wait()
        if method == 'kernel.credit':
            row = getattr(self, 'outbound_streams', {}).get(params['execution_id'])
            owned = ctx.executions.get(params['execution_id'])
            if row is None and owned and owned.done.is_set():
                return  # credits already in flight when the owned stream terminated
            if not row or type(params['n']) is not int or not 0 < params['n'] <= 8:
                raise KernelError('CONTROL.DENIED', 'Invalid stream credit')
            if row._value + params['n'] > 8:
                raise KernelError('EXECUTION.PROTOCOL_ERROR', 'Credit window exceeded')
            for _ in range(params['n']):
                row.release()
            return
        if method == 'kernel.start_call' and params.get('startup'):
            if self.instance.state != 'LINKED' or params.get('target') != '$self':
                raise KernelError('PLUGIN.NOT_READY')
            if params['export'] not in self.instance.exports:
                raise KernelError('COMPOSITION.EXPORT_MISSING')
            self.host.background.append((ctx, params['export'], params['payload']))
            return {'deferred': True}
        parent = self.parent(params)
        if method == 'kernel.authority':
            return ctx.authority(parent)
        if method == 'kernel.emit':
            ctx.emit(params['topic'], params['detail'], parent=parent)
            return {'accepted': True}
        if method == 'kernel.read_resource':
            return await ctx.read_resource(params['ref'], parent=parent,
                          offset=params.get('offset', 0), length=params.get('length', 65536))
        if method == 'kernel.spawn_instance':
            handle = await ctx.spawn_instance(params['template'], params.get('bindings', {}), params.get('config', {}), parent=parent, control=params.get('control'))
            return {'token': handle.token, 'instance_id': handle.instance_id}
        target = params.get('target')
        if isinstance(target, dict):
            target = ctx.handles.get(target.get('token'))
        elif target == '$self':
            target = ctx.self_handle
        if method in ('kernel.call_host', 'kernel.start_call'):
            execution = await ctx.start_call(target, params['export'], params['payload'], parent=parent, streaming=params.get('streaming', False))
            if method == 'kernel.start_call':
                return {'execution_id': execution.id}
            if params.get('streaming'):
                if not hasattr(self, 'outbound_streams'):
                    self.outbound_streams = {}
                credit = asyncio.Semaphore(8)
                self.outbound_streams[execution.id] = credit
                # Identify the child before any progress; recipient grants/replenishes credit.
                await self.peer.notify('kernel.stream_open', {'request_token': params['request_token'], 'execution_id': execution.id})
                seq = 0
                try:
                    async for item in execution.stream():
                        await credit.acquire()
                        await self.peer.notify('kernel.progress', {'execution_id': execution.id, 'seq': seq, 'value': item})
                        seq += 1
                finally:
                    self.outbound_streams.pop(execution.id, None)
            return await execution.wait()
        raise KernelError('EXECUTION.PROTOCOL_ERROR', 'Unknown transport method')

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.host.audit('worker.closing', instance=self.instance.id, affected_executions=sorted(self.streams))
        cleanup_errors = []
        if self.peer and not self.peer.closed:
            try:
                response = await asyncio.wait_for(self.peer.request('kernel.shutdown', {}), 0.5)
                cleanup_errors = response.get('cleanup_errors', [])
            except BaseException:
                pass
            await self.peer.close()
        if self.container:
            proc = await asyncio.create_subprocess_exec('docker', 'rm', '-f', self.container,
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
        if self.process and self.process.returncode is None:
            if os.name != 'nt':
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                self.process.kill()
            await self.process.wait()
        if hasattr(self, 'stderr_task'):
            self.stderr_task.cancel()
            await asyncio.gather(self.stderr_task, return_exceptions=True)
        if cleanup_errors:
            raise KernelError('PLUGIN.CLEANUP_FAILED', ','.join(cleanup_errors))
