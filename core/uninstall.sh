#!/bin/bash
set -euo pipefail
if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "需要 root"
  exit 1
fi
systemctl disable --now ip-sentinel-agent 2>/dev/null || true
apt-get remove -y ip-sentinel-agent 2>/dev/null || dpkg -r ip-sentinel-agent || true
echo "节点软件包已移除。"
echo "彻底清理: rm -rf /etc/ip-sentinel /var/lib/ip-sentinel /var/log/ip-sentinel"
