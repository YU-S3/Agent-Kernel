from agentlab_app.server import start_server


async def apply(ctx,config):
    server=None
    async def start(p,c):
        nonlocal server
        if server is None:
            server=await start_server(config['report_dir'],config.get('host','127.0.0.1'),config.get('port',8765),config.get('profiles_dir'))
        return {'url':server['url'],'mode':'mixed'}
    async def status(p,c):return {'running':server is not None,'url':server['url'] if server else None}
    async def close():
        if server:await server['close']()
    ctx.on_cleanup(close);ctx.expose('start',start);ctx.expose('status',status)
