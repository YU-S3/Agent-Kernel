import asyncio
import os
from agentlab.abi import PluginOperationError


async def apply(ctx, config):
    count = 0
    async def echo(payload, call):
        nonlocal count
        count += 1
        return {'value': payload, 'prefix': config.get('prefix', ''), 'count': count}
    async def infer(payload, call):
        if payload.get('observation') is None:
            return {'tool': 'double', 'input': payload['input']}
        return {'answer': payload['observation']}
    async def execute(payload, call):
        return payload['input'] * 2
    async def slow(payload, call):
        await asyncio.sleep(payload)
        return 'done'
    async def emit(payload, call):
        for n in range(payload):
            await call.progress(n)
        return payload
    async def fail(payload, call):
        if payload == 'business':
            raise PluginOperationError('provider.rate_limited')
        if payload == 'crash':
            os._exit(17)
        raise RuntimeError('unhandled')
    async def probe(payload, call):
        import socket, pathlib, subprocess, sys
        checks = {}
        for key, action in {
            'socket': lambda: socket.create_connection((payload['host'], payload['port']), timeout=.3),
            'file': lambda: pathlib.Path(payload['file']).read_text(),
            'root_write': lambda: pathlib.Path('/blocked').write_text('x'),
        }.items():
            try:
                result = action()
                if hasattr(result, 'close'): result.close()
                checks[key] = 'allowed'
            except Exception as exc:
                checks[key] = type(exc).__name__
        checks['secret_present'] = payload['secret'] in os.environ
        checks['memory_max'] = pathlib.Path('/sys/fs/cgroup/memory.max').read_text().strip()
        checks['pids_max'] = pathlib.Path('/sys/fs/cgroup/pids.max').read_text().strip()
        checks['cpu_max'] = pathlib.Path('/sys/fs/cgroup/cpu.max').read_text().strip()
        return checks
    for name, fn in [('echo',echo),('infer',infer),('execute',execute),('slow',slow),('emit',emit),('fail',fail),('probe',probe)]:
        ctx.expose(name, fn)
