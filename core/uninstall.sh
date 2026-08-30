#!/bin/bash

# ==========================================================
# 脚本名称: uninstall.sh
# 功能: 停止服务并完全删除 Agent（含系统包与 apt 缓存）
# ==========================================================

# ----------------------------------------------------------
# [权限鉴权] 防止非管理员误触导致组件残留挂起
# ----------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
  echo -e "\033[31m❌ 权限被拒绝: 卸载 IP-Sentinel 需要最高系统权限。\033[0m"
  echo -e "💡 请切换到 root 用户 (执行 su root 或 sudo -i) 后重新运行指令。"
  exit 1
fi

INSTALL_DIR="/opt/ip_sentinel"
MANIFEST_TMP=""

_load_install_manifest() {
  if [ -f "${INSTALL_DIR}/.install_manifest" ]; then
    MANIFEST_TMP="$(mktemp)"
    cp "${INSTALL_DIR}/.install_manifest" "$MANIFEST_TMP"
  fi
}

_manifest_value() {
  local key="$1"
  [ -n "$MANIFEST_TMP" ] && [ -f "$MANIFEST_TMP" ] || return 1
  grep "^${key}=" "$MANIFEST_TMP" 2>/dev/null | cut -d= -f2- | tail -n1
}

_manifest_packages() {
  [ -n "$MANIFEST_TMP" ] && [ -f "$MANIFEST_TMP" ] || return 0
  grep '^PKG=' "$MANIFEST_TMP" 2>/dev/null | cut -d= -f2- | sort -u
}

