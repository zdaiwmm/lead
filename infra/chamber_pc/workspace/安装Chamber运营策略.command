#!/bin/bash
set -eu
cd "$(dirname "$0")"
echo '本次更新 Chamber 工作台、群运营策略和专用统计队列。请仅在终端的 sudo 提示中输入 PC 管理员密码。'
exec /usr/bin/python3 activate.py --policy
