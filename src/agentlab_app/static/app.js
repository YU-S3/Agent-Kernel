'use strict';
const $=id=>document.getElementById(id), NS='http://www.w3.org/2000/svg';
let state={instances:{},executions:{}},events=[],runId=null,cursor=0,mode='binding',replayMode=false,replayCursor=0,timer=null,positions={},manual={},camera={x:0,y:0,w:1000,h:500},cameraReady=false,userCamera=false,pointer=null,lastNodes=0,polling=false;
const svg=$('graph');
let routeSignature='',routes=[],generation=0,previewVersion=0,hot={},draft='',knownUsage=0,unknownUsage=0;
const names={live:'真实模型 · 日常对话','viz-basic':'离线示例 · 读文件','viz-subagent':'离线示例 · 子代理','viz-tool-sandbox':'离线示例 · 容器工具'};
let liveProfiles={},datasets=[];
function options(){const result={tools:[...document.querySelectorAll('[name=tool]:checked')].map(e=>e.value),workspace:$('workspace').value,max_steps:Number($('maxSteps').value),request_limit:$('requestLimit').value?Number($('requestLimit').value):null};if($('taskSource').value==='searchqa')result.benchmark={dataset_id:$('dataset').value,start:Number($('taskStart').value),count:Number($('taskCount').value)};return result}
const el=(name,attrs={})=>{const n=document.createElementNS(NS,name);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);return n};
const text=(value,x,y,cls='')=>{let n=el('text',{x,y,class:cls});n.textContent=value;return n};
function showError(err){$('warning').textContent=String(err.message||err);}
async function api(path,body){const resp=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!resp.ok)throw Error(await resp.text());return resp.json();}
function extent(){const r=svg.getBoundingClientRect();return {w:Math.max(1,r.width),h:Math.max(1,r.height)}}
function updateCamera(){svg.setAttribute('viewBox',`${camera.x} ${camera.y} ${camera.w} ${camera.h}`)}
function viewNodes(){
 if(mode!=='scope')return Object.fromEntries(Object.entries(state.instances).map(([id,n])=>[id,{...n,label:n.plugin}]));
 let nodes={};const add=(id,label,owner)=>{if(id)nodes[id]={label,owner,state:'scope'}};
 add(state.root_scope,'Composition scope',null);
 for(const [id,n]of Object.entries(state.instances)){add(n.scope,`Instance: ${id}`,n.owner_scope);if(n.owner_scope&&!nodes[n.owner_scope])add(n.owner_scope,'Execution scope',state.root_scope)}
 for(const [id,e]of Object.entries(state.executions)){add(e.scope,`${e.method} · ${id.slice(0,6)}`,e.owner_scope)}
 return nodes;
}
function edgesFor(nodes){
 let edges=[];
 if(mode==='binding')for(const [to,n]of Object.entries(nodes))for(const [alias,target]of Object.entries(n.bindings||{}))edges.push({from:to,to:target,label:alias});
 if(mode==='execution')for(const e of Object.values(state.executions))if(e.caller&&nodes[e.caller]&&nodes[e.instance])edges.push({from:e.caller,to:e.instance,label:e.method});
 if(mode==='scope')for(const [id,n]of Object.entries(nodes))if(n.owner&&nodes[n.owner])edges.push({from:n.owner,to:id,label:'owns'});
 const grouped=new Map();for(const e of edges){const key=e.from+'/'+e.to,old=grouped.get(key);if(old){if(!old.labels.includes(e.label))old.labels.push(e.label);}else grouped.set(key,{...e,labels:[e.label]});}return [...grouped.values()].map(e=>({...e,label:e.labels.join(', ')}));
}
function arrange(nodes,edges){
 const next=AgentGraph.layout(nodes,edges,mode==='scope'?state.root_scope:state.entry?.instance);
 for(const [id,p]of Object.entries(next))positions[mode+':'+id]=p;
}
function position(id){return manual[mode+':'+id]||positions[mode+':'+id]}
function fit(){const nodes=viewNodes(),ps=Object.keys(nodes).map(position).filter(Boolean);if(!ps.length)return;const size=extent();let x=Math.min(...ps.map(p=>p.x))-85,y=Math.min(...ps.map(p=>p.y))-85,w=Math.max(...ps.map(p=>p.x+190))-x+85,h=Math.max(...ps.map(p=>p.y+72))-y+85;const oldW=w,oldH=h;w=Math.max(w,h*size.w/size.h);h=w*size.h/size.w;x-=(w-oldW)/2;y-=(h-oldH)/2;camera={x,y,w,h};cameraReady=true;updateCamera();}
function zoom(factor,cx,cy){const r=svg.getBoundingClientRect(),ux=(cx-r.left)/r.width,uy=(cy-r.top)/r.height;camera.x+=camera.w*ux*(1-factor);camera.y+=camera.h*uy*(1-factor);camera.w*=factor;camera.h*=factor;cameraReady=true;userCamera=true;updateCamera();}
function render(){
 const nodes=viewNodes(),edges=edgesFor(nodes),ids=Object.keys(nodes);arrange(nodes,edges);$('edges').replaceChildren();$('nodes').replaceChildren();
 const rects=ids.map(position),now=Date.now(),signature=JSON.stringify([mode,ids,edges,rects]);if(signature!==routeSignature){routes=edges.map((e,i)=>position(e.from)&&position(e.to)?AgentGraph.route(position(e.from),position(e.to),rects,i):null);routeSignature=signature;}
 for(const [index,e]of edges.entries()){
  const a=position(e.from),b=position(e.to);if(!a||!b)continue;
  const path=routes[index],active=(!replayMode||timer)&&((hot[e.from+'/'+e.to]||0)>now||Object.values(state.executions).some(x=>x.caller===e.from&&x.instance===e.to&&x.state==='running'));
  const line=el('path',{d:path.d,class:'edge '+mode+(active?' hot':''),'marker-end':'url(#arrow)','data-from':e.from,'data-to':e.to});const title=el('title');title.textContent=e.from+' → '+e.to+' · '+e.label;line.append(title);$('edges').append(line);
  if(active){const dot=el('circle',{r:3.4,class:'flow-dot'}),motion=el('animateMotion',{path:path.d,dur:'.75s',repeatCount:'indefinite'});dot.append(motion);$('edges').append(dot)}
 }
 for(const id of ids){const n=nodes[id],p=position(id);let status=String(n.state||'idle').toLowerCase();if(mode!=='scope'){
  const ex=Object.values(state.executions).filter(e=>e.instance===id);if(ex.some(e=>e.state==='running'||e.state==='created'))status='running';else if(ex.length)status=ex[ex.length-1].state;
  if(n.state==='DISPOSED')status='disposed';}
 const g=el('g',{class:'node '+status,'data-id':id,transform:`translate(${p.x},${p.y})`});g.append(el('rect',{width:190,height:72,rx:10}));g.append(text(id.length>25?id.slice(0,24)+'…':id,12,22));g.append(text((n.label||'').slice(0,30),12,43,'small'));g.append(text((state.entry?.instance===id?'ENTRY · ':'')+status,12,61,'small'));$('nodes').append(g);}
 $('empty').hidden=ids.length>0;$('graphLabel').textContent=`${state.preview?'配置预览 · ':''}${state.composition||'等待配置'} · ${ids.length} 个组件 · ${edges.length} 条连线`;
 if(!cameraReady||($('autoFit').checked&&!userCamera&&ids.length>lastNodes))fit();else updateCamera();lastNodes=ids.length;
}
function observe(event){
 if(event.caller&&event.instance)hot[event.caller+'/'+event.instance]=Date.now()+4000;
 if(event.event==='execution.progress'&&event.instance===state.entry?.instance&&event.detail?.kind==='text_delta'){draft+=event.detail.text;$('result').textContent=draft;}
 if(event.topic==='model.receipt'){const u=event.detail.raw_usage;if(u&&Number.isInteger(u.total_tokens))knownUsage+=u.total_tokens;else if(event.detail.http_request_sent!==false)unknownUsage++;$('usage').textContent=`已返回用量 ${knownUsage.toLocaleString()} tokens${unknownUsage?' · '+unknownUsage+' 次用量未知':''}`;}
}
function logEvents(rows){
 const holder=$('events'),fragment=document.createDocumentFragment();
 for(const event of rows.slice(-500)){const b=document.createElement('button');b.className='event';const span=document.createElement('span');span.textContent='#'+event.seq;b.append(span,document.createTextNode(`${event.event} · ${event.instance||event.execution||''}`));b.onclick=()=>$('details').textContent=JSON.stringify(event,null,2);fragment.append(b)}
 if(rows.length>=500)holder.replaceChildren(fragment);else holder.append(fragment);
 while(holder.children.length>500)holder.firstChild.remove();holder.scrollTop=holder.scrollHeight;
}
function log(event){logEvents([event])}
function applyEvent(e){
 observe(e);
 if(e.event==='instance.spawn')state.instances[e.instance]={plugin:e.plugin,version:e.version,bindings:e.bindings,scope:e.scope,owner_scope:e.owner_scope,state:'READY'};
 if(e.event==='instance.dispose'&&state.instances[e.instance])state.instances[e.instance].state='DISPOSED';
 if(e.event==='execution.created')state.executions[e.execution]={instance:e.instance,caller:e.caller,parent:e.parent,method:e.method,scope:e.scope,owner_scope:e.owner_scope,state:'created'};
 if(e.event.startsWith('execution.')&&state.executions[e.execution]&&e.event!=='execution.progress'&&e.event!=='execution.created')state.executions[e.execution].state=e.event.split('.')[1]==='started'?'running':e.event.split('.')[1];
 log(e);render();$('count').textContent=`${e.seq} events`;
}
function resetView(){renderBenchmark(null);hot={};draft='';$('result').textContent='回答将在这里显示';$('usage').textContent='';$('count').textContent='0 events';$('details').textContent='点击节点或事件查看详情；箭头表示实际依赖或调用方向。';$('approvals').replaceChildren();knownUsage=unknownUsage=0;positions={};manual={};cameraReady=false;userCamera=false;lastNodes=0;$('events').replaceChildren();$('warning').textContent='';}
async function refreshRuns(){const runs=await api('/api/runs');const selected=runId;$('runList').replaceChildren();const placeholder=document.createElement('option');placeholder.value='';placeholder.textContent='选择已有会话';$('runList').append(placeholder);for(const r of runs){const o=document.createElement('option');o.value=r.id;o.textContent=`${liveProfiles[r.profile]||names[r.profile]||r.profile} · ${r.status} · ${r.id.slice(0,6)}`;$('runList').append(o)}if(selected)$('runList').value=selected;}
async function poll(){if(!runId||replayMode||polling)return;polling=true;try{
 const activeId=runId,version=generation;const [snapshot,stream]=await Promise.all([api(`/api/runs/${runId}`),api(`/api/runs/${runId}/events?after=${cursor}`)]);if(activeId!==runId||version!==generation||replayMode)return;
 const changed=state.run_status!==snapshot.run_status;if(Object.keys(snapshot.instances||{}).length)state=snapshot;else state={...state,run_status:snapshot.run_status};for(const e of stream.events){events.push(e);observe(e);}logEvents(stream.events);cursor=stream.cursor;if(stream.gap)$('warning').textContent='观察事件有缺口；当前快照已恢复。完整记录可从磁盘回放。';render();
 renderBenchmark(snapshot.benchmark);$('status').textContent=`${snapshot.run_status} · ${runId.slice(0,8)}`;if(snapshot.result)$('result').textContent=snapshot.benchmark?`已结算 ${snapshot.benchmark.finished}/${snapshot.benchmark.total} 题；逐题结果与轨迹见右侧进度表。`:snapshot.result.answer??JSON.stringify(snapshot.result,null,2);else if(snapshot.error)$('result').textContent=JSON.stringify(snapshot.error,null,2);else if(!draft)$('result').textContent=snapshot.run_status==='loading'?'正在装载插件，配置图保持可见…':'模型正在处理，输出将实时显示…';$('count').textContent=`${cursor} events`;
 const active=['loading','running'].includes(snapshot.run_status);$('cancel').disabled=!active;$('continue').disabled=active||!snapshot.actions?.continue;$('history').disabled=!snapshot.actions?.history;$('continue').textContent=snapshot.benchmark?'继续剩余题目':snapshot.actions?.history?'继续此会话':'再次执行';$('pauseBenchmark').hidden=!snapshot.benchmark;$('pauseBenchmark').disabled=!snapshot.actions?.pause||snapshot.benchmark?.pause_requested;
 if(snapshot.run_status==='running'){
  const approvals=await api(`/api/runs/${runId}/approvals`);$('approvals').replaceChildren();for(const item of approvals){const div=document.createElement('div');div.className='approval';div.textContent=`工具审批：${item.tool}`;if(item.arguments){const pre=document.createElement('pre');pre.textContent=JSON.stringify(item.arguments,null,2);div.append(pre)}for(const [label,allow]of [['允许',true],['拒绝',false]]){const b=document.createElement('button');b.textContent=label;b.onclick=async()=>{try{await api(`/api/runs/${runId}/approvals`,{id:item.id,allow});div.remove();}catch(e){showError(e)}};div.append(b)}$('approvals').append(div);}}
 else $('approvals').replaceChildren();
 $('connection').textContent='已连接 · localhost';if(changed)await refreshRuns();
 }catch(e){showError(e);$('connection').textContent='连接中断'}finally{polling=false}}
