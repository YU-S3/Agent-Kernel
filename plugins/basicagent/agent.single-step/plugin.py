from agentlab_plugins.driver import apply as driver_apply

async def apply(ctx, config):
    return await driver_apply(ctx, {**config, 'mode': 'single_step'})
