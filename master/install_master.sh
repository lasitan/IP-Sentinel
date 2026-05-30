#!/bin/bash

# ==========================================================
# 脚本名称: install_master.sh
# 功能: Master 安装、升级与 SQLite 初始化
# ==========================================================

# ----------------------------------------------------------
# 需要 root 权限
# ----------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
  echo -e "\033[31m❌ 权限被拒绝: 部署 IP-Sentinel 需要最高系统权限。\033[0m"
  echo -e "💡 请切换到 root 用户 (执行 su root 或 sudo -i) 后重新运行指令。"
  exit 1
fi

SECURE_TMP=$(mktemp -d /tmp/ips_master_install.XXXXXX)
trap 'rm -rf "$SECURE_TMP"' EXIT HUP INT QUIT TERM

# ----------------------------------------------------------
# 环境检测
# ----------------------------------------------------------
is_systemd() {
    command -v systemctl >/dev/null 2>&1 || return 1
    [ -d /run/systemd/system ] || return 1
    return 0
}

get_os_info() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        echo "$PRETTY_NAME"
    else
        uname -srm
    fi
}

get_virt_info() {
    if grep -qaE 'docker|containerd|podman' /proc/1/cgroup 2>/dev/null || [ -f /.dockerenv ]; then
        echo "Docker/OCI Container"
    elif grep -qa container=lxc /proc/1/environ 2>/dev/null || [ -d /proc/vz ]; then
        echo "LXC/OpenVZ"
    elif command -v systemd-detect-virt >/dev/null 2>&1; then
        systemd-detect-virt
    else
        echo "Unknown/Bare Metal"
    fi
}

echo -e "\n======================================"
echo -e "📊 \033[36mIP-Sentinel Master 环境检查\033[0m"
echo -e "--------------------------------------"
echo -e "OS 架构   : $(get_os_info)"
echo -e "虚拟化    : $(get_virt_info)"
if is_systemd; then
    echo -e "Init 系统 : systemd ✅"
else
    echo -e "Init 系统 : 非 systemd ⚠️ (将使用 cron 看门狗)"
fi
echo -e "======================================\n"
sleep 1

REPO_RAW_URL="https://raw.githubusercontent.com/lasitan/IP-Sentinel/main"
UV_PATH="/usr/local/bin:/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
UV_BIN="/usr/local/bin/uv"

ensure_uv() {
    export PATH="${UV_PATH}"
    if command -v uv >/dev/null 2>&1; then
        UV_BIN="$(command -v uv)"
        return 0
    fi
    echo "⏳ 正在安装 uv (Astral)..."
    mkdir -p /usr/local/bin
    if curl -fsSL --connect-timeout 30 https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="/usr/local/bin" sh >/dev/null 2>&1; then
        UV_BIN="/usr/local/bin/uv"
    elif curl -fsSL --connect-timeout 30 https://astral.sh/uv/install.sh | sh >/dev/null 2>&1; then
        export PATH="/root/.local/bin:${PATH}"
        UV_BIN="$(command -v uv)"
    else
        echo -e "\033[31m❌ 致命错误：uv 安装失败，请检查网络或手动安装: https://docs.astral.sh/uv/\033[0m"
        exit 1
    fi
    if ! command -v uv >/dev/null 2>&1; then
        echo -e "\033[31m❌ 致命错误：uv 未加入 PATH。\033[0m"
        exit 1
    fi
    UV_BIN="$(command -v uv)"
    echo -e "\033[32m✅ uv 已就绪: $($UV_BIN --version 2>/dev/null | head -n1)\033[0m"
}

uv_sync_project() {
    local root="$1"
    echo "⏳ 正在同步 Python 运行时 (${root})..."
    if ! (cd "$root" && "$UV_BIN" sync --frozen --no-dev); then
        (cd "$root" && "$UV_BIN" sync --no-dev) || {
            echo -e "\033[31m❌ uv sync 失败，请检查 pyproject.toml / uv.lock。\033[0m"
            return 1
        }
    fi
    return 0
}

# [链路容灾] 双栈冗余防抖抓取，确立本地态势版本号
TARGET_VERSION=$( (curl -fsSL --connect-timeout 5 --retry 2 "${REPO_RAW_URL}/version.txt" || curl -4 -fsSL --connect-timeout 5 --retry 2 "${REPO_RAW_URL}/version.txt") 2>/dev/null | grep "^MASTER_VERSION=" | cut -d'=' -f2 | tr -d '[:space:]')
TARGET_VERSION=${TARGET_VERSION:-"4.0.7"}

