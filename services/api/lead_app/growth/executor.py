"""A bound browser serves finite tool requests; it never chooses business work."""
import asyncio
from uuid import UUID

from .contracts import Contract


class NativeReceiptRequest(Contract):
    task_id: UUID


def native_receipt_guard(actor,c,task,reading_receipt=True):
    """Retired business guards do not gate factual reconciliation on project state."""
    from ..db import one
    current = one(c,"SELECT * FROM lead.tasks WHERE id=%s",(task["id"],))
    if (not current or current["type"]!="community.engage"
        or current["input"].get("migration",{}).get("owner")!="receipt_only"
        or str(current["input"].get("profile_id"))!=str(actor.profile["id"])):
        raise ValueError("original_receipt_scope")
    account = one(c,"""SELECT * FROM lead.social_accounts WHERE id::text=%s AND profile_id=%s
        AND external_account_id=%s""",(current["input"].get("account_id") or current["input"]["migration"].get("original_account_id"),
            actor.profile["id"],current["input"].get("external_account_id")))
    if not account:
        raise ValueError("original_account_unavailable")
    return account,None


async def read_original_native(actor,task_id,reconcile):
    from ..db import one, tx
    async with actor.lock:
        with tx(actor.aid) as c:
            task = one(c,"SELECT * FROM lead.tasks WHERE id=%s",(task_id,))
            if not task:
                raise ValueError("original_receipt_scope")
            account,_ = native_receipt_guard(actor,c,task)
        # Only the persisted peer, original text and original session identity are
        # read. No discovery, joining, replying, copywriting or scheduling enters.
        return await reconcile(actor,task,account,(task["result"] or {}).get("target_id"),40)


async def reconcile_native(aid,task_id,profile_id,invoke):
    from .. import browser
    from ..db import one, tx
    with tx(aid) as c:
        task = one(c,"SELECT * FROM lead.tasks WHERE id=%s",(task_id,))
        if (not task or task["type"]!="community.engage"
            or task["input"].get("migration",{}).get("owner")!="receipt_only"
            or str(task["input"].get("profile_id"))!=str(profile_id)):
            raise ValueError("original_receipt_scope")
        if one(c,"""SELECT 1 FROM lead.takeover_sessions WHERE profile_id=%s
            AND state IN ('active','requested','ending') AND expires_at>now()""",(profile_id,)):
            return {"state":"unknown","reason":"owner_controls_original_profile"}
    actor = await browser.gateway.get(aid,profile_id)
    observed = await asyncio.wait_for(invoke(actor,str(task_id)),120)
    with tx(aid) as c:
        actual = one(c,"SELECT result FROM lead.tasks WHERE id=%s",(task_id,))["result"] or {}
    if observed.get("state")=="confirmed":
        message = actual.get("reply_state")=="confirmed" and actual.get("reply_confirmed_at") and actual.get("reply_external_id")
        joined = actual.get("join_state")=="confirmed" and actual.get("join_confirmed_at")
        if not (message or joined) or (message and observed.get("external_id")!=actual.get("reply_external_id")):
            return {"state":"unknown","reason":"original_receipt_not_persisted"}
    if observed.get("state")=="failed" and actual.get("reply_state")!="failed":
        return {"state":"unknown","reason":"original_failure_not_persisted"}
    return observed


async def serve_browser(binding, *, port, telegram_primitives=None,native_receipt_reader=None):
    import uvicorn

    from .. import browser, executor_scope
    from ..account_profiles import directory
    from ..db import one, tx
    from ..identity_store import agent_tenant
    from ..scope import service_scope, tenant_scope

    if binding.channel!="telegram" or type(port) is not int or port!=18092:
        raise ValueError("unsupported_bound_browser")
    with service_scope(),tenant_scope(agent_tenant(binding.agent_id),None):
        with tx(binding.agent_id) as c:
            profile = one(c,"SELECT * FROM lead.browser_profiles WHERE slot=%s",(binding.profile_slot,))
        if not profile:
            raise ValueError("bound_profile_missing")
        folder = directory(binding.agent_id,profile)
        if not (folder/"identity.json").is_file() or not (folder/"storage").is_dir():
            raise ValueError("bound_profile_storage_missing")
        if telegram_primitives is not None:
            # Only the already attested private browser primitives are retained.
            # Resident selection and business-policy modules are never invoked.
            from .. import telegram
            from ..adapters import telegram_media
            telegram_primitives.TelegramNeedsAuth = telegram.TelegramNeedsAuth
            telegram_primitives.TelegramTargetMismatch = telegram.TelegramTargetMismatch
            browser.telegram = telegram_primitives
            telegram_media.telegram = telegram_primitives
        if native_receipt_reader:
            async def native_receipt(aid:UUID,body:NativeReceiptRequest):
                if str(aid)!=binding.agent_id:
                    raise ValueError("executor_agent_scope")
                return await reconcile_native(str(aid),body.task_id,profile["id"],native_receipt_reader)
            browser.app.add_api_route("/agents/{aid}/legacy-native/receipt",native_receipt,methods=["POST"])

        with executor_scope.profile(binding.agent_id,profile["id"]):
            server = uvicorn.Server(uvicorn.Config(browser.app,host="127.0.0.1",port=port,
                access_log=False,log_level="error",timeout_graceful_shutdown=5))
            try:
                await server.serve()
            finally:
                await browser.gateway.close(binding.agent_id)
                await asyncio.sleep(0)
