"""Trusted container backend. Dynamic source is never evaluated in the Host."""
import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path
from agentlab.abi import KernelError
from .common import fail, emit

IMAGE='python@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159'


async def apply(ctx, config):
    root=Path(config['root']).resolve(); receipts=[];running={}
    image=config.get('image',IMAGE)
    if '@sha256:' not in image: fail('SANDBOX.UNPINNED_IMAGE')
    async def command(*args):
        try:proc=await asyncio.create_subprocess_exec('docker',*args,cwd='/',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        except OSError:raise KernelError('CONTROL.SANDBOX_UNAVAILABLE','Docker executable unavailable',outcome_known=True) from None
        out,err=await proc.communicate()
        return proc.returncode,out.decode(errors='replace'),err.decode(errors='replace')
    async def remove(name):
        try: await asyncio.wait_for(command('rm','-f',name),10)
        except (asyncio.TimeoutError,KernelError): pass

    async def run(p,call):
        kind=p.get('kind')
        if kind not in ('shell','python') or not isinstance(p.get('code'),str) or len(p['code'])>8192: fail('SANDBOX.INVALID_REQUEST')
        if 'tool:'+kind not in ctx.authority(call)['permissions']: fail('SANDBOX.DENIED')
        timeout=min(float(config.get('timeout_seconds',3)),max(.05,call.deadline-__import__('time').monotonic()))
        name='agentlab-'+uuid.uuid4().hex
        row={'container':name,'execution':call.execution_id,'kind':kind,'image':image,'network':'none',
             'read_only_root':True,'workspace_read_only':True,'memory_bytes':134217728,'pids':32,'cpus':.5,
             'timeout_seconds':timeout,'output_limit_bytes':8192,'cleanup':False}
        proc=None
        try:
            with tempfile.TemporaryDirectory(prefix='agentlab-output-') as outdir:
                os.chmod(outdir,0o777)
                argv=['python','-I','-B','-c',p['code']] if kind=='python' else ['/bin/sh','-c',p['code']]
                code,output,error=await command('create','--name',name,'--network','none','--read-only','--cap-drop','ALL',
                    '--security-opt','no-new-privileges','--pids-limit','32','--memory','128m','--memory-swap','128m','--cpus','0.5',
                    '--user','65534:65534','--tmpfs','/tmp:rw,noexec,nosuid,size=16777216',
                    '--mount',f'type=bind,src={root},dst=/workspace,readonly',
                    '--mount',f'type=bind,src={outdir},dst=/output','--workdir','/workspace',image,*argv)
                if code: raise KernelError('CONTROL.SANDBOX_UNAVAILABLE','Container create failed',outcome_known=True)
                _,inspection,_=await command('inspect',name)
                data=json.loads(inspection)[0];hc=data['HostConfig']
                row['actual']={k:hc[k] for k in ('NetworkMode','ReadonlyRootfs','Memory','PidsLimit','NanoCpus','CapDrop','SecurityOpt')}
                row['mounts']=[{'destination':m['Destination'],'rw':m['RW']} for m in data['Mounts']]
                proc=await asyncio.create_subprocess_exec('docker','start','-a',name,cwd='/',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                running[call.execution_id]=(name,proc)
                async def drain(stream):
                    buf=bytearray();total=0
                    while True:
                        chunk=await stream.read(4096)
                        if not chunk:break
                        total+=len(chunk);buf.extend(chunk[:max(0,8192-len(buf))])
                    return buf.decode(errors='replace'),total
                tasks=[asyncio.create_task(drain(proc.stdout)),asyncio.create_task(drain(proc.stderr))]
                try:
                    await asyncio.wait_for(proc.wait(),timeout)
                    out,err=await asyncio.gather(*tasks)
                    _,inspection,_=await command('inspect',name);state=json.loads(inspection)[0]['State']
                    row.update(exit_code=state['ExitCode'],oom_killed=state['OOMKilled'],stdout=out[0],stderr=err[0],
                               stdout_bytes=out[1],stderr_bytes=err[1],status='completed')
                except asyncio.TimeoutError:
                    row['status']='timeout'
                    raise KernelError('EXECUTION.TIMEOUT','Sandbox deadline',outcome_known=False)
                finally:
                    await remove(name)
                    if proc.returncode is None: proc.kill();await proc.wait()
                    for task in tasks:
                        if not task.done():task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
                artifacts=[]
                for path in Path(outdir).iterdir():
                    if path.is_file() and not path.is_symlink() and path.stat().st_size<=8192:
                        artifacts.append({'path':path.name,'text':path.read_text(errors='replace')})
                row['artifacts']=artifacts
        except asyncio.CancelledError:
            row['status']='cancelled';raise
        except KernelError as exc:
            row.setdefault('status','failed');row['error']=exc.envelope;raise
        finally:
            await remove(name)
            if proc and proc.returncode is None: proc.kill();await proc.wait()
            running.pop(call.execution_id,None)
            try:
                code,_,_=await command('inspect',name)
                row['cleanup']=code!=0
            except KernelError:
                row['cleanup']=False;row['cleanup_error']='SANDBOX_UNAVAILABLE'
            receipts.append(row)
            emit(ctx,'sandbox.receipt',row,call)
        return dict(row)
    async def inspect(p,c):return list(receipts)
    async def cancel(p,c):
        row=running.get(p['execution'])
        if c.caller_id is not None:fail('SANDBOX.CANCEL_DENIED')
        if row: await remove(row[0])
        return {'accepted':bool(row)}
    ctx.expose('run',run);ctx.expose('inspect',inspect);ctx.expose('cancel',cancel)