MASTER_DIR="/opt/ip_sentinel_master"
DB_FILE="${MASTER_DIR}/sentinel.db"

echo "========================================================"
echo "      IP-Sentinel Master v${TARGET_VERSION}"
echo "========================================================"

# ==========================================================
# [指令接管] 云端 OTA 重构流引擎拦截
# ==========================================================
if [ "$SILENT_MASTER_OTA" == "true" ]; then
    echo -e "\n⏳ [OTA] 开始自动升级..."
    ACTION_CHOICE=1
    UPGRADE_MODE="true"
    KEEP_DB="true"
    
    if [ -f "${MASTER_DIR}/master.conf" ]; then
        source "${MASTER_DIR}/master.conf"
        
        if grep -q "^MASTER_VERSION=" "${MASTER_DIR}/master.conf"; then
            sed -i "s/^MASTER_VERSION=.*/MASTER_VERSION=\"$TARGET_VERSION\"/" "${MASTER_DIR}/master.conf"
        else
            echo "MASTER_VERSION=\"$TARGET_VERSION\"" >> "${MASTER_DIR}/master.conf"
        fi
    fi
    echo -e "\033[32m✅ OTA 模式：将保留配置并更新程序。\033[0m"
else
    echo -e "\n请选择操作:"
    echo "  1) 安装 Master"
    echo "  2) 卸载 Master"
    read -p "请输入选择 [1-2] (默认1): " ACTION_CHOICE

    ACTION_CHOICE=${ACTION_CHOICE:-1}

    if [ "$ACTION_CHOICE" == "2" ]; then
        echo -e "\n⏳ 正在拉取卸载程序..."
        curl -fsSL --connect-timeout 10 --retry 3 "${REPO_RAW_URL}/master/uninstall_master.sh" -o "${SECURE_TMP}/uninstall_master.sh"
        chmod +x "${SECURE_TMP}/uninstall_master.sh"
        bash "${SECURE_TMP}/uninstall_master.sh"
        rm -f "/tmp/uninstall_master.sh"
        exit 0
    fi

    # 已安装时询问是否保留配置升级
    UPGRADE_MODE="false"
    KEEP_DB="true"

    if [ "$ACTION_CHOICE" == "1" ] && [ -f "${MASTER_DIR}/master.conf" ]; then
        echo -e "\n\033[33m💡 检测到本机已安装 Master。\033[0m"
        read -p "是否保留现有配置并升级？(y/n, 默认 y): " UPGRADE_CHOICE
        if [[ -z "$UPGRADE_CHOICE" || "$UPGRADE_CHOICE" =~ ^[Yy]$ ]]; then
            UPGRADE_MODE="true"
            read -p "👉 是否保留历史节点数据库 (SQLite)？(y/n, 默认y): " DB_CHOICE
            if [[ "$DB_CHOICE" =~ ^[Nn]$ ]]; then
                KEEP_DB="false"
            fi
            
            source "${MASTER_DIR}/master.conf"
            
            if grep -q "^MASTER_VERSION=" "${MASTER_DIR}/master.conf"; then
                sed -i "s/^MASTER_VERSION=.*/MASTER_VERSION=\"$TARGET_VERSION\"/" "${MASTER_DIR}/master.conf"
            else
                echo "MASTER_VERSION=\"$TARGET_VERSION\"" >> "${MASTER_DIR}/master.conf"
            fi
            
            echo -e "\033[32m✅ 升级模式，目标版本 v${TARGET_VERSION}。\033[0m"
        else
            echo -e "\033[33m🔄 将重新配置，现有 Master 数据会被清除。\033[0m"
        fi
    fi
fi

# ----------------------------------------------------------
# [环境清洗] 执行装配前系统清理动作
# ----------------------------------------------------------
echo -e "\n⏳ 正在验证本地环境与数据..."

if [ "$UPGRADE_MODE" == "true" ]; then
    if [ "$KEEP_DB" == "false" ]; then
        rm -f "$DB_FILE" 2>/dev/null
        echo -e "🗑️ 历史节点数据库已按指令清空。"
    else
        echo -e "📦 已保留 SQLite 节点数据库。"
    fi
else
    rm -rf "$MASTER_DIR" 2>/dev/null
