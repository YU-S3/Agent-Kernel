async def apply(ctx, config):
    async def run(payload, call):
        first = await ctx.call('helper', 'echo', payload, parent=call)
        return await ctx.call('next', 'echo', first, parent=call)
    async def stream(payload, call):
        async for item in ctx.stream('helper', 'emit', payload, parent=call):
            await call.progress(item)
        return 'done'
    async def spawn(payload, call):
        handle = await ctx.spawn_instance('child', {}, {'prefix': 'child'}, parent=call)
        return await ctx.call(handle, 'echo', payload, parent=call)
    async def recurse(payload, call):
        return await ctx.call(ctx.self_handle, 'recurse', payload, parent=call)
    for name, fn in [('run',run),('stream',stream),('spawn',spawn),('recurse',recurse)]:
        ctx.expose(name, fn)