_cleanup_playwright() {
  local uv_bin="$1"
  echo "[4/6] 正在清理 Playwright 与浏览器缓存..."
  if [ -d "$INSTALL_DIR" ] && [ -n "$uv_bin" ] && [ -x "$uv_bin" ]; then
    (cd "$INSTALL_DIR" && "$uv_bin" run playwright uninstall --all) >/dev/null 2>&1 || true
  fi
  rm -rf /root/.cache/ms-playwright /home/*/.cache/ms-playwright 2>/dev/null || true
}

_cleanup_packages_and_cache() {
  echo "[6/6] 正在卸载安装时引入的系统包并清理包管理器缓存..."
  local pkg_mgr uv_bin pkgs=()
  pkg_mgr="$(_manifest_value PKG_MGR)"
  uv_bin="$(_manifest_value UV_BIN)"
  while IFS= read -r pkg; do
    [ -n "$pkg" ] && pkgs+=("$pkg")
  done < <(_manifest_packages)

  if [ ${#pkgs[@]} -eq 0 ] && [ -z "$pkg_mgr" ]; then
    if command -v apt-get >/dev/null 2>&1; then
      pkg_mgr="apt"
      pkgs=(curl python3 cron procps openssl ca-certificates)
    fi
  fi

  case "$pkg_mgr" in
    apt)
      if [ ${#pkgs[@]} -gt 0 ]; then
        apt-get remove -y --purge "${pkgs[@]}" >/dev/null 2>&1 || true
      fi
      apt-get autoremove -y --purge >/dev/null 2>&1 || true
      apt-get clean >/dev/null 2>&1 || true
      rm -rf /var/cache/apt/archives/* /var/lib/apt/lists/* 2>/dev/null || true
      ;;
    dnf|yum|microdnf)
      if [ ${#pkgs[@]} -gt 0 ]; then
        "$pkg_mgr" remove -y "${pkgs[@]}" >/dev/null 2>&1 || true
      fi
      "$pkg_mgr" clean all >/dev/null 2>&1 || true
      ;;
    apk)
      if [ ${#pkgs[@]} -gt 0 ]; then
        apk del "${pkgs[@]}" >/dev/null 2>&1 || true
      fi
      rm -rf /var/cache/apk/* 2>/dev/null || true
      ;;
    pacman)
      if [ ${#pkgs[@]} -gt 0 ]; then
        pacman -Rns --noconfirm "${pkgs[@]}" >/dev/null 2>&1 || true
      fi
      pacman -Sc --noconfirm >/dev/null 2>&1 || true
      ;;
  esac

  if [ -n "$uv_bin" ] && [ -f "$uv_bin" ]; then
    rm -f "$uv_bin"
  elif [ -f /usr/local/bin/uv ]; then
    rm -f /usr/local/bin/uv
  elif [ -f /root/.local/bin/uv ]; then
    rm -f /root/.local/bin/uv
  fi
  rm -rf /root/.local/share/uv /root/.cache/uv /root/.local/bin/uvx 2>/dev/null || true

  rm -f /tmp/ip_sentinel_agent_ota_*.sh /tmp/ip_sentinel_agent_uninstall_*.sh \
        /tmp/ip_sentinel_uninstall_*.sh /tmp/ota_agent.*.sh 2>/dev/null || true

  [ -n "$MANIFEST_TMP" ] && rm -f "$MANIFEST_TMP"
}

echo "========================================================"
echo "      🗑️ 准备卸载 IP-Sentinel (边缘节点 Edge Agent)"

CONFIG_FILE="${INSTALL_DIR}/config.conf"
if [ -f "$CONFIG_FILE" ]; then
    CURRENT_VER=$(grep "^AGENT_VERSION=" "$CONFIG_FILE" | cut -d'"' -f2)
    [ -n "$CURRENT_VER" ] && echo "        📍 目标版本: v${CURRENT_VER}"
fi
echo "========================================================"

_load_install_manifest
UV_FOR_PW="$(_manifest_value UV_BIN)"
[ -z "$UV_FOR_PW" ] && UV_FOR_PW="$(command -v uv 2>/dev/null || true)"

# ----------------------------------------------------------
# 停止并移除 systemd 服务
# ----------------------------------------------------------
echo "[1/6] 正在停止并删除 Systemd 服务..."
if command -v systemctl >/dev/null 2>&1; then
    echo "💡 检测到 Systemd 环境，正在抹除 Systemd 服务单元..."
    systemctl kill --signal=SIGKILL ip-sentinel-agent-daemon.service >/dev/null 2>&1 || true
    systemctl disable --now ip-sentinel-runner.service ip-sentinel-runner.timer \
        ip-sentinel-updater.service ip-sentinel-updater.timer \
        ip-sentinel-report.service ip-sentinel-report.timer \
        ip-sentinel-agent-daemon.service >/dev/null 2>&1
    rm -f /etc/systemd/system/ip-sentinel-runner.service
    rm -f /etc/systemd/system/ip-sentinel-runner.timer
    rm -f /etc/systemd/system/ip-sentinel-updater.service
    rm -f /etc/systemd/system/ip-sentinel-updater.timer
    rm -f /etc/systemd/system/ip-sentinel-report.service
    rm -f /etc/systemd/system/ip-sentinel-report.timer
    rm -f /etc/systemd/system/ip-sentinel-agent-daemon.service
    systemctl daemon-reload
    systemctl reset-failed
else
    echo "💡 未检测到 Systemd，跳过此步骤..."
fi

# ----------------------------------------------------------
# 结束相关进程
# ----------------------------------------------------------
echo "[2/6] 正在终止后台进程与定时任务..."
pkill -9 -f "tg_daemon.sh" >/dev/null 2>&1
pkill -9 -f "agent_daemon.py" >/dev/null 2>&1
pkill -9 -f "agent_daemon.sh" >/dev/null 2>&1
pkill -9 -f "uv run" >/dev/null 2>&1
pkill -9 -f "ip_sentinel/py/" >/dev/null 2>&1
pkill -9 -f "agent_ws.py" >/dev/null 2>&1
pkill -9 -f "runner.py" >/dev/null 2>&1
pkill -9 -f "updater.py" >/dev/null 2>&1
pkill -9 -f "report.py" >/dev/null 2>&1
pkill -9 -f "mod_quality.py" >/dev/null 2>&1
pkill -9 -f "mod_google.py" >/dev/null 2>&1
pkill -9 -f "mod_trust.py" >/dev/null 2>&1
pkill -9 -f "runner.sh" >/dev/null 2>&1
pkill -9 -f "updater.sh" >/dev/null 2>&1
pkill -9 -f "tg_report.sh" >/dev/null 2>&1
pkill -9 -f "mod_google.sh" >/dev/null 2>&1
pkill -9 -f "mod_trust.sh" >/dev/null 2>&1
pkill -9 -f "sentinel_scheduler.sh" >/dev/null 2>&1

# ----------------------------------------------------------
# [任务清洗] 基于内存管道流彻底擦除系统底层调度劫持
# ----------------------------------------------------------
echo "[3/6] 正在清理系统定时任务 (Cron)..."
crontab -l 2>/dev/null | grep -v "ip_sentinel" | crontab - >/dev/null 2>&1 || true

for CRON_FILE in "/var/spool/cron/crontabs/root" "/etc/crontabs/root"; do
    if [ -f "$CRON_FILE" ]; then
        grep -v "ip_sentinel" "$CRON_FILE" > "${CRON_FILE}.tmp" 2>/dev/null || true
        cat "${CRON_FILE}.tmp" > "$CRON_FILE" 2>/dev/null || true
        rm -f "${CRON_FILE}.tmp" 2>/dev/null
    fi
done
rm -f /etc/local.d/ip_sentinel.start 2>/dev/null
rm -f /etc/local.d/ip_sentinel_scheduler.start 2>/dev/null

if grep -q "sentinel_scheduler.sh" /etc/profile 2>/dev/null; then
    sed -i '/sentinel_scheduler\.sh/d' /etc/profile 2>/dev/null || true
fi

_cleanup_playwright "$UV_FOR_PW"

# ----------------------------------------------------------
# 删除安装目录
# ----------------------------------------------------------
echo "[5/6] 正在抹除核心程序、配置文件与系统痕迹..."
if [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
fi

_cleanup_packages_and_cache

echo "========================================================"
echo "✅ 完全卸载完成，IP-Sentinel Agent 及安装依赖已移除。"
echo "💡 提示：如果安装时在防火墙放行了 Webhook 随机端口，请您按需手动关闭。"
echo "👋 感谢您的使用，期待未来再次为您守护资产！"
echo "========================================================"
