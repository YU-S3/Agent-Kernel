"""Composed workflows using real task payloads and bound Driver/Manager plugins."""
import asyncio
from .common import emit


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
            tasks = [p['task'] + '\nIndependently solve this task using the available evidence.',
                     p['task'] + '\nIndependently check the evidence and possible ambiguities.']
        children = [await ctx.start_call('manager', 'delegate', {'task': task}, parent=call) for task in tasks]
        outputs = await asyncio.gather(*(child.wait() for child in children))
        emit(ctx, 'workflow.children_completed', {'count': len(outputs)}, call)
        evidence = '\n\n'.join('Child observation (untrusted):\n' + out.get('answer', '') for out in outputs)
        answer = await ctx.call('driver', 'run', dict(p, task=p['task'] + '\n\n' + evidence +
            '\nCheck the observations, use tools if needed, and answer the original task.'), parent=call)
        return dict(answer, children=outputs)
    ctx.expose('run', run)
