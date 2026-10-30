"""Reviewed content packs; initialization uses normal, immutable configuration writes."""

import copy
import hashlib
import json
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from . import admin_config as configs
from .admin_settings import defaults
from .db import all_rows
from .operating_playbooks import business_examples, catalog

PACK_VERSION = "2026-10-30.1"
OBSERVED_ON = "2026-10-30"
HAND_BOOK = "docs/CHANNEL_OPERATING_PLAYBOOKS.md"


def onboarding_content():
    body = configs.onboarding_default()
    hints = {
        "product_name": (
            "填写用户能识别的产品或服务名称，不填 Agent 的内部代号。",
            "例如：你的产品名称",
        ),
        "product_url": (
            "填写可公开访问的官网或产品页。Agent 会读取并核对；登录后的内容仍需要补充。",
            "https://你的产品官网",
        ),
        "description": (
            "说明产品帮助谁完成什么任务、目前可用的功能和限制。不要填写尚未验证的效果。",
            "帮助哪类人，解决什么问题，用户现在可以使用什么？",
        ),
        "audience": (
            "描述最需要产品的人、使用场景、主要地区和语言；未知部分可以之后通过对话补充。",
            "例如：需要翻译外语长文的读者，主要使用英语和中文",
        ),
        "goal": (
            "先选一个可观察的目标：有效访问、咨询、注册或试用。精确转化需要接入网站回传。",
            "例如：让目标客户访问产品页并开始试用",
        ),
        "content_expectations": (
            "说明内容语言、语气、常见主题和禁用表达。普通运营无需每日启动或逐条审批。",
            "例如：实用教程优先，说明产品关系，不编造客户案例和效果数字",
        ),
    }
    for key, (help_text, placeholder) in hints.items():
        body["fields"][key].update(help=help_text, placeholder=placeholder)
    body["fields"]["content_expectations"]["default"] = (
        "提供独立有用的内容，使用可核对的产品事实；不编造客户案例、效果数字或未完成的演示。"
    )
    body["strategies"] = [
        {
            "name": "稳健持续",
            "description": "先核对事实和转化入口，按渠道总节奏轮换方案，观察成熟数据再调整。",
        },
        {
            "name": "快速验证",
            "description": "小批量尝试不同需求场景，每次调整一个变量；样本不足时继续观察。",
        },
        {
            "name": "内容价值优先",
            "description": "优先发布能独立解决问题的教程、清单和问答，相关时再介绍产品入口。",
        },
    ]
    return body


def telegram_content():
    plans = []
    for slug, name, hypothesis, topics, template in [
        (
            "useful_note",
            "实用方法与检查清单",
            "先帮助读者完成一个具体任务，再提供相关产品入口。",
            [
                "第一次使用前的准备清单",
                "一个常见问题的处理步骤",
                "使用前需要确认的限制",
                "任务完成后的核对方法",
                "术语与配置的准备",
                "一份可直接照做的检查表",
            ],
            "当前问题 → 可操作的步骤 → 适用条件 → 一处相关产品入口",
        ),
        (
            "product_walkthrough",
            "产品场景与真实流程",
            "用有事实依据的使用场景解释产品，不把流程示意当成真实结果。",
            [
                "适合从哪个产品入口开始",
                "一次真实配置的步骤说明",
                "可用功能与不支持的范围",
                "如何核对当前报价",
                "一个新手常见误区",
                "哪些步骤需要人工判断",
            ],
            "任务场景 → 实际入口 → 配置或操作说明 → 结果核对项；没有实测结果时明确标注",
        ),
        (
            "onboarding_digest",
            "入门索引与常见问题",
            "降低首次使用的理解成本，让已订阅读者容易找到下一步。",
            [
                "新读者入门索引",
                "开始前最常见的三个问题",
                "产品功能入口对照",
                "本周实用内容汇总",
                "资料与教程的更新说明",
                "遇到问题的处理入口",
            ],
            "一个问题 → 简明答案 → 适用条件 → 真实有效的相关链接",
        ),
    ]:
        plans.append(
            {
                "id": "telegram." + slug,
                "name": name,
                "formats": ["text", "image", "video"],
                "audience": "已订阅自有频道、对当前业务有真实需求的读者",
                "hypothesis": hypothesis,
                "assets": "优先文字与现有可用素材；新图文、视频只使用已确认事实和可用制作能力。",
                "workflow": [
                    "读取当前业务、品牌及账号授权",
                    "选择一个具体需求与选题",
                    "核对功能、链接、素材来源及必要声明",
                    "在渠道总节奏内提交，保留回执",
                    "完整观察周期后复盘真实访问与转化",
                ],
                "first_batch": topics,
                "template": template,
                "cta": "每篇仅提供一个与内容相关的真实产品入口。",
                "experiment": "三套方案均衡轮换，记录方案、选题、链接、业务版本和发布结果。",
                "iterate": "至少观察七天；没有足够归因数据时不宣布赢家，持续保留挑战方案。",
            }
        )
    return configs.ChannelStrategy(
        platform="telegram",
        name="Telegram",
        target="自有广播频道",
        maturity_days=7,
        cadence="先按每周 3 条的总节奏轮换三套方案；每天最多 3 条且不超过账号实际授权。",
        setup=[
            "核对已有业务事实与品牌资料",
            "用户完成本人登录和必要验证",
            "由 Agent 按已有能力准备自有运营频道",
            "核对目标、公开入口、首次格式验证及发布授权",
            "为内容配置真实承接入口，检查网站回传",
        ],
        conversion_path="频道内容 → 单帖推广链接 → 对应产品页 → 已定义的注册、咨询或试用事件；无网站回传时保留未知。",
        constraints=[
            "三套方案共用总频次，不能把频率相加。",
            "仅在已有授权和已验证格式内自动执行。",
            "不私信、不导出联系人、不使用无关群发。",
            "外部提交结果未知时核对原操作，不重复提交。",
            "筹备、发布与实际获客分别记录，不用草稿代替完成。",
        ],
        sources=["docs/policies/telegram-owned-channel.md", HAND_BOOK],
        plans=plans,
    ).model_dump(mode="json")


