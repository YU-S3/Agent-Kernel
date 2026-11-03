// Generic Node SDK: Content-Length JSON-RPC, not a model or agent implementation.
import {pathToFileURL} from 'node:url';
import {resolve, relative, join} from 'node:path';
import {randomUUID, createHash} from 'node:crypto';
import {readdir, readFile, lstat} from 'node:fs/promises';
const pending = new Map(), active = new Map(), calls = new Map(), credit = new Map();
const exports = new Map(), ready = [], cleanup = [];
const streams = new Map();
let state = 'new', buffer = Buffer.alloc(0), instanceId, startup;
const failure = (code, message=code, known=true) => ({code,message,plugin_id:null,instance_id:instanceId,execution_id:null,phase:'invoke',cause:null,outcome_known:known});
async function packageDigest(root) {
  const files={};
  async function walk(dir) {
    for(const name of (await readdir(dir)).sort()) {
      if(name==='__pycache__'||name==='.git')continue;
      const path=join(dir,name), stat=await lstat(path);
      if(stat.isSymbolicLink())throw failure('COMPOSITION.VERSION_MISMATCH');
      if(stat.isDirectory())await walk(path);
      else if(stat.isFile())files[relative(root,path).replaceAll('\\','/')]=createHash('sha256').update(await readFile(path)).digest('hex');
    }
  }
  await walk(root);
  const canonical=JSON.stringify(Object.fromEntries(Object.entries(files).sort(([a],[b])=>a<b?-1:a>b?1:0)))
    .replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
  return 'sha256:'+createHash('sha256').update(canonical).digest('hex');
}
async function send(message) {
  const body = Buffer.from(JSON.stringify(message));
  if (body.length > 1048576) throw failure('EXECUTION.PROTOCOL_ERROR');
  if (!process.stdout.write(Buffer.concat([Buffer.from(`Content-Length: ${body.length}\r\n\r\n`), body])))
    await new Promise(resolve => process.stdout.once('drain', resolve));
}
const notify = (method, params) => send({jsonrpc:'2.0',method,params});
function request(method, params) {
  if (pending.size >= 64) return Promise.reject(failure('EXECUTION.CAPACITY_EXCEEDED'));
  const id = randomUUID();
  return new Promise((resolve,reject) => {pending.set(id,{resolve,reject});send({jsonrpc:'2.0',id,method,params}).catch(reject);});
}
function takeCredit(id, signal) {
  if(signal.aborted)return Promise.reject(failure('EXECUTION.CANCELLED'));
  let row = credit.get(id);
  if (!row) {row={n:0,waiters:[]};credit.set(id,row);}
  if (row.n) {row.n--;return Promise.resolve();}
  return new Promise((resolve,reject) => {
    const waiter = () => {signal.removeEventListener('abort', cancel);resolve();};
    const cancel = () => {row.waiters=row.waiters.filter(x=>x!==waiter);reject(failure('EXECUTION.CANCELLED'));};
    row.waiters.push(waiter);signal.addEventListener('abort',cancel,{once:true});
  });
}
function callParams(target, method, payload, parent) {
  if (state!=='active') throw failure('PLUGIN.NOT_READY');
  return {target,export:method,payload,parent_execution_id:parent.execution_id};
}
const ctx = {
  self_handle:'$self',
  expose(name,fn) {if(state!=='initialize'||exports.has(name))throw failure('PLUGIN.INIT_FAILED');exports.set(name,fn);},
  on_ready(fn){if(state!=='initialize')throw failure('PLUGIN.NOT_READY');ready.push(fn);},
  on_cleanup(fn){if(['draining','disposed'].includes(state))throw failure('PLUGIN.DISPOSED');cleanup.push(fn);},
  call(target,method,payload,{parent}){return request('kernel.call_host',callParams(target,method,payload,parent));},
  async start_call(target,method,payload,{parent}) {
    const params = state==='activate'&&parent===startup?{startup:true,target,export:method,payload}:callParams(target,method,payload,parent);
    const h=await request('kernel.start_call',params);
    return {wait:()=>h.deferred?Promise.reject(failure('PLUGIN.NOT_READY')):request('kernel.wait_execution',h),cancel:()=>request('kernel.cancel',h)};
  },
  async *stream(target,method,payload,{parent}) {
    const token=randomUUID(), row={queue:[],wake:null,id:null,seq:0,done:false,error:null};streams.set(token,row);
    const operation=request('kernel.call_host',{...callParams(target,method,payload,parent),streaming:true,request_token:token})
      .catch(error=>{row.error=error;}).finally(()=>{row.done=true;row.wake?.();});
    try {while(!row.done||row.queue.length){
      if(!row.queue.length){await new Promise(r=>{row.wake=r;});row.wake=null;continue;}
      yield row.queue.shift();await notify('kernel.credit',{execution_id:row.id,n:1});
    }await operation;if(row.error)throw row.error;}
    finally {if(!row.done&&row.id)await request('kernel.cancel',{execution_id:row.id});streams.delete(token);}
  },
  spawn_instance(template,bindings,config,{parent}){return request('kernel.spawn_instance',{template,bindings,config,parent_execution_id:parent.execution_id});},
  read_resource(ref,{parent,offset=0,length=65536}){return request('kernel.read_resource',{ref,offset,length,parent_execution_id:parent.execution_id});}
};
async function handle(method,p) {
  if(method==='kernel.initialize') {
    if(state!=='new'||p.kernel_api!==1)throw failure('PLUGIN.INIT_FAILED');
    if(await packageDigest(p.root)!==p.plugin_definition_digest)throw failure('COMPOSITION.VERSION_MISMATCH');
    const appPath=resolve(process.argv[2]);
    if(relative(p.root,appPath).startsWith('..'))throw failure('CONTROL.DENIED');
    state='initialize';instanceId=p.instance_id;
    ctx.instance_id=instanceId;ctx.scope_id=p.scope_id;
    startup={execution_id:'startup:'+randomUUID(),scope_id:p.scope_id,caller_id:null,parent_execution_id:null,deadline:null};
    const app=await import(pathToFileURL(appPath).href);
    await app.apply(ctx,p.config);state='registered';return [...exports.keys()];
  }
  if(method==='kernel.activate') {
    if(state!=='registered')throw failure('PLUGIN.NOT_READY');
    state='activate';for(const fn of ready)await fn(startup);state='active';return {ready:true};
  }
  if(method==='kernel.credit') {
    if(!Number.isInteger(p.n)||p.n<=0||p.n>8)throw failure('EXECUTION.PROTOCOL_ERROR');
    const row=credit.get(p.execution_id)??{n:0,waiters:[]};credit.set(p.execution_id,row);
    for(let n=0;n<p.n;n++){if(row.waiters.length)row.waiters.shift()();else row.n++;}return;
  }
  if(method==='kernel.stream_open'){const row=streams.get(p.request_token);if(!row)throw failure('EXECUTION.PROTOCOL_ERROR');row.id=p.execution_id;return;}
  if(method==='kernel.progress'){
    const row=[...streams.values()].find(r=>r.id===p.execution_id);
    if(!row||p.seq!==row.seq||row.queue.length>=8)throw failure('EXECUTION.PROTOCOL_ERROR');
    row.seq++;row.queue.push(p.value);row.wake?.();return;
  }
  if(method==='kernel.invoke') {
    if(state!=='active'||calls.has(p.execution_id))throw failure('PLUGIN.NOT_READY');
    const controller=new AbortController();calls.set(p.execution_id,controller);let seq=0;
    const call={execution_id:p.execution_id,scope_id:p.scope_id,caller_id:p.caller_id,parent_execution_id:p.parent_id,signal:controller.signal,deadline:p.deadline,progress:async value=>{
      if(p.streaming){await takeCredit(p.execution_id,controller.signal);await notify('kernel.progress',{execution_id:p.execution_id,seq:seq++,value});}
    }};
    try {return await exports.get(p.export)(p.payload,call);}
    finally {calls.delete(p.execution_id);credit.delete(p.execution_id);}
  }
  if(method==='kernel.cancel') {const c=calls.get(p.execution_id);c?.abort();await new Promise(r=>setImmediate(r));return {accepted:true,stopped:!calls.has(p.execution_id)};}
  if(method==='kernel.shutdown') {state='draining';for(const c of calls.values())c.abort();const errors=[];for(const fn of [...cleanup].reverse())try{await fn();}catch(e){errors.push(e.name);}state='disposed';return {cleanup_errors:errors};}
  throw failure('EXECUTION.PROTOCOL_ERROR');
}
async function receive(m) {
  if(m.jsonrpc!=='2.0')throw failure('EXECUTION.PROTOCOL_ERROR');
  if(!m.method){const p=pending.get(m.id);if(p){pending.delete(m.id);m.error?p.reject(m.error.data):p.resolve(m.result);}return;}
  const id=m.id??randomUUID();if(active.has(id)||active.size>=64)throw failure('EXECUTION.PROTOCOL_ERROR');active.set(id,true);
  try {const result=await handle(m.method,m.params??{});if(m.id!==undefined)await send({jsonrpc:'2.0',id:m.id,result});}
  catch(e){if(m.id!==undefined)await send({jsonrpc:'2.0',id:m.id,error:{code:-32000,message:e.code??'PLUGIN.UNHANDLED_EXCEPTION',data:e.code?e:failure('PLUGIN.UNHANDLED_EXCEPTION',e.name,false)}});else throw e;}
  finally {active.delete(id);}
}
process.stdin.on('data',chunk=>{
  buffer=Buffer.concat([buffer,chunk]);
  try {while(true){
    const end=buffer.indexOf('\r\n\r\n');if(end<0){if(buffer.length>128)throw Error();break;}
    const match=/^Content-Length: (\d+)$/.exec(buffer.subarray(0,end).toString());if(!match)throw Error();
    const size=Number(match[1]);if(size<1||size>1048576)throw Error();if(buffer.length<end+4+size)break;
    const msg=JSON.parse(buffer.subarray(end+4,end+4+size));buffer=buffer.subarray(end+4+size);
    receive(msg).catch(()=>process.exit(2));
  }}catch{process.exit(2);}
});
process.stdin.on('end',()=>process.exit(0));
