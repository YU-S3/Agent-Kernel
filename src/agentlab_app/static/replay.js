/* Presentation batches only; every original event remains in order. */
'use strict';
window.AgentReplay=(()=>{
 const delta=e=>e?.event==='execution.progress'&&['text_delta','reasoning_delta','tool_delta'].includes(e.detail?.kind);
 function end(rows,start,raw=false){
  if(start>=rows.length)return rows.length;
  let next=start+1;
  if(!raw&&delta(rows[start]))while(next<rows.length&&next-start<512&&delta(rows[next]))next++;
  return next;
 }
 function groups(rows,raw=false){
  const out=[];
  for(let i=0;i<rows.length;){const next=end(rows,i,raw);out.push(rows.slice(i,next));i=next;}
  return out;
 }
 function label(rows){
  const e=rows[0];
  if(delta(e))return `流式片段 ×${rows.length}（非独立请求）`;
  if(e.topic==='model.request')return `模型请求开始 · ${e.instance}`;
  if(e.topic==='model.response')return `完整模型响应 · ${e.instance}`;
  const cause=e.error?.cause?.code||e.error?.code;
  return `${e.event} · ${e.instance||e.execution||''}${cause?' · '+cause:''}`;
 }
 // Providers already emit the assembled model.response. Keep transport chunks
 // in the original timeline for playback state/debugging, not the normal log.
 const visible=(rows,raw=false)=>raw?rows:rows.filter(e=>e.event!=='execution.progress');
 return {end,groups,label,visible};
})();
