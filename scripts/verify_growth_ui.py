"""Real desktop/narrow browser checks of the canonical minimal workspace, synthetic data only."""
import argparse
import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

from verify_growth_runtime import (
    ROOT,
    environment,
    free_port,
    read_manifest,
    validate_state,
)


async def verify(state):
    import httpx
    from camoufox.pkgman import launch_path
    from playwright.async_api import async_playwright
    web = ROOT/"apps/web"
    fixture = web/"app/growth-check"
    if fixture.exists():
        raise ValueError("Refusing to overwrite an existing verification page")
    report_dir = state/"reports/ui"
    report_dir.mkdir(parents=True,exist_ok=True)
    originals = {name:(web/name).read_bytes() for name in ("next-env.d.ts","tsconfig.json")}
    fixture.mkdir()
    fixture.joinpath("page.tsx").write_text('''"use client";
import GrowthStatus from "../growth-status";
import SocialAccounts from "../social-accounts";
import ResearchProviders from "../admin/research-providers";
import {useState} from "react";
import "../admin/admin.css";
const agent={id:"growth-ui-fixture",desired_state:"running",onboarding_state:{},accounts:[
 {id:"account-one",platform:"facebook",state:"connected",display_name:"测试账号甲",operating_intent:{desired:"running",revision:3}},
 {id:"account-two",platform:"facebook",state:"connected",display_name:"测试账号乙",operating_intent:{desired:"paused",revision:7},discovery:{state:"succeeded",output:{missing_conditions:[{code:"target_discovery_unresolved"}]}}}]};
export default function Check(){const [providers,setProviders]=useState({});return <main style={{maxWidth:920,margin:"32px auto",padding:20}}><h1>合成数据验证</h1>
<SocialAccounts agent={agent} platforms={[]} readOnly={false} onConnect={p=>{document.title=JSON.stringify(p)}} onComplete={async()=>{}}/>
<GrowthStatus aid="growth-ui-fixture" agent={agent} readOnly={false}/>
<div className="admin-shell" style={{display:"block",height:"auto",minHeight:0}}><ResearchProviders value={providers} change={setProviders} status={{tavily:{"web.search":{available:false,reason:"missing_key"}}}}/>
</div><output hidden id="provider-fixture-state">{JSON.stringify(providers)}</output></main>}''')
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    requests,commands,errors,external = [],[],[],[]
    log = (report_dir/"server.log").open("w")
    process = subprocess.Popen(["node",str(web/"node_modules/next/dist/bin/next"),"dev","--hostname","127.0.0.1","--port",str(port)],
        cwd=web,env={**environment(state),"LEAD_WEB_DIST_DIR":".next-growth-check"},stdout=log,stderr=subprocess.STDOUT)
    data = {"agent_id":"growth-ui-fixture","objective":{"description":"让需要产品的人获得核心价值"},
        "results":{"new_units":None,"measurement":"missing"},
        "current_learning":[{"state":"active","question":"陌生用户能否独立完成第一次价值行为？","learning_signal":"真实场景中的使用结果"}],
        "current_work":[{"id":"reading","title":"调查产品与真实需求","state":"running"},
            {"id":"publishing","title":"推进渠道操作","state":"running","platform":"facebook"}],
        "waiting":[{"id":"identity","title":"核对账号与可运营位置","state":"waiting_user","platform":"x",
                    "wait":{"reason":"challenge","responsibility":"owner","check_at":"2026-11-13T10:00:00Z"}}],
        "owner_actions":[{"id":"identity"}],"recent_work":[
            {"id":"probe","title":"检查受影响能力","state":"succeeded","probe":{"state":"still_blocked","resumed_count":0}}]}
    try:
        async with httpx.AsyncClient(trust_env=False,timeout=2) as client:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Verification web service exited; inspect its private log")
                try:
                    if (await client.get(base+"/growth-check")).status_code==200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(.4)
            else:
                raise RuntimeError("Verification web service readiness timed out")
        async with async_playwright() as playwright:
            browser = await playwright.firefox.launch(executable_path=launch_path(),headless=True)
            try:
                context = await browser.new_context(viewport={"width":1440,"height":1100})
                async def route(request):
                    url = request.request.url
                    if not url.startswith(base+"/"):
                        external.append(url)
                        return await request.abort()
                    if "/api/" not in url:
                        return await request.continue_()
                    requests.append(url.removeprefix(base))
                    if url.endswith("/growth/commands"):
                        commands.append(request.request.post_data_json)
                        return await request.fulfill(json={"ok":True,"revision":8})
                    if url.endswith("/growth"):
                        return await request.fulfill(json=data)
                    if "/analytics/overview" in url:
                        return await request.fulfill(json={"agent_id":"growth-ui-fixture","series":[],"countries":[],"events":{}})
                    if url.endswith("/basis"):
                        return await request.fulfill(json={"completion_rule":"保留真实出处和反证"})
                    if url.endswith("/activity"):
                        return await request.fulfill(json={"task":{"state":"running"},"records":[],"actions":[],"output":{}})
                    return await request.fulfill(json={"agent_id":"growth-ui-fixture"})
                await context.route("**/*",route)
                page = await context.new_page()
                page.on("pageerror",lambda error:errors.append(str(error)))
                await page.goto(base+"/growth-check",wait_until="networkidle")
                await page.get_by_text("未知",exact=True).wait_for()
                await page.get_by_text("需要在原窗口完成验证",exact=False).wait_for()
                await page.get_by_text("陌生用户能否独立完成第一次价值行为？",exact=True).wait_for()
                await page.get_by_role("button",name="查看近期记录",exact=True).click()
                await page.get_by_text("检查完成，此能力仍受阻",exact=True).wait_for()
                await page.get_by_text("服务端密钥未配置",exact=False).wait_for()
                await page.get_by_label("Tavily",exact=True).check()
                await page.get_by_label("连接共享额度（credits）",exact=True).fill("2.5")
                assert json.loads(await page.locator("#provider-fixture-state").inner_text())["tavily"]["limit_units"] == 2500
                assert await page.get_by_text("Facebook",exact=False).count()
                assert not any("/plans" in url for url in requests)
                assert not await page.get_by_text("日计划",exact=False).count()
                assert not await page.get_by_text("周计划",exact=False).count()
                evidence = page.get_by_role("button",name="调查产品与真实需求",exact=False)
                await evidence.focus()
                await page.keyboard.press("Enter")
                await page.get_by_role("dialog").wait_for()
                await page.get_by_text("保留真实出处和反证",exact=True).wait_for()
                await page.keyboard.press("Escape")
                assert await evidence.evaluate("element=>element===document.activeElement")
                for width,name in ((1440,"desktop"),(390,"narrow")):
                    await page.set_viewport_size({"width":width,"height":1100})
                    await page.screenshot(path=str(report_dir/(name+".png")),full_page=True)
                    assert await page.evaluate("document.documentElement.scrollWidth<=innerWidth"),name
                await page.get_by_role("button",name="Facebook，自动推进中",exact=True).click()
                await page.get_by_role("combobox",name="选择 Facebook 账号").select_option("account-two")
                await page.get_by_text("账号已连接，平台尚未核实可执行位置或接入能力。",exact=False).wait_for()
                await page.get_by_role("button",name="恢复此账号",exact=True).click()
                await page.wait_for_timeout(100)
                assert commands[-1]["payload"]=={"account_id":"account-two"}
                assert commands[-1]["kind"]=="account.resume" and commands[-1]["expected_revision"]==7
                await page.screenshot(path=str(report_dir/"account-dialog-narrow.png"),full_page=True)
                assert await page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                assert not await page.get_by_role("button",name="启用自动化",exact=False).count()
                await page.get_by_role("button",name="连接另一个账号",exact=True).click()
                assert json.loads(await page.title())["new_account"] is True
                await page.goto(base+"/workflow-guide.html",wait_until="networkidle")
                for width,name in ((1440,"guide-desktop"),(390,"guide-narrow")):
                    await page.set_viewport_size({"width":width,"height":1100})
                    await page.screenshot(path=str(report_dir/(name+".png")),full_page=True)
                    assert await page.evaluate("document.documentElement.scrollWidth<=innerWidth"),name
                assert not errors,errors
                assert not external,external
            finally:
                await browser.close()
        (report_dir/"report.json").write_text(json.dumps({"ok":True,"fixture":"synthetic-only","external_actions":0,
            "checks":["parallel_work_visible","owner_wait_local","unknown_outcome","no_period_plans","no_plan_requests","keyboard_dialog_focus","desktop","390px_no_overflow","exact_account_control","independent_new_account","capability_gap_visible","workflow_guide"],
            "requests":requests,"commands":commands,"page_errors":errors},ensure_ascii=False,indent=2))
        print(json.dumps({"ok":True,"report":str(report_dir/"report.json")},ensure_ascii=False))
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()
        shutil.rmtree(fixture)
        for name,content in originals.items():
            (web/name).write_bytes(content)


if __name__=="__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir",type=Path,required=True)
    args = parser.parse_args()
    state = validate_state(args.state_dir)
    read_manifest(state)
    safe = environment(state)
    for key in list(os.environ):
        if key.startswith(("LEAD_","OPENAI_","DEEPSEEK_","ANTHROPIC_")):
            del os.environ[key]
    os.environ.update(safe)
    asyncio.run(verify(state))
