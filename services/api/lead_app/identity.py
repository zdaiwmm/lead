"""First-party device sampling page; never measures website visitors."""

import base64
import hashlib
import ipaddress
import json

from .config import STATE, env
from .db import j, one, require_agent, tx, uid
from .scope import current_tenant

COLLECT = r"""(async()=>{
 const hash=arr=>Array.from(arr).reduce((h,n)=>Math.imul(h^n,16777619)>>>0,2166136261);
 const c=document.createElement('canvas');const ctx=c.getContext('2d');ctx.font='18px Arial';ctx.fillText('lead persistent identity',10,25);
 const a=new OfflineAudioContext(1,5000,44100),osc=a.createOscillator(),comp=a.createDynamicsCompressor();osc.type='triangle';osc.frequency.value=10000;comp.threshold.value=-50;comp.knee.value=40;comp.ratio.value=12;comp.attack.value=0;comp.release.value=.25;osc.connect(comp);comp.connect(a.destination);osc.start();const b=await a.startRendering();
 const gl=document.createElement('canvas').getContext('webgl');const ext=gl?.getExtension('WEBGL_debug_renderer_info');
 const result={ua:navigator.userAgent,platform:navigator.platform,language:navigator.language,languages:navigator.languages,timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,screen:[screen.width,screen.height],hardwareConcurrency:navigator.hardwareConcurrency,canvasPixels:hash(ctx.getImageData(0,0,c.width,c.height).data),audioHash:hash(new Uint8Array(b.getChannelData(0).buffer)),webglVendor:ext?gl.getParameter(ext.UNMASKED_VENDOR_WEBGL):null,webglRenderer:ext?gl.getParameter(ext.UNMASKED_RENDERER_WEBGL):null,webRTCDisabled:typeof RTCPeerConnection==='undefined'};
 document.querySelector('#result').textContent=JSON.stringify(result);document.querySelector('#result').dataset.ready='true';
})().catch(e=>{document.querySelector('#result').textContent=JSON.stringify({error:e.name})});"""
SCRIPT_HASH = base64.b64encode(hashlib.sha256(COLLECT.encode()).digest()).decode()


def page():
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>lead 环境采集</title><h1>本地设备特征</h1><pre id="result">正在采集…</pre><script>'
        + COLLECT
        + "</script></html>"
    )


