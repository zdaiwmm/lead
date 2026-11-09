"""Exercise the shipped SDK queue with a delayed first receipt, without a browser."""

import subprocess
from pathlib import Path


def test_early_attribution_waits_for_first_pageview_and_duplicate_load_is_ignored():
    sdk = Path("services/api/lead_app/pixel.js").resolve()
    script = r"""
    const vm = require('node:vm'), fs = require('node:fs'), assert = require('node:assert/strict');
    const crypto = require('node:crypto').webcrypto;
    const source = fs.readFileSync(process.argv[1], 'utf8');
    const data = new Map(), storage = {getItem:k=>data.get(k),setItem:(k,v)=>data.set(k,v)};
    let release, resolved=false, posts=0;
    const receipt = new Promise(resolve => release=resolve);
    const early = new Promise((resolve,reject) => {
      global.queued = {args:['attribution'], resolve:v=>{resolved=true;resolve(v)},reject};
    });
    const context = {URL,crypto,console,setInterval:()=>{},addEventListener:()=>{},
      localStorage:storage,sessionStorage:storage,location:{href:'https://site.example/?lr=x_fixture'},
      history:{pushState(){},replaceState(){}},
      document:{currentScript:{src:'https://collect.example/lead.js',dataset:{connection:'c1',key:'k'}},hidden:false,hasFocus:()=>true},
      fetch:async (_url,options)=>{posts++;const body=JSON.parse(options.body);
        assert.equal(body.event_name,'PageView');assert.equal(body.ref,'x_fixture');
        await receipt;return {ok:true,json:async()=>({attribution:{connection_id:'c1',touchpoint_id:'touch-1'},engagement_seconds:10})}}};
    context.window=context;context.leadq=()=>{};context.leadq.queue=[global.queued];
    vm.createContext(context);vm.runInContext(source,context);
    assert.equal(resolved,false);
    vm.runInContext(source,context);
    release();
    early.then(value=>{assert.equal(value.touchpoint_id,'touch-1');assert.equal(posts,1)})
      .catch(e=>{console.error(e);process.exitCode=1});
    """
    result = subprocess.run(
        ["node", "-e", script, str(sdk)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


def run_session_sdk(scenario):
    sdk = Path("services/api/lead_app/pixel.js").resolve()
    harness = r"""
    const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
    const makeStore=()=>{const values=new Map();return {getItem:k=>values.get(k),setItem:(k,v)=>values.set(k,v),values}};
    const persistent=makeStore(), temporary=makeStore(), events=[], intervals=[];
    persistent.setItem('lead:c1:did','live-anonymous');temporary.setItem('lead:c1:user','live-user');
    let state='active';
    const ctx={URL,crypto:require('node:crypto').webcrypto,console,
      localStorage:persistent,sessionStorage:temporary,
      setInterval:(fn,ms)=>intervals.push({fn,ms}),addEventListener:()=>{},
      location:{href:'https://site.example/?lead_test=lt_session&lr=tg_example'},
      history:{state:null,pushState(){},replaceState(_s,_t,url){ctx.location.href=url}},
      document:{currentScript:{src:'https://collect.example/lead.js',dataset:{connection:'c1',key:'k'}},hidden:false,hasFocus:()=>true},
      fetch:async(url,options)=>{
        const body=JSON.parse(options.body);
        if(url.endsWith('/test')){if(body.action==='end')state='ended';return {ok:true,json:async()=>({id:'test-1',state,active:state==='active',expires_at:'2099-01-01T00:00:00Z'})}}
        events.push(body);return {ok:true,json:async()=>({ok:true,attribution:{connection_id:'c1',touchpoint_id:null,test_code:body.test_code},engagement_seconds:10})};
      }};
    ctx.window=ctx;vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
    (async()=>{
    """
    script = (
        harness + scenario + "\n})().catch(e=>{console.error(e);process.exitCode=1});"
    )
    result = subprocess.run(
        ["node", "-e", script, str(sdk)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_session_sdk_uses_fixed_events_and_cleans_isolated_identity():
    run_session_sdk(r"""
      await ctx.leadq('identify','test-user');
      await ctx.leadq('track','CompleteRegistration',{}, {event_id:'signup:test-user'});
      await ctx.leadq('track','AddToCart',{content_id:'lamp',quantity:1});
      const source=await ctx.leadq('attribution');
      assert.equal(source.test_code,'lt_session');
      assert(events.every(e=>e.test_code==='lt_session'));
      assert(events.every(e=>e.anonymous_id!=='live-anonymous'));
      const saved=JSON.stringify(source);
      await ctx.leadq('endTest');
      assert(!ctx.location.href.includes('lead_test'));
      assert.equal(temporary.getItem('lead:c1:test'),'');
      assert.equal(temporary.getItem('lead:c1:test-identity'),'');
      assert.equal(temporary.getItem('lead:c1:test-ref'),'');
      await ctx.leadq('track','Lead',{});
      assert.equal(events.at(-1).test_code,null);
      assert.equal(events.at(-1).user_id,'live-user');
      assert.equal(events.at(-1).anonymous_id,'live-anonymous');
      assert.equal(JSON.parse(saved).test_code,'lt_session');
    """)


def test_sdk_detects_remote_end_without_emitting_live_pageview():
    run_session_sdk(r"""
      await ctx.leadq('attribution');
      await ctx.leadq('testStatus');
      const count=events.length;
      state='ended';
      assert.equal((await ctx.leadq('testStatus')).state,'ended');
      assert.equal(events.length,count);
      assert(!ctx.location.href.includes('lead_test'));
      assert.equal(temporary.getItem('lead:c1:test'),'');
      assert(!ctx.location.href.includes('lr='));
      for(let i=0;i<130;i++)intervals.find(timer=>timer.ms===1000).fn();
      assert.equal(events.length,count);
    """)


def test_sdk_expiration_blocks_events_until_explicit_exit():
    run_session_sdk(r"""
      await ctx.leadq('attribution');
      await ctx.leadq('testStatus');
      state='expired';
      assert.equal((await ctx.leadq('testStatus')).state,'expired');
      const count=events.length;
      await assert.rejects(ctx.leadq('track','Lead',{}),/过期/);
      await assert.rejects(ctx.leadq('identify','must-not-become-live'),/过期/);
      assert.equal(events.length,count);
      assert.equal(temporary.getItem('lead:c1:user'),'live-user');
      await ctx.leadq('endTest');
      await ctx.leadq('track','Lead',{});
      assert.equal(events.at(-1).test_code,null);
    """)


def test_direct_entries_do_not_reuse_promotional_visits():
    source = Path("services/api/lead_app/pixel.js").resolve()
    script = r"""
    const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
    const code=fs.readFileSync(process.argv[1],'utf8');
    function store(){const m=new Map();return {getItem:k=>m.get(k),setItem:(k,v)=>m.set(k,v)}}
    const local=store(),tab=store(),site='https://site.example/';
    const token='lc1.'+Buffer.from(JSON.stringify({id:'click1',exp:4102444800})).toString('base64url')+'.test';
    let now=Date.now();class Clock extends Date{static now(){return now}}
    async function load(path,referrer='',type='navigate',storage=tab){
      const sent=[];const ctx={URL,Date:Clock,atob,crypto:require('node:crypto').webcrypto,
        localStorage:local,sessionStorage:storage, location:{href:site+path},setInterval(){},addEventListener(){},
        performance:{getEntriesByType:()=>[{type}]},
        history:{replaceState(){},pushState(_a,_b,url){ctx.location.href=new URL(url,ctx.location.href).href}},
        document:{referrer,currentScript:{src:site+'sdk',dataset:{connection:'c',key:'k'}}},
        fetch:async(url,options)=>{sent.push(JSON.parse(options.body));return {ok:true,json:async()=>({attribution:{}})}}};
      ctx.window=ctx;vm.createContext(ctx);vm.runInContext(code,ctx);await ctx.leadq('attribution');
      return {first:sent[0],ctx,sent};
    }
    (async()=>{
      const direct=(await load('')).first;
      const landing=(await load('?lr=promo&lc='+token)).first;
      assert(!direct.ref && !direct.click_token);assert.equal(landing.ref,'promo');
      assert.notEqual(landing.session_id,direct.session_id);
      const internal=await load('order',site);assert.equal(internal.first.session_id,landing.session_id);
      assert.equal(internal.first.click_token,token);
      internal.ctx.history.pushState(null,'','/account');await internal.ctx.leadq('attribution');
      assert.equal(internal.sent.at(-1).session_id,landing.session_id);
      const reload=await load('account','','reload');assert.equal(reload.first.session_id,landing.session_id);
      const other=(await load('','','navigate',store())).first;assert(!other.ref && !other.click_token);
      const again=(await load('')).first;assert(!again.ref && !again.click_token);
      assert.notEqual(again.session_id,landing.session_id);assert.equal(again.anonymous_id,landing.anonymous_id);
      const newPromo=await load('?lr=promo');now+=31*60*1000;
      const expired=(await load('checkout',site+'?lr=promo')).first;
      assert(!expired.ref && !expired.click_token);assert.notEqual(expired.session_id,newPromo.first.session_id);
    })().catch(err=>{console.error(err);process.exitCode=1})
    """
    subprocess.run(["node", "-e", script, str(source)], check=True, timeout=10)
