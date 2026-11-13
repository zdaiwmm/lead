/** Explicit real-model C22 driver; not part of automatic tests. */
import assert from 'node:assert/strict';
import {mkdirSync,readFileSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {Store} from '../services/local-executor/src/store.ts';
import {capabilities} from '../services/local-executor/src/capabilities.ts';
import {Daemon} from '../services/local-executor/src/daemon.ts';
import {post} from '../services/local-executor/src/transport.ts';

const descriptor=JSON.parse(readFileSync(process.env.LEAD_C22_DESCRIPTOR||'', 'utf8'));
const folder=resolve(process.env.LEAD_C22_STATE||'');
const sites=process.env.LEAD_C22_ONLY_SITE==='b'?['b']:['a','b'];
assert.ok(process.env.LEAD_C22_STATE, 'explicit_private_state_required');
mkdirSync(folder,{recursive:true,mode:0o700});
async function fixture(path,body){
  const response=await fetch(descriptor.url+'/fixture/'+path,{
    method:body===undefined?'GET':'POST',headers:{'content-type':'application/json','x-fixture-token':descriptor.fixture_token},
    ...(body===undefined?{}:{body:JSON.stringify(body)})});
  if(!response.ok)throw new Error('fixture_http_'+response.status+':'+await response.text());
  return await response.json();
}
const health=await fixture('health');
assert.equal(health.source_sha,descriptor.source_sha);
const store=new Store(join(folder,'connector'));
const caps=await capabilities();
assert.equal(caps.ready,true,'本人Codex需要ChatGPT登录并提供Sol/medium');
if(!store.data.binding){
  const pairing=await fixture('pairing',{});
  const paired=await post(descriptor.url,'/executor/v1/pair',{
    code:pairing.code,name:'Lead C22 original-task fixture',platform:process.platform,capabilities:caps});
  store.bind({url:descriptor.url,token:paired.token,epoch:paired.epoch,device_id:paired.device_id,name:'C22 fixture'});
}
const daemon=new Daemon(store,undefined,undefined,caps);
let daemonError;
const running=daemon.run().catch(error=>{daemonError=error;});
let previous='';
try{
  const before=await fixture('c22/status');
  if(!before.cases[sites[0]])console.log(JSON.stringify(await fixture('c22/start',{site:sites[0]})));
  while(true){
    if(daemonError)throw daemonError;
    const state=await fixture('c22/status');
    writeFileSync(join(folder,'status.json'),JSON.stringify(state,null,2),{mode:0o600});
    const progress={cases:Object.fromEntries(Object.entries(state.cases).map(([name,value])=>[name,{
      completed:value.completed,active_stage:value.active_stage?.stage,first_failure:value.first_failure,
      stages:value.stages?.map(stage=>({stage:stage.stage,outcome:stage.outcome,seconds:stage.seconds,source_post_delta:stage.source_post_delta}))}])),
      requests:state.requests.map(row=>({id:row.id,state:row.state,thread_id:row.thread_id,turn_id:row.turn_id,usage:row.usage})),failure:state.failure};
    const serialized=JSON.stringify(progress);
    if(serialized!==previous){console.log(serialized);previous=serialized;}
    if(state.failure){
      // Business proof may precede the final model message by a few seconds.
      // Wait for that same native request, then re-enter cached original work.
      if(state.failure==='native_codex_request_not_completed:login'){
        if(state.requests.some(row=>['failed','interrupted','execution_unknown','canceled'].includes(row.state)))
          throw new Error('original_native_request_did_not_complete:'+state.failure);
        if(state.requests.length&&state.requests.every(row=>row.state==='completed')){
          const site=Object.entries(state.cases).find(([,value])=>value.first_failure===state.failure)?.[0];
          console.log(JSON.stringify(await fixture('c22/resume',{site})));
        }
        await new Promise(r=>setTimeout(r,1000));
        continue;
      }
      throw new Error('first_failure_original_state_retained:'+state.failure);
    }
    if(sites.includes('a')&&state.cases.a?.completed&&!state.cases.b){
      console.log(JSON.stringify(await fixture('c22/start',{site:'b'})));
    }
    if(sites.every(site=>state.cases[site]?.completed)){
      writeFileSync(join(folder,'completed.json'),JSON.stringify(state,null,2),{mode:0o600});
      break;
    }
    await new Promise(r=>setTimeout(r,3000));
  }
}finally{
  await daemon.close();
  await running;
}
