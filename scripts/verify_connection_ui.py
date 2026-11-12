"""Synthetic UI checks of connection input and versioned browser control; no real account calls."""
import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

from verify_growth_runtime import ROOT, free_port, validate_state

FIXTURE = '''"use client";
import {useState} from "react";
import ConnectionDetails from "../connection-details";
import AccountBrowser from "../account-browser";
import SocialAccounts from "../social-accounts";
export default function Check(){const [target,setTarget]=useState<any>(null),[browser,setBrowser]=useState<any>(null);
const agent={id:"connection-ui-fixture",desired_state:"running",onboarding_state:{},accounts:[
 {id:"web-one",platform:"web",connection_origin:"https://alpha.example",state:"connected",operating_intent:{desired:"running",revision:1}},
 {id:"web-two",platform:"web",connection_origin:"https://beta.example",state:"connected",operating_intent:{desired:"paused",revision:2}}]};
return <main style={{maxWidth:920,margin:"32px auto",padding:20}}><h1>连接验证</h1>
<SocialAccounts agent={agent} platforms={[]} readOnly={false} onConnect={p=>setTarget(p)} onComplete={async()=>{}}/>
<button onClick={()=>setTarget({id:"web",name:"网站"})}>连接新网站</button>
{target&&<ConnectionDetails target={target} onClose={()=>setTarget(null)} onConnect={details=>{setBrowser({...details,platform:target.id});setTarget(null)}}/>}
{browser&&<AccountBrowser aid="connection-ui-fixture" platform={browser.platform} name="网站" siteUrl={browser.site_url} login={browser.login}
 onCredentialsSent={()=>setBrowser((value:any)=>value?{...value,login:undefined}:value)} onClose={()=>setBrowser(null)} onVerified={async()=>setBrowser(null)}/>}
</main>}'''


