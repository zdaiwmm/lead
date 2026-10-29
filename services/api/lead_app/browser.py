"""Single local browser gateway. Each actor owns one persistent OS process/profile.

Workers never get a browser connection or arbitrary evaluation endpoint. Cross-process
profile ownership uses flock; the database epoch fences stale controllers. Social
publishing remains disabled until a platform adapter is verified.
"""

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import platform
import time
from contextlib import asynccontextmanager, nullcontext
from urllib.parse import urlsplit
from uuid import UUID

from camoufox.async_api import AsyncCamoufox
from camoufox.fingerprints import generate_fingerprint
from camoufox.utils import launch_options
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .adapters.catalog import SPECS
from .config import BROWSER_BUILD, BROWSER_SDK, STATE, env, proxy_for
from .db import j, one, require_agent, tx, uid
from .domain import PLATFORMS
from .forward_proxy import BrowserForwardProxy
from .scope import current_tenant, scoped_agent_method, service_scope
from .security import internal_token, token
from .telemetry import TelemetryMiddleware


class BrowserBusy(ValueError):
    code = "browser_busy"


class Actor:
    def __init__(self, aid):
        self.aid = aid
        self.lock = asyncio.Lock()
        self.used = time.monotonic()
        self.manager = None
        self.context = None
        self.page = None
        self.file = None
        self.session = None
        self.epoch = None
        self.research_pages = {}
        self.forward_proxy = None
        self.disconnected = False

    def browser_closed(self, *_):
        self.disconnected = True

    async def launch(self):
        from .resources import ensure_capacity

        ensure_capacity(256)
        with tx(self.aid) as c:
            require_agent(c, self.aid)
            if one(
                c,
                "SELECT 1 FROM lead.profile_maintenance WHERE state='active' AND expires_at>now()",
            ):
                raise BrowserBusy("环境正在备份，请稍后重试")
        folder = STATE / "profiles" / self.aid
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = (folder / "actor.lock").open("a")
        fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = folder / "identity.json"
        if not identity.exists():
            fingerprint = await asyncio.to_thread(
                generate_fingerprint,
                os={"Darwin": "macos", "Linux": "linux", "Windows": "windows"}[
                    platform.system()
                ],
                window=(1280, 800),
            )
            # Stable, native engine entropy avoids assigning indistinguishable
            # audio identities when fpgen happens to draw the same common device.
            seed = int(hashlib.sha256(self.aid.encode()).hexdigest()[:8], 16) or 1
            identity.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "fingerprint": fingerprint,
                        "config": {"audio:seed": seed},
                    }
                )
            )
            os.chmod(identity, 0o600)
        saved = json.loads(identity.read_text())
        fingerprint = saved.get("fingerprint", saved)
        native_config = saved.get("config", {}) if saved.get("version") == 2 else {}
        digest = hashlib.sha256(identity.read_bytes()).hexdigest()
        proxy = proxy_for(self.aid)
        self.forward_proxy = await BrowserForwardProxy(self.aid, proxy).start()
        launch_settings = dict(
            browser=BROWSER_BUILD,
            user_data_dir=str(folder / "storage"),
            fingerprint=fingerprint,
            config=native_config,
            headless=True,
            window=(1280, 800),
            locale="en-US",
            block_webrtc=True,
            env={
                name: value
                for name, value in os.environ.items()
                if name
                in {
                    "PATH",
                    "HOME",
                    "TMPDIR",
                    "TMP",
                    "TEMP",
                    "LANG",
                    "LC_ALL",
                    "DISPLAY",
                    "WAYLAND_DISPLAY",
                    "XDG_RUNTIME_DIR",
                    "TZ",
                    "SYSTEMROOT",
                    "WINDIR",
                }
            },
            proxy=self.forward_proxy.browser_proxy,
            firefox_user_prefs={
                "network.proxy.socks_remote_dns": True,
                "network.dns.disableIPv6": True,
                "network.proxy.no_proxies_on": "",
                "network.proxy.allow_hijacking_localhost": True,
                "network.proxy.failover_direct": False,
                "network.dns.disablePrefetch": True,
                "network.prefetch-next": False,
                "network.predictor.enabled": False,
                "network.http.speculative-parallel-limit": 0,
                "network.http.http3.enable": False,
                "media.peerconnection.enabled": False,
            },
            accept_downloads=False,
            i_know_what_im_doing=True,
        )
        try:
            from . import network_namespace

            if network_namespace.required():
                from camoufox.addons import DefaultAddons

                if fingerprint.get("os", "").lower() != "linux":
                    raise ValueError("此环境需要在原系统继续运行，不能自动改写设备身份")
                (folder / "storage").mkdir(exist_ok=True, mode=0o700)
                (STATE / "assets" / self.aid).mkdir(
                    parents=True, exist_ok=True, mode=0o700
                )
                launch_settings["exclude_addons"] = list(DefaultAddons)
                options = await asyncio.to_thread(launch_options, **launch_settings)
                self.manager = AsyncCamoufox(
                    persistent_context=True,
                    from_options=network_namespace.apply(
                        options, self.aid, self.forward_proxy
                    ),
                )
            else:
                self.manager = AsyncCamoufox(persistent_context=True, **launch_settings)
            self.context = await self.manager.__aenter__()
            self.disconnected = False
            self.context.on("close", self.browser_closed)
            # Website isolation from local services. Only the authenticated lab path
            # is reachable on loopback; all social requests must resolve publicly.
            await self.context.route("**/*", self.route)
            await self.context.add_cookies(
                [
                    {
                        "name": "lead_lab",
                        "value": token("lab:" + self.aid),
                        "url": "http://127.0.0.1:8088",
                        "httpOnly": True,
                        "sameSite": "Strict",
                    }
                ]
            )
            self.page = (
                self.context.pages[0]
                if self.context.pages
                else await self.context.new_page()
            )
            with tx(self.aid) as c:
                a = require_agent(c, self.aid, True)
                profile = one(c, "SELECT * FROM lead.browser_profiles")
                c.execute(
                    "UPDATE lead.browser_sessions SET state='closed',ended_at=now() WHERE ended_at IS NULL"
                )
                lease = one(
                    c,
                    "UPDATE lead.profile_leases SET epoch=epoch+1,owner_node_id='local',owner_actor_id=%s,control_version=%s,state='held',expires_at=now()+interval '90 seconds' RETURNING epoch",
                    (str(os.getpid()), a["control_version"]),
                )
                self.epoch = lease["epoch"]
                self.session = uid()
                c.execute(
                    "INSERT INTO lead.browser_sessions(id,tenant_id,agent_id,profile_id,node_id,actor_id,epoch,control_version,state,heartbeat_at) VALUES(%s,%s,%s,%s,'local',%s,%s,%s,'ready',now())",
                    (
                        self.session,
                        current_tenant(),
                        self.aid,
                        profile["id"],
                        str(os.getpid()),
                        self.epoch,
                        a["control_version"],
                    ),
                )
                c.execute(
                    "UPDATE lead.browser_profiles SET state='ready',config_hash=%s,engine_version=%s,sdk_version=%s WHERE id=%s",
                    (digest, BROWSER_BUILD, BROWSER_SDK, profile["id"]),
                )
                c.execute(
                    "UPDATE lead.network_identities SET mode=%s,state=%s WHERE id=%s",
                    (
                        "dedicated" if proxy else "shared_development",
                        "pending",
                        profile["network_identity_id"],
                    ),
                )
                # A crash between a click and acknowledgement always reconciles.
                c.execute(
                    "UPDATE lead.actions SET state='unknown' WHERE state='dispatching'"
                )
        except BaseException:
            await self.close()
            raise

    async def route(self, route):
        p = urlsplit(route.request.url)
        if p.scheme in ("data", "blob", "about"):
            return await route.continue_()
        if p.hostname in ("127.0.0.1", "localhost", "::1"):
            if (
                p.hostname == "127.0.0.1"
                and p.port == 8088
                and p.path.startswith("/lab/" + self.aid + "/")
            ):
                return await route.continue_()
            return await route.abort()
        # The profile CONNECT proxy validates and pins destination DNS for the
        # actual browser connection; the controller must not resolve it through
        # a second host-side DNS path.
        if (
            p.scheme not in ("http", "https")
            or not p.hostname
            or p.username
            or p.password
            or p.port not in (None, 80, 443)
        ):
            return await route.abort()
        await route.continue_()

    async def close(self):
        if self.manager:
            try:
                await self.manager.__aexit__(None, None, None)
            except Exception:
                pass
            self.manager = None
        if self.forward_proxy:
            await self.forward_proxy.close()
            self.forward_proxy = None
        try:
            if self.session:
                with tx(self.aid) as c:
                    c.execute(
                        "UPDATE lead.browser_sessions SET state='closed',ended_at=now() WHERE id=%s AND ended_at IS NULL",
                        (self.session,),
                    )
                    c.execute(
                        "UPDATE lead.profile_leases SET state='released',expires_at=now() WHERE epoch=%s",
                        (self.epoch,),
                    )
                    c.execute("UPDATE lead.browser_profiles SET state='stopped'")
        finally:
            if self.file:
                self.file.close()
                self.file = None

    def touch(self):
        self.used = time.monotonic()

    def gate(self, c, action):
        from .resources import ensure_capacity

        ensure_capacity(64)
        recovery = one(c, "SELECT state FROM lead.recovery_control WHERE singleton")
        if not recovery or recovery["state"] != "normal":
            raise ValueError("恢复数据库尚待核对外部动作，已停止发布")
        if one(
            c,
            "SELECT 1 FROM lead.profile_maintenance WHERE state='active' AND expires_at>now()",
        ):
            raise ValueError("环境正在备份，已停止提交")
        if (
            env().get("LEAD_DEPLOYMENT_MODE") == "production"
            and not one(
                c, "SELECT lead.backup_ready() AND lead.offsite_backup_ready() AS ready"
            )["ready"]
        ):
            raise ValueError("数据库恢复点已过期，已停止新的发布")
        platform = action["intent"].get("platform")
        from .adapters.catalog import SPECS

        if platform not in ("lab", "telegram") and platform not in SPECS:
            raise ValueError("该频道的自动发布适配尚未开放")
        a = require_agent(c, self.aid, True)
        if (
            a["desired_state"] in ("paused", "archived")
            or a["control_version"] != action["expected_control_version"]
            or a["config_revision"] != action["expected_config_revision"]
        ):
            raise ValueError("Agent 已暂停、被接管或配置已更改")
        if one(
            c,
            "SELECT 1 FROM lead.takeover_sessions WHERE state IN ('requested','active','ending') AND expires_at>now()",
        ):
            raise ValueError("用户正在接管")
        lease = one(
            c,
            "SELECT * FROM lead.profile_leases WHERE epoch=%s AND state='held' AND expires_at>now()",
            (self.epoch,),
        )
        if not lease:
            raise ValueError("浏览器控制租约已失效")
        grant = one(
            c,
            "SELECT * FROM lead.authorization_grants WHERE id=%s AND target_id=%s AND revoked_at IS NULL",
            (action["grant_id"], action["target_id"]),
        )
        if not grant or "publish" not in grant["allowed_actions"]:
            raise ValueError("授权已失效")
        if platform in SPECS:
            from .social_publish import gate

            gate(self, c, action)
        if platform == "telegram":
            task = one(
                c, "SELECT input FROM lead.tasks WHERE id=%s", (action["task_id"],)
            )
            target = one(
                c,
                "SELECT t.*,s.platform,s.external_account_id,s.state AS account_state FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id WHERE t.id=%s",
                (action["target_id"],),
            )
            if (
                not target
                or target["platform"] != "telegram"
                or target["account_state"] != "connected"
                or target["state"] != "active"
            ):
                raise ValueError("频道或账号已失效")
            cap = one(
                c,
                "SELECT * FROM lead.platform_capabilities WHERE platform='telegram' AND action_type='publish' AND target_type='channel'",
            )
            if not cap or cap["permission_state"] != "approved":
                raise ValueError("频道操作尚未通过权限范围审查")
            bootstrap = task["input"].get("bootstrap_verification", False)
            if bootstrap:
                card = one(
                    c,
                    "SELECT * FROM lead.interaction_cards WHERE id=%s AND type='verify_publish' AND state='accepted'",
                    (task["input"]["key"],),
                )
                if (
                    not card
                    or card["payload"]["body"] != action["intent"]["body"]
                    or card["payload"]["grant_id"] != str(action["grant_id"])
                    or card["payload"]["target_id"] != str(action["target_id"])
                ):
                    raise ValueError("连接验证授权与当前内容不一致")
            elif (
                a["desired_state"] != "running"
                or cap["technical_state"] != "verified"
                or cap["adapter_version"] != SPECS[platform].version
                or not target["verified_permissions"].get("publishing_verified")
            ):
                raise ValueError("请先完成这个频道的真实发布验证")
            maximum = int(grant["limits"].get("publish_per_day", 3))
            used = one(
                c,
                "SELECT count(*) AS n FROM lead.actions WHERE target_id=%s AND id<>%s AND state IN ('dispatching','unknown','confirmed') AND created_at>now()-interval '24 hours'",
                (action["target_id"], action["id"]),
            )["n"]
            if used >= maximum:
                raise ValueError("此频道已达到今天的发布上限")
            if env().get("LEAD_DEPLOYMENT_MODE") == "production":
                if not one(
                    c,
                    "SELECT 1 FROM lead.identity_checks i JOIN lead.network_identities n ON n.id=i.network_identity_id WHERE i.result='passed' AND i.valid_until>now() AND n.mode='dedicated' AND n.state='verified'",
                ):
                    raise ValueError("独立出口与设备验证尚未通过")
        return a

    async def publish_telegram(self, action_id):
        from . import telegram

        async with self.lock:
            self.touch()
            with tx(self.aid) as c:
                action = one(c, "SELECT * FROM lead.actions WHERE id=%s", (action_id,))
                if not action:
                    raise ValueError("发布动作不存在")
                if action["state"] == "confirmed":
                    return {"state": "confirmed", "external_id": action["external_id"]}
                if action["intent"].get("platform") != "telegram":
                    raise ValueError("发布渠道与动作不一致")
                target = one(
                    c,
                    "SELECT t.*,s.external_account_id FROM lead.account_targets t JOIN lead.social_accounts s ON s.id=t.account_id WHERE t.id=%s",
                    (action["target_id"],),
                )
                if not target:
                    raise ValueError("频道不存在")
                if one(
                    c,
                    "SELECT 1 FROM lead.takeover_sessions WHERE state IN ('active','requested','ending') AND expires_at>now()",
                ):
                    return (
                        {"state": "unknown", "reason": "等待用户结束接管后核对"}
                        if action["state"] in ("unknown", "dispatching")
                        else {"state": "prepared", "reason": "用户正在接管"}
                    )
                if action["state"] == "dispatching":
                    c.execute(
                        "UPDATE lead.actions SET state='unknown' WHERE id=%s",
                        (action_id,),
                    )
                    action["state"] = "unknown"
                if action["state"] == "prepared":
                    self.gate(c, action)
                prior = one(
                    c,
                    "SELECT * FROM lead.action_attempts WHERE action_id=%s ORDER BY attempt DESC LIMIT 1",
                    (action_id,),
                )
            try:
                await telegram.inspect_target(
                    self.page, target["canonical_url"], target["external_account_id"]
                )
            except telegram.TelegramNeedsAuth:
                with tx(self.aid) as c:
                    c.execute(
                        "UPDATE lead.social_accounts SET state='needs_auth' WHERE id=%s",
                        (target["account_id"],),
                    )
                return {
                    "state": "unknown"
                    if action["state"] == "unknown"
                    else "needs_auth",
                    "error": "ACCOUNT_NEEDS_AUTH",
                }
            except telegram.TelegramTargetMismatch:
                with tx(self.aid) as c:
                    c.execute(
                        "UPDATE lead.social_accounts SET state='needs_auth' WHERE id=%s",
                        (target["account_id"],),
                    )
                return {
                    "state": "unknown" if action["state"] == "unknown" else "blocked",
                    "error": "ACCOUNT_OR_TARGET_CHANGED",
                }
            if action["state"] == "unknown":
                if not prior or not prior["observation"]:
                    return {
                        "state": "unknown",
                        "reason": "缺少提交前观察，禁止自动重发",
                    }
                observed = await telegram.receipt(
                    self.page,
                    action["intent"]["body"],
                    prior["observation"]["baseline_mid"],
                    action["intent"]["marker"],
                )
                if not observed:
                    return {
                        "state": "unknown",
                        "reason": "尚未找到唯一的服务器消息，禁止重发",
                    }
            elif action["state"] == "prepared":
                baseline = await telegram.baseline(self.page)
                await telegram.fill(self.page, action["intent"]["body"])
                # Re-inspect identity immediately before the commit barrier.
                identity = await telegram.inspect_account(self.page)
                current = await self.page.evaluate(telegram.TARGET)
                if (
                    identity["external_account_id"] != target["external_account_id"]
                    or current["target_id"] != target["external_target_id"]
                ):
                    raise ValueError("账号或频道在提交前发生变化")
                with tx(self.aid) as c:
                    self.gate(c, action)
                    c.execute(
                        "UPDATE lead.actions SET state='dispatching' WHERE id=%s AND state='prepared'",
                        (action_id,),
                    )
                    attempt = one(
                        c,
                        "INSERT INTO lead.action_attempts(tenant_id,agent_id,action_id,browser_session_id,attempt,epoch,control_version,observed_account_id,observed_target_id,observation) SELECT %s,%s,%s,%s,coalesce(max(attempt),0)+1,%s,%s,%s,%s,%s FROM lead.action_attempts WHERE action_id=%s RETURNING id",
                        (
                            current_tenant(),
                            self.aid,
                            action_id,
                            self.session,
                            self.epoch,
                            action["expected_control_version"],
                            target["external_account_id"],
                            target["external_target_id"],
                            j(
                                {
                                    "baseline_mid": baseline,
                                    "marker": action["intent"]["marker"],
                                }
                            ),
                            action_id,
                        ),
                    )
                try:
                    with tx(self.aid) as c:
                        self.gate(c, action)
                        await telegram.submit(self.page)
                    observed = None
                    for _ in range(40):
                        observed = await telegram.receipt(
                            self.page,
                            action["intent"]["body"],
                            baseline,
                            action["intent"]["marker"],
                        )
                        if observed:
                            break
                        await asyncio.sleep(0.5)
                    if not observed:
                        raise ValueError("发布后的消息回执尚未确认")
                except Exception:
                    with tx(self.aid) as c:
                        c.execute(
                            "UPDATE lead.actions SET state='unknown' WHERE id=%s AND state='dispatching'",
                            (action_id,),
                        )
                        c.execute(
                            "UPDATE lead.action_attempts SET outcome='unknown',ended_at=now(),error_code='ACK_UNCERTAIN' WHERE id=%s",
                            (attempt["id"],),
                        )
                    return {"state": "unknown", "reason": "发布结果需核对，未再次点击"}
            else:
                return {"state": action["state"]}
            folder = STATE / "evidence" / self.aid
            folder.mkdir(exist_ok=True, parents=True, mode=0o700)
            path = folder / (str(action_id) + ".png")
            await self.page.screenshot(path=str(path))
            path.chmod(0o600)
            external_id = target["external_target_id"] + ":" + observed["mid"]
            with tx(self.aid) as c:
                c.execute(
                    "UPDATE lead.actions SET state='confirmed',external_id=%s,evidence_ref=%s,confirmed_at=now() WHERE id=%s",
                    (external_id, str(path), action_id),
                )
                c.execute(
                    "UPDATE lead.action_attempts SET outcome='confirmed',ended_at=now(),evidence_ref=%s WHERE action_id=%s AND attempt=(SELECT max(attempt) FROM lead.action_attempts WHERE action_id=%s)",
                    (str(path), action_id, action_id),
                )
            return {
                "state": "confirmed",
                "external_id": external_id,
                "reconciled": action["state"] == "unknown",
            }

    async def publish_lab(self, action_id, fault=False):
        async with self.lock:
            self.touch()
            with tx(self.aid) as c:
                action = one(c, "SELECT * FROM lead.actions WHERE id=%s", (action_id,))
                if not action:
                    raise ValueError("动作不存在")
                if action["state"] == "confirmed":
                    return {"state": "confirmed", "external_id": action["external_id"]}
                if action["state"] in ("unknown", "dispatching"):
                    # Reconciliation reads the actual fixture result. It never clicks twice.
                    post = one(
                        c,
                        "SELECT * FROM lead.lab_posts WHERE operation_key=%s",
                        (action["operation_key"],),
                    )
                    if action["state"] == "dispatching":
                        c.execute(
                            "UPDATE lead.actions SET state='unknown' WHERE id=%s",
                            (action_id,),
                        )
                    if post:
                        c.execute(
                            "UPDATE lead.actions SET state='confirmed',external_id=%s,confirmed_at=now() WHERE id=%s",
                            (str(post["id"]), action_id),
                        )
                        return {
                            "state": "confirmed",
                            "external_id": str(post["id"]),
                            "reconciled": True,
                        }
                    return {"state": "unknown", "reason": "尚未确认，禁止自动再次提交"}
                if action["state"] not in ("prepared",):
                    return {"state": action["state"]}
                self.gate(c, action)
            await self.page.goto(
                "http://127.0.0.1:8088/lab/" + self.aid + "/",
                wait_until="domcontentloaded",
            )
            await self.page.locator("[name=body]").fill(action["intent"]["body"])
            await self.page.locator("[name=operation_key]").fill(
                action["operation_key"]
            )
            with tx(self.aid) as c:
                self.gate(c, action)
                c.execute(
                    "UPDATE lead.actions SET state='dispatching' WHERE id=%s AND state='prepared'",
                    (action_id,),
                )
                attempt = one(
                    c,
                    "INSERT INTO lead.action_attempts(tenant_id,agent_id,action_id,browser_session_id,attempt,epoch,control_version,observed_account_id,observed_target_id) SELECT %s,%s,%s,%s,coalesce(max(attempt),0)+1,%s,%s,%s,%s FROM lead.action_attempts WHERE action_id=%s RETURNING id",
                    (
                        current_tenant(),
                        self.aid,
                        action_id,
                        self.session,
                        self.epoch,
                        action["expected_control_version"],
                        self.aid,
                        self.aid,
                        action_id,
                    ),
                )
            try:
                # Agent-row lock is the commit barrier against pause/takeover.
                with tx(self.aid) as c:
                    self.gate(c, action)
                    await self.page.get_by_role(
                        "button", name="发布", exact=True
                    ).click(timeout=15000)
                await self.page.locator(
                    '[data-post-key="' + action["operation_key"] + '"]'
                ).wait_for(timeout=15000)
                if fault:
                    raise ConnectionError("injected lost acknowledgement")
                evidence = STATE / "evidence" / self.aid
                evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = evidence / (str(action_id) + ".png")
                await self.page.screenshot(path=str(path))
                with tx(self.aid) as c:
                    post = one(
                        c,
                        "SELECT id FROM lead.lab_posts WHERE operation_key=%s",
                        (action["operation_key"],),
                    )
                    if not post:
                        raise ValueError("发布证据缺失")
                    c.execute(
                        "UPDATE lead.actions SET state='confirmed',external_id=%s,evidence_ref=%s,confirmed_at=now() WHERE id=%s",
                        (str(post["id"]), str(path), action_id),
                    )
                    c.execute(
                        "UPDATE lead.action_attempts SET outcome='confirmed',ended_at=now(),evidence_ref=%s WHERE id=%s",
                        (str(path), attempt["id"]),
                    )
                return {"state": "confirmed", "external_id": str(post["id"])}
            except Exception:
                with tx(self.aid) as c:
                    c.execute(
                        "UPDATE lead.actions SET state='unknown' WHERE id=%s AND state='dispatching'",
                        (action_id,),
                    )
                    c.execute(
                        "UPDATE lead.action_attempts SET outcome='unknown',ended_at=now(),error_code='ACK_UNCERTAIN' WHERE id=%s",
                        (attempt["id"],),
                    )
                return {"state": "unknown", "reason": "发布结果待核对"}


