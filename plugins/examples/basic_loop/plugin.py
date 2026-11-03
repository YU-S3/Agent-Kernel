async def apply(ctx, config):
    async def run(payload, call):
        request = {'input': payload, 'observation': None}
        for _ in range(config.get('max_steps', 4)):
            response = await ctx.call('model', 'infer', request, parent=call)
            if 'answer' in response:
                return response['answer']
            request['observation'] = await ctx.call('tools', 'execute', response, parent=call)
        return {'ok': False, 'reason': 'application_step_limit'}
    ctx.expose('run', run)
