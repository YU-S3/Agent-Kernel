"""PluginHost: uniform manifest assembly; algorithms are ordinary exports."""
import asyncio
from copy import deepcopy
import importlib.util
import inspect
import json
import platform
from pathlib import Path
import sys
import uuid
import time
from types import SimpleNamespace
from jsonschema import Draft202012Validator
from .abi import Composition, PluginDefinition, PluginInstance, Handle, KernelError, digest, value
from .control import Control
from .supervisor import Supervisor
from .contracts import validate, MANIFEST, COMPOSITION, local_schema
from .identity import runtime_identity
from .loader import python_apply


class PluginContext:
    def __init__(self, host, instance):
        self.host, self.instance = host, instance
        self.instance_id, self.scope_id = instance.id, instance.scope.scope_id
        self.self_handle = instance.handle
        self.ready = []
        self.handles = {instance.handle.token: instance.handle}
        self.executions = {}
        self.startup = SimpleNamespace(execution_id='startup:' + uuid.uuid4().hex,
            scope_id=self.scope_id, caller_id=None, parent_execution_id=None, deadline=None)

    def expose(self, name, fn):
        if self.instance.state != 'ALLOCATED' or name in self.instance.exports or not inspect.iscoroutinefunction(fn):
            raise KernelError('PLUGIN.INIT_FAILED', 'Expose requires a unique async callable', phase='apply')
        self.instance.exports[name] = fn

    def on_cleanup(self, fn):
        self.instance.scope.defer(fn)

    def on_ready(self, fn):
        if self.instance.state != 'ALLOCATED':
            raise KernelError('PLUGIN.NOT_READY', phase='apply')
        self.ready.append(fn)

    def parent(self, call):
        ex = self.host.supervisor.executions.get(getattr(call, 'execution_id', None))
        if not ex or ex.context is not call or ex.instance is not self.instance or ex.done.is_set():
            raise KernelError('CONTROL.DENIED', 'Invalid call context')
        return ex

    def target(self, target):
        if isinstance(target, str):
            bound = self.instance.spec.get('bindings', {}).get(target)
            if not bound:
                raise KernelError('CONTROL.DENIED', 'Unbound alias')
            return self.host.instances[bound]
        if not isinstance(target, Handle) or self.handles.get(target.token) is not target:
            raise KernelError('CONTROL.DENIED', 'Unowned handle')
        result = self.host.instances.get(target.instance_id)
        if not result or result.handle is not target or result.scope.closed:
            raise KernelError('PLUGIN.DISPOSED')
        return result

    async def start_call(self, target, method, payload, *, parent, streaming=False):
        if parent is self.startup and self.instance.state == 'LINKED':
            inst = self.target(target)
            if inst is not self.instance:
                raise KernelError('PLUGIN.NOT_READY', 'Startup may only schedule owned background work')
            if method not in inst.exports:
                raise KernelError('COMPOSITION.EXPORT_MISSING')
            self.host.background.append((self, method, value(payload)))
            return DeferredExecution()
        if self.host.state != 'READY' or self.instance.state != 'READY':
            raise KernelError('PLUGIN.NOT_READY')
        ex = self.parent(parent)
        inst = self.target(target)
        result = self.host._invoke(inst.id, method, payload, parent=ex, caller_id=self.instance_id, streaming=streaming)
        self.executions[result.id] = result
        return result

    async def call(self, target, method, payload, *, parent):
        if self.host.state != 'READY':
            raise KernelError('PLUGIN.NOT_READY')
        execution = await self.start_call(target, method, payload, parent=parent)
        try:
            return await execution.wait()
        except asyncio.CancelledError:
            execution.cancel()
            raise

    async def stream(self, target, method, payload, *, parent):
        execution = await self.start_call(target, method, payload, parent=parent, streaming=True)
        try:
            async for item in execution.stream():
                yield item
        finally:
            if not execution.done.is_set():
                execution.cancel()

    async def spawn_instance(self, template, bindings, config, *, parent, control=None):
        ex = self.parent(parent)
        handle = await self.host.spawn(self, template, bindings, config, ex, control=control)
        self.handles[handle.token] = handle
        return handle

    async def read_resource(self, ref, *, parent, offset=0, length=65536):
        self.parent(parent)
        return self.host.control.read_resource(ref, subject=self.instance_id, offset=offset, length=length)

    def authority(self, call):
        """Authenticated ancestry and intersected permissions; no application concepts."""
        ex = self.parent(call)
        chain, permissions = [], None
        while ex:
            policy = {**self.host.data.get('control', {}), **ex.instance.spec.get('control', {})}
            own = set(policy.get('permissions', []))
            permissions = own if permissions is None else permissions & own
            chain.append({'instance': ex.instance.id, 'execution': ex.id,
                          'scope': ex.scope.scope_id, 'instance_scope': ex.instance.scope.scope_id})
            ex = ex.parent
        return {'chain': chain, 'permissions': sorted(permissions or [])}

    def scope_cleanup(self, scope_ref, callback, *, parent):
        """Trusted local resource ownership, restricted to authenticated ancestors."""
        ex = self.parent(parent)
        while ex:
            for scope in (ex.scope, ex.instance.scope):
                if scope.scope_id == scope_ref and not scope.closed:
                    scope.defer(callback)
                    return
            ex = ex.parent
        raise KernelError('CONTROL.DENIED', 'Scope is not an active ancestor')

    def emit(self, topic, detail, *, parent):
        self.parent(parent)
        self.host.audit('plugin.detail', instance=self.instance_id, execution=parent.execution_id,
                        scope=parent.scope_id, topic=topic, detail=detail)


