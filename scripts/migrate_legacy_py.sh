#!/bin/bash
# Python → Go：仅信任 TG_TOKEN / CHAT_ID / REGION_CODE，其余强制重配。
# 禁止由 PUBLIC_IP / MASTER_PUBLIC_IP 合成 https://IP；不迁移自签证书。

migrate_conf_key_set() {
  local file="$1" key="$2" val="$3"
  local line="${key}=\"${val}\""
  if [ ! -f "$file" ]; then
    echo "$line" >> "$file"
    return
  fi
  if grep -qE "^[[:space:]]*${key}=" "$file"; then
    sed -i -E "s|^[[:space:]]*${key}=.*|${line}|" "$file"
  else
    echo "$line" >> "$file"
  fi
}

migrate_conf_get() {
  local file="$1" key="$2" line
  [ -f "$file" ] || return 0
  line="$(grep -E "^[[:space:]]*${key}=" "$file" | head -n1 || true)"
  [ -n "$line" ] || return 0
  printf '%s\n' "$line" | sed -E "s/^[[:space:]]*${key}=//" | sed -E 's/\r$//; s/^[[:space:]]+//; s/[[:space:]]+$//; s/^"//; s/"$//; s/^'\''//; s/'\''$//'
}

stop_unit_if_any() {
  local name="$1"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl stop "$name" 2>/dev/null || true
    systemctl disable "$name" 2>/dev/null || true
  fi
}

purge_playwright_browsers() {
  # 清理 Python 版 Playwright 无头浏览器缓存（系统 Chromium 将另行安装）
  rm -rf \
    /root/.cache/ms-playwright \
    /root/.cache/ms-playwright-* \
    /var/lib/ip_sentinel/.cache/ms-playwright \
    /opt/ip_sentinel/ms-playwright \
    /opt/ip_sentinel/.cache/ms-playwright \
    /opt/ip_sentinel_master/ms-playwright \
    /opt/ip_sentinel_master/.cache/ms-playwright \
    2>/dev/null || true
  if [ -d /home ]; then
    find /home -maxdepth 4 \( -type d -name 'ms-playwright' -o -type d -name 'ms-playwright-*' \) \
      -exec rm -rf {} + 2>/dev/null || true
  fi
}

purge_python_runtime_dir() {
  local root="$1"
  [ -d "$root" ] || return 0
  rm -rf \
    "${root}/.venv" \
    "${root}/py" \
    "${root}/uv.lock" \
    "${root}/pyproject.toml" \
    "${root}/.python-version" \
    "${root}/__pycache__" \
    2>/dev/null || true
  rm -rf "${root}/ms-playwright" "${root}/.cache/ms-playwright" 2>/dev/null || true
  # 丢弃旧自签证书
  rm -f "${root}/core/ws_cert.pem" "${root}/core/ws_key.pem" 2>/dev/null || true
  purge_playwright_browsers
}

purge_legacy_cron() {
  local pattern="$1"
  if command -v crontab >/dev/null 2>&1; then
    local tmp
    tmp="$(mktemp)"
    crontab -l 2>/dev/null | grep -vF "$pattern" > "$tmp" || true
    crontab "$tmp" 2>/dev/null || true
    rm -f "$tmp"
  fi
}

# 控制端：只保留 TG_TOKEN；强制 polling；不信任 PUBLIC_IP / 旧 webhook
migrate_legacy_master() {
  local legacy="${1:-/opt/ip_sentinel_master}"
  local conf_dir="${2:-/etc/ip-sentinel-master}"
  local data_dir="${3:-/var/lib/ip-sentinel-master}"
  local new_conf="${conf_dir}/master.conf"
  local legacy_conf="${legacy}/master.conf"

  local found=false
  [ -f "$legacy_conf" ] && found=true
  [ -f "${legacy}/sentinel.db" ] && found=true
  if [ -d "${legacy}/py" ] || [ -d "${legacy}/.venv" ]; then
    found=true
  fi
  [ "$found" = false ] && return 0

  echo "检测到旧版 Python 控制端 (${legacy})，开始迁移（仅保留 TG_TOKEN）…"

  stop_unit_if_any "ip-sentinel-master"
  pkill -f "run_master.py" 2>/dev/null || true
  purge_legacy_cron "ip-sentinel-master"
  purge_legacy_cron "run_master.py"
  purge_legacy_cron "/opt/ip_sentinel_master"

  mkdir -p "$conf_dir" "$data_dir" "$data_dir/core" "$data_dir/logs"

  local token=""
  token="$(migrate_conf_get "$legacy_conf" "TG_TOKEN")"
  if [ -z "$token" ]; then
    token="$(migrate_conf_get "$new_conf" "TG_TOKEN")"
  fi

  # 重写配置：不拷贝旧 PUBLIC_IP / FORUM_* / 自签相关
  if [ -n "$token" ]; then
    cat > "$new_conf" <<EOF
# migrated from Python — only TG_TOKEN trusted; reconfigure via /nginx
TG_TOKEN="${token}"
MASTER_DIR="${data_dir}"
DB_FILE="${data_dir}/sentinel.db"
LISTEN_ADDR="127.0.0.1:19530"
TG_MODE="polling"
IS_OFFICIAL_GATEWAY="false"
ENABLE_MASTER_OTA="false"
EOF
    echo "已写入精简配置 → ${new_conf}（仅 TG_TOKEN；TG_MODE=polling）"
  else
    echo "警告: 旧配置无 TG_TOKEN，安装脚本将交互索取"
  fi

  if [ -f "${legacy}/sentinel.db" ] && [ ! -f "${data_dir}/sentinel.db" ]; then
    cp -a "${legacy}/sentinel.db" "${data_dir}/sentinel.db"
    for s in -wal -shm; do
      [ -f "${legacy}/sentinel.db${s}" ] && cp -a "${legacy}/sentinel.db${s}" "${data_dir}/sentinel.db${s}" || true
    done
    echo "已迁移数据库 → ${data_dir}/sentinel.db"
  fi

  # 仅论坛话题状态（非连接配置）；不迁移自签 pem
  if [ -f "${legacy}/core/forum_topics.json" ] && [ ! -f "${data_dir}/core/forum_topics.json" ]; then
    mkdir -p "${data_dir}/core"
    cp -a "${legacy}/core/forum_topics.json" "${data_dir}/core/forum_topics.json"
  fi
  if [ -f "${legacy}/.tg_offset" ] && [ ! -f "${data_dir}/.tg_offset" ]; then
    cp -a "${legacy}/.tg_offset" "${data_dir}/.tg_offset"
  fi

  purge_python_runtime_dir "$legacy"
  rm -f /etc/systemd/system/ip-sentinel-master.service 2>/dev/null || true
  if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload 2>/dev/null || true
  fi
  echo "Python 控制端已清理。请在 TG 发送 /nginx 配置 https 域名后，节点再填 MASTER_BASE_URL。"
}

