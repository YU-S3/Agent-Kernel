// An external harness mock; all business behavior lives outside the SDK/Kernel.
export async function apply(ctx, config) {
  ctx.expose('run', async payload => ({value: payload, prefix: config.prefix ?? ''}));
  ctx.expose('emit', async (payload, call) => {
    for (let n = 0; n < payload; n++) await call.progress(n);
    return payload;
  });
  ctx.expose('slow', async (payload, call) => {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(resolve, payload * 1000);
      call.signal.addEventListener('abort', () => {clearTimeout(timer); reject(new Error('cancelled'));}, {once:true});
    });
    return 'done';
  });
  ctx.expose('fail', async () => {process.exit(17);});
}