async def check(actor):
    """Measure this browser's egress; never promotes an unconfigured shared IP.

    This is a fixed-exit check, not a claim to pass arbitrary bot detection.
    It sends no account cookies to the two measurement services.
    """
    aid = actor.aid
    async with actor.lock:
        with tx(aid) as c:
            require_agent(c, aid, True)
            if one(
                c,
                "SELECT 1 FROM lead.takeover_sessions WHERE state IN ('requested','active','ending') AND expires_at>now()",
            ):
                raise ValueError("请先结束浏览器接管")
            profile = one(c, "SELECT * FROM lead.browser_profiles")
            network = one(
                c,
                "SELECT * FROM lead.network_identities WHERE id=%s",
                (profile["network_identity_id"],),
            )
        result = {
            "scope": "fixed_exit_v1",
            "probes": {},
            "device": None,
            "errors": [],
            "os_egress_enforced": bool(
                actor.forward_proxy.boundary
                and actor.forward_proxy.boundary.get("state") == "verified"
            ),
            "os_boundary": actor.forward_proxy.boundary or {"state": "not_enforced"},
        }
        sample = await actor.context.new_page()
        try:
            await sample.goto(
                f"http://127.0.0.1:8088/lab/{aid}/identity/", timeout=15000
            )
            await sample.locator("#result[data-ready=true]").wait_for(timeout=10000)
            result["device"] = json.loads(await sample.locator("#result").inner_text())
            for name, url in [
                ("ipify", "https://api.ipify.org?format=json"),
                ("cloudflare", "https://www.cloudflare.com/cdn-cgi/trace"),
            ]:
                try:
                    response = await sample.goto(
                        url, wait_until="domcontentloaded", timeout=20000
                    )
                    if response is None or not response.ok:
                        raise ValueError("probe_navigation_failed")
                    text = await response.text()
                    data = (
                        json.loads(text)
                        if name == "ipify"
                        else dict(
                            line.split("=", 1)
                            for line in text.splitlines()
                            if "=" in line
                        )
                    )
                    ip = ipaddress.ip_address(data["ip"])
                    if not ip.is_global:
                        raise ValueError("non_public_probe_ip")
                    result["probes"][name] = {"ip": str(ip), "region": data.get("loc")}
                except Exception as e:
                    result["errors"].append({"probe": name, "error": type(e).__name__})
        finally:
            await sample.close()
        ips = {v["ip"] for v in result["probes"].values()}
        expected = env().get("LEAD_EXPECTED_IP_" + aid.replace("-", "").upper()) or (
            str(network["expected_ip"]) if network["expected_ip"] else None
        )
        result["fixed_proxy"] = bool(actor.forward_proxy.upstream)
        result["expected_ip_matches"] = bool(
            expected and len(ips) == 1 and expected in ips
        )
        result["probe_agreement"] = len(result["probes"]) == 2 and len(ips) == 1
        from .managed_network import policy

        network_policy = policy(aid) or {}
        wanted_region = network_policy.get("region") or network.get("expected_region")
        wanted_timezone = network_policy.get("timezone")
        wanted_locale = network_policy.get("locale")
        result["region_matches"] = bool(
            wanted_region
            and result["probes"].get("cloudflare", {}).get("region") == wanted_region
        )
        device = result["device"] or {}
        result["timezone_matches"] = bool(
            wanted_timezone and device.get("timezone") == wanted_timezone
        )
        result["locale_matches"] = bool(
            wanted_locale
            and device.get("language", "").lower() == wanted_locale.lower()
        )
        baseline_path = STATE / "profiles" / aid / "observed-device.json"
        stable = {
            k: device.get(k)
            for k in (
                "ua",
                "platform",
                "timezone",
                "language",
                "screen",
                "hardwareConcurrency",
                "canvasPixels",
                "audioHash",
                "webglVendor",
                "webglRenderer",
            )
        }
        if baseline_path.exists():
            baseline = json.loads(baseline_path.read_text())
            changed = [k for k, v in baseline.items() if stable.get(k) != v]
        else:
            changed = []
            if device and not device.get("error"):
                from .ops import atomic_json

                atomic_json(baseline_path, stable)
        result["device_drift"] = changed
        production = env().get("LEAD_DEPLOYMENT_MODE") == "production"
        result["production_policy_complete"] = bool(
            result["os_egress_enforced"]
            and result["region_matches"]
            and result["timezone_matches"]
            and result["locale_matches"]
            and not changed
        )
        passed = bool(
            not changed
            and (not production or result["production_policy_complete"])
            and result["fixed_proxy"]
            and result["expected_ip_matches"]
            and result["probe_agreement"]
            and result["device"]
            and result["device"].get("webRTCDisabled")
        )
        observed = next(iter(ips)) if len(ips) == 1 else None
        check_id = uid()
        folder = STATE / "evidence" / aid
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (check_id + ".json")
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        path.chmod(0o600)
        with tx(aid) as c:
            # Across-agent collisions are visible here for the local workspace;
            # the database unique index remains the final concurrent barrier.
            if passed:
                try:
                    with c.transaction():
                        c.execute(
                            "UPDATE lead.network_identities SET mode='dedicated',state='verified',expected_ip=%s,verified_at=now(),valid_until=now()+interval '15 minutes' WHERE id=%s",
                            (observed, network["id"]),
                        )
                except __import__("psycopg").errors.UniqueViolation:
                    passed = False
                    result["errors"].append(
                        {"error": "exit_already_bound_to_another_agent"}
                    )
            if not passed:
                c.execute(
                    "UPDATE lead.network_identities SET state=%s WHERE id=%s",
                    ("failed" if expected else "pending", network["id"]),
                )
            c.execute(
                "INSERT INTO lead.identity_checks(id,tenant_id,agent_id,profile_id,network_identity_id,engine_version,config_hash,observed_ip,result,checks,evidence_ref,valid_until) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()+interval '15 minutes')",
                (
                    check_id,
                    current_tenant(),
                    aid,
                    profile["id"],
                    network["id"],
                    profile["engine_version"],
                    profile["config_hash"],
                    observed,
                    "passed" if passed else "failed" if expected else "unknown",
                    j(result),
                    str(path),
                ),
            )
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return {
            "id": check_id,
            "verified": passed,
            "observed_ip": observed,
            "checks": result,
        }
