# Chamber PC 执行器和工作台发布

`resident/` 保存专用执行器、模型适配器及设备端回归测试。定向发布客户端 `release.py` 使用已登记设备的 SSH 配置。可从仓库根目录运行 `PYTHONPATH=services/api:tests python -m pytest infra/chamber_pc/resident/test_target_operations.py` 复测执行器，数据库按仓库测试流程准备。

本地验证时，在 `apps/web` 运行 `npm run build`，然后从仓库根目录运行 `python3 infra/chamber_pc/workspace/policy_package.py`。`baselines.json` 保存安装器校验用的已知基线摘要。

正式交付按[开发到生产的发布顺序](../../docs/OPERATIONS.md#开发到生产的发布顺序)执行。从 GitHub 已验证的 SHA 运行 `python3 infra/chamber_pc/prepare_release.py "$SHA" --workspace`，再用输出目录中的模块和安装包部署。

`verify_patrol_window.py` 按实际巡群期限读取 PC 服务日志，生成生产验收结果。