async function preview(){const id=++previewVersion;generation++;runId=null;pause();replayMode=false;cursor=0;resetView();$('cancel').disabled=true;$('continue').disabled=true;$('history').disabled=true;$('status').textContent='正在生成配置预览…';try{const data=await api('/api/preview',{profile:$('profile').value,options:options()});if(id!==previewVersion)return;state=data;cameraReady=false;render();$('status').textContent='配置预览 · 尚未发送请求';$('empty').textContent='此自定义配置将在装载后显示';}catch(e){showError(e)}}
async function taskPayload(){
 if($('taskSource').value==='searchqa')return '';
 const files=[...$('taskImages').files];if(!files.length)return $('task').value;
 if(files.length>4||files.some(f=>f.size>4*1024*1024||!['image/png','image/jpeg','image/webp'].includes(f.type)))throw Error('图片仅支持 PNG/JPEG/WebP，最多 4 张，每张 4 MiB');
 const c=await api('/api/model');if(!c.supports_images)throw Error('当前模型未声明支持图片；请先核对服务能力并修改模型设置。尚未发送模型请求。');
 const parts=await Promise.all(files.map(f=>new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve({type:'image_url',image_url:{url:reader.result}});reader.onerror=reject;reader.readAsDataURL(f)})));
 return [{type:'text',text:$('task').value},...parts];
}
$('taskImages').onchange=()=>$('imageStatus').textContent=[...$('taskImages').files].map(f=>f.name).join('、')||'最多 4 张，每张 4 MiB；需在模型设置中声明支持图片。';
async function start(){previewVersion++;pause();replayMode=false;generation++;runId=null;events=[];cursor=0;hot={};draft='';knownUsage=unknownUsage=0;resetView();$('status').textContent='正在创建会话…';$('result').textContent='正在装载插件…';$('usage').textContent='';render();$('run').disabled=true;try{const out=await api('/api/runs',{profile:$('profile').value,task:await taskPayload(),options:options()});runId=out.id;await refreshRuns();await poll();}catch(e){showError(e);$('status').textContent='未启动，请检查提示';}finally{$('run').disabled=false}}
async function replay(){if(!runId)runId=$('runList').value;if(!runId)return;pause();generation++;const data=await api('/api/replay/'+runId);events=data.events;state={composition:data.snapshot.composition,entry:data.snapshot.entry,root_scope:data.snapshot.root_scope,instances:{},executions:{}};replayMode=true;replayCursor=0;resetView();render();$('status').textContent='磁盘回放 · 0 / '+events.length;if(data.gap)$('warning').textContent='磁盘事件存在缺口';$('result').textContent=JSON.stringify(data.result,null,2);}
function step(){if(!replayMode||replayCursor>=events.length){pause();return;}applyEvent(events[replayCursor++]);$('status').textContent=`磁盘回放 · ${replayCursor} / ${events.length}`;}
function pause(){if(timer)clearInterval(timer);timer=null;if(replayMode){hot={};render();}}
$('run').onclick=start;$('pauseBenchmark').onclick=()=>api(`/api/runs/${runId}/pause`,{}).then(poll).catch(showError);$('cancel').onclick=()=>api(`/api/runs/${runId}/cancel`,{}).catch(showError);
$('continue').onclick=async()=>{try{const task=await taskPayload();pause();replayMode=false;draft='';await api(`/api/runs/${runId}/continue`,{task});await poll()}catch(e){showError(e)}};
$('history').onclick=()=>api(`/api/runs/${runId}/history`).then(data=>$('details').textContent=JSON.stringify(data,null,2)).catch(showError);
$('runList').onchange=()=>{previewVersion++;generation++;runId=$('runList').value;cursor=0;events=[];replayMode=false;resetView();poll()};
$('live').onclick=()=>{if(!runId)runId=$('runList').value;if(!runId)return;pause();replayMode=false;cursor=0;events=[];resetView();poll()};$('replay').onclick=()=>replay().catch(showError);$('reset').onclick=()=>replay().catch(showError);
$('play').onclick=async()=>{try{if(!replayMode)await replay();pause();timer=setInterval(step,500/Number($('speed').value));}catch(e){showError(e)}};$('pause').onclick=pause;$('step').onclick=async()=>{try{pause();if(!replayMode)await replay();step();}catch(e){showError(e)}};
$('tabs').onclick=e=>{const b=e.target.closest('[data-mode]');if(!b)return;mode=b.dataset.mode;for(const n of $('tabs').children)n.classList.toggle('active',n===b);cameraReady=false;render()};
$('fit').onclick=()=>{userCamera=false;fit()};$('layout').onclick=()=>{positions={};manual={};userCamera=false;cameraReady=false;render()};
for(const [id,factor]of [['zoomIn',.8],['zoomOut',1.25]])$(id).onclick=()=>{const r=svg.getBoundingClientRect();zoom(factor,r.left+r.width/2,r.top+r.height/2)};
svg.addEventListener('wheel',e=>{e.preventDefault();zoom(e.deltaY>0?1.13:.885,e.clientX,e.clientY)},{passive:false});
svg.addEventListener('pointerdown',e=>{if(e.button!==0)return;const id=e.target.closest('.node')?.dataset.id;const pos=id?position(id):null;pointer={id,pointerId:e.pointerId,x:e.clientX,y:e.clientY,camera:{...camera},pos:pos?{...pos}:null,moved:false};svg.setPointerCapture(e.pointerId)});
svg.addEventListener('pointermove',e=>{if(!pointer||pointer.pointerId!==e.pointerId)return;const dx=e.clientX-pointer.x,dy=e.clientY-pointer.y,r=extent();if(Math.hypot(dx,dy)>3)pointer.moved=true;if(!pointer.moved)return;userCamera=true;if(pointer.id){manual[mode+':'+pointer.id]={x:pointer.pos.x+dx*camera.w/r.w,y:pointer.pos.y+dy*camera.h/r.h};render()}else{camera.x=pointer.camera.x-dx*camera.w/r.w;camera.y=pointer.camera.y-dy*camera.h/r.h;cameraReady=true;updateCamera()}});
function pointerEnd(e){if(!pointer)return;if(pointer.id&&!pointer.moved)$('details').textContent=JSON.stringify({id:pointer.id,...viewNodes()[pointer.id]},null,2);if(svg.hasPointerCapture(e.pointerId))svg.releasePointerCapture(e.pointerId);pointer=null}svg.addEventListener('pointerup',pointerEnd);svg.addEventListener('pointercancel',pointerEnd);
window.addEventListener('resize',()=>{if(!cameraReady)return;if(!userCamera){fit();return;}const r=extent();camera.h=camera.w*r.h/r.w;updateCamera()});
async function showModel(){try{const c=await api('/api/model');$('apiBase').value=c.base_url;$('apiModel').value=c.model;$('thinking').value=c.thinking;$('effort').value=c.reasoning_effort;$('apiTimeout').value=c.timeout;$('outputLimit').value=c.max_tokens??'';$('supportsImages').checked=!!c.supports_images;$('apiKey').value='';$('modelStatus').textContent=`${c.model} · ${c.key_configured?'密钥已配置（尚未验证连接）':'尚未配置密钥'}`;return c}catch(e){showError(e)}}
$('modelSettings').onclick=async()=>{await showModel();$('modelMessage').textContent='';$('modelDialog').showModal()};$('closeModel').onclick=()=>$('modelDialog').close();
$('modelForm').onsubmit=async e=>{e.preventDefault();try{await api('/api/model',{base_url:$('apiBase').value,model:$('apiModel').value,api_key:$('apiKey').value,thinking:$('thinking').value,reasoning_effort:$('effort').value,max_tokens:$('outputLimit').value?Number($('outputLimit').value):null,timeout:Number($('apiTimeout').value),stream:true,supports_images:$('supportsImages').checked});$('apiKey').value='';$('modelMessage').textContent='已保存。新对话使用此配置；已有会话保持原模型。';await showModel();}catch(e){$('modelMessage').textContent=e.message}};
$('profile').onchange=()=>{updateInputs();preview()};for(const input of document.querySelectorAll('[name=tool],#workspace,#maxSteps,#requestLimit'))input.onchange=()=>{if(liveProfiles[$('profile').value])preview()};
async function initialize(){try{const p=await api('/api/profiles');liveProfiles=p.live_profiles||{live:'Basic · 工具循环'};for(const name of p.profiles){const o=document.createElement('option');o.textContent=liveProfiles[name]||names[name]||(name.startsWith('viz-')?'离线示例 · '+name.slice(4):name);o.value=name;$('profile').append(o)}$('connection').textContent='已连接 · localhost';await showModel();await loadDatasets();updateInputs();await Promise.all([refreshRuns(),preview()]);}catch(e){showError(e)}}

