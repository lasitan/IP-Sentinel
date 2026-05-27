#!/bin/bash

# ==========================================================
# 脚本名称: uninstall.sh
# 功能: 停止服务并删除 Agent 文件
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

echo "========================================================"
echo "      🗑️ 准备卸载 IP-Sentinel (边缘节点 Edge Agent)"

CONFIG_FILE="${INSTALL_DIR}/config.conf"
if [ -f "$CONFIG_FILE" ]; then
    CURRENT_VER=$(grep "^AGENT_VERSION=" "$CONFIG_FILE" | cut -d'"' -f2)
    [ -n "$CURRENT_VER" ] && echo "        📍 目标版本: v${CURRENT_VER}"
fi
echo "========================================================"

# ----------------------------------------------------------
# 停止并移除 systemd 服务
# ----------------------------------------------------------
echo "[1/4] 正在停止并删除 Systemd 服务..."
if command -v systemctl >/dev/null 2>&1; then
    echo "💡 检测到 Systemd 环境，正在抹除 Systemd 服务单元..."
    # 强制结束服务进程
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
echo "[2/4] 正在终止后台进程与定时任务..."
pkill -9 -f "tg_daemon.sh" >/dev/null 2>&1
pkill -9 -f "agent_daemon.py" >/dev/null 2>&1
pkill -9 -f "agent_daemon.sh" >/dev/null 2>&1
pkill -9 -f "uv run" >/dev/null 2>&1
pkill -9 -f "python3.*webhook.py" >/dev/null 2>&1
pkill -9 -f "ip_sentinel/py/" >/dev/null 2>&1
pkill -9 -f "webhook.py" >/dev/null 2>&1
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
echo "[3/4] 正在清理系统定时任务 (Cron)..."
# 通过管道原位清洗避免落地到 /tmp，免疫提权或外部劫持探测
crontab -l 2>/dev/null | grep -v "ip_sentinel" | crontab - >/dev/null 2>&1 || true

# 扫除高受限环境 (如 Alpine) 中的额外触发隐患
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

# ----------------------------------------------------------
# 删除安装目录
# ----------------------------------------------------------
echo "[4/4] 正在抹除核心程序、配置文件与系统痕迹..."
if [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
fi

echo "========================================================"
echo "✅ 卸载完成，IP-Sentinel Agent 已移除。"
echo "💡 提示：如果安装时在防火墙放行了 Webhook 随机端口，请您按需手动关闭。"
echo "👋 感谢您的使用，期待未来再次为您守护资产！"
echo "========================================================"