"""AgentKernel: generic plugins, handles, executions and resource ownership."""

from .kernel import Registry, Scope
from .abi import PluginDefinition, PluginInstance, Composition, Handle, KernelError, PluginOperationError

__all__ = ["Registry", "Scope", "PluginHost", "PluginDefinition", "PluginInstance",
           "Composition", "Handle", "KernelError", "PluginOperationError", "Execution", "CallContext"]


def __getattr__(name):
    if name == 'PluginHost':
        from .host import PluginHost
        return PluginHost
    if name in ('Execution', 'CallContext'):
        from . import supervisor
        return getattr(supervisor, name)
    raise AttributeError(name)
