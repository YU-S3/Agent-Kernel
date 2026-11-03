"""Stateless runtime decisions. No benchmark names, answers, or scoring feedback."""


async def apply(ctx,config):
    async def decide(p,c):
        reason=None
        if p.get('near_limit'):reason='context_pressure'
        elif p.get('stagnant_steps',0)>=config.get('stagnant_steps',3):reason='no_new_evidence'
        elif p.get('steps',0)>=config.get('max_steps',12)-1:reason='step_limit'
        return {'action':'finalize' if reason else 'continue','reason':reason,
            'max_completion_tokens':config.get('final_tokens',2048)}
    ctx.expose('decide',decide)