function updateInputs(){
 const live=!!liveProfiles[$('profile').value];if(!live)$('taskSource').value='chat';const bench=$('taskSource').value==='searchqa';
 $('imageInput').hidden=bench||!live;$('benchmarkOptions').hidden=!bench;$('task').disabled=bench;$('task').hidden=bench;document.querySelector('label[for=task]').hidden=bench;$('runtimeOptions').hidden=!live;$('nativeTools').hidden=bench;
 $('run').textContent=bench?'开始评测':live?'开始新任务':'运行离线示例';
 $('profileHint').textContent=live?'使用模型设置中的真实 API。切换 Agent 配置会改变实际插件和工作流。':'固定离线响应，仅用于接口测试，不是模型评测。';
 if(bench)$('profileHint').textContent+=' SearchQA 推荐 Basic 工具循环；也支持规划、记忆和子代理。';
 $('taskSource').disabled=!live;
}
function datasetInfo(){const d=datasets.find(x=>x.id===$('dataset').value);$('datasetInfo').textContent=d?`${d.split} · ${d.count} 题 · ${d.normalization||'旧格式'} · SHA256 ${d.sha256.slice(0,12)}`:'请先导入 SearchQA JSON / JSONL';if(d){$('taskCount').max=d.count;$('taskStart').max=d.count}}
async function loadDatasets(selected){datasets=await api('/api/datasets');datasets.sort((a,b)=>Number(b.normalization==='searchqa.public-input.v2')-Number(a.normalization==='searchqa.public-input.v2'));$('dataset').replaceChildren();for(const d of datasets){const o=document.createElement('option');o.value=d.id;o.textContent=`${d.name} · ${d.split} (${d.count})${d.normalization==='searchqa.public-input.v2'?' · 输入 v2':' · 旧输入'}`;$('dataset').append(o)}if(selected)$('dataset').value=selected;else if(datasets.some(d=>d.split==='validation'))$('dataset').value=datasets.find(d=>d.split==='validation').id;datasetInfo();}
function renderBenchmark(b){
 $('benchmarkPanel').hidden=!b;if(!b)return;
 $('benchmarkStatus').textContent=`${b.status==='completed_with_errors'?'已结束 · 含错误':b.status} · ${b.finished||0} / ${b.total}`;
 $('benchmarkProgress').max=b.total||1;$('benchmarkProgress').value=b.finished||0;
 const pct=x=>x==null?'—':(100*x).toFixed(1)+'%';
 const correct=b.correct??(b.tasks||[]).reduce((n,r)=>n+(r.em||0),0), all=b.finished?correct/b.finished:null;
 $('benchmarkStats').textContent=`EM ${pct(b.em)} / F1 ${pct(b.f1)}（${b.scored||0} 题已评分）· 含错误题正确率 ${pct(all)}（${correct}/${b.finished||0} 已结束题） · 接口/执行错误 ${b.errors||0} · ${b.tokens||0} tokens · ${b.requests||0} 次请求 · 未知用量 ${b.unknown_requests||0}${b.current?' · 当前第 '+b.current.index+' 题':''}${b.pause_requested?' · 等待当前题结束后暂停':''}`;
 $('benchmarkRows').replaceChildren();
 for(const r of [...(b.tasks||[]),...(b.current?[{...b.current,status:'running'}]:[])]){
  const tr=document.createElement('tr');for(const value of [`${r.index} · ${r.id}`,r.status+(r.terminal_reason?' · '+r.terminal_reason:''),r.em==null?'—':`${pct(r.em)} / ${pct(r.f1)}`,r.tokens==null?'—':`${r.tokens} / ${r.requests}`,r.elapsed_seconds==null?'—':r.elapsed_seconds+'s',r.answer??r.error?.cause?.code??r.error?.code??'处理中']){const td=document.createElement('td');td.textContent=value;tr.append(td)}
  const btn=document.createElement('button');btn.textContent='轨迹';btn.onclick=()=>api(`/api/runs/${runId}/history?session=${encodeURIComponent(r.session)}`).then(v=>$('details').textContent=JSON.stringify({task:r,history:v},null,2)).catch(showError);tr.firstChild.append(document.createElement('br'),btn);$('benchmarkRows').append(tr);
 }
}
$('taskSource').onchange=()=>{if($('taskSource').value==='searchqa'&&!['live','live-plan-execute','live-memory','live-subagent','live-multi-agent','live-dynamic-spawn'].includes($('profile').value))$('profile').value='live';updateInputs();preview()};
$('dataset').onchange=()=>{datasetInfo();preview()};for(const id of ['taskStart','taskCount'])$(id).onchange=preview;
$('importDataset').onclick=async()=>{const f=$('datasetFile').files[0];if(!f){$('importStatus').textContent='请选择 JSON / JSONL 文件';return}try{const r=await fetch(`/api/datasets/import?name=${encodeURIComponent(f.name)}&split=${encodeURIComponent($('datasetSplit').value)}`,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:f});if(!r.ok)throw Error(await r.text());const d=await r.json();await loadDatasets(d.id);$('importStatus').textContent=`已导入 ${d.count} 题`;await preview()}catch(e){$('importStatus').textContent=e.message}};

initialize();setInterval(poll,800);
// Read-only diagnostic snapshot used by UI integration checks, never an execution API.
window.agentlabDiagnostics=()=>({runId,mode,replayMode,replayCursor,cursor,camera:{...camera},manual:structuredClone(manual),nodeCount:$('nodes').children.length,state:structuredClone(state)});
