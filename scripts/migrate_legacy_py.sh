#!/bin/bash
# Python 布局 → Go/deb 布局：供 install 脚本 source。
# 兼容旧版 master.conf / config.conf（KEY="VALUE"，含注释行）。
# 保留全部业务键（TG_TOKEN、FORUM_*、PUBLIC_IP 等），只改路径并补齐 Go 缺省键。

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

# 按旧版格式读取：TG_TOKEN="..."，跳过注释，剥离引号与 CR
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

# 将 Python 版 master.conf 规范为 Go 布局（保留全部原键）
normalize_master_conf() {
  local conf="$1" data_dir="$2"
  [ -f "$conf" ] || return 0
  migrate_conf_key_set "$conf" "MASTER_DIR" "$data_dir"
  migrate_conf_key_set "$conf" "DB_FILE" "${data_dir}/sentinel.db"
  if [ -z "$(migrate_conf_get "$conf" "LISTEN_ADDR")" ]; then
    migrate_conf_key_set "$conf" "LISTEN_ADDR" "127.0.0.1:19530"
  fi
  if [ -z "$(migrate_conf_get "$conf" "TG_MODE")" ]; then
    migrate_conf_key_set "$conf" "TG_MODE" "polling"
  fi
  # 旧版常有 PUBLIC_IP、无 PUBLIC_BASE_URL
  if [ -z "$(migrate_conf_get "$conf" "PUBLIC_BASE_URL")" ]; then
    local pip
    pip="$(migrate_conf_get "$conf" "PUBLIC_IP")"
    if [ -n "$pip" ]; then
      migrate_conf_key_set "$conf" "PUBLIC_BASE_URL" "https://${pip}"
    fi
  fi
}

# 控制端：/opt/ip_sentinel_master → /etc + /var/lib
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

  echo "检测到旧版 Python 控制端 (${legacy})，开始无感迁移…"

  stop_unit_if_any "ip-sentinel-master"
  pkill -f "run_master.py" 2>/dev/null || true
  pkill -f "ip_sentinel_master.*python" 2>/dev/null || true
  purge_legacy_cron "ip-sentinel-master"
  purge_legacy_cron "run_master.py"
  purge_legacy_cron "/opt/ip_sentinel_master"

  mkdir -p "$conf_dir" "$data_dir" "$data_dir/core" "$data_dir/logs"

  local legacy_token="" new_token=""
  legacy_token="$(migrate_conf_get "$legacy_conf" "TG_TOKEN")"
  new_token="$(migrate_conf_get "$new_conf" "TG_TOKEN")"

  # 整文件拷贝以保留 FORUM_* / IP_PREF / PUBLIC_IP / ENABLE_MASTER_OTA 等
  if [ -f "$legacy_conf" ]; then
    if [ ! -f "$new_conf" ] || [ -z "$new_token" ]; then
      cp -a "$legacy_conf" "$new_conf"
      echo "已迁移配置 → ${new_conf}（保留旧版全部键）"
    else
      echo "新配置已有 TG_TOKEN，保留 ${new_conf}（不覆盖）"
    fi
  fi

  if [ -f "$new_conf" ]; then
    normalize_master_conf "$new_conf" "$data_dir"
    echo "配置已规范化: MASTER_DIR/DB_FILE/LISTEN_ADDR/TG_MODE"
    if [ -n "$(migrate_conf_get "$new_conf" "TG_TOKEN")" ]; then
      echo "已读取 TG_TOKEN（来自旧版 master.conf）"
    elif [ -n "$legacy_token" ]; then
      migrate_conf_key_set "$new_conf" "TG_TOKEN" "$legacy_token"
      echo "已从旧配置写回 TG_TOKEN"
    fi
  fi

  if [ -f "${legacy}/sentinel.db" ] && [ ! -f "${data_dir}/sentinel.db" ]; then
    cp -a "${legacy}/sentinel.db" "${data_dir}/sentinel.db"
    for s in -wal -shm; do
      [ -f "${legacy}/sentinel.db${s}" ] && cp -a "${legacy}/sentinel.db${s}" "${data_dir}/sentinel.db${s}" || true
    done
    echo "已迁移数据库 → ${data_dir}/sentinel.db"
  fi

  if [ -d "${legacy}/core" ]; then
    mkdir -p "${data_dir}/core"
    for f in forum_topics.json ws_cert.pem ws_key.pem; do
      if [ -f "${legacy}/core/${f}" ] && [ ! -f "${data_dir}/core/${f}" ]; then
        cp -a "${legacy}/core/${f}" "${data_dir}/core/${f}"
      fi
    done
  fi
  if [ -f "${legacy}/.tg_offset" ] && [ ! -f "${data_dir}/.tg_offset" ]; then
    cp -a "${legacy}/.tg_offset" "${data_dir}/.tg_offset"
  fi

  purge_python_runtime_dir "$legacy"
  rm -f /etc/systemd/system/ip-sentinel-master.service 2>/dev/null || true
  if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload 2>/dev/null || true
  fi

  echo "Python 控制端运行时已清理；配置与数据已保留。"
}

