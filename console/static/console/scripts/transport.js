/* Authenticated requests, in-flight GET sharing and bounded page refresh scheduling. */
var {api, pageRead} = (function(){


const apiReads=new Map();
async function api(p,o={}){
  const started=performance.now();
  let authHeaders={};
  try{const t=sessionStorage.getItem('herdr_hub_token')||'';if(t)authHeaders={'Authorization':'Bearer '+t};}catch(e){}
  const mergedHeaders={'Content-Type':'application/json','X-HAFlow-Console-Request':'1',...authHeaders,...(o.headers||{})};
  const key=JSON.stringify([p,mergedHeaders]);
  const share=(o.method||'GET').toUpperCase()==='GET'&&!o.signal;
  if(share&&apiReads.has(key))return apiReads.get(key);
  const request=(async()=>{
  const r=await fetch(p,{...o,headers:mergedHeaders});
  const d=await r.json();
  if(!r.ok||d.ok===false)throw new Error(d.error||('HTTP '+r.status));
  const latency=document.getElementById('bottomPollLatency');
  if(latency){
    const ms=Math.max(1,Math.round(performance.now()-started));
    latency.textContent=ms>=1000?(ms/1000).toFixed(1)+'s':ms+'ms';
  }
  return d.data??d;
  })();
  if(share)apiReads.set(key,request);
  try{return await request}finally{if(share&&apiReads.get(key)===request)apiReads.delete(key)}
}
const pageReads=new Map();
function pageRead(kind,path){
  let identity='';try{identity=sessionStorage.getItem('herdr_hub_token')||''}catch(e){}
  const key=JSON.stringify([kind,identity]);
  const previous=pageReads.get(key);
  if(previous){
    previous.nextPath=path;
    if(previous.path===path)return previous.promise;
    return previous.promise.catch(()=>null).then(()=>previous.nextPath===path?pageRead(kind,path):null);
  }
  const entry={path,nextPath:path,promise:null};
  entry.promise=api(path).finally(()=>{if(pageReads.get(key)===entry)pageReads.delete(key)});
  pageReads.set(key,entry);
  return entry.promise;
}
return {api, pageRead};
})();
