"""Local workspace: canonical containment and no symlink traversal."""
from pathlib import Path
from .common import fail


async def apply(ctx, config):
    root = Path(config['root']).resolve()
    if not root.is_dir(): fail('WORKSPACE.MISSING')
    def resolve(name):
        path = Path(name)
        if path.is_absolute() or '..' in path.parts: fail('WORKSPACE.ESCAPE')
        target = root / path
        current = root
        for part in path.parts:
            current /= part
            if current.is_symlink(): fail('WORKSPACE.SYMLINK')
        if not target.resolve().is_relative_to(root): fail('WORKSPACE.ESCAPE')
        return target
    async def read(p,call):
        if not set(config.get('read_permissions',['tool:read_file'])) & set(ctx.authority(call)['permissions']): fail('WORKSPACE.DENIED')
        path = resolve(p['path'])
        try:
            if not path.is_file(): fail('WORKSPACE.NOT_FOUND')
            if path.stat().st_size > 65536: fail('WORKSPACE.TOO_LARGE')
            return {'text':path.read_text(encoding='utf-8')}
        except UnicodeDecodeError: fail('WORKSPACE.NOT_UTF8')
        except OSError: fail('WORKSPACE.NOT_READABLE')
    async def write(p,call):
        if 'workspace:write' not in ctx.authority(call)['permissions'] or not config.get('writable'): fail('WORKSPACE.READ_ONLY')
        path = resolve(p['path'])
        try:path.write_text(p['text'],encoding='utf-8')
        except OSError:fail('WORKSPACE.NOT_WRITABLE')
        return {'written':True}
    async def listing(p,call): return {'files':[f.name for f in sorted(root.iterdir()) if f.is_file() and not f.is_symlink()]}
    async def resolved(p,call): return {'relative':resolve(p['path']).relative_to(root).as_posix()}
    ctx.expose('read',read);ctx.expose('write',write);ctx.expose('list',listing);ctx.expose('resolve',resolved)
