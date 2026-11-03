"""Supervisor: generic bounded executions, structured cancellation and progress."""
import asyncio
import time
import uuid
from .abi import KernelError, value


class CallContext:
    def __init__(self, execution):
        self.execution_id = execution.id
        self.scope_id = execution.scope.scope_id
        self.caller_id = execution.caller_id
        self.parent_execution_id = execution.parent.id if execution.parent else None
        self.deadline = execution.deadline
        self._execution = execution

    async def progress(self, item):
        execution = self._execution
        execution.supervisor.host.audit('execution.progress', execution=execution.id,
                                        instance=execution.instance.id, detail=value(item))
        if execution.streaming and not execution.cancellation.is_set():
            put = asyncio.create_task(execution.progress_queue.put(value(item)))
            cancelled = asyncio.create_task(execution.cancellation.wait())
            try:
                await asyncio.wait((put, cancelled), return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (put, cancelled):
                    if not task.done(): task.cancel()
                await asyncio.gather(put, cancelled, return_exceptions=True)


class Execution:
    def __init__(self, supervisor, instance, method, payload, parent, caller_id,
                 timeout, streaming, owner_scope):
        self.id = uuid.uuid4().hex
        self.supervisor, self.instance = supervisor, instance
        self.local_only = method in instance.definition.manifest.get('local_only', [])
        self.method, self.payload = method, payload if self.local_only else value(payload)
        self.parent, self.caller_id = parent, caller_id
        self.scope = owner_scope.child()
        self.stack = (parent.stack if parent else ()) + (instance.id,)
        deadline = time.monotonic() + timeout
        self.deadline = min(deadline, parent.deadline) if parent else deadline
        self.state, self.result, self.error = 'created', None, None
        self.streaming = streaming
        self.progress_queue = asyncio.Queue(maxsize=8)
        self.done = asyncio.Event()
        self.cancellation = asyncio.Event()
        self.children = set()
        self.context = CallContext(self)
        self.task = None
        if parent:
            parent.children.add(self)

    def cancel(self):
        if not self.done.is_set():
            self.state = 'cancelling'
            self.cancellation.set()
            if self.task:
                asyncio.get_running_loop().call_soon(self.task.cancel)

    async def wait(self):
        await self.done.wait()
        if self.error:
            raise self.error
        return self.result if self.local_only else value(self.result)

    async def stream(self):
        while True:
            if self.done.is_set() and self.progress_queue.empty():
                await self.wait()
                return
            item = asyncio.create_task(self.progress_queue.get())
            done = asyncio.create_task(self.done.wait())
            try:
                ready, _ = await asyncio.wait((item, done), return_when=asyncio.FIRST_COMPLETED)
                if item in ready or item.done():
                    yield item.result()
            finally:
                for task in (item, done):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(item, done, return_exceptions=True)


class Supervisor:
    def __init__(self, host):
        self.host, self.executions = host, {}

    def start(self, instance, method, payload, *, parent=None, caller_id=None,
              timeout=None, streaming=False, owner_scope=None):
        limits = instance.spec.get('execution', {})
        maximum, queue = limits.get('max_inflight', 1), limits.get('max_queue', 16)
        if parent and instance.id in parent.stack:
            # Opt-in services may accept nested calls only with an immediately
            # available slot. Never queue behind a slot held by an ancestor.
            if not limits.get('reentrant',False) or instance.admitted >= maximum:
                raise KernelError('EXECUTION.REENTRANCY_DENIED', instance_id=instance.id)
        if instance.admitted >= maximum + queue:
            raise KernelError('EXECUTION.CAPACITY_EXCEEDED', instance_id=instance.id)
        policy = {**self.host.data.get('control', {}), **instance.spec.get('control', {})}
        bound = policy.get('max_wall_seconds', self.host.max_wall_seconds)
        if timeout is not None and (type(timeout) not in (int, float) or timeout <= 0):
            raise KernelError('CONTROL.DENIED', 'Invalid deadline')
        instance.admitted += 1
        ex = Execution(self, instance, method, payload, parent, caller_id,
                       min(timeout, bound) if timeout is not None else bound, streaming,
                       owner_scope or (parent.scope if parent else instance.scope))
        self.executions[ex.id] = ex
        self.host.audit('execution.created', instance=instance.id, execution=ex.id,
                        scope=ex.scope.scope_id, caller=caller_id, parent=parent.id if parent else None,
                        owner_scope=ex.scope.parent.scope_id,
                        method=method, input_digest=None if ex.local_only else __import__('agentlab.abi', fromlist=['digest']).digest(payload),
                        local_only=ex.local_only, deadline=ex.deadline)
        ex.task = asyncio.create_task(self._run(ex))
        return ex

    async def _run(self, ex):
        async def execute():
            async with ex.instance.semaphore:
                ex.instance.active += 1
                ex.state = 'running'
                self.host.audit('execution.started', execution=ex.id, instance=ex.instance.id,
                    input_digest=None if ex.local_only else __import__('agentlab.abi',fromlist=['digest']).digest(ex.payload))
                try:
                    return await ex.instance.exports[ex.method](ex.payload, ex.context)
                finally:
                    ex.instance.active -= 1
        try:
            result = await asyncio.wait_for(execute(), max(0.001, ex.deadline - time.monotonic()))
            ex.result, ex.state = result if ex.local_only else value(result), 'succeeded'
        except asyncio.TimeoutError:
            ex.cancellation.set()
            ex.error = KernelError('EXECUTION.TIMEOUT', outcome_known=False)
            ex.state = 'failed'
        except asyncio.CancelledError:
            ex.cancellation.set()
            ex.error = KernelError('EXECUTION.CANCELLED', outcome_known=False)
            ex.state = 'cancelled'
        except KernelError as exc:
            ex.error = exc
            ex.state = 'unknown' if not exc.envelope['outcome_known'] and exc.envelope['code'] in (
                'EXECUTION.RESULT_UNKNOWN', 'EXECUTION.TRANSPORT_LOST') else 'failed'
        except Exception as exc:
            ex.error = KernelError('PLUGIN.UNHANDLED_EXCEPTION', type(exc).__name__, outcome_known=False)
            ex.state = 'failed'
        finally:
            for child in list(ex.children):
                child.cancel()
            if ex.children:
                await asyncio.gather(*(c.wait() for c in list(ex.children)), return_exceptions=True)
            errors = await ex.scope.aclose()
            if ex.error:
                # A callee's error retains its origin while traversing caller scopes.
                for key, origin in {'plugin_id':ex.instance.definition.id, 'instance_id':ex.instance.id,
                                    'execution_id':ex.id}.items():
                    if ex.error.envelope.get(key) is None:
                        ex.error.envelope[key] = origin
                ex.error.envelope = self.host.control.redact(ex.error.envelope)
                ex.error.diagnostics.extend(errors)
            ex.instance.admitted -= 1
            if ex.parent:
                ex.parent.children.discard(ex)
            ex.done.set()
            self.host.audit('execution.' + ex.state, execution=ex.id, instance=ex.instance.id,
                            error=ex.error.envelope if ex.error else None, cleanup=errors)

    async def drain(self, instances):
        pending = [e for e in self.executions.values() if e.instance.id in instances and not e.done.is_set()]
        for ex in pending:
            ex.cancel()
        await asyncio.gather(*(e.wait() for e in pending), return_exceptions=True)