async def verify(state):
    import httpx
    from camoufox.pkgman import launch_path
    from playwright.async_api import async_playwright

    state=validate_state(state)
    state.mkdir(parents=True,exist_ok=True,mode=0o700)
    report=state/'reports/connection-ui'
    report.mkdir(parents=True,exist_ok=True)
    web=ROOT/'apps/web'
    fixture=web/'app/connection-check'
    if fixture.exists():
        raise ValueError('Refusing to overwrite a verification page')
    originals={name:(web/name).read_bytes() for name in ('next-env.d.ts','tsconfig.json')}
    fixture.mkdir()
    (fixture/'page.tsx').write_text(FIXTURE)
    port=free_port()
    base=f'http://127.0.0.1:{port}'
    safe={k:v for k,v in os.environ.items() if not k.startswith(('LEAD_','OPENAI_','DEEPSEEK_','ANTHROPIC_'))}
    safe.update(LEAD_WEB_DIST_DIR='.next-connection-check',LEAD_API_ORIGIN='http://127.0.0.1:'+str(free_port()))
    log=(report/'server.log').open('w')
    process=subprocess.Popen(['node',str(web/'node_modules/next/dist/bin/next'),'dev','--hostname','127.0.0.1','--port',str(port)],cwd=web,env=safe,stdout=log,stderr=subprocess.STDOUT)
    errors,external,connects,controls,inputs,ends=[],[],[],[],[],[]
    read_override=None
    session={'status':'active','control_mode':'automatic','challenge_version':1,'owner_action':None}
    image=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
    try:
        async with httpx.AsyncClient(trust_env=False,timeout=2) as client:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError('Verification web service exited')
                try:
                    if (await client.get(base+'/connection-check')).status_code==200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(.4)
            else:
                raise RuntimeError('Verification web service did not become ready')
        async with async_playwright() as playwright:
            browser=await playwright.firefox.launch(executable_path=launch_path(),headless=True)
            try:
                context=await browser.new_context(viewport={'width':1440,'height':1000})
                async def route(request):
                    url=request.request.url
                    if not url.startswith(base+'/'):
                        external.append(url)
                        return await request.abort()
                    if '/api/' not in url and '/browser-control/' not in url:
                        return await request.continue_()
                    body=request.request.post_data_json if request.request.method=='POST' else None
                    if url.endswith('/connect'):
                        connects.append(body)
                        return await request.fulfill(json={'takeover_id':'fixture-connection','account_id':'fixture-account','automatic_connection':True,**session})
                    if url.endswith('/connections/fixture-connection/control'):
                        controls.append(body)
                        assert body['challenge_version']==session['challenge_version']
                        if body['action']=='resume':
                            session.update(control_mode='automatic',challenge_version=session['challenge_version']+1,owner_action=None)
                        else:
                            session.update(status='canceled',challenge_version=session['challenge_version']+1)
                        return await request.fulfill(json=session)
                    if url.endswith('/connections/fixture-connection'):
                        return await request.fulfill(json=read_override or session)
                    if '/browser/screenshot' in url or '/browser-control/' in url:
                        return await request.fulfill(body=image,content_type='image/png',headers={'X-Lead-Frame-Width':'1280','X-Lead-Frame-Height':'800'})
                    if url.endswith('/browser/input'):
                        inputs.append(body)
                        return await request.fulfill(json={'ok':True})
                    if '/takeover/end' in url:
                        ends.append(body)
                        return await request.fulfill(json={'ok':True})
                    return await request.fulfill(json={})
                await context.route('**/*',route)
                page=await context.new_page()
                page.on('pageerror',lambda error:errors.append(str(error)))
                await page.goto(base+'/connection-check',wait_until='networkidle')
                await page.get_by_role('button',name='网站，自动推进中',exact=True).click()
                await page.get_by_role('combobox',name='选择 网站 账号').select_option('web-two')
                await page.locator('.social-account-detail > strong').filter(has_text='https://beta.example').wait_for()
                await page.get_by_role('button',name='关闭',exact=True).click()
                await page.get_by_role('button',name='连接新网站',exact=True).click()
                await page.get_by_label('网站地址',exact=True).fill('https://alpha.example/')
                await page.get_by_label('账号',exact=True).fill('fixture-user')
                await page.get_by_role('button',name='开始连接',exact=True).click()
                await page.locator('.field-error[role=alert]').wait_for()
                assert not connects
                await page.get_by_label('密码',exact=True).fill('synthetic-only-secret')
                assert await page.get_by_label('密码',exact=True).get_attribute('type')=='password'
                assert 'synthetic-only-secret' not in await page.locator('body').inner_text()
                for width,label in ((1440,'form-desktop'),(390,'form-narrow')):
                    await page.set_viewport_size({'width':width,'height':1000})
                    await page.screenshot(path=str(report/(label+'.png')),full_page=True)
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth'),label
                await page.get_by_role('button',name='开始连接',exact=True).click()
                await page.get_by_text('自动连接中，画面仅供查看',exact=True).wait_for()
                assert len(connects)==1 and connects[0]['platform']=='web'
                assert connects[0]['site_url']=='https://alpha.example/'
                assert connects[0]['login']=={'username':'fixture-user','password':'synthetic-only-secret'}
                assert await page.get_by_role('textbox',name='浏览器键盘输入',exact=True).is_disabled()
                assert await page.get_by_role('button',name='浏览器后退',exact=True).is_disabled()
                await page.locator('.account-browser-screen > img').click(position={'x':20,'y':20})
                await page.wait_for_timeout(100)
                assert not inputs
                session.update(control_mode='owner',challenge_version=2,owner_action='请在原窗口输入验证码')
                stale_owner=dict(session)
                await page.get_by_text('请在原窗口输入验证码',exact=True).wait_for()
                keyboard=page.get_by_role('textbox',name='浏览器键盘输入',exact=True)
                assert await keyboard.is_enabled()
                await keyboard.fill('123456')
                await page.wait_for_timeout(100)
                assert inputs[-1]['challenge_version']==2
                await page.screenshot(path=str(report/'owner-narrow.png'),full_page=True)
                assert await page.evaluate("document.querySelector('.browser-header-actions').getBoundingClientRect().right<=innerWidth"),'owner_controls_visible'
                await page.get_by_role('button',name='本人步骤已完成，继续连接',exact=True).click()
                await page.get_by_text('自动连接中，画面仅供查看',exact=True).wait_for()
                assert controls[-1]=={'challenge_version':2,'action':'resume'}
                assert await keyboard.is_disabled()
                read_override=stale_owner
                await page.wait_for_timeout(2300)
                assert await keyboard.is_disabled()
                assert not await page.get_by_role('button',name='本人步骤已完成，继续连接',exact=True).count()
                read_override=None
                for width,label in ((1440,'browser-desktop'),(390,'browser-narrow')):
                    await page.set_viewport_size({'width':width,'height':1000})
                    await page.screenshot(path=str(report/(label+'.png')),full_page=True)
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth'),label
                await page.get_by_role('button',name='取消连接',exact=True).click()
                await page.get_by_role('button',name='连接新网站',exact=True).wait_for()
                assert controls[-1]=={'challenge_version':3,'action':'cancel'}
                assert not ends
                await page.locator('.account-browser-overlay').wait_for(state='detached')
                assert not await page.get_by_label('密码',exact=True).count()
                session.update(status='active',control_mode='automatic',challenge_version=1)
                await page.get_by_role('button',name='连接新网站',exact=True).click()
                await page.get_by_label('网站地址',exact=True).fill('https://alpha.example/')
                await page.get_by_role('button',name='开始连接',exact=True).click()
                await page.get_by_text('自动连接中，画面仅供查看',exact=True).wait_for()
                await page.get_by_role('button',name='关闭账号浏览器',exact=True).click()
                await page.locator('.account-browser-overlay').wait_for(state='detached')
                assert len(ends)==1 and len(controls)==2
                assert len(connects)==2 and 'login' not in connects[-1]
                assert not await page.get_by_label('密码',exact=True).count()
                assert not errors,errors
                assert not external,external
            finally:
                await browser.close()
        (report/'report.json').write_text(json.dumps({'ok':True,'fixture':'synthetic-only','external_requests':0,
            'checks':['generic_site_entry','origin_account_selection','optional_credential_pair_validation','secret_masked_and_cleared','one_connect_request','automatic_input_disabled','owner_version_input','versioned_resume','stale_owner_response_rejected','versioned_cancel','close_keeps_background_observer','1440px','390px_no_overflow'],
            'connect_requests':len(connects),'control_actions':controls,'input_requests':len(inputs),'page_errors':errors},ensure_ascii=False,indent=2))
        print(json.dumps({'ok':True,'report':str(report/'report.json')},ensure_ascii=False))
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()
        shutil.rmtree(fixture)
        for name,data in originals.items():
            (web/name).write_bytes(data)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--state-dir',required=True,type=Path)
    asyncio.run(verify(parser.parse_args().state_dir))