class Gateway:
    def __init__(self):
        self.actors = {}
        self.locks = {}
        self.slots = asyncio.Semaphore(int(env().get("LEAD_BROWSER_CONCURRENCY") or 3))
        self.waiting = 0

    async def evict_idle(self):
        for aid, actor in sorted(
            list(self.actors.items()), key=lambda pair: pair[1].used
        ):
            if actor.lock.locked() or actor.research_pages:
                continue
            with tx(aid) as c:
                if one(
                    c,
                    "SELECT 1 FROM lead.takeover_sessions WHERE state IN ('requested','active','ending') AND expires_at>now()",
                ):
                    continue
            await self.close(aid)
            return

    @scoped_agent_method
    async def get(self, aid):
        aid = str(UUID(aid))
        async with self.locks.setdefault(aid, asyncio.Lock()):
            with tx(aid) as c:
                if one(
                    c,
                    "SELECT 1 FROM lead.profile_maintenance WHERE state='active' AND expires_at>now()",
                ):
                    raise BrowserBusy("此环境正在备份，请稍后重试")
            if aid in self.actors:
                existing = self.actors[aid]
                if not existing.disconnected:
                    existing.touch()
                    return existing
                if existing.lock.locked():
                    raise BrowserBusy("浏览器正在恢复，请稍后重试")
                self.actors.pop(aid)
                try:
                    await existing.close()
                finally:
                    self.slots.release()
            if self.slots.locked():
                await self.evict_idle()
            self.waiting += 1
            try:
                await asyncio.wait_for(self.slots.acquire(), timeout=90)
            finally:
                self.waiting -= 1
            actor = Actor(aid)
            try:
                await actor.launch()
            except BaseException:
                try:
                    await actor.close()
                finally:
                    self.slots.release()
                raise
            self.actors[aid] = actor
            return actor

    @scoped_agent_method
    async def close(self, aid):
        async with self.locks.setdefault(aid, asyncio.Lock()):
            actor = self.actors.pop(aid, None)
            if actor:
                try:
                    async with actor.lock:
                        await actor.close()
                finally:
                    self.slots.release()

    async def maintenance(self):
        with service_scope():
            while True:
                await asyncio.sleep(20)
                if self.waiting:
                    await self.evict_idle()
                for aid, actor in list(self.actors.items()):
                    try:
                        if actor.disconnected and not actor.lock.locked():
                            await self.close(aid)
                        elif actor.lock.locked() or time.monotonic() - actor.used < 300:
                            with tx(aid) as c:
                                c.execute(
                                    "UPDATE lead.profile_leases SET expires_at=now()+interval '90 seconds' WHERE epoch=%s AND state='held'",
                                    (actor.epoch,),
                                )
                                c.execute(
                                    "UPDATE lead.browser_sessions SET heartbeat_at=now() WHERE id=%s",
                                    (actor.session,),
                                )
                        else:
                            await self.close(aid)
                    except Exception:
                        logging.warning(
                            "Browser maintenance unavailable; next cycle retries."
                        )