class DeferredExecution:
    async def wait(self):
        raise KernelError('PLUGIN.NOT_READY', 'Background work cannot be awaited during startup')


class PluginHost:
    def __init__(self, registry, *, audit_path=None):
        from .kernel import Scope
        self.registry, self.control = registry, Control()
        self.scope, self.instances = Scope(), {}
        self.state, self.background = 'RESOLVED', []
        self.supervisor = Supervisor(self)
        self.audit_path = Path(audit_path) if audit_path else None
        from .observation import ObservationLog
        self.observation = ObservationLog()
        # A restored Host appends to the same durable trace. Never reset cursors
        # or silently discard the earlier lifecycle after a process restart.
        if self.audit_path and self.audit_path.exists():
            with self.audit_path.open(encoding='utf-8') as stream:
                for line in stream:
                    row = json.loads(line)
                    if row['seq'] <= self.observation.sequence:
                        raise KernelError('OBSERVATION.INVALID_SEQUENCE')
                    self.observation.sequence = row['seq']
                    self.observation.rows.append(row)
        self.records = self.observation.rows
        self.max_wall_seconds = 300
        self._lock = None

    @property
    def lock(self):
        return deepcopy(self._lock)

    def audit(self, event, **fields):
        for key in ('detail',):
            if key in fields:
                raw=json.dumps(self.control.redact(value(fields[key])),ensure_ascii=False)
                if len(raw.encode('utf-8'))>16384:
                    fields[key]={'truncated':True,'sha256':__import__('hashlib').sha256(raw.encode()).hexdigest(),
                                 'preview':raw[:2048],'original_bytes':len(raw.encode())}
        row = self.observation.append(self.control.redact(value(dict(event=event, **fields))))
        if self.audit_path:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(row) + '\n')

    def preflight(self, data):
        validate(COMPOSITION, data)
        if data.get('kernel_api') != 1 or not data.get('composition'):
            raise KernelError('COMPOSITION.VERSION_MISMATCH', phase='resolve')
        packages = {}
        for row in data.get('packages', []):
            definition = self.registry.definition(row['id'])
            if row['id'] in packages or row['version'] != definition.version or row['digest'] != definition.digest or PluginDefinition.read(definition.root).digest != definition.digest:
                raise KernelError('COMPOSITION.VERSION_MISMATCH', row['id'], phase='resolve')
            packages[row['id']] = definition
        specs = data.get('instances', {})
        for ident, spec in {**specs, **data.get('templates', {})}.items():
            definition = packages.get(spec.get('plugin'))
            if not definition:
                raise KernelError('COMPOSITION.VERSION_MISMATCH', ident, phase='resolve')
            manifest = definition.manifest
            validate(MANIFEST, manifest)
            if not set(manifest.get('local_only', [])).issubset(manifest['exports']):
                raise KernelError('COMPOSITION.CONFIG_INVALID', 'Unknown local_only export')
            loader = manifest.get('loader', {})
            if manifest.get('kernel_api') != 1 or loader.get('adapter') not in ('python', 'jsonrpc_stdio'):
                raise KernelError('COMPOSITION.VERSION_MISMATCH', 'Invalid loader', phase='resolve')
            if loader['adapter'] == 'jsonrpc_stdio' and (not isinstance(loader.get('command'), list) or not loader['command'] or spec.get('execution', {}).get('isolation') != 'process'):
                raise KernelError('COMPOSITION.VERSION_MISMATCH', 'stdio needs process isolation', phase='resolve')
            exports = manifest.get('exports', [])
            if not exports or len(set(exports)) != len(exports):
                raise KernelError('COMPOSITION.EXPORT_MISSING', ident, phase='resolve')
            schema = manifest.get('config_schema', {})
            if isinstance(schema, str):
                path = (definition.root / schema).resolve()
                if not path.is_relative_to(definition.root):
                    raise KernelError('CONTROL.DENIED', 'Schema escapes package')
                schema = json.loads(path.read_text())
            try:
                local_schema(schema)
                Draft202012Validator.check_schema(schema)
                Draft202012Validator(schema).validate(spec.get('config', {}))
            except Exception as exc:
                raise KernelError('COMPOSITION.CONFIG_INVALID', type(exc).__name__, phase='resolve') from None
            if ident in specs:
                self.validate_bindings(spec, specs, packages)
            limits = spec.get('execution', {})
            if type(limits.get('max_inflight', 1)) is not int or limits.get('max_inflight', 1) < 1 or type(limits.get('max_queue', 16)) is not int or limits.get('max_queue', 16) < 0:
                raise KernelError('COMPOSITION.CONFIG_INVALID', 'Invalid capacity')
            policy = {**data.get('control', {}), **spec.get('control', {})}
            self.control.narrow(data.get('control', {}), policy)
            self.control.validate(spec, policy)
            self.control.check_config(spec.get('config', {}), policy)
            if manifest.get('local_only') and spec.get('execution', {}).get('isolation', 'local') != 'local':
                raise KernelError('COMPOSITION.CONFIG_INVALID', 'local_only exports require trusted in-process execution')
            if not set(manifest.get('requested_permissions', [])).issubset(policy.get('permissions', [])):
                raise KernelError('CONTROL.DENIED', 'Unapproved permission request')
        if data.get('entry'):
            self.check_entry(data['entry'], specs, packages)
        return packages

    def validate_bindings(self, spec, specs, packages):
        definition = packages[spec['plugin']]
        required = definition.manifest.get('requires', {})
        optional = definition.manifest.get('optional_requires', {})
        bindings = spec.get('bindings', {})
        if not set(required).issubset(bindings) or not set(bindings).issubset(required.keys() | optional.keys()):
            raise KernelError('COMPOSITION.BINDING_MISSING', phase='resolve')
        for alias, target in bindings.items():
            if not isinstance(target, str) or target not in specs:
                raise KernelError('COMPOSITION.INSTANCE_MISSING', alias, phase='resolve')
            target_definition = packages.get(specs[target]['plugin'])
            if target_definition is None:
                raise KernelError('COMPOSITION.VERSION_MISMATCH', target, phase='resolve')
            available = target_definition.manifest['exports']
            if not set({**optional,**required}[alias]).issubset(available):
                raise KernelError('COMPOSITION.EXPORT_MISSING', alias, phase='resolve')

    def check_entry(self, entry, specs, packages):
        if entry['instance'] not in specs:
            raise KernelError('COMPOSITION.INSTANCE_MISSING')
        if entry['method'] not in packages[specs[entry['instance']]['plugin']].manifest['exports']:
            raise KernelError('COMPOSITION.EXPORT_MISSING')

    async def allocate(self, ident, spec, scope):
        definition = self.packages[spec['plugin']]
        inst = PluginInstance(ident, definition, deepcopy(spec), scope, Handle(ident))
        inst.semaphore = asyncio.Semaphore(spec.get('execution', {}).get('max_inflight', 1))
        inst.context = PluginContext(self, inst)
        self.instances[ident] = inst
        self.audit('instance.spawn', instance=ident, plugin=definition.id, version=definition.version,
                   scope=scope.scope_id, owner_scope=scope.parent.scope_id if scope.parent else None,
                   bindings=spec.get('bindings', {}))
        def disposed():
            inst.state = 'DISPOSED'
            self.audit('instance.dispose', instance=ident, scope=scope.scope_id)
        scope.defer(disposed)
        return inst

    async def register_instance(self, inst):
        if inst.spec.get('execution', {}).get('isolation', 'local') == 'local':
            apply, cleanup = python_apply(inst.definition)
            inst.scope.defer(cleanup)
            await asyncio.wait_for(apply(inst.context, deepcopy(inst.spec.get('config', {}))), 10)
        else:
            from .transport import Worker
            inst.worker = Worker(self, inst)
            inst.scope.defer(inst.worker.close)
            exports = await inst.worker.initialize()
            for name in exports:
                async def invoke(payload, call, name=name):
                    return await inst.worker.invoke(name, payload, call)
                inst.exports[name] = invoke
        if set(inst.exports) != set(inst.definition.manifest['exports']):
            raise KernelError('COMPOSITION.EXPORT_MISSING', instance_id=inst.id, phase='link')
        inst.state = 'REGISTERED'

    async def ready_instance(self, inst):
        try:
            inst.context.startup.deadline = time.monotonic() + 5
            if inst.worker:
                await inst.worker.activate()
            for callback in inst.context.ready:
                await asyncio.wait_for(callback(inst.context.startup), 5)
        except Exception as exc:
            raise KernelError('PLUGIN.START_FAILED', type(exc).__name__, instance_id=inst.id, phase='ready') from None

    async def load(self, composition, *, expected_lock=None):
        if self.state != 'RESOLVED':
            raise KernelError('PLUGIN.NOT_READY', 'Create a fresh host for a new composition')
        self.data = value(composition.data if isinstance(composition, Composition) else composition)
        try:
            self.packages = self.preflight(self.data)
            identity = runtime_identity(self.data, self.packages, refresh=expected_lock is not None)
            identity = value(identity)
            identity['digest'] = digest(identity)
            if expected_lock is not None and value(expected_lock) != identity:
                raise KernelError('COMPOSITION.VERSION_MISMATCH', 'Runtime lock changed')
            self._lock = identity
            self.max_wall_seconds = self.data.get('control', {}).get('max_wall_seconds', 300)
            for ident, spec in self.data['instances'].items():
                await self.allocate(ident, spec, self.scope.child())
            self.state = 'ALLOCATED'
            for inst in list(self.instances.values()):
                await self.register_instance(inst)
            self.state = 'LINKED'
            for inst in self.instances.values():
                inst.state = 'LINKED'
            for inst in list(self.instances.values()):
                await self.ready_instance(inst)
            self.state = 'READY'
            for inst in self.instances.values():
                inst.state = 'READY'
            for context, method, payload in self.background:
                self._invoke(context.instance_id, method, payload, caller_id=context.instance_id)
            self.audit('composition.ready', composition=self.data['composition'], digest=self._lock['digest'])
            return self
        except BaseException as exc:
            self.state = 'FAILED'
            await self.supervisor.drain(set(self.instances))
            errors = await self.scope.aclose()
            for inst in self.instances.values():
                inst.state = 'DISPOSED'
            error = exc if isinstance(exc, KernelError) else KernelError('PLUGIN.INIT_FAILED', type(exc).__name__, phase='apply')
            error.envelope = self.control.redact(error.envelope)
            error.diagnostics.extend(errors)
            self.audit('composition.failed', error=error.envelope, cleanup=errors)
            raise error from None

    def _invoke(self, instance, method, payload, **kwargs):
        if self.state != 'READY':
            raise KernelError('PLUGIN.NOT_READY')
        inst = self.instances.get(instance)
        if not inst:
            raise KernelError('COMPOSITION.INSTANCE_MISSING')
        if inst.state != 'READY' or inst.scope.closed:
            raise KernelError('PLUGIN.DISPOSED')
        if method not in inst.exports:
            raise KernelError('COMPOSITION.EXPORT_MISSING')
        if method not in inst.definition.manifest.get('local_only', []):
            try:
                payload = value(payload)
            except (TypeError, ValueError):
                raise KernelError('EXECUTION.PROTOCOL_ERROR', 'Public values must be JSON-safe') from None
        elif kwargs.get('parent') and kwargs['parent'].instance.worker is not None:
            raise KernelError('CONTROL.DENIED', 'Remote callers cannot use local_only exports')
        return self.supervisor.start(inst, method, payload, **kwargs)

    def invoke(self, instance, method, payload, *, timeout=None, streaming=False):
        return self._invoke(instance, method, payload, timeout=timeout, streaming=streaming)

    def publish_resource(self, instance_id, content, *, ttl=60):
        inst = self.instances.get(instance_id)
        if self.state != 'READY' or not inst or inst.scope.closed:
            raise KernelError('PLUGIN.NOT_READY')
        return self.control.publish_resource(content, subject=instance_id, scope=inst.scope, ttl=ttl)

    async def run(self, composition=None, payload=None, *, entry=None):
        if composition is not None:
            await self.load(composition)
        selected = entry or self.data.get('entry')
        if not selected:
            raise KernelError('COMPOSITION.ENTRY_MISSING')
        return await self.invoke(selected['instance'], selected['method'], payload).wait()

    async def spawn(self, ctx, template, bindings, config, parent, control=None):
        spec = deepcopy(self.data.get('templates', {}).get(template))
        if spec is None:
            raise KernelError('CONTROL.DENIED', 'Template not approved')
        # Dynamic bindings are limited to capabilities the creator already holds.
        targets = set(ctx.instance.spec.get('bindings', {}).values()) | {h.instance_id for h in ctx.handles.values()}
        if not set(bindings.values()).issubset(targets):
            raise KernelError('CONTROL.DENIED', 'Cannot delegate an unowned binding')
        spec.update(bindings=value(bindings), config=value(config))
        parent_policy = {**self.data.get('control', {}), **ctx.instance.spec.get('control', {})}
        child_policy = {**parent_policy, **spec.get('control', {})}
        if control is not None:
            self.control.narrow(child_policy, {**child_policy, **value(control)})
            child_policy.update(value(control))
        self.control.narrow(parent_policy, child_policy)
        if not set(child_policy.get('permissions', [])).issubset(parent_policy.get('permissions', [])) or not set(child_policy.get('env', [])).issubset(parent_policy.get('env', [])):
            raise KernelError('CONTROL.DENIED', 'Child permissions exceed parent')
        for key in ('memory_mb', 'pids', 'cpus', 'max_wall_seconds'):
            if key in parent_policy and child_policy.get(key, parent_policy[key]) > parent_policy[key]:
                raise KernelError('CONTROL.DENIED', 'Child limit exceeds parent')
        if parent_policy.get('network') == 'none' and child_policy.get('network') != 'none':
            raise KernelError('CONTROL.DENIED', 'Child network exceeds parent')
        spec['control'] = child_policy
        ident = ctx.instance_id + '-' + uuid.uuid4().hex
        specs = {key: i.spec for key, i in self.instances.items()}
        specs[ident] = spec
        data = {**self.data, 'instances': specs}
        self.preflight(data)
        inst = await self.allocate(ident, spec, parent.scope.child())
        try:
            await self.register_instance(inst)
            inst.state = 'LINKED'
            await self.ready_instance(inst)
            inst.state = 'READY'
            for context, method, payload in list(self.background):
                if context is inst.context:
                    self.background.remove((context, method, payload))
                    self._invoke(ident, method, payload, caller_id=ident)
            return inst.handle
        except BaseException:
            await inst.scope.aclose()
            inst.state = 'DISPOSED'
            raise

    async def unload(self, composition_id=None):
        if composition_id and composition_id != self.data['composition']:
            raise KernelError('CONTROL.DENIED', 'Composition identity mismatch')
        self.state = 'DRAINING'
        await self.supervisor.drain(set(self.instances))
        errors = await self.scope.aclose()
        for inst in self.instances.values():
            inst.state = 'DISPOSED'
        self.state = 'DISPOSED'
        self.audit('composition.disposed', cleanup=errors)
        return errors