# 节点：只保留 TG_TOKEN / CHAT_ID / REGION_CODE；MASTER_BASE_URL 必须重配为 https://域名
migrate_legacy_agent() {
  local legacy="${1:-/opt/ip_sentinel}"
  local conf_dir="${2:-/etc/ip-sentinel}"
  local data_dir="${3:-/var/lib/ip-sentinel}"
  local log_dir="${4:-/var/log/ip-sentinel}"
  local new_conf="${conf_dir}/config.conf"
  local legacy_conf="${legacy}/config.conf"

  local found=false
  [ -f "$legacy_conf" ] && found=true
  if [ -d "${legacy}/py" ] || [ -d "${legacy}/.venv" ]; then
    found=true
  fi
  [ "$found" = false ] && return 0

  echo "检测到旧版 Python 节点 (${legacy})，开始迁移（仅保留 TG_TOKEN/CHAT_ID/REGION_CODE）…"

  stop_unit_if_any "ip-sentinel-agent"
  pkill -f "agent_daemon.py" 2>/dev/null || true
  purge_legacy_cron "ip-sentinel-agent"
  purge_legacy_cron "agent_daemon.py"
  purge_legacy_cron "/opt/ip_sentinel"

  mkdir -p "$conf_dir" "$data_dir" "$data_dir/core" "$data_dir/data" "$log_dir"

  local token="" chat="" region=""
  token="$(migrate_conf_get "$legacy_conf" "TG_TOKEN")"
  chat="$(migrate_conf_get "$legacy_conf" "CHAT_ID")"
  region="$(migrate_conf_get "$legacy_conf" "REGION_CODE")"
  [ -z "$token" ] && token="$(migrate_conf_get "$new_conf" "TG_TOKEN")"
  [ -z "$chat" ] && chat="$(migrate_conf_get "$new_conf" "CHAT_ID")"
  [ -z "$region" ] && region="$(migrate_conf_get "$new_conf" "REGION_CODE")"
  region="${region:-UNKNOWN}"

  cat > "$new_conf" <<EOF
# migrated from Python — only TG_TOKEN/CHAT_ID/REGION_CODE trusted
TG_TOKEN="${token}"
CHAT_ID="${chat}"
REGION_CODE="${region}"
INSTALL_DIR="${data_dir}"
LOG_FILE="${log_dir}/sentinel.log"
ENABLE_GOOGLE="true"
ENABLE_TRUST="true"
ENABLE_MAPS_GEO="true"
ENABLE_OTA="true"
# MASTER_BASE_URL must be https://domain (no IP); set by install / admin
MASTER_BASE_URL=""
EOF
  echo "已写入精简配置 → ${new_conf}（需重新配置 MASTER_BASE_URL=https://域名）"

  if [ -d "${legacy}/data" ]; then
    mkdir -p "${data_dir}/data"
    cp -a "${legacy}/data/." "${data_dir}/data/" 2>/dev/null || true
  fi
  if [ -f "${legacy}/logs/sentinel.log" ] && [ ! -f "${log_dir}/sentinel.log" ]; then
    cp -a "${legacy}/logs/sentinel.log" "${log_dir}/sentinel.log" || true
  fi

  purge_python_runtime_dir "$legacy"
  rm -f /etc/systemd/system/ip-sentinel-agent.service 2>/dev/null || true
  if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload 2>/dev/null || true
  fi
  echo "Python 节点已清理。请配置 MASTER_BASE_URL 为控制端 https://域名。"
}
