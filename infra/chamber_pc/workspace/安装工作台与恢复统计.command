#!/bin/bash
set -eu
cd "$(dirname "$0")"
echo '本次会在 PC 上更新工作台与 API，仅恢复 Chamber 报表；积压清空后接入双向 TLS 的专用 Temporal 队列。'
echo '若出现 sudo 提示，请在此终端输入家庭 PC 登录用户的管理员密码。'
exec /usr/bin/python3 activate.py
