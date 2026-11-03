'use strict';
// Layout is derived only from generic edges/entry. No plugin-name rules.
globalThis.AgentGraph={
 layout(nodes,edges,entry){
  const ids=Object.keys(nodes),ranks={},queue=[];
  if(nodes[entry])queue.push([entry,0]);
  for(const id of ids)if(!edges.some(e=>e.to===id)&&id!==entry)queue.push([id,0]);
  if(!queue.length&&ids.length)queue.push([ids[0],0]);
  while(queue.length){const [id,r]=queue.shift();if(ranks[id]!==undefined)continue;ranks[id]=r;for(const e of edges)if(e.from===id&&nodes[e.to]&&ranks[e.to]===undefined)queue.push([e.to,r+1]);}
  const groups={};for(const id of ids)(groups[ranks[id]??0]??=[]).push(id);
  const positions={};let col=0;
  for(const rank of Object.keys(groups).map(Number).sort((a,b)=>a-b)){
   let group=groups[rank];group.sort((a,b)=>{const bary=id=>{const ys=edges.filter(e=>e.to===id&&positions[e.from]).map(e=>positions[e.from].y);return ys.length?ys.reduce((s,y)=>s+y,0)/ys.length:0};return bary(a)-bary(b)});
   const rows=Math.min(7,group.length);
   for(let i=0;i<group.length;i++)positions[group[i]]={x:40+(col+Math.floor(i/7))*320,y:60+(i%7)*128+(7-rows)*32};
   col+=Math.ceil(group.length/7);
  }return positions;
 },
 route(a,b,rects,index=0){
  const s={x:a.x+190,y:a.y+36},t={x:b.x,y:b.y+36},sx=s.x+18+(index%5)*4,tx=t.x-18-(index%5)*4;
  const ys=[...new Set([s.y,t.y,...rects.flatMap(r=>[r.y-20-(index%6)*4,r.y+92+(index%6)*4])])];
  const candidates=[ [s,{x:(sx+tx)/2,y:s.y},{x:(sx+tx)/2,y:t.y},t] ];
  for(const y of ys)candidates.push([s,{x:sx,y:s.y},{x:sx,y},{x:tx,y},{x:tx,y:t.y},t]);
  const hits=(p,q,r)=>p.x===q.x ? p.x>r.x&&p.x<r.x+190&&Math.max(p.y,q.y)>r.y&&Math.min(p.y,q.y)<r.y+72 : p.y>r.y&&p.y<r.y+72&&Math.max(p.x,q.x)>r.x&&Math.min(p.x,q.x)<r.x+190;
  const cost=ps=>ps.slice(1).reduce((sum,q,i)=>sum+Math.abs(q.x-ps[i].x)+Math.abs(q.y-ps[i].y)+rects.filter(r=>hits(ps[i],q,r)).length*100000,0);
  let points=candidates[0],best=Infinity;
  for(const candidate of candidates){const score=cost(candidate);if(score<best){best=score;points=candidate;}}
  return {points,d:points.map((p,i)=>(i?'L':'M')+p.x+','+p.y).join(' ')};
 }
};
