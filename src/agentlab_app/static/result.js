/* Conversation presentation only. Original messages/receipts are never edited. */
'use strict';
window.AgentResult=(()=>{
 function unwrap(value){
  let row=value;
  for(let n=0;n<8&&row&&typeof row==='object'&&!Array.isArray(row);n++){
   if(typeof row.answer==='string')return row;
   if(row.result!==undefined&&row.result!==null){row=row.result;continue;}
   break;
  }
  return row;
 }
 function answer(value){
  const row=unwrap(value);
  if(typeof row==='string')return row;
  if(row==null)return '';
  if(typeof row!=='object')return String(row);
  if(typeof row.answer==='string')return row.answer||'本次没有返回可用回答。';
  if(typeof row.content==='string')return row.content;
  if(row.error){const e=row.error;return `运行未完成：${e.message||e.cause?.code||e.code||'未知错误'}`;}
  if(typeof row.finished==='number'&&typeof row.total==='number')return `已结算 ${row.finished}/${row.total} 题；逐题结果与轨迹见右侧进度表。`;
  return '已返回结构化结果，请通过“运行详情”查看。';
 }
 function mathToken(src,block){
  const pairs=block?[['$$','$$'],['\\[','\\]']]:[['\\(','\\)'],['$','$']];
  for(const [open,close]of pairs){
   if(!src.startsWith(open)||(open==='$'&&src.startsWith('$$')))continue;
   if(open==='$'&&/\s/.test(src[1]||''))continue;
   let end=open.length;
   while((end=src.indexOf(close,end))>=0){
    let slashes=0;for(let i=end-1;i>=0&&src[i]==='\\';i--)slashes++;
    if(slashes%2){end+=close.length;continue;}
    const text=src.slice(open.length,end);
    if(!text.trim()||(!block&&text.includes('\n'))||(open==='$'&&(/\s/.test(text.at(-1))||/\d/.test(src[end+1]||''))))return;
    return {raw:src.slice(0,end+close.length),text,display:block};
   }
  }
 }
 function render(node,text){
  text=String(text??'');
  if(node.dataset.renderedText===text)return;
  node.dataset.renderedText=text;
  if(!window.marked||!window.DOMPurify||!window.katex){node.textContent=text;return;}
  const formulas=[],nonce=crypto.randomUUID();
  const parser=new marked.Marked({gfm:true,breaks:true});
  for(const block of [true,false]){
   const name=block?'displayMath':'inlineMath';
   parser.use({extensions:[{name,level:block?'block':'inline',
    start(src){const indexes=(block?['$$','\\[']:['$','\\(']).map(d=>src.indexOf(d)).filter(i=>i>=0);return indexes.length?Math.min(...indexes):undefined;},
    tokenizer(src){const token=mathToken(src,block);if(token)return {type:name,...token};},
    renderer(token){const id=formulas.push(token)-1;return `<span data-agent-math="${nonce}:${id}"></span>`+(block?'\n':'');}
   }]});
  }
  try{
   const fragment=DOMPurify.sanitize(parser.parse(text),{RETURN_DOM_FRAGMENT:true,USE_PROFILES:{html:true},
    FORBID_TAGS:['img','style','form','input','button','textarea','select'],FORBID_ATTR:['style','id','name'],ALLOW_DATA_ATTR:false,ADD_ATTR:['data-agent-math']});
   // Render only placeholders created by this parse. Prevent model HTML from
   // choosing a formula index by assigning a per-render nonce first.
   for(const span of fragment.querySelectorAll('[data-agent-math]')){
    const ref=span.getAttribute('data-agent-math');
    const token=ref.startsWith(nonce+':')?formulas[Number(ref.slice(nonce.length+1))]:null;
    span.removeAttribute('data-agent-math');if(!token)continue;
    katex.render(token.text,span,{displayMode:token.display,throwOnError:false,trust:false,
     strict:'ignore',maxExpand:1000,maxSize:20,macros:{}});
   }
   for(const link of fragment.querySelectorAll('a')){link.target='_blank';link.rel='noopener noreferrer';}
   node.replaceChildren(fragment);
  }catch(_){node.textContent=text;}
 }
 function history(node,data){
  const messages=(data?.messages||[]).filter(m=>m.role!=='system');
  node.replaceChildren();delete node.dataset.renderedText;
  if(!messages.length){render(node,'这个会话还没有消息。');return;}
  for(const m of messages){
   const text=Array.isArray(m.content)?m.content.map(p=>p.type==='text'?p.text:p.type==='image_url'?'[图片附件]':'').join('\n'):m.content||'';
   if(!text&&!m.tool_calls?.length)continue;
   const technical=m.role==='tool'||!text;
   const item=document.createElement(technical?'details':'div');item.className='chat-message '+m.role;
   const label=document.createElement(technical?'summary':'div');label.className='chat-role';
   label.textContent=technical?(m.role==='tool'?'工具结果':'工具调用'):m.role==='user'?'你':'助手';item.append(label);
   const body=document.createElement('div');body.className='message-body';
   if(technical){const pre=document.createElement('pre');pre.textContent=text||JSON.stringify(m.tool_calls,null,2);body.append(pre);}
   else render(body,text);
   item.append(body);node.append(item);
  }
 }
 return {answer,unwrap,render,history};
})();
