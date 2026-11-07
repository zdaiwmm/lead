export const commonSections = [
  {
    id: "overview",
    label: "总览",
    description: "先看运行状态，再处理需要你补充的信息。",
  },
  {
    id: "agents",
    label: "Agent 管理",
    description: "查找业务、管理账号，或暂停与恢复运营。",
  },
  {
    id: "business",
    label: "业务与运营",
    description: "告诉 Agent 推广什么、怎样表达，以及运营边界。",
  },
  {
    id: "costs",
    label: "用量与费用",
    description: "查看实际消耗、费用估算和可用额度。",
  },
];

export const businessSections = [
  {
    id: "brand",
    label: "品牌与知识",
    description: "维护产品事实、表达方式和有来源的资料。",
  },
  {
    id: "strategies",
    label: "内容与节奏",
    description: "调整各渠道的内容方向和运营节奏。",
  },
  {
    id: "channel-rules",
    label: "发布边界",
    description: "设置渠道允许做什么，以及发布频率上限。",
  },
];
export const costSections = [
  {
    id: "usage",
    label: "用量与费用",
    description: "费用来自实际调用；缺少计价时保留未知。",
  },
  {
    id: "budgets",
    label: "额度管理",
    description: "调整每日 token 额度，已发生的消耗继续保留。",
  },
];
export const settingsGroups = [
  {
    label: "模型服务",
    items: [
      {
        id: "models",
        label: "模型与工具",
        description: "选择默认模型；按任务选模型和调用参数按需展开。",
      },
      {
        id: "model-catalog",
        label: "可选模型",
        description: "维护模型选择列表。添加名称不会自动接通服务。",
      },
      {
        id: "pricing",
        label: "模型定价",
        description: "维护费用估算的单价与来源，历史调用保留原单价。",
      },
    ],
  },
  {
    label: "运行与提醒",
    items: [
      {
        id: "runtime",
        label: "工作时段与异常恢复",
        description: "需要限制运营时间、并发或重试时再调整。",
      },
      {
        id: "notifications",
        label: "站内提醒",
        description: "选择提醒事项、接收人和免打扰时段。",
      },
      {
        id: "external-notifications",
        label: "邮件与 Webhook",
        description: "需要在产品外接收提醒时配置，默认关闭。",
      },
    ],
  },
  {
    label: "数据与成员",
    items: [
      {
        id: "attribution",
        label: "数据与归因",
        description: "自定义转化目标、归因窗口和数据有效期。",
      },
      {
        id: "members",
        label: "成员权限",
        description: "多人协作时，分配功能权限和可访问的 Agent。",
      },
    ],
  },
  {
    label: "模板与记录",
    items: [
      {
        id: "content",
        label: "资料初始化",
        description: "首次配置时预览已有研究，再选择要采用的内容。",
      },
      {
        id: "onboarding",
        label: "创建引导",
        description: "需要定制新建 Agent 的表单时再调整。",
      },
      {
        id: "experiences",
        label: "经验审核",
        description: "审核有依据的运营观察，再决定是否纳入策略。",
      },
      {
        id: "history",
        label: "变更记录",
        description: "查阅配置版本、修改人和前后差异。",
      },
    ],
  },
];
export const settingsSections = settingsGroups.flatMap((group) => group.items);
export const sections = [
  ...commonSections,
  ...businessSections,
  ...costSections,
  ...settingsSections,
  {
    id: "settings",
    label: "高级设置",
    description: "低频配置集中在这里。已有默认值可以继续使用，按需要调整。",
  },
  {
    id: "readiness",
    label: "配置检查明细",
    description: "逐项核对资料、模型服务、账号授权和数据接入。",
  },
];

export function resolveSection(value: string) {
  if (value === "business") return "brand";
  if (value === "costs") return "usage";
  return sections.some((item) => item.id === value) ? value : "overview";
}
export function sectionGroup(section: string) {
  if (businessSections.some((item) => item.id === section)) return "business";
  if (costSections.some((item) => item.id === section)) return "costs";
  if (settingsSections.some((item) => item.id === section)) return "settings";
  return section === "readiness" ? "overview" : section;
}
export type Navigate = (section: string, agentId?: string) => void;
