# Chamber PC 执行器和工作台发布

`resident/` 保存专用执行器、模型适配器及设备端回归测试。定向发布客户端 `release.py` 使用已登记设备的 SSH 配置。可从仓库根目录运行 `PYTHONPATH=services/api:tests python -m pytest infra/chamber_pc/resident/test_target_operations.py` 复测执行器，数据库按仓库测试流程准备。

工作台构建使用 `apps/web` 的 `npm run build`。随后在仓库根目录运行 `python3 infra/chamber_pc/workspace/policy_package.py`，会在打包脚本旁生成运营策略增量包和校验值。`baselines.json` 保存安装器校验用的已知基线摘要。系统级安装沿用 PC 管理员入口。

`verify_patrol_window.py` 按实际巡群期限读取 PC 服务日志，生成生产验收结果。
