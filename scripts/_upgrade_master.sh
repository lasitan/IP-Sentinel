#!/bin/bash
set -eu
VER=5.0.9
ARCH=$(dpkg --print-architecture)
DEB="ip-sentinel-master_${VER}_${ARCH}.deb"
URL="https://github.com/lasitan/IP-Sentinel/releases/download/v${VER}/${DEB}"
cd /tmp
curl -fsSL -o "$DEB" "$URL"
dpkg -i "./$DEB"
rm -f "./$DEB"
systemctl restart ip-sentinel-master
sleep 2
dpkg -l ip-sentinel-master | tail -1
grep MASTER_VERSION /etc/ip-sentinel-master/master.conf || true
journalctl -u ip-sentinel-master -n 8 --no-pager
