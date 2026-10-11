#!/bin/bash
set -euo pipefail
if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "需要 root"
  exit 1
fi
systemctl disable --now ip-sentinel-agent 2>/dev/null || true
if command -v apt-get >/dev/null 2>&1; then
  apt-get remove -y ip-sentinel-agent || dpkg -r ip-sentinel-agent || true
else
  dpkg -r ip-sentinel-agent || true
fi
echo "节点软件包已移除。配置仍可能保留在 /etc/ip-sentinel/ ，数据在 /var/lib/ip-sentinel/ 。"
echo "如需彻底清理: rm -rf /etc/ip-sentinel /var/lib/ip-sentinel /var/log/ip-sentinel"
