"""Python technical loader; package-relative imports without global sys.path edits."""
import importlib
import sys
import types
import uuid
from pathlib import Path
from .abi import KernelError


def python_apply(definition):
    module, name = definition.manifest['loader']['target'].rsplit(':', 1)
    namespace = '_agentkernel_' + uuid.uuid4().hex
    package = types.ModuleType(namespace)
    package.__path__ = [str(definition.root)]
    package.__package__ = namespace
    sys.modules[namespace] = package

    def cleanup():
        for key in list(sys.modules):
            if key == namespace or key.startswith(namespace + '.'):
                sys.modules.pop(key, None)
    try:
        loaded = importlib.import_module(namespace + '.' + module)
        if not Path(loaded.__file__).resolve().is_relative_to(definition.root):
            raise KernelError('CONTROL.DENIED', 'Python loader escapes package')
        return getattr(loaded, name), cleanup
    except BaseException:
        cleanup()
        raise
