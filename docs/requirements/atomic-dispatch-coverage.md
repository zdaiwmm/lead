# 动作入口与执行归属

按 atomic-dispatch.md 的编号对照 GitHub main。入口为 services/api/lead_app 下的模块和函数。一个函数可以持有多个相邻步骤。下表标出当前可以安全交付的边界；独立调度开关只覆盖已实现并验收的边界。原设计中的选择分支、产物版本、冷却、授权、业务键和资源约束仍适用。

| 动作 | 当前入口 | 执行归属与准入 |
|---|---|---|
| A1 | activities.chat_job | 消息序列，解释指令，按需调用模型 |
| A2 | domain.control_in_tx | 控制事务，锁定 Agent 后更新版本 |
| A3 | domain.update_preferences | 偏好版本与消息顺序 |
| A4 | website_brief.analyze | 来源读取与业务资料草案 |
| A5 | domain._save_confirmed_brief | 用户确认后保存 |
| A6 | activities.chat_job | 当前消息回执 |
| B1 | identity.check | 登录会话，校验现有身份 |
| B2 | account_setup.recover_saved_login | 登录会话，要求既有绑定 |
| B3 | channels.probe_in_tx | 能力核验及持久化 |
| B4 | account_setup.prepare_accounts | 关联现有目标的筹备流程 |
| B5 | account_setup.prepare_requested_channel | 有范围的筹建委托，外部动作 |
| B6 | channels.store_target | 当前目标身份与核实权限 |
| B7 | owned_audience.inspect | 登录会话，只读受众 |
| B8 | community_runtime.update_own_bio | 登录会话，资料修改授权 |
| C1 | operating_work.execute | 查询计划，可复用已有草案 |
| C2 | operating_work.open_research_index | 来源和实际浏览器 Actor |
| C3 | operating_work.execute | 试点可先保存 journey / validate 原文 |
| C4 | operating_work.passages | 与观察绑定的原文片段，结论由报告步骤产出 |
| C5 | operating_work.execute | 先验证入口来源，再评估适配 |
| C6 | operating_work.execute | 产品访问路径和证据 |
| C7 | operating_work.execute | 依赖已保存原文及模型 |
| D1 | community_discovery.advance | 检索批次和请求版本 |
| D2 | community_discovery.advance | 公开目录与游标 |
| D3 | community_discovery.advance | 登录态 Actor 和账号搜索冷却 |
| D4 | community_discovery.queue_repository_entries | 归一化仓库来源入口，其他来源随当前适配器 |
| D5 | community_discovery.observation_shortlist | 规则和来源筛选仍归发现任务 |
| D6 | community_discovery.advance | 单批评估，适配器决定本地或模型 |
| D7 | community_discovery.advance | 绑定原批次的去重候选 |
| E1 | community_runtime.observe_group | 登录群身份及最近上下文 |
| E2 | community_runtime.community_cycle | 群规在前，机会判定在后 |
| E3 | community_runtime.browser_step | 登录会话、群身份、委托及原提交意图 |
| E4 | community_runtime.join_receipt | 同一入群意图只读核验 |
| E5 | community_schedule.next_pending_inventory | 到期审批清单及原执行器 |
| E6 | community_runtime.community_cycle | 原消息、委托及内容草案 |
| E7 | community_runtime.check_draft | 核对稿件、证据与授权 |
| E8 | community_runtime.browser_step | 核对原意图后单次提交 |
| E9 | community_runtime.own_reply_receipt | 原目标及已发消息的回读 |
| E10 | community_runtime.membership_review | 当前账号、目标和复查时间窗 |
| E11 | community_runtime.leave_restricted | 原目标、退出许可及回执 |
| E12 | community_runtime.correct_voice | 原消息及编辑回执 |
| F1 | telegram_inbox.cycle | 现有账号的会话扫描 |
| F2 | telegram_inbox.browser_step | 登录会话，读取当前线程增量 |
| F3 | telegram_inbox.cycle | 绑定对方与最新消息 |
| F4 | telegram_inbox.cycle | 草稿和原消息版本 |
| F5 | telegram_inbox.browser_step | 锁内核对原线程并提交 |
| F6 | telegram_inbox.browser_step | 同一会话回执 |
| G1 | atomic_dispatch.execute_channel | 首批独立后台位，按平台和资料版本去重 |
| G2 | content_backlog.reserve | 内容需求预留 |
| G3 | content_backlog.prepare_one | 文案和实际模型预留 |
| G4 | creative_pipeline.produce | 对应内容版本的素材需求 |
| G5 | creative_worker.render | 渲染进程和机器容量 |
| G6 | creative_pipeline.produce | 核验并登记素材 |
| G7 | content_backlog.prepare_one | 组装已引用的文案与素材 |
| H1 | channel_calendar.gate | 目标和排期窗口 |
| H2 | activities._publication_cycle | 已核验目标、稿件和位置 |
| H3 | promotion.tracking_url | 本次位置使用的归因链接 |
| H4 | activities.prepare_action | 权限、控制版本和稳定意图 |
| H5 | activities.execute_task | 已登记的发布动作 |
| H6 | publication_delivery.review_actor | 原平台结果只读核对 |
| H7 | publication_delivery.persist | 已核对发布的收尾记录 |
| I1 | analytics_ingest._admit | 独立采集入口，原事件键 |
| I2 | analytics_ingest.process_ingest | 原事件领取、去重和校验 |
| I3 | analytics_ingest.process_ingest | 已冻结的事件归因 |
| I4 | analytics_reporting.refresh_scope | 试点可与事件投递并行，按就绪时间领取已提交的待汇总日期 |
| I5 | pixel_events.setup_state | 检查测量配置和证据缺口 |
| I6 | analytics_reporting.read_report | 指定作用域和成熟统计窗口 |
| J1 | operating_work.strategy_report | 业务与研究证据、模型 |
| J2 | growth_experiments.materialize | 假设、指标与实验身份 |
| J3 | growth_experiments.first_task | 绑定实验的下一项工作 |
| J4 | growth_experiments.review_trial | 当前实验及成熟窗口 |
| J5 | growth_experiments.advance | 保存继续、调整或结束决策 |
| J6 | operating_feedback.after_task | 新证据和幂等后继任务 |
| K1 | worker.deliver_workspace | outbox 唤醒和 Temporal 投递 |
| K2 | analytics_ingest._claim | 采集实例的租约；其他动作沿各自领取入口 |
| K3 | db.emit | 领域事务内记录后继事件 |
| K4 | analytics_delivery.reconcile | 采集投递代数修复；其他任务沿各自恢复入口 |
| K5 | publication_recovery.request | 未知发布意图的回读；群和私信仍由原账号持有者对账 |
| K6 | worker.model_became_available | 恢复唤醒再核验；账号与控制按原领域入口 |

当前没有通用的 78 类工作领取器。个人账号的 B/D/E/F/H 操作需要原 Actor、授权与稳定回执；素材和策略有各自的执行与预算边界。表中同一函数内的步骤在通过各自的中断、互斥、幂等和混合工作集验证前，沿原所有者执行。
