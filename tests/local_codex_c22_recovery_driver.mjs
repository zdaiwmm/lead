/** Real daemon for an independently coordinated original Task; never starts a Case. */
import assert from 'node:assert/strict';
import {mkdirSync,readFileSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {Store} from '../services/local-executor/src/store.ts';
import {capabilities} from '../services/local-executor/src/capabilities.ts';
import {Daemon} from '../services/local-executor/src/daemon.ts';

const descriptor=JSON.parse(readFileSync(process.env.LEAD_C22_DESCRIPTOR||'', 'utf8'));
assert.ok(process.env.LEAD_C22_STATE&&process.env.LEAD_C22_RECOVERY_TASK,'explicit_original_task_and_state_required');
const folder=resolve(process.env.LEAD_C22_STATE);
mkdirSync(folder,{recursive:true,mode:0o700});
const task=process.env.LEAD_C22_RECOVERY_TASK;
const store=new Store(join(folder,'connector'));
assert.ok(store.data.binding,'reuse_original_paired_device');
assert.equal(store.data.binding.url,descriptor.url,'reuse_original_api');
const caps=await capabilities();
assert.equal(caps.ready,true,'本人Codex需要ChatGPT登录并提供Sol/medium');
async function status(){
 const response=await fetch(descriptor.url+'/fixture/c22/status',{headers:{'x-fixture-token':descriptor.fixture_token}});
 assert.ok(response.ok,'original_fixture_status_unavailable');return await response.json();
}
const original=await status();
const terminal=new Set(['completed','failed','interrupted','execution_unknown','canceled']);
const before=new Set(original.requests.filter(row=>row.task_id!==task||terminal.has(row.state)).map(row=>row.id));
const daemon=new Daemon(store,undefined,undefined,caps);
let daemonError;
const running=daemon.run().catch(error=>{daemonError=error;});
let previous='';
try{
 for(;;){
  if(daemonError)throw daemonError;
  const current=await status();
  const fresh=current.requests.filter(row=>row.task_id===task&&!before.has(row.id));
  const progress=fresh.map(({id,task_id,state,thread_id,turn_id,usage,result})=>({id,task_id,state,thread_id,turn_id,usage,result}));
  writeFileSync(join(folder,'recovery-request-status.json'),JSON.stringify({originalTask:task,requests:progress},null,2),{mode:0o600});
  const serialized=JSON.stringify(progress);
  if(serialized!==previous){console.log(serialized);previous=serialized;}
  if(fresh.length){
   assert.equal(fresh.length,1,'one_explicit_recovery_request');
   if(['completed','failed','interrupted','execution_unknown','canceled'].includes(fresh[0].state)){
    assert.equal(fresh[0].state,'completed','original_recovery_native_request_failed');break;
   }
  }
  await new Promise(resolve=>setTimeout(resolve,1000));
 }
}finally{
 await daemon.close();await running;
}
