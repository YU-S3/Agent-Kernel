"""Byte identities for the installed runtime and dependencies (not only versions)."""
import hashlib
from importlib.metadata import distributions
from pathlib import Path
import platform
import shutil
import sys
import json
from urllib.parse import urlparse, unquote
from .abi import KernelError, digest

_hash_cache = {}
_dependency_snapshots = {}


def file_hash(path):
    path = Path(path)
    stat = path.stat()
    key = (str(path.absolute()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if key in _hash_cache:
        return _hash_cache[key]
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    result = h.hexdigest()
    _hash_cache[key] = result
    return result


def dependency_identity(*, refresh=False):
    key = tuple(sys.path)
    if key in _dependency_snapshots and not refresh:
        return _dependency_snapshots[key]
    dependencies = []
    # Hash actual RECORD-tracked bytes too: editing a dependency without updating
    # RECORD must change the lock. Cache only files whose OS stat identity is unchanged.
    for dist in distributions():
        name = dist.metadata.get('Name')
        if name:
            record = dist.read_text('RECORD') or dist.read_text('SOURCES.txt') or ''
            files = {str(f): file_hash(dist.locate_file(f)) if dist.locate_file(f).is_file() else 'missing'
                     for f in dist.files or [] if f.hash is not None}
            direct = json.loads(dist.read_text('direct_url.json') or '{}')
            if direct.get('dir_info', {}).get('editable') and direct.get('url', '').startswith('file:'):
                project = Path(unquote(urlparse(direct['url']).path))
                roots = [project / 'src'] if (project / 'src').is_dir() else [project / name for name in
                    (dist.read_text('top_level.txt') or '').splitlines() if name]
                for root in roots:
                    for path in root.rglob('*.py'):
                        if '__pycache__' not in path.parts:
                            files['editable/' + str(path.relative_to(project))] = file_hash(path)
                if (project / 'pyproject.toml').is_file():
                    files['editable/pyproject.toml'] = file_hash(project / 'pyproject.toml')
            dependencies.append({'name': name, 'version': dist.version, 'record_sha256':
                hashlib.sha256(record.encode()).hexdigest(), 'content_digest': digest(files)})
    result = sorted(dependencies, key=lambda d: (d['name'], d['version']))
    _dependency_snapshots[key] = result
    return result


def runtime_identity(data, packages, *, refresh=False):
    dependencies = dependency_identity(refresh=refresh)
    executables = {'python': {'path': str(Path(sys.executable).resolve()), 'sha256': file_hash(sys.executable)}}
    for ident, spec in {**data['instances'], **data.get('templates', {})}.items():
        loader = packages[spec['plugin']].manifest['loader']
        if loader['adapter'] != 'jsonrpc_stdio' or spec.get('execution', {}).get('backend', 'docker') != 'local':
            continue
        binary = loader['command'][0]
        path = str(packages[spec['plugin']].root / binary) if binary.startswith('./') else shutil.which(binary)
        if not path:
            raise KernelError('CONTROL.ENFORCEMENT_UNAVAILABLE', 'External executable unavailable', phase='resolve')
        executables[ident] = {'path': str(Path(path).resolve()), 'sha256': file_hash(path)}
    return {'composition': data, 'python': sys.version, 'platform': platform.platform(),
            'executables': executables, 'dependencies': dependencies,
            'kernel': {p.name: file_hash(p) for p in Path(__file__).parent.glob('*.py')}}


def verify_dependencies():
    """Explicit offline integrity gate; raise if a RECORD-tracked byte has changed."""
    import base64
    checked = 0
    for dist in distributions():
        for entry in dist.files or []:
            if entry.hash is None:
                continue
            path = dist.locate_file(entry)
            if not path.is_file():
                raise KernelError('COMPOSITION.VERSION_MISMATCH', 'Installed dependency file missing', phase='resolve')
            h = hashlib.new(entry.hash.mode)
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    h.update(block)
            if base64.urlsafe_b64encode(h.digest()).rstrip(b'=').decode() != entry.hash.value:
                raise KernelError('COMPOSITION.VERSION_MISMATCH', 'Installed dependency content changed', phase='resolve')
            checked += 1
    return {'record_files_verified': checked}