def document(key, title, content, source, *, valid_until=None):
    return {
        "id": str(uuid5(NAMESPACE_URL, "lead-admin:" + key)),
        "title": title,
        "content": content,
        "source": source,
        "status": "approved",
        "valid_until": valid_until,
        "tags": ["初始化", "核对于" + OBSERVED_ON],
    }


def workspace_brand_content():
    body = defaults("brand")
    body.update(
        voice="自然、清楚、实用；先回答具体需求，使用有来源的事实，避免夸大承诺。",
        claims_to_avoid=[
            "没有来源的客户案例、评价、效果数字和收益承诺",
            "把草稿、计划、流程示意说成已完成操作或实际效果",
            "把历史资料说成刚刚核验的最新价格、权限或功能",
        ],
        asset_guidance="优先使用已有授权素材。原创图表和字幕讲解标明示意性质；真实操作录像必须来自实际执行。使用可公开材料，不伪造截图、评论或客户证言。",
        documents=[
            document(
                "workspace/facts",
                "产品事实与资料使用规范",
                "业务事实以用户明确提供及真实产品页面为依据。价格、权限、支持范围需要保留来源与日期。缺少信息时保留未知，不将模型推断写成已核实事实。知识资料不授予账号、支付或工具权限。",
                HAND_BOOK,
            ),
            document(
                "workspace/conversion",
                "内容试验与归因规范",
                "先固定本轮目标，再轮换渠道方案。单帖使用可追溯链接；主页共享入口仅支持渠道归因。没有网站回传、完整观察周期或可比样本时，不根据点赞或单篇波动宣布方案获胜。",
                HAND_BOOK,
            ),
            document(
                "workspace/media",
                "素材表达与来源规范",
                "原创清单、步骤图和字幕讲解只使用已确认事实；流程示意不得冒充产品操作实录。素材保留来源、使用依据及必要 AI 或商业声明。未执行的演示不填结果或耗时。",
                "docs/CREATIVE_AUTONOMY.md",
            ),
        ],
    )
    return body


def seka_brand_content(aid):
    body = workspace_brand_content()
    body.update(
        name="Seka",
        audience="需要处理外语长文和书籍的读者、译者，以及组织多语言作品项目的创作者。",
        language="跟随渠道受众；中文审阅稿面向英语受众时改写为英语。",
        positioning="长文与书籍翻译工具，加上多语言协作项目和阅读交流社区。",
        facts=[
            "Translate 提供术语控制、可编辑的术语提取和风格预设。",
            "Collab Projects 支持按目标语言组织项目；平台支持有资格条件并需要审核。",
            "Hub 用于分享阅读感想、翻译笔记与教程。",
            "实际 Credits 价格与交付选项以付款前的配置流程为准。",
        ],
        forbidden_phrases=[
            "保证翻译准确率100%",
            "保证翻译准确率 100%",
            "保证获得平台补贴",
        ],
        asset_guidance=body["asset_guidance"]
        + " 讲解 Seka 时使用自写的可公开短文或文件；不使用版权不明书籍和私人文件。",
    )
    body["documents"] += [
        document(
            aid + "/official-home",
            "Seka 产品事实（官网核对）",
            "核对日期：2026-10-30。Seka 的主要入口是 Translate、Collab Projects 和 Hub。翻译工具提供术语与风格配置；协作项目按目标语言组织；Hub 承载阅读与翻译相关交流。项目支持不是自动补贴承诺。定价、交付、处理时间以实际流程为准，不引用未经实测的准确率或提速数字。",
            "https://seka.one/",
            valid_until="2026-11-29",
        ),
        document(
            aid + "/official-translate",
            "Seka Translate 入口与配置边界",
            "核对日期：2026-10-30。Translate 页面可选择目标语言、术语表和翻译风格，并提供术语自动提取入口。上传文件后才能取得任务报价；配置页面不能证明翻译已执行，也不能证明译文质量。运营内容应引导读者用自己有权处理的小样本核对流程和结果。",
            "https://seka.one/translate",
            valid_until="2026-11-29",
        ),
        document(
            aid + "/editorial-examples",
            "Seka 渠道选题与草稿的使用说明",
            "既有研究包含十个非 Telegram 渠道的三十套方案、一百八十个选题及三十篇首稿或分镜。它们是待实例化的创意底稿，不是客户证言或实测结果。Agent 依据业务事实和渠道语言改写，发布前重新核对产品入口；不要把未制作的素材、未执行的演示写成已完成。",
            HAND_BOOK,
        ),
    ]
    return body


