"""Local read-only observer plugins; not part of the agent call path."""
import json
from pathlib import Path
from agentlab.observation import snapshot
from .common import fail


async def bridge_apply(ctx,config):
    async def state(p,c):return snapshot(ctx.host)
    async def events(p,c):return ctx.host.observation.read(p.get('after',0))
    ctx.expose('snapshot',state);ctx.expose('events',events)


async def trace_apply(ctx,config):
    async def replay(p,c):
        path=Path(config['path'])
        rows=[json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return {'events':rows,'gap':any(b['seq']!=a['seq']+1 for a,b in zip(rows,rows[1:]))}
    ctx.expose('replay',replay)