gateway = Gateway()


@asynccontextmanager
async def lifespan(app):
    maintenance = asyncio.create_task(gateway.maintenance())
    yield
    maintenance.cancel()
    for aid in list(gateway.actors):
        await gateway.close(aid)


app = FastAPI(
    title="lead internal browser gateway",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


@app.middleware("http")
async def auth(request, call_next):
    if request.headers.get("authorization") != "Bearer " + internal_token():
        return Response(status_code=403)
    try:
        with service_scope():
            return await call_next(request)
    except (ValueError, LookupError) as e:
        message = str(e)
        if not any("\u4e00" <= ch <= "\u9fff" for ch in message):
            message = "浏览器状态不允许此操作"
        return JSONResponse(
            {"detail": message[:250], "code": getattr(e, "code", None)}, status_code=409
        )


class OpenRequest(BaseModel):
    platform: str = "lab"


class PublishRequest(BaseModel):
    action_id: str


class InputRequest(BaseModel):
    kind: str
    x: float = 0
    y: float = 0
    text: str = Field(default="", max_length=4000)


class ResearchCommand(BaseModel):
    run_id: UUID
    command_id: UUID
    operation: str
    args: dict = Field(default_factory=dict)


class TelegramTargetRequest(BaseModel):
    account_id: UUID
    url: str = Field(max_length=2000)


@app.post("/agents/{aid}/account/inspect")
async def inspect_account(aid: UUID, body: OpenRequest):
    from .adapters import get_adapter

    actor = await gateway.get(str(aid))
    async with actor.lock:
        return await get_adapter(body.platform).account(actor.page)


@app.post("/agents/{aid}/target/inspect")
async def inspect_target(aid: UUID, body: TelegramTargetRequest):
    from .adapters import get_adapter

    aid = str(aid)
    with tx(aid) as c:
        account = one(
            c,
            "SELECT * FROM lead.social_accounts WHERE id=%s AND state='connected'",
            (body.account_id,),
        )
        if not account:
            raise HTTPException(404, "已连接账号不存在")
        if one(
            c,
            "SELECT 1 FROM lead.takeover_sessions WHERE state IN ('active','requested','ending') AND expires_at>now()",
        ):
            raise ValueError("请先结束浏览器接管")
    actor = await gateway.get(aid)
    async with actor.lock:
        return await get_adapter(account["platform"]).target(
            actor.page, body.url, account["external_account_id"]
        )


@app.post("/agents/{aid}/telegram/account")
async def inspect_telegram_account(aid: UUID):
    from .telegram import inspect_account

    actor = await gateway.get(str(aid))
    async with actor.lock:
        return await inspect_account(actor.page)


@app.post("/agents/{aid}/telegram/target")
async def inspect_telegram_target(aid: UUID, body: TelegramTargetRequest):
    from .telegram import inspect_target

    aid = str(aid)
    with tx(aid) as c:
        account = one(
            c,
            "SELECT * FROM lead.social_accounts WHERE id=%s AND platform='telegram' AND state='connected'",
            (body.account_id,),
        )
        if not account:
            raise HTTPException(404, "已连接账号不存在")
        if one(
            c,
            "SELECT 1 FROM lead.takeover_sessions WHERE state IN ('active','requested','ending') AND expires_at>now()",
        ):
            raise HTTPException(409, "请先结束浏览器接管")
    actor = await gateway.get(aid)
    async with actor.lock:
        return await inspect_target(
            actor.page, body.url, account["external_account_id"]
        )


@app.post("/agents/{aid}/publish")
async def publish_action(aid: UUID, body: PublishRequest):
    aid = str(aid)
    with tx(aid) as c:
        action = one(
            c, "SELECT intent FROM lead.actions WHERE id=%s", (body.action_id,)
        )
        if not action:
            raise HTTPException(404)
    actor = await gateway.get(aid)
    if (
        action["intent"].get("platform") in SPECS
        and env().get("LEAD_DEPLOYMENT_MODE") == "production"
    ):
        from .identity import check

        measured = await check(actor)
        if not measured["verified"]:
            return {"state": "blocked", "reason": "固定独立出口核验未通过"}
    if action["intent"].get("platform") == "telegram":
        if action["intent"].get("media"):
            from .social_publish import execute

            return await execute(actor, body.action_id)
        return await actor.publish_telegram(body.action_id)
    if action["intent"].get("platform") == "lab":
        return await actor.publish_lab(body.action_id)
    if action["intent"].get("platform") in SPECS:
        from .social_publish import execute

        return await execute(actor, body.action_id)
    raise HTTPException(409, "该渠道尚未通过发布适配验证")


@app.post("/agents/{aid}/research/command")
async def research_command(aid: UUID, body: ResearchCommand):
    from .research import ResearchPage

    aid = str(aid)
    run_id = str(body.run_id)
    with tx(aid) as c:
        require_agent(c, aid)
        if not one(c, "SELECT id FROM lead.browser_runs WHERE id=%s", (run_id,)):
            raise HTTPException(404, "浏览任务不存在")
    actor = await gateway.get(aid)
    controller = actor.research_pages.setdefault(run_id, ResearchPage(actor, run_id))
    return await controller.command(body.operation, body.args, str(body.command_id))


@app.post("/agents/{aid}/research/{rid}/close")
async def close_research(aid: UUID, rid: UUID):
    actor = gateway.actors.get(str(aid))
    if actor:
        async with actor.lock:
            controller = actor.research_pages.pop(str(rid), None)
            if controller and controller.context:
                await controller.context.close()
    return {"ok": True}


@app.get("/health")
async def health():
    return {
        "ok": True,
        "active_profiles": len(gateway.actors),
        "capacity": int(env().get("LEAD_BROWSER_CONCURRENCY") or 3),
        "engine": "camoufox",
    }


@app.post("/agents/{aid}/identity/check")
async def check_identity(aid: UUID):
    from .identity import check

    return await check(await gateway.get(str(aid)))


@app.post("/agents/{aid}/open")
async def open_browser(aid: str, body: OpenRequest):
    actor = await gateway.get(aid)
    async with actor.lock:
        url = (
            "http://127.0.0.1:8088/lab/" + aid + "/"
            if body.platform == "lab"
            else next((p[2] for p in PLATFORMS if p[0] == body.platform), None)
        )
        if not url:
            raise HTTPException(400, "未知频道")
        await actor.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        return {"url": actor.page.url, "session_id": actor.session}


@app.post("/agents/{aid}/publish-lab")
async def publish(aid: str, body: PublishRequest):
    actor = await gateway.get(aid)
    return await actor.publish_lab(body.action_id)


@app.get("/agents/{aid}/screenshot")
async def screenshot(aid: str):
    actor = await gateway.get(aid)
    async with actor.lock:
        return Response(
            await actor.page.screenshot(),
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )


@app.post("/agents/{aid}/input")
async def user_input(aid: str, body: InputRequest):
    actor = await gateway.get(aid)
    async with actor.lock:
        with tx(aid) as c:
            a = require_agent(c, aid, True)
            if not one(
                c,
                "SELECT 1 FROM lead.takeover_sessions ts JOIN lead.browser_sessions bs ON bs.id=ts.browser_session_id JOIN lead.profile_leases l ON l.profile_id=ts.profile_id WHERE ts.state='active' AND ts.expires_at>now() AND ts.control_version=%s AND bs.id=%s AND bs.epoch=%s AND bs.ended_at IS NULL AND l.epoch=bs.epoch AND l.state='held' AND l.expires_at>now()",
                (a["control_version"], actor.session, actor.epoch),
            ):
                raise HTTPException(409, "接管已过期")
        if body.kind == "click":
            await actor.page.mouse.click(
                max(0, min(body.x, 1280)), max(0, min(body.y, 900))
            )
        elif body.kind == "text":
            await actor.page.keyboard.insert_text(body.text)
        elif body.kind == "key" and body.text in (
            "Enter",
            "Tab",
            "Backspace",
            "Escape",
            "ArrowDown",
            "ArrowUp",
            "ControlOrMeta+A",
        ):
            await actor.page.keyboard.press(body.text)
        elif body.kind == "scroll":
            await actor.page.mouse.wheel(0, max(-800, min(body.y, 800)))
        else:
            raise HTTPException(400, "不支持的输入")
        actor.touch()
        return {"ok": True}


@app.post("/agents/{aid}/close")
async def close(aid: str):
    await gateway.close(aid)
    return {"ok": True}


class BackupRequest(BaseModel):
    automated: bool = False


@app.post("/agents/{aid}/backup/begin")
async def begin_backup(aid: UUID, body: BackupRequest | None = None):
    from .backups import claim_profile_backup

    aid = str(aid)
    async with gateway.locks.setdefault(aid, asyncio.Lock()):
        actor = gateway.actors.get(aid)
        async with actor.lock if actor else nullcontext():
            guard_id = claim_profile_backup(
                aid, allow_running=body.automated if body else False
            )
            if actor:
                try:
                    await actor.close()
                finally:
                    gateway.actors.pop(aid, None)
                    gateway.slots.release()
    return {"guard_id": guard_id}


app.add_middleware(TelemetryMiddleware, service="lead-browser")