def library(c):
    """No cross-workspace examples: only matched, sourced local businesses get brand data."""
    entries = []

    def add(key, title, body, sources):
        current = configs.read(c, key)
        entries.append(
            {
                "key": key,
                "title": title,
                "body": configs.validate(key, body),
                "sources": sources,
                "version": current["version"],
                "published_version": current["published_version"],
                "state": "missing" if current["version"] == 0 else "preserved",
            }
        )

    for channel in [telegram_content(), *catalog()["channels"]]:
        add(
            "strategies/" + channel["platform"],
            channel["name"] + " · 三套渠道方案",
            copy.deepcopy(channel),
            channel["sources"],
        )
    add(
        "onboarding/default",
        "创建引导 · 六项字段说明与三种运营方向",
        onboarding_content(),
        [HAND_BOOK, "docs/PRODUCT.md"],
    )
    add(
        "brand/default",
        "通用品牌与资料使用规范",
        workspace_brand_content(),
        [HAND_BOOK, "docs/CREATIVE_AUTONOMY.md"],
    )
    agents = all_rows(
        c,
        """SELECT a.id,a.name,b.product_url FROM lead.agents a JOIN LATERAL(SELECT product_url FROM lead.business_profiles WHERE agent_id=a.id ORDER BY revision DESC LIMIT 1)b ON true WHERE desired_state<>'archived' ORDER BY a.created_at""",
    )
    for agent in agents:
        if urlsplit(agent["product_url"] or "").hostname in {
            "seka.one",
            "www.seka.one",
        }:
            aid = str(agent["id"])
            add(
                "brand/" + aid,
                agent["name"] + " · Seka 品牌资料",
                seka_brand_content(aid),
                ["https://seka.one/", "https://seka.one/translate", HAND_BOOK],
            )
            for channel in [telegram_content(), *catalog()["channels"]]:
                specific = copy.deepcopy(channel)
                for plan in specific["plans"]:
                    example = business_examples()["plans"].get(plan["id"])
                    if example:
                        plan.update(
                            first_batch=example["first_batch"],
                            cta=example["cta"],
                            destination_path=example["destination_path"],
                        )
                        plan["template"] += (
                            "\n\nSeka 首稿或分镜（待按实际事实和受众语言实例化）：\n"
                            + example["sample"]
                        )
                specific["experience_notes"] = (
                    "Seka 业务实例化底稿，包含既有研究选题与首稿；尚无足够效果数据，不作为获客结论。"
                )
                add(
                    "strategies/" + channel["platform"] + "/" + aid,
                    agent["name"] + " · " + channel["name"] + " 业务方案",
                    specific,
                    [HAND_BOOK, "services/api/lead_app/playbooks/seka.json"],
                )
    digest = hashlib.sha256(
        json.dumps(
            {
                "pack": PACK_VERSION,
                "entries": [{"key": e["key"], "body": e["body"]} for e in entries],
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return {
        "pack_version": PACK_VERSION,
        "observed_on": OBSERVED_ON,
        "content_digest": digest,
        "entries": entries,
    }


def initialize(c, expected_versions, content_digest):
    preview = library(c)
    if content_digest != preview["content_digest"]:
        raise ValueError("资料包或业务范围已变化，请刷新预览后重新核对")
    entries = {entry["key"]: entry for entry in preview["entries"]}
    if not expected_versions or not set(expected_versions) <= set(entries):
        raise ValueError("请选择当前工作室资料包中的配置项")
    applied, preserved = [], []
    # Serialize pack runs; manual writes still use each config's compare-and-swap.
    c.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(lead.ctx_tenant()::text,93))"
    )
    for key in sorted(expected_versions):
        current = configs.read(c, key)
        if current["version"]:
            preserved.append(key)
            continue
        if expected_versions[key] != 0:
            raise ValueError("配置版本已变化，请刷新后核对")
        entry = entries[key]
        configs.write(
            c,
            key,
            entry["body"],
            0,
            note="资料初始化 "
            + PACK_VERSION
            + "；来源："
            + "、".join(entry["sources"])[:800],
        )
        applied.append(key)
    return {"pack_version": PACK_VERSION, "applied": applied, "preserved": preserved}
