#!/bin/bash
set -euo pipefail
if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "需要 root"
  exit 1
fi
systemctl disable --now ip-sentinel-master 2>/dev/null || true
apt-get remove -y ip-sentinel-master 2>/dev/null || dpkg -r ip-sentinel-master || true
echo "控制端软件包已移除。"
echo "彻底清理配置/数据: rm -rf /etc/ip-sentinel-master /var/lib/ip-sentinel-master"
