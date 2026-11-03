"""Shared v1 values; no application-specific ports or counters."""
import hashlib
import json
from pathlib import Path
from dataclasses import dataclass, field
from copy import deepcopy
import uuid


def value(data):
    return json.loads(json.dumps(data, allow_nan=False))


def digest(data):
    return 'sha256:' + hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class KernelError(Exception):
    def __init__(self, code, message='', *, plugin_id=None, instance_id=None,
                 execution_id=None, phase='invoke', outcome_known=True, cause=None):
        self.envelope = dict(code=code, message=message or code, plugin_id=plugin_id,
                             instance_id=instance_id, execution_id=execution_id,
                             phase=phase, outcome_known=outcome_known, cause=cause)
        self.diagnostics = []
        # Keep args free of unredacted messages; Host may sanitize the envelope.
        super().__init__(code)

    def __str__(self):
        return self.envelope['code'] + ': ' + self.envelope['message']

    @classmethod
    def from_wire(cls, obj):
        obj = dict(obj)
        obj.pop('diagnostics', None)
        if not isinstance(obj.get('code'), str) or not isinstance(obj.get('message'), str) or type(obj.get('outcome_known')) is not bool:
            raise ValueError('Invalid error envelope')
        return cls(**obj)


class PluginOperationError(KernelError):
    def __init__(self, original_code, message=''):
        super().__init__('PLUGIN.REPORTED_ERROR', message, cause={'code': original_code})


@dataclass(frozen=True)
class PluginDefinition:
    id: str
    version: str
    digest: str
    root: Path
    manifest: dict
    kernel_api: int = 1

    @property
    def api_version(self):
        return self.kernel_api

    @classmethod
    def read(cls, root):
        root = Path(root).resolve()
        manifest = json.loads((root / 'plugin.json').read_text(encoding='utf-8'))
        files = {}
        for path in sorted(root.rglob('*')):
            if '__pycache__' in path.parts or '.git' in path.parts:
                continue
            if path.is_symlink():
                raise KernelError('COMPOSITION.VERSION_MISMATCH', 'Package symlinks are not allowed', phase='resolve')
            if path.is_file():
                files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        # Thin manifests may bind installed Python implementations. Their actual
        # source bytes are part of the definition, never just an import name.
        import importlib.util
        for name in manifest.get('python_dependencies', []):
            spec = importlib.util.find_spec(name)
            if not spec or not spec.origin:
                raise KernelError('COMPOSITION.VERSION_MISMATCH', 'Python dependency unavailable')
            source = Path(spec.origin).resolve()
            paths = source.parent.rglob('*') if spec.submodule_search_locations else [source]
            for path in sorted(paths):
                if path.is_file() and '__pycache__' not in path.parts:
                    files['python/' + name + '/' + path.relative_to(source.parent).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        return cls(manifest['id'], manifest['version'], digest(files), root, manifest,
                   manifest.get('kernel_api', 0))


@dataclass(frozen=True)
class Composition:
    data: dict

    @classmethod
    def read(cls, path):
        import yaml
        return cls(value(yaml.safe_load(Path(path).read_text(encoding='utf-8'))))


@dataclass(frozen=True)
class Handle:
    instance_id: str
    token: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class PluginInstance:
    id: str
    definition: PluginDefinition
    spec: dict
    scope: object
    handle: Handle
    exports: dict = field(default_factory=dict)
    state: str = 'ALLOCATED'
    context: object = None
    worker: object = None
    active: int = 0
    admitted: int = 0
    semaphore: object = None
