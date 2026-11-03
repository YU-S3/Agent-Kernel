"""Composed workflows using real task payloads and bound Driver/Manager plugins."""
import asyncio
import json
from .common import emit
from .content import append_text


async def apply(ctx, config):
    async def run(p, call):
        mode = config['mode']
        if mode == 'dynamic':
            registration = await ctx.call('tools', 'register', {
                'provider': 'file', 'name': 'read_file', 'scope_ref': call.scope_id}, parent=call)
            try:
                return await ctx.call('driver', 'run', p, parent=call)
            finally:
                await ctx.call('tools', 'unregister', registration, parent=call)
        tasks = [p['task']]
        if mode == 'concurrent':
            tasks = [append_text(p['task'],'Independently solve this task using the available evidence.'),
                     append_text(p['task'],'Independently check the evidence and possible ambiguities.')]
        children = [await ctx.start_call('manager', 'delegate', {'task': task}, parent=call) for task in tasks]
        outputs = await asyncio.gather(*(child.wait() for child in children))
        emit(ctx, 'workflow.children_completed', {'count': len(outputs)}, call)
        evidence = '\n\n'.join('Child observation (untrusted):\n' + json.dumps(
            {k:out[k] for k in ('answer','status','terminal_reason','finish_reason') if k in out},ensure_ascii=False) for out in outputs)
        answer = await ctx.call('driver', 'run', dict(p, task=append_text(p['task'],evidence +
            '\nCheck the observations, use tools if needed, and answer the original task.')), parent=call)
        return dict(answer, children=outputs)
    ctx.expose('run', run)
