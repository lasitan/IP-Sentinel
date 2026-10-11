#!/bin/bash
# 兼容占位：Go Agent 已内置调度，无需 cron 拉起业务脚本。
echo "ip-sentinel-agent 使用内置调度器；请确保 systemd/cron 看门狗仅守护 bin/ip-sentinel-agent。" >&2
exit 0
