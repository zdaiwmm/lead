"""Product-owned event names. No third-party SDK or automatic form scraping."""

CATALOG = [
    ("CompleteRegistration", "注册成功", "账号创建成功", "user"),
    ("Activation", "有效使用", "完成已定义的核心行为", "activation"),
    ("Purchase", "支付成功", "订单支付确认成功", "order"),
    ("ViewContent", "浏览内容", "查看指定内容或产品详情", "event"),
    ("Lead", "提交线索", "成功提交意向表单", "event"),
    ("StartTrial", "开始试用", "试用成功开通", "event"),
    ("Subscribe", "付费订阅", "付费订阅成功开通", "event"),
    ("Contact", "联系商家", "发起电话、邮件或在线咨询", "event"),
    ("Search", "搜索", "执行站内搜索", "event"),
    ("AddToCart", "加入购物车", "商品成功加入购物车", "event"),
    ("AddToWishlist", "加入心愿单", "商品成功加入心愿单", "event"),
    ("InitiateCheckout", "开始结账", "开始结账流程", "event"),
    ("AddPaymentInfo", "添加支付信息", "成功添加支付方式", "event"),
    ("CustomizeProduct", "定制产品", "使用产品定制工具", "event"),
    ("Donate", "捐赠", "完成捐赠", "event"),
    ("FindLocation", "查找位置", "查找实体门店或位置", "event"),
    ("Schedule", "预约", "成功预约服务", "event"),
    ("SubmitApplication", "提交申请", "成功提交申请", "event"),
]


def catalog():
    return [
        {
            "name": name,
            "label": label,
            "when": when,
            "dedup": dedup,
            "custom": name == "Activation",
        }
        for name, label, when, dedup in CATALOG
    ]
