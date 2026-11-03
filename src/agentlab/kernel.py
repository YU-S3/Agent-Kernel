"""Generic Scope ownership and explicit plugin registry."""
import inspect as _inspect
import uuid


class DisposalError(RuntimeError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("Resource cleanup failed: " + "; ".join(map(str, errors)))


class Scope:
    """Own effects and children; release all in reverse order, including on failure."""
    def __init__(self):
        self._cleanup = []
        self.closed = False
        self.scope_id = uuid.uuid4().hex
        self.parent = None

    def defer(self, cleanup):
        if self.closed:
            raise RuntimeError("Scope is closed")
        self._cleanup.append(cleanup)

    def child(self):
        child = Scope()
        child.parent = self
        self.defer(child.close)
        return child

    async def aclose(self):
        if self.closed:
            return []
        self.closed = True
        if self.parent and self.close in self.parent._cleanup:
            self.parent._cleanup.remove(self.close)
        errors = []
        while self._cleanup:
            callback = self._cleanup.pop()
            try:
                child = getattr(callback, '__self__', None)
                if isinstance(child, Scope):
                    errors.extend(await child.aclose())
                else:
                    result = callback()
                    if _inspect.isawaitable(result):
                        import asyncio
                        await asyncio.wait_for(result, 5)
            except BaseException as exc:
                errors.append({'code': 'PLUGIN.CLEANUP_FAILED', 'cause': type(exc).__name__})
        return errors

    def close(self):
        if self.closed:
            return
        self.closed = True
        errors = []
        while self._cleanup:
            try:
                self._cleanup.pop()()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise DisposalError(errors)


class Registry:
    def __init__(self):
        self._plugins = {}

    def register(self, plugin):
        from .abi import PluginDefinition
        if not isinstance(plugin, PluginDefinition) or plugin.api_version != 1:
            raise ValueError("Unsupported plugin definition")
        if not plugin.id or not plugin.version or plugin.id in self._plugins:
            raise ValueError("Missing or duplicate identity: " + plugin.id)
        self._plugins[plugin.id] = plugin

    def definition(self, name):
        from .abi import KernelError
        plugin = self._plugins.get(name)
        if plugin is None:
            raise KernelError('COMPOSITION.INSTANCE_MISSING', name, phase='resolve')
        return plugin


def __getattr__(name):
    if name in {'Execution', 'CallContext'}:
        from . import supervisor
        return getattr(supervisor, name)
    if name == 'PluginHost':
        from .host import PluginHost
        return PluginHost
    if name in {'PluginDefinition', 'PluginInstance', 'Composition', 'Handle', 'KernelError', 'PluginOperationError'}:
        from . import abi
        return getattr(abi, name)
    raise AttributeError(name)