fi

# ==========================================================
# 安装系统依赖与 uv
# ==========================================================
echo -e "\n[1/4] 正在探测核心依赖 (curl, sqlite3, crontab, pgrep, uv, openssl)..."

REQUIRED_CMDS=("curl" "sqlite3" "crontab" "pgrep" "openssl")
MISSING_CMDS=()

for cmd in "${REQUIRED_CMDS[@]}"; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        MISSING_CMDS+=("$cmd")
    fi
done

if [ ${#MISSING_CMDS[@]} -gt 0 ]; then
    echo "⏳ 发现缺失依赖: ${MISSING_CMDS[*]}，正在尝试自动补齐..."
    
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -y >/dev/null 2>&1
        apt-get install -y --no-install-recommends curl sqlite3 cron procps openssl ca-certificates >/dev/null 2>&1
        systemctl enable cron >/dev/null 2>&1 && systemctl start cron >/dev/null 2>&1
    elif command -v yum >/dev/null 2>&1 || command -v dnf >/dev/null 2>&1 || command -v microdnf >/dev/null 2>&1; then
        PKG_MGR="yum"
        OPT_ARGS=""
        if command -v dnf >/dev/null 2>&1; then
            PKG_MGR="dnf"
            OPT_ARGS="--setopt=install_weak_deps=False"
        elif command -v microdnf >/dev/null 2>&1; then
            PKG_MGR="microdnf"
        fi
        
        echo -e "\033[90m   (正在安装 epel-release 扩展源，请稍候...)\033[0m"
        $PKG_MGR install -y epel-release >/dev/null 2>&1 || true
        
        echo -e "\033[90m   (正在拉取核心组件...)\033[0m"
        $PKG_MGR install -y $OPT_ARGS curl sqlite cronie procps-ng openssl
        systemctl enable crond >/dev/null 2>&1 && systemctl start crond >/dev/null 2>&1
    elif command -v apk >/dev/null 2>&1; then
        echo "Alpine 探测到系统类型为 Alpine Linux，正在执行轻量级安装..."
        apk add --no-cache curl sqlite cronie procps bash openssl ca-certificates || apk add --no-cache curl sqlite procps bash openssl ca-certificates
        mkdir -p /var/spool/cron/crontabs
        rc-update add crond default >/dev/null 2>&1
        service crond start >/dev/null 2>&1
    elif command -v pacman >/dev/null 2>&1; then
        pacman -Sy --noconfirm curl sqlite cronie procps-ng openssl >/dev/null 2>&1
        mkdir -p /root/.cache/crontab 2>/dev/null
        systemctl enable cronie >/dev/null 2>&1 && systemctl start cronie >/dev/null 2>&1
    else
        echo -e "\033[31m❌ 自动安装失败：系统未知的包管理器。\033[0m"
        echo -e "\033[33m⚠️ 请手动执行以下安装命令后重新运行本脚本：\033[0m"
        echo -e "  Debian/Ubuntu: \033[36mapt-get update && apt-get install -y --no-install-recommends curl sqlite3 cron procps openssl\033[0m"
        echo -e "  CentOS/RHEL:   \033[36myum install -y curl sqlite cronie procps-ng openssl\033[0m"
        echo -e "  Alpine Linux:  \033[36mapk add --no-cache curl sqlite cronie procps bash openssl\033[0m"
        echo -e "  Arch Linux:    \033[36mpacman -Sy curl sqlite cronie procps-ng openssl\033[0m"
        exit 1
    fi
    
    for cmd in "${REQUIRED_CMDS[@]}"; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            echo -e "\033[31m❌ 致命错误：核心命令 '$cmd' 仍未找到！\033[0m"
            echo -e "请手动修复您的包管理器源，或联系 VPS 供应商。"
            exit 1
        fi
    done
fi
ensure_uv
echo -e "\033[32m✅ 基础环境检测通过。\033[0m"

mkdir -p "$MASTER_DIR"

# ==========================================================
# [配置总线] 构建交互与策略文件固化
# ==========================================================
if [ "$UPGRADE_MODE" == "false" ]; then
    echo -e "\n[2/4] 配置 Telegram Bot:"
    read -p "请输入 Telegram Bot Token: " TG_TOKEN
    
    echo -e "\n请选择您的部署环境身份:"
    echo "  1) 私有 Master (默认，支持 OTA)"
    echo "  2) 官方公共网关 (隐藏全局 OTA，防滥用)"
    read -p "请输入选择 [1-2] (默认1): " GATEWAY_TYPE
    GATEWAY_TYPE=${GATEWAY_TYPE:-1}
    
    IS_OFFICIAL_GATEWAY="false"
    ENABLE_MASTER_OTA="false"
    if [ "$GATEWAY_TYPE" == "2" ]; then
        IS_OFFICIAL_GATEWAY="true"
        echo -e "\033[33m⚠️ 官方公共网关模式下，Master 与节点的 OTA 已禁用。\033[0m"
    else
        echo -e "\n[2.1/4] Master OTA 远程升级"
        echo -e "💡 开启后，可在 Telegram 菜单中升级 Master。"
        read -p "是否允许 Master 接收 OTA 升级？(y/n, 默认 y): " M_OTA_CHOICE
        if [[ "$M_OTA_CHOICE" =~ ^[Nn]$ ]]; then
            ENABLE_MASTER_OTA="false"
            echo -e "🛡️ \033[33m已关闭 Master OTA，仅支持 SSH 手动升级。\033[0m"
        else
            ENABLE_MASTER_OTA="true"
            echo -e "✅ \033[32m已开启 Master OTA。\033[0m"
        fi
    fi

    echo -e "\n[2.2/4] 论坛话题模式 (Forum Topics)"
    echo -e "💡 开启后，每个节点自动在指定超级群组中创建独立话题，"
    echo -e "   日志/报告/控制台操作均在对应话题内进行。"
    echo -e "   需先将 Bot 加入群组并设为管理员（含「管理话题」权限），"
    echo -e "   并在群组设置中开启 Topics。"
    read -p "是否启用论坛话题模式？(y/n, 默认 n): " FORUM_CHOICE
    FORUM_MODE="false"
    FORUM_CHAT_ID=""
    if [[ "$FORUM_CHOICE" =~ ^[Yy]$ ]]; then
        FORUM_MODE="true"
        echo -e "\033[33m💡 请将 Bot 拉入目标超级群组，发送任意消息后，"
        echo -e "   可通过 @getidsbot 或 @RawDataBot 获取群组 Chat ID（通常为 -100 开头）。\033[0m"
        read -p "请输入超级群组 Chat ID: " RAW_FORUM_ID
        FORUM_CHAT_ID=$(echo "$RAW_FORUM_ID" | tr -cd '0-9-')
        while [ -z "$FORUM_CHAT_ID" ]; do
            read -p "⚠️ Chat ID 不能为空，请重新输入: " RAW_FORUM_ID
            FORUM_CHAT_ID=$(echo "$RAW_FORUM_ID" | tr -cd '0-9-')
        done
        echo -e "✅ \033[32m已启用话题模式，群组 ID: ${FORUM_CHAT_ID}\033[0m"
    fi

    cat > "${MASTER_DIR}/master.conf" << EOF
# IP-Sentinel Master 本地固化配置 (v${TARGET_VERSION})
MASTER_VERSION="$TARGET_VERSION"
TG_TOKEN="$TG_TOKEN"
DB_FILE="$DB_FILE"
MASTER_DIR="$MASTER_DIR"
IS_OFFICIAL_GATEWAY="$IS_OFFICIAL_GATEWAY"
ENABLE_MASTER_OTA="$ENABLE_MASTER_OTA"
FORUM_MODE="$FORUM_MODE"
FORUM_CHAT_ID="$FORUM_CHAT_ID"
EOF
fi

if [ "$UPGRADE_MODE" == "true" ]; then
    if ! grep -q "^IS_OFFICIAL_GATEWAY=" "${MASTER_DIR}/master.conf"; then
        echo "IS_OFFICIAL_GATEWAY=\"false\"" >> "${MASTER_DIR}/master.conf"
    fi
    if ! grep -q "^ENABLE_MASTER_OTA=" "${MASTER_DIR}/master.conf"; then
        echo "ENABLE_MASTER_OTA=\"false\"" >> "${MASTER_DIR}/master.conf"
    fi
    if ! grep -q "^FORUM_MODE=" "${MASTER_DIR}/master.conf"; then
        echo "FORUM_MODE=\"false\"" >> "${MASTER_DIR}/master.conf"
    fi
    if ! grep -q "^FORUM_CHAT_ID=" "${MASTER_DIR}/master.conf"; then
        echo "FORUM_CHAT_ID=\"\"" >> "${MASTER_DIR}/master.conf"
    fi
    if ! grep -q "^FORUM_OWNER_CHAT_ID=" "${MASTER_DIR}/master.conf"; then
        echo "FORUM_OWNER_CHAT_ID=\"\"" >> "${MASTER_DIR}/master.conf"
    fi
fi

# ----------------------------------------------------------
# [数据存储] 初始化 SQLite 表结构基线
# ----------------------------------------------------------
echo -e "\n[3/4] 正在初始化 SQLite 数据库表结构..."
sqlite3 "$DB_FILE" <<EOF
CREATE TABLE IF NOT EXISTS nodes (
    chat_id TEXT,
    node_name TEXT,
    agent_ip TEXT,
    agent_port TEXT,
    last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
    region TEXT DEFAULT 'UNKNOWN',
    node_alias TEXT,
    enable_google TEXT DEFAULT 'true',
    enable_trust TEXT DEFAULT 'true',
    enable_ota TEXT DEFAULT 'false',
    PRIMARY KEY(chat_id, node_name)
);

CREATE TABLE IF NOT EXISTS ip_trend_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_name TEXT,
    check_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    scam_score INTEGER,
    goog_status TEXT,
    nf_status TEXT,
    gpt_status TEXT
);
EOF
echo "✅ 数据库创建成功: $DB_FILE"

chmod 600 "${MASTER_DIR}/master.conf"
chmod 600 "$DB_FILE"

# ==========================================================
# [原子交接] 防变砖双缓冲下载，确保执行层无断层覆写
# ==========================================================
echo -e "\n[4/4] 正在下载 Master 程序 (Python)..."

TMP_PY="${SECURE_TMP}/py_master"
mkdir -p "${TMP_PY}/master"
curl -fsSL --connect-timeout 10 --retry 3 "${REPO_RAW_URL}/py/run_master.py" -o "${TMP_PY}/run_master.py"
MASTER_PY_MODS="__init__.py config.py db.py flags.py security.py telegram_api.py agent_client.py handlers.py bot.py __main__.py"
for MPY in $MASTER_PY_MODS; do
    curl -fsSL --connect-timeout 10 --retry 3 "${REPO_RAW_URL}/py/master/${MPY}" -o "${TMP_PY}/master/${MPY}"
done

curl -fsSL --connect-timeout 10 --retry 3 "${REPO_RAW_URL}/pyproject.toml" -o "${SECURE_TMP}/pyproject.toml"
curl -fsSL --connect-timeout 10 --retry 3 "${REPO_RAW_URL}/uv.lock" -o "${SECURE_TMP}/uv.lock"
curl -fsSL --connect-timeout 10 --retry 3 "${REPO_RAW_URL}/.python-version" -o "${SECURE_TMP}/.python-version"

if [ ! -s "${TMP_PY}/run_master.py" ] || [ ! -s "${TMP_PY}/master/bot.py" ] || \
   [ ! -s "${SECURE_TMP}/pyproject.toml" ] || [ ! -s "${SECURE_TMP}/uv.lock" ]; then
    echo -e "\033[31m❌ 下载失败：核心文件缺失，请检查网络或 GitHub Raw。\033[0m"
    echo "已中止更新，现有 Master 未被覆盖。"
    rm -rf "$TMP_PY" "${SECURE_TMP}/pyproject.toml" "${SECURE_TMP}/uv.lock" "${SECURE_TMP}/.python-version"
    exit 1
fi

echo "⏳ 校验通过，正在停止旧进程..."
if is_systemd; then
    systemctl kill --signal=SIGKILL ip-sentinel-master.service >/dev/null 2>&1 || true
    systemctl stop ip-sentinel-master.service >/dev/null 2>&1 || true
fi
pkill -9 -f "tg_master.sh" >/dev/null 2>&1 || true
pkill -9 -f "run_master.py" >/dev/null 2>&1 || true
pkill -9 -f "master.bot" >/dev/null 2>&1 || true
pkill -9 -f "uv run.*run_master" >/dev/null 2>&1 || true

rm -f "${MASTER_DIR}/tg_master.sh" 2>/dev/null
rm -rf "${MASTER_DIR}/py" 2>/dev/null
mv "$TMP_PY" "${MASTER_DIR}/py"
chmod +x "${MASTER_DIR}/py/run_master.py" 2>/dev/null || true

mv "${SECURE_TMP}/pyproject.toml" "${MASTER_DIR}/pyproject.toml"
mv "${SECURE_TMP}/uv.lock" "${MASTER_DIR}/uv.lock"
mv "${SECURE_TMP}/.python-version" "${MASTER_DIR}/.python-version"

ensure_uv
uv_sync_project "${MASTER_DIR}" || exit 1

if is_systemd; then
    echo "💡 检测到 Systemd 环境，正在部署原生守护服务..."
    
    cat > /etc/systemd/system/ip-sentinel-master.service << EOF
[Unit]
Description=IP-Sentinel Master Command Center Service
After=network.target

[Service]
Environment="PATH=${UV_PATH}"
SyslogIdentifier=ip-sentinel
Type=simple
ExecStart=${UV_BIN} run python py/run_master.py
Restart=always
RestartSec=5
User=root
WorkingDirectory=${MASTER_DIR}
CPUSchedulingPolicy=idle
IOSchedulingClass=idle

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable --now ip-sentinel-master.service
    systemctl restart ip-sentinel-master.service
    
    crontab -l 2>/dev/null | grep -v "tg_master.sh" | grep -v "run_master.py" | crontab - >/dev/null 2>&1 || true
else
    echo "💡 未检测到 Systemd，回退到 Cron 看门狗调度模式..."
    crontab -l 2>/dev/null | grep -v "tg_master.sh" | grep -v "run_master.py" > "${SECURE_TMP}/cron_master" || true
    echo "* * * * * pgrep -f run_master.py >/dev/null || nohup ${UV_BIN} run --directory ${MASTER_DIR} python py/run_master.py >/dev/null 2>&1 &" >> "${SECURE_TMP}/cron_master"
    [ -f "${SECURE_TMP}/cron_master" ] && crontab "${SECURE_TMP}/cron_master" 2>/dev/null
    
    pgrep -f run_master.py >/dev/null || { nohup ${UV_BIN} run --directory "${MASTER_DIR}" python py/run_master.py >/dev/null 2>&1 & disown 2>/dev/null; }
fi

# ==========================================================
# [状态汇报] 根据操作场景分发回执
# ==========================================================
echo "========================================================"
if [ "$UPGRADE_MODE" == "true" ]; then
    echo "🎉 Master 升级完成。"
    echo "服务已重启，可继续管理 Agent 节点。"
    
    if [ "$SILENT_MASTER_OTA" == "true" ] && [ -n "$OTA_CHAT_ID" ] && [ -n "$TG_TOKEN" ]; then
        echo -e "\n📡 正在发送 OTA 完成通知…"
        curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
            -d "chat_id=${OTA_CHAT_ID}" \
            -d "parse_mode=Markdown" \
            -d "text=✨ *Master 升级完成*
🚀 当前版本：\`v${TARGET_VERSION}\`
服务已重新启动。" > /dev/null
    fi
else
    echo "🎉 Master 安装完成。"
    echo "Telegram Bot 已启动，等待 Agent 节点注册。"
fi
echo "========================================================"

if [ "$UPGRADE_MODE" == "false" ]; then
    echo -e "\n📡 正在向开源社区汇报装机量 (完全匿名，不收集IP)..."
    MASTER_COUNT=$(curl -s -m 3 "https://ip-sentinel-count.samanthaestime296.workers.dev/ping/master" || echo "")

    if [ -n "$MASTER_COUNT" ] && [[ "$MASTER_COUNT" =~ ^[0-9]+$ ]]; then
        echo -e "\033[32m✅ 您是全球第 ${MASTER_COUNT} 位 IP-Sentinel Master 用户。\033[0m"
    else
        echo -e "\033[32m✅ 感谢部署 IP-Sentinel Master。\033[0m"
    fi
fi

echo -e "\n========================================================"
echo -e "⭐ \033[33m如果本项目对您有帮助，欢迎在 GitHub 点 Star。\033[0m"
echo -e "💡 \033[32m您的每一颗 Star 都是我们持续迭代架构、开发 Web 视窗化控制台的动力源泉。\033[0m"
echo -e "👉 \033[36m\033[4m\033]8;;https://github.com/lasitan/IP-Sentinel\033\\点击此处直达 GitHub 仓库点亮 Star 🌟\033[0m\033]8;;\033\\"
echo -e "========================================================\n"