# 节点：/opt/ip_sentinel → /etc + /var/lib
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

  echo "检测到旧版 Python 节点 (${legacy})，开始无感迁移…"

  stop_unit_if_any "ip-sentinel-agent"
  pkill -f "agent_daemon.py" 2>/dev/null || true
  pkill -f "ip_sentinel/py/" 2>/dev/null || true
  purge_legacy_cron "ip-sentinel-agent"
  purge_legacy_cron "agent_daemon.py"
  purge_legacy_cron "/opt/ip_sentinel"

  mkdir -p "$conf_dir" "$data_dir" "$data_dir/core" "$data_dir/data" "$log_dir"

  local legacy_token="" new_token=""
  legacy_token="$(migrate_conf_get "$legacy_conf" "TG_TOKEN")"
  new_token="$(migrate_conf_get "$new_conf" "TG_TOKEN")"

  if [ -f "$legacy_conf" ]; then
    if [ ! -f "$new_conf" ] || [ -z "$new_token" ]; then
      cp -a "$legacy_conf" "$new_conf"
      echo "已迁移配置 → ${new_conf}（保留旧版全部键）"
    else
      echo "新配置已有 TG_TOKEN，保留 ${new_conf}（不覆盖）"
    fi
  fi

  if [ -f "$new_conf" ]; then
    migrate_conf_key_set "$new_conf" "INSTALL_DIR" "$data_dir"
    migrate_conf_key_set "$new_conf" "LOG_FILE" "${log_dir}/sentinel.log"

    local base
    base="$(migrate_conf_get "$new_conf" "MASTER_BASE_URL")"
    if [ -z "$base" ]; then
      local mip
      mip="$(migrate_conf_get "$new_conf" "MASTER_PUBLIC_IP")"
      if [ -n "$mip" ]; then
        migrate_conf_key_set "$new_conf" "MASTER_BASE_URL" "https://${mip}:19530"
        echo "已根据 MASTER_PUBLIC_IP 写入 MASTER_BASE_URL=https://${mip}:19530"
      fi
    fi
  fi

  if [ -d "${legacy}/data" ]; then
    mkdir -p "${data_dir}/data"
    cp -a "${legacy}/data/." "${data_dir}/data/" 2>/dev/null || true
  fi
  if [ -f "${legacy}/logs/sentinel.log" ] && [ ! -f "${log_dir}/sentinel.log" ]; then
    cp -a "${legacy}/logs/sentinel.log" "${log_dir}/sentinel.log" || true
  fi
  if [ -d "${legacy}/core" ]; then
    mkdir -p "${data_dir}/core"
    for f in .last_ip; do
      if [ -f "${legacy}/core/${f}" ] && [ ! -f "${data_dir}/core/${f}" ]; then
        cp -a "${legacy}/core/${f}" "${data_dir}/core/${f}"
      fi
    done
  fi

  purge_python_runtime_dir "$legacy"
  rm -f /etc/systemd/system/ip-sentinel-agent.service 2>/dev/null || true
  if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload 2>/dev/null || true
  fi

  echo "Python 节点运行时已清理；配置与数据已保留。"
}
