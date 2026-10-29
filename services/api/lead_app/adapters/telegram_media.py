from .. import telegram
from .catalog import SPECS, validate
from .core import (
    ScopeChanged,
    click,
    fill,
    first_visible,
    verify_fields,
    wait_ready,
)


class TelegramMediaAdapter:
    spec = SPECS["telegram"]

    async def account(self, page):
        return await telegram.inspect_account(page)

    async def target(self, page, url, expected_account):
        return await telegram.inspect_target(page, url, expected_account)

    async def prepare(self, page, target, publication, media, paths):
        kind = validate("telegram", publication, media)
        if kind == "text":
            raise ValueError("文字发布使用文字账本适配器")
        own = await self.account(page)
        checked = await self.target(
            page, target["canonical_url"], own["external_account_id"]
        )
        if checked["external_target_id"] != target["external_target_id"]:
            raise ScopeChanged("当前频道与授权目标不一致")
        baseline = await telegram.baseline(page)
        await click(page, (".btn-attach", 'button[aria-label="Attach"]'))
        items = (
            '.btn-menu-item:has-text("Photo or Video")',
            '[role=menuitem]:has-text("Photo or Video")',
        )
        async with page.expect_file_chooser(timeout=15000) as chooser:
            await click(page, items)
        await (await chooser.value).set_files([str(p) for p in paths])
        await first_visible(page, (".popup-new-media", ".popup-send-photo"))
        await fill(
            page,
            (
                ".popup-new-media .input-message-input[contenteditable=true]",
                ".popup-send-photo .input-message-input[contenteditable=true]",
            ),
            publication["body"],
        )
        await wait_ready(page)
        final = (
            ".popup-new-media .btn-send",
            ".popup-new-media .btn-primary",
            ".popup-send-photo .btn-send",
        )
        button = await first_visible(page, final)
        if not await button.is_enabled():
            raise ValueError("媒体还未准备好")
        return {
            "kind": kind,
            "fields": [
                {
                    "selectors": [
                        ".popup-new-media .input-message-input[contenteditable=true]",
                        ".popup-send-photo .input-message-input[contenteditable=true]",
                    ],
                    "value": publication["body"],
                }
            ],
            "baseline_mid": baseline,
            "target_id": target["external_target_id"],
            "account_id": own["external_account_id"],
            "final_selectors": list(final),
            "prepared_url": page.url,
        }

    async def before_commit(self, page, target, observation, expected_account):
        own = await self.account(page)
        current = await page.evaluate(telegram.TARGET)
        if (
            own["external_account_id"] != expected_account
            or current["target_id"] != target["external_target_id"]
        ):
            raise ScopeChanged("媒体提交前账号或频道发生变化")
        await first_visible(page, observation["final_selectors"])
        await verify_fields(page, observation["fields"])

    async def submit(self, page, observation):
        await click(page, observation["final_selectors"])

    async def receipt(self, page, target, publication, observation, expected_account):
        own = await self.account(page)
        if own["external_account_id"] != expected_account:
            raise ScopeChanged("当前账号已经改变")
        result = await telegram.receipt(
            page,
            publication["body"],
            observation["baseline_mid"],
            publication["marker"],
        )
        return (
            {"external_id": target["external_target_id"] + ":" + result["mid"]}
            if result
            else None
        )

    async def reconcile(self, page, target, publication, observation, expected_account):
        checked = await self.target(page, target["canonical_url"], expected_account)
        if checked["external_target_id"] != target["external_target_id"]:
            raise ScopeChanged("核对目标发生变化")
        return await self.receipt(
            page, target, publication, observation, expected_account
        )
