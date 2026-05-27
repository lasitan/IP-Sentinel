#!/bin/bash

# ==========================================================
# 脚本名称: install.sh
# 功能: Agent 安装、升级与卸载
# ==========================================================

# ==========================================================
# 需要 root 权限
# ==========================================================
if [ "$EUID" -ne 0 ]; then
  echo -e "\033[31m❌ 权限被拒绝: 部署 IP-Sentinel 需要最高系统权限。\033[0m"
  echo -e "💡 请切换到 root 用户 (执行 su root 或 sudo -i) 后重新运行指令。"
  exit 1
fi

# 临时目录，脚本退出时自动删除
SECURE_TMP=$(mktemp -d /tmp/ips_install.XXXXXX)
trap 'rm -rf "$SECURE_TMP"' EXIT HUP INT QUIT TERM

# ==========================================================
# 检测系统环境与 init 类型
# ==========================================================
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
echo -e "📊 \033[36mIP-Sentinel 环境检查\033[0m"
echo -e "--------------------------------------"
echo -e "OS 架构   : $(get_os_info)"
echo -e "虚拟化    : $(get_virt_info)"
if is_systemd; then
    echo -e "Init 系统 : systemd ✅"
else
    echo -e "Init 系统 : 非 systemd ⚠️ (将使用循环调度脚本)"
fi
echo -e "======================================\n"
sleep 1

REPO_RAW_URL="https://raw.githubusercontent.com/hotyue/IP-Sentinel/main"
INSTALL_DIR="/opt/ip_sentinel"
CONFIG_FILE="${INSTALL_DIR}/config.conf"
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

# 从远端获取 Agent 版本号
TARGET_VERSION=$( (curl -fsSL --connect-timeout 5 --retry 2 "${REPO_RAW_URL}/version.txt" || curl -4 -fsSL --connect-timeout 5 --retry 2 "${REPO_RAW_URL}/version.txt") 2>/dev/null | grep "^AGENT_VERSION=" | cut -d'=' -f2 | tr -d '[:space:]')
TARGET_VERSION=${TARGET_VERSION:-"4.1.1"}

version_lt() {
    test "$(printf '%s\n' "$1" "$2" | sort -V | head -n 1)" = "$1" && test "$1" != "$2"
}

# ==========================================================
# 安装系统依赖与 uv
# ==========================================================
echo -e "\n[1/7] 正在探测并安装基础环境依赖 (curl, jq, cron, procps, uv)..."
REQUIRED_CMDS=("curl" "jq" "crontab" "pgrep" "openssl")
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
        apt-get install -y --no-install-recommends curl jq cron procps openssl ca-certificates >/dev/null 2>&1
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
        $PKG_MGR install -y $OPT_ARGS curl jq cronie procps-ng openssl
        systemctl enable crond >/dev/null 2>&1 && systemctl start crond >/dev/null 2>&1
        
    elif command -v apk >/dev/null 2>&1; then
        echo "Alpine 探测到系统类型为 Alpine Linux，正在执行轻量级安装..."
        apk add --no-cache curl jq cronie procps bash openssl ca-certificates || apk add --no-cache curl jq procps bash openssl ca-certificates
        mkdir -p /var/spool/cron/crontabs
        rc-update add crond default >/dev/null 2>&1
        service crond start >/dev/null 2>&1
        
    elif command -v pacman >/dev/null 2>&1; then
        pacman -S --needed --noconfirm curl jq cronie procps-ng openssl >/dev/null 2>&1
        mkdir -p /root/.cache/crontab 2>/dev/null
        systemctl enable cronie >/dev/null 2>&1 && systemctl start cronie >/dev/null 2>&1
        
    else
        echo -e "\033[31m❌ 自动安装失败：系统未知的包管理器。\033[0m"
        echo -e "\033[33m⚠️ 请根据您的操作系统，手动执行以下安装命令后重新运行本脚本：\033[0m"
        echo -e "  Debian/Ubuntu: \033[36mapt-get update && apt-get install -y --no-install-recommends curl jq cron procps openssl\033[0m"
        echo -e "  CentOS/RHEL:   \033[36myum install -y curl jq cronie procps-ng openssl\033[0m"
        echo -e "  Alpine Linux:  \033[36mapk add --no-cache curl jq cronie procps bash openssl\033[0m"
        echo -e "  Arch Linux:    \033[36mpacman -Syu --needed curl jq cronie procps-ng openssl\033[0m"
        exit 1
    fi
    
    for cmd in "${REQUIRED_CMDS[@]}"; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            echo -e "\033[31m❌ 致命错误：核心命令 '$cmd' 仍未找到！\033[0m"
            echo -e "这通常是因为您的系统源配置错误或缺失基础组件库导致。"
            echo -e "请手动修复您的包管理器源，或联系 VPS 供应商重新格式化系统。"
            exit 1
        fi
    done
fi
ensure_uv
echo -e "\033[32m✅ 基础环境检测通过。\033[0m"

# ----------------------------------------------------------
# 下载区域地图并引导配置
# ----------------------------------------------------------
echo -e "\n[2/7] 正在下载区域地图 (map.json)..."
curl -fsSL --connect-timeout 10 --retry 3 "${REPO_RAW_URL}/data/map.json" -o "${SECURE_TMP}/map.json"
if [ ! -s "${SECURE_TMP}/map.json" ]; then
    echo -e "\033[31m❌ 拉取全球地图失败！请检查网络或 GitHub 仓库地址。\033[0m"
    exit 1
fi

# OTA 模式：跳过交互菜单
if [ "$SILENT_OTA" == "true" ]; then
    echo -e "\n⏳ [OTA] 开始自动升级..."
    ACTION_CHOICE=1
    UPGRADE_MODE="true"
    KEEP_LOGS="true"
    source "$CONFIG_FILE"
else
    echo -e "\n请选择操作:"
    echo "  1) 🚀 部署边缘节点 (进入全球节点配置)"
    echo "  2) 🗑️ 一键卸载 IP-Sentinel"
    read -p "请输入选择 [1-2] (默认1): " ACTION_CHOICE

    ACTION_CHOICE=${ACTION_CHOICE:-1}

    if [ "$ACTION_CHOICE" == "2" ]; then
        echo -e "\n⏳ 正在拉取卸载程序..."
        curl -fsSL --connect-timeout 10 --retry 3 "${REPO_RAW_URL}/core/uninstall.sh" -o "${SECURE_TMP}/ip_uninstall.sh"
        chmod +x "${SECURE_TMP}/ip_uninstall.sh"
        bash "${SECURE_TMP}/ip_uninstall.sh"
        rm -f "${SECURE_TMP}/ip_uninstall.sh"
        exit 0
    fi

    # 检测是否已安装，询问是否保留配置升级
    UPGRADE_MODE="false"
    KEEP_LOGS="true"

    if [ "$ACTION_CHOICE" == "1" ] && [ -f "$CONFIG_FILE" ]; then
        echo -e "\n\033[33m💡 检测到本机已安装 IP-Sentinel。\033[0m"
        read -p "是否保留现有配置并升级？(y/n, 默认 y): " UPGRADE_CHOICE
        if [[ -z "$UPGRADE_CHOICE" || "$UPGRADE_CHOICE" =~ ^[Yy]$ ]]; then
            UPGRADE_MODE="true"
            read -p "👉 是否保留历史运行日志？(y/n, 默认y): " LOG_CHOICE
            if [[ "$LOG_CHOICE" =~ ^[Nn]$ ]]; then
                KEEP_LOGS="false"
            fi
            
            source "$CONFIG_FILE"
            echo -e "\033[32m✅ 升级模式：将保留配置并更新程序文件。\033[0m"
        else
            echo -e "\033[33m🔄 将重新配置，现有 Agent 数据会被清除。\033[0m"
        fi
    fi
fi

# ==========================================================
# 清理旧版 cron 与本地数据（升级时可保留日志）
# ==========================================================
echo -e "\n⏳ 正在清理系统定时任务中的旧版条目..."

crontab -l 2>/dev/null | grep -v "ip_sentinel" > "${SECURE_TMP}/cron_clean" || true
[ -f "${SECURE_TMP}/cron_clean" ] && crontab "${SECURE_TMP}/cron_clean" >/dev/null 2>&1
rm -f "${SECURE_TMP}/cron_clean"

for CRON_FILE in "/var/spool/cron/crontabs/root" "/etc/crontabs/root"; do
    if [ -f "$CRON_FILE" ]; then
        grep -v "ip_sentinel" "$CRON_FILE" > "${CRON_FILE}.tmp" 2>/dev/null || true
        cat "${CRON_FILE}.tmp" > "$CRON_FILE" 2>/dev/null || true
        rm -f "${CRON_FILE}.tmp" 2>/dev/null
    fi
done
rm -f /etc/local.d/ip_sentinel.start 2>/dev/null

if [ "$UPGRADE_MODE" == "true" ]; then
    if [ "$KEEP_LOGS" == "false" ]; then
        rm -rf "${INSTALL_DIR}/logs" 2>/dev/null
        echo -e "🗑️ 历史日志已按指令清空。"
    else
        echo -e "📦 已保留历史配置与日志。"
    fi
else
    if [ -d "$INSTALL_DIR" ]; then
        rm -rf "${INSTALL_DIR}/core" "${INSTALL_DIR}/data" "${INSTALL_DIR}/config.conf" "${INSTALL_DIR}/.last_ip" 2>/dev/null
    fi
fi
echo -e "\033[32m✅ 环境清理完成。\033[0m"

# ==========================================================
# 交互式选择区域与节点参数
# ==========================================================
if [ "$UPGRADE_MODE" == "false" ]; then

    echo -e "\n\033[36m📍 请选择大洲 (Continent):\033[0m"
    jq -r '.continents[] | "\(.id)|\(.name)"' "${SECURE_TMP}/map.json" > "${SECURE_TMP}/continents.txt"
    i=1; CONT_MAP=()
    while IFS="|" read -r cont_id cont_name; do
        echo "  $i) $cont_name"
        CONT_MAP[$i]="$cont_id"
        ((i++))
    done < "${SECURE_TMP}/continents.txt"

    read -p "请输入选择 [1-$((i-1))] (默认1): " CONT_SEL
    CONT_SEL=${CONT_SEL:-1}
    CONT_ID="${CONT_MAP[$CONT_SEL]}"

    echo -e "\n\033[36m📍 请选择 [$CONT_ID] 下的国家/地区...\033[0m"
    jq -r ".continents[] | select(.id==\"$CONT_ID\") | .countries[] | \"\(.id)|\(.name)|\(.keyword_file)\"" "${SECURE_TMP}/map.json" > "${SECURE_TMP}/countries.txt"
    i=1; COUNTRY_MAP=(); KEYWORD_MAP=()
    while IFS="|" read -r c_id c_name k_file; do
        echo "  $i) $c_name"
        COUNTRY_MAP[$i]="$c_id"
        KEYWORD_MAP[$i]="$k_file"
        ((i++))
    done < "${SECURE_TMP}/countries.txt"

    read -p "请输入选择 [1-$((i-1))] (默认1): " C_SEL
    C_SEL=${C_SEL:-1}
    COUNTRY_ID="${COUNTRY_MAP[$C_SEL]}"
    KEYWORD_FILE="${KEYWORD_MAP[$C_SEL]}"
    REGION_CODE="$COUNTRY_ID" 

    echo -e "\n\033[36m📍 请选择 [$COUNTRY_ID] 的省/州...\033[0m"
    jq -r ".continents[] | select(.id==\"$CONT_ID\") | .countries[] | select(.id==\"$COUNTRY_ID\") | .states[] | \"\(.id)|\(.name)\"" "${SECURE_TMP}/map.json" > "${SECURE_TMP}/states.txt"
    STATE_COUNT=$(wc -l < "${SECURE_TMP}/states.txt")

    if [ "$STATE_COUNT" -eq 1 ]; then
        IFS="|" read -r STATE_ID STATE_NAME < "${SECURE_TMP}/states.txt"
        echo -e "\033[32m💡 该国家仅有一个省/州 [$STATE_NAME]，已自动选择。\033[0m"
    else
        i=1; STATE_MAP=()
        while IFS="|" read -r s_id s_name; do
            echo "  $i) $s_name"
            STATE_MAP[$i]="$s_id"
            ((i++))
        done < "${SECURE_TMP}/states.txt"
        read -p "请输入选择 [1-$((i-1))] (默认1): " S_SEL
        S_SEL=${S_SEL:-1}
        STATE_ID="${STATE_MAP[$S_SEL]}"
    fi

    echo -e "\n\033[36m📍 请选择城市:\033[0m"
    jq -r ".continents[] | select(.id==\"$CONT_ID\") | .countries[] | select(.id==\"$COUNTRY_ID\") | .states[] | select(.id==\"$STATE_ID\") | .cities[] | \"\(.id)|\(.name)\"" "${SECURE_TMP}/map.json" > "${SECURE_TMP}/cities.txt"
    CITY_COUNT=$(wc -l < "${SECURE_TMP}/cities.txt")

    if [ "$CITY_COUNT" -eq 1 ]; then
        IFS="|" read -r CITY_ID CITY_NAME < "${SECURE_TMP}/cities.txt"
        echo -e "\033[32m💡 该区域仅有一个城市 [$CITY_NAME]，已自动选择。\033[0m"
    else
        i=1; CITY_MAP=(); CITY_NAME_MAP=()
        while IFS="|" read -r c_id c_name; do
            echo "  $i) $c_name"
            CITY_MAP[$i]="$c_id"
            CITY_NAME_MAP[$i]="$c_name"
            ((i++))
        done < "${SECURE_TMP}/cities.txt"
        read -p "请输入选择 [1-$((i-1))] (默认1): " CI_SEL
        CI_SEL=${CI_SEL:-1}
        CITY_ID="${CITY_MAP[$CI_SEL]}"
        CITY_NAME="${CITY_NAME_MAP[$CI_SEL]}"
    fi

    rm -f "${SECURE_TMP}/map.json" "${SECURE_TMP}/continents.txt" "${SECURE_TMP}/countries.txt" "${SECURE_TMP}/states.txt" "${SECURE_TMP}/cities.txt"

    mkdir -p "${INSTALL_DIR}/core"
    mkdir -p "${INSTALL_DIR}/data/keywords"
    mkdir -p "${INSTALL_DIR}/data/regions/${COUNTRY_ID}/${STATE_ID}"
    mkdir -p "${INSTALL_DIR}/logs"

    echo -e "\n[3/7] 正在配置功能模块 (默认全部启用，可在 Telegram 中远程开关)..."
    ENABLE_GOOGLE="true"
    ENABLE_TRUST="true"

    echo -e "\n[4/7] 是否连接 Master 进行 Telegram 远程管理？ (y/n)"
    read -p "请输入选择 [y/n] (默认n): " TG_CHOICE
    TG_TOKEN=""
    CHAT_ID=""
    AGENT_PORT="9527"
    if [[ "$TG_CHOICE" =~ ^[Yy]$ ]]; then
        echo -e "\n请选择 Master 接入方式 (私有部署支持 OTA 远程升级):"
        echo "  1) 私有 Master (自建 Bot Token，推荐)"
        echo "  2) 官方公共网关 (@OmniBeacon_bot)"
        read -p "请输入选择 [1-2] (默认1): " MASTER_TYPE
        MASTER_TYPE=${MASTER_TYPE:-1}
        
        if [ "$MASTER_TYPE" == "2" ]; then
            TG_TOKEN="OFFICIAL_GATEWAY_MODE" 
            TG_API_URL="https://omni-gateway.samanthaestime296.workers.dev" 
            ENABLE_OTA="false"
            echo -e "\033[32m✅ 已自动连接官方安全网关 (@OmniBeacon_bot)。\033[0m"
            echo -e "\033[33m👉 请确保您已在 TG 中关注官方机器人并发送过 /start，否则将无法接收消息。\033[0m"
            echo -e "\n\033[33m⚠️ 安全说明\033[0m"
            echo -e "\033[33m使用官方公共网关时，本节点的 OTA 远程升级功能已禁用。\033[0m"
            echo -e "\033[33m如需 OTA，请部署私有 Master 后重新安装 Agent。\033[0m"
        else
            echo -e "\n\033[36m📘 私有 Bot 创建教程: \033[4m\033]8;;https://blog.iot-architect.com/engineering-practice/create-private-telegram-bot-via-botfather/\033\\👉 [点击此处直接在浏览器中打开]\033]8;;\033\\ 👈\033[0m"
            echo -e "\033[90m   (若您的终端较老不支持点击，请手动复制: https://blog.iot-architect.com/engineering-practice/create-private-telegram-bot-via-botfather/ )\033[0m"
            read -p "请输入您的私有 Telegram Bot Token: " RAW_TOKEN
            USER_TOKEN=$(echo "$RAW_TOKEN" | tr -cd 'a-zA-Z0-9_:-')
            while [ -z "$USER_TOKEN" ]; do
                read -p "⚠️ Token 不能为空或包含非法字符，请重新输入: " RAW_TOKEN
                USER_TOKEN=$(echo "$RAW_TOKEN" | tr -cd 'a-zA-Z0-9_:-')
            done
            
            TG_TOKEN="$USER_TOKEN"
            TG_API_URL="https://api.telegram.org/bot${TG_TOKEN}/sendMessage"
            echo -e "\033[32m✅ 已记录您的私有机器人 Token。\033[0m"
            
            echo -e "\n\033[36m[4.1/7] OTA 远程升级\033[0m"
            echo -e "💡 开启后，可通过 Telegram 对本节点执行远程升级。"
            read -p "是否允许本节点接收 OTA 升级指令？(y/n, 默认 y): " OTA_CHOICE
            if [[ "$OTA_CHOICE" =~ ^[Nn]$ ]]; then
                ENABLE_OTA="false"
                echo -e "🛡️ \033[33m已关闭 OTA 权限，本节点未来将只能通过 SSH 手动升级。\033[0m"
            else
                ENABLE_OTA="true"
                echo -e "✅ \033[32m已开启 OTA 远程升级。\033[0m"
            fi
        fi

        echo -e "\n\033[33m💡 提示：如果您不知道下方自己的 Chat ID 是什么，可以关注 @userinfobot 获取。\033[0m"
        echo -e "\033[36m📘 查看图文教程: \033[4m\033]8;;https://blog.iot-architect.com/engineering-practice/get-telegram-personal-id-via-userinfobot/\033\\👉 [点击此处直接在浏览器中打开]\033]8;;\033\\ 👈\033[0m"
        echo -e "\033[90m   (若您的终端较老不支持点击，请手动复制: https://blog.iot-architect.com/engineering-practice/get-telegram-personal-id-via-userinfobot/ )\033[0m"
        read -p "请输入你的 Chat ID (必须准确，否则无法联控): " RAW_CHAT_ID
        CHAT_ID=$(echo "$RAW_CHAT_ID" | tr -cd '0-9-')
        
        echo -e "\n\033[36m[4.2/7] 正在构建 Webhook 安全通信隧道...\033[0m"
        echo -n "🎲 正在探测可用随机端口..."
        while true; do
            RANDOM_PORT=$((RANDOM % 55536 + 10000))
            if ! (ss -tuln 2>/dev/null | grep -q ":$RANDOM_PORT " || netstat -tuln 2>/dev/null | grep -q ":$RANDOM_PORT "); then
                break
            fi
            echo -n "."
        done
        echo -e " 完成！"
        
        echo -e "💡 系统为您生成的推荐随机高位端口为: \033[32m$RANDOM_PORT\033[0m"
        echo -e "\033[33m(该端口已通过本地占用校验，可直接使用)\033[0m"
        
        while true; do
            read -p "请输入 Webhook 监听端口 (回车采用推荐, 或手动输入): " INPUT_PORT
            
            if [ -z "$INPUT_PORT" ]; then
                AGENT_PORT="$RANDOM_PORT"
                break
            else
                if [[ "$INPUT_PORT" =~ ^[0-9]+$ ]] && [ "$INPUT_PORT" -ge 1 ] && [ "$INPUT_PORT" -le 65535 ]; then
                    if (ss -tuln 2>/dev/null | grep -q ":$INPUT_PORT " || netstat -tuln 2>/dev/null | grep -q ":$INPUT_PORT "); then
                        echo -e "\033[31m❌ 端口 $INPUT_PORT 已被占用，请重新输入或使用推荐端口。\033[0m"
                    else
                        AGENT_PORT="$INPUT_PORT"
                        break
                    fi
                else
                    echo -e "\033[31m❌ 输入非法！端口范围应为 1-65535。\033[0m"
                fi
            fi
        done
        echo -e "✅ 已锁定 Webhook 通讯端口: \033[32m$AGENT_PORT\033[0m"
    fi

    # ----------------------------------------------------------
    # 检测公网 IP 与出站接口
    # ----------------------------------------------------------
    echo -e "\n\033[36m[4.5/7] 正在检测公网 IP 与出站接口...\033[0m"

    DETECT_V4=$( (curl -4 -s -m 3 api.ip.sb/ip || curl -4 -s -m 3 ifconfig.me || curl -4 -s -m 3 ipv4.icanhazip.com) 2>/dev/null | grep -E "^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+" | head -n 1 | tr -d '[:space:]')
    DETECT_V6=$( (curl -6 -s -m 3 api.ip.sb/ip || curl -6 -s -m 3 ifconfig.me || curl -6 -s -m 3 ipv6.icanhazip.com) 2>/dev/null | grep -E "^[0-9a-fA-F:]+.*:" | head -n 1 | tr -d '[:space:]')

    IP_OPTIONS=()
    IP_PROTO=()

    [[ -n "$DETECT_V4" ]] && { IP_OPTIONS+=("$DETECT_V4"); IP_PROTO+=("4"); }
    [[ -n "$DETECT_V6" ]] && { IP_OPTIONS+=("$DETECT_V6"); IP_PROTO+=("6"); }

    if [ ${#IP_OPTIONS[@]} -eq 0 ]; then
        echo -e "\033[33m⚠️ 未能自动检测到公网 IP，请手动输入。\033[0m"
        read -p "请输入您要绑定的公网 IP (v4 或 v6): " RAW_PUBLIC_IP
        PUBLIC_IP=$(echo "$RAW_PUBLIC_IP" | tr -cd 'a-fA-F0-9.:[]')
        [[ "$PUBLIC_IP" == *":"* ]] && IP_PREF="6" || IP_PREF="4"
    else
        echo "📍 检测到以下公网 IP，请选择用于注册的地址:"
        for i in "${!IP_OPTIONS[@]}"; do
            num=$((i+1))
            if [ "${IP_PROTO[$i]}" == "4" ]; then
                echo "  $num) 🌐 IPv4: ${IP_OPTIONS[$i]} (默认选项)"
            else
                echo "  $num) 🌌 IPv6: ${IP_OPTIONS[$i]}"
            fi
        done
        CUSTOM_OPT=$(( ${#IP_OPTIONS[@]} + 1 ))
        echo "  $CUSTOM_OPT) ✍️ 手动指定其他 IP (适合多 IP 站群机)"
        
        read -p "请输入选择 (默认1): " IP_CHOICE
        IP_CHOICE=${IP_CHOICE:-1}
        
        if [ "$IP_CHOICE" -le "${#IP_OPTIONS[@]}" ] && [ "$IP_CHOICE" -gt 0 ]; then
            idx=$((IP_CHOICE-1))
            PUBLIC_IP="${IP_OPTIONS[$idx]}"
            IP_PREF="${IP_PROTO[$idx]}"
        elif [ "$IP_CHOICE" -eq "$CUSTOM_OPT" ]; then
            read -p "请输入您要绑定的公网 IP (v4 或 v6): " PUBLIC_IP
            [[ "$PUBLIC_IP" == *":"* ]] && IP_PREF="6" || IP_PREF="4"
        else
            PUBLIC_IP="${IP_OPTIONS[0]}"
            IP_PREF="${IP_PROTO[0]}"
        fi
    fi

    # IPv6 地址加方括号，便于 URL 解析
    if [[ "$PUBLIC_IP" == *":"* ]] && [[ "$PUBLIC_IP" != *"["* ]]; then
        SAFE_PUBLIC_IP="[${PUBLIC_IP}]"
    else
        SAFE_PUBLIC_IP="$PUBLIC_IP"
    fi

    echo -n "正在测试出站连接 (NAT/双栈)..."
    RAW_TEST_IP=$(echo "$SAFE_PUBLIC_IP" | tr -d '[]')
    
    if [[ "$RAW_TEST_IP" == *":"* ]]; then
        TEST_TARGET="https://[2606:4700:4700::1111]"
    else
        TEST_TARGET="https://1.1.1.1"
    fi
    
    if curl --interface "$RAW_TEST_IP" -sI -m 3 "$TEST_TARGET" >/dev/null 2>&1; then
        echo -e " \033[32m✅ 已绑定网卡接口。\033[0m"
        BIND_IP="$SAFE_PUBLIC_IP"
    else
        echo -e " \033[33m⚠️ 检测到 NAT 环境，将使用系统默认路由，不绑定网卡。\033[0m"
        BIND_IP=""
    fi
    echo -e "\033[32m✅ 已设置公网 IP: $SAFE_PUBLIC_IP\033[0m"

    # 节点 ID (NODE_NAME) 与展示别名 (NODE_ALIAS)
    IP_HASH=$(echo "${SAFE_PUBLIC_IP:-127.0.0.1}" | md5sum | cut -c 1-4 | tr 'a-z' 'A-Z')
    NODE_NAME="$(hostname | tr -cd 'a-zA-Z0-9' | cut -c 1-10)-${IP_HASH}"
    NODE_ALIAS="$NODE_NAME"

    if [[ -n "$TG_TOKEN" ]] && [[ -n "$CHAT_ID" ]]; then
        echo -e "\n\033[36m[4.8/7] 节点展示别名设定 (用于面板友好显示)...\033[0m"
        echo -e "💡 系统底层的不可变主键为: \033[33m${NODE_NAME}\033[0m"
        read -p "请输入节点展示别名 (如'纽约机房', 回车使用默认): " CUSTOM_ALIAS

        if [ -n "$CUSTOM_ALIAS" ]; then
            NODE_ALIAS=$(echo "$CUSTOM_ALIAS" | tr -d '"'\''\`\$\|&;<>\n\r' | cut -c 1-20)
            [ -z "$NODE_ALIAS" ] && NODE_ALIAS="$NODE_NAME"
        fi
        echo -e "✅ 已锁定节点展示别名: \033[32m$NODE_ALIAS\033[0m"
    fi

    # 5. 远程拉取冷数据并解析固化
    echo -e "\n[5/7] 正在从云端数据仓库拉取 [${CITY_NAME}] 节点的底层规则..."
    REGION_JSON_FILE="${INSTALL_DIR}/data/regions/${COUNTRY_ID}/${STATE_ID}/${CITY_ID}.json"
    curl -fsSL --connect-timeout 10 --retry 3 "${REPO_RAW_URL}/data/regions/${COUNTRY_ID}/${STATE_ID}/${CITY_ID}.json" -o "$REGION_JSON_FILE"

    if [ ! -s "$REGION_JSON_FILE" ]; then
        echo "❌ 拉取或解析规则失败！请检查 Forgejo 仓库是否公开或网络是否畅通。"
        exit 1
    fi

    REGION_NAME=$(jq -r '.region_name' "$REGION_JSON_FILE")
    BASE_LAT=$(jq -r '.google_module.base_lat' "$REGION_JSON_FILE")
    BASE_LON=$(jq -r '.google_module.base_lon' "$REGION_JSON_FILE")
    LANG_PARAMS=$(jq -r '.google_module.lang_params' "$REGION_JSON_FILE")
    VALID_URL_SUFFIX=$(jq -r '.google_module.valid_url_suffix' "$REGION_JSON_FILE")

    cat > "$CONFIG_FILE" << EOF
# IP-Sentinel 本地固化配置 (生成时间: $(date '+%Y-%m-%d %H:%M:%S'))
AGENT_VERSION="$TARGET_VERSION"
REGION_CODE="$REGION_CODE"
REGION_NAME="$REGION_NAME"
BASE_LAT="$BASE_LAT"
BASE_LON="$BASE_LON"
LANG_PARAMS="$LANG_PARAMS"
VALID_URL_SUFFIX="$VALID_URL_SUFFIX"

# 模块开关状态
ENABLE_GOOGLE="$ENABLE_GOOGLE"
ENABLE_TRUST="$ENABLE_TRUST"

TG_TOKEN="$TG_TOKEN"
TG_API_URL="$TG_API_URL"
CHAT_ID="$CHAT_ID"
AGENT_PORT="$AGENT_PORT"
INSTALL_DIR="$INSTALL_DIR"
LOG_FILE="${INSTALL_DIR}/logs/sentinel.log"

IP_PREF="$IP_PREF"
PUBLIC_IP="$SAFE_PUBLIC_IP"
BIND_IP="$BIND_IP"

NODE_NAME="$NODE_NAME"
NODE_ALIAS="$NODE_ALIAS"

ENABLE_OTA="$ENABLE_OTA"
EOF

    chmod 600 "$CONFIG_FILE"

fi

# ----------------------------------------------------------
# 升级时迁移旧版配置字段
# ----------------------------------------------------------
if [ "$UPGRADE_MODE" == "true" ]; then
    if ! grep -q "PUBLIC_IP=" "$CONFIG_FILE"; then
        echo -e "\n🔄 正在迁移旧版配置 (补充 PUBLIC_IP 等字段)..."
        
        MIGRATE_IP=$(curl -${IP_PREF:-4} -s -m 5 api.ip.sb/ip | tr -d '[:space:]')
        [[ "$MIGRATE_IP" == *":"* ]] && [[ "$MIGRATE_IP" != *"["* ]] && MIGRATE_IP="[${MIGRATE_IP}]"
        
        echo -n "正在测试出站连接..."
        RAW_TEST_IP=$(echo "$MIGRATE_IP" | tr -d '[]')
        if [[ "$RAW_TEST_IP" == *":"* ]]; then
            TEST_TARGET="https://[2606:4700:4700::1111]"
        else
            TEST_TARGET="https://1.1.1.1"
        fi
        
        if curl --interface "$RAW_TEST_IP" -sI -m 3 "$TEST_TARGET" >/dev/null 2>&1; then
            echo -e " \033[32m✅ 已绑定网卡接口。\033[0m"
            NEW_BIND_IP="$MIGRATE_IP"
        else
            echo -e " \033[33m⚠️ NAT 环境，不绑定网卡。\033[0m"
            NEW_BIND_IP=""
        fi
        
        sed -i "s/^BIND_IP=.*/BIND_IP=\"$NEW_BIND_IP\"/" "$CONFIG_FILE"
        echo "PUBLIC_IP=\"$MIGRATE_IP\"" >> "$CONFIG_FILE"
        
        SAFE_PUBLIC_IP="$MIGRATE_IP"
        BIND_IP="$NEW_BIND_IP"
    else
        SAFE_PUBLIC_IP="${PUBLIC_IP}"
    fi

    if ! grep -q "^NODE_NAME=" "$CONFIG_FILE"; then
        TMP_HASH=$(echo "${SAFE_PUBLIC_IP:-127.0.0.1}" | md5sum | cut -c 1-4 | tr 'a-z' 'A-Z')
        NODE_NAME="$(hostname | tr -cd 'a-zA-Z0-9' | cut -c 1-10)-${TMP_HASH}"
        NODE_ALIAS="$NODE_NAME"
        echo "NODE_NAME=\"$NODE_NAME\"" >> "$CONFIG_FILE"
        echo "NODE_ALIAS=\"$NODE_ALIAS\"" >> "$CONFIG_FILE"
    else
        NODE_NAME=$(grep "^NODE_NAME=" "$CONFIG_FILE" | cut -d'"' -f2)
        NODE_ALIAS=$(grep "^NODE_ALIAS=" "$CONFIG_FILE" | cut -d'"' -f2)
        if [ -z "$NODE_ALIAS" ]; then
            NODE_ALIAS="$NODE_NAME"
            echo "NODE_ALIAS=\"$NODE_ALIAS\"" >> "$CONFIG_FILE"
        fi
    fi

    if ! grep -q "^ENABLE_OTA=" "$CONFIG_FILE"; then
        echo "ENABLE_OTA=\"false\"" >> "$CONFIG_FILE"
        ENABLE_OTA="false"
    else
        ENABLE_OTA=$(grep "^ENABLE_OTA=" "$CONFIG_FILE" | cut -d'"' -f2)
    fi
fi

# ==========================================================
# 下载程序文件，校验通过后再停止旧进程
# ==========================================================
echo -e "\n[6/7] 正在部署核心引擎与热数据..."
mkdir -p "${INSTALL_DIR}/data/keywords"

TMP_UNINSTALL="${SECURE_TMP}/uninstall.sh"
curl -fsSL --connect-timeout 10 --retry 3 "${REPO_RAW_URL}/core/uninstall.sh" -o "$TMP_UNINSTALL"

TMP_PY="${SECURE_TMP}/py_update"
mkdir -p "$TMP_PY"
PY_FILES="__init__.py config.py log_util.py network.py persona.py geo_probe.py mod_google.py mod_trust.py mod_quality.py runner.py report.py webhook.py updater.py agent_daemon.py"
for PY_FILE in $PY_FILES; do
    curl -fsSL --connect-timeout 10 --retry 3 "${REPO_RAW_URL}/py/${PY_FILE}" -o "${TMP_PY}/${PY_FILE}"
done

curl -fsSL --connect-timeout 10 --retry 3 "${REPO_RAW_URL}/pyproject.toml" -o "${SECURE_TMP}/pyproject.toml"
curl -fsSL --connect-timeout 10 --retry 3 "${REPO_RAW_URL}/uv.lock" -o "${SECURE_TMP}/uv.lock"
curl -fsSL --connect-timeout 10 --retry 3 "${REPO_RAW_URL}/.python-version" -o "${SECURE_TMP}/.python-version"

# 校验下载文件完整性，失败则不覆盖现有安装
if [ ! -s "$TMP_UNINSTALL" ] || [ ! -s "${TMP_PY}/runner.py" ] || \
   [ ! -s "${TMP_PY}/webhook.py" ] || [ ! -s "${TMP_PY}/agent_daemon.py" ] || \
   [ ! -s "${SECURE_TMP}/pyproject.toml" ] || [ ! -s "${SECURE_TMP}/uv.lock" ]; then
    echo -e "\033[31m❌ 下载失败：核心文件缺失或为空，请检查网络或 GitHub Raw 可用性。\033[0m"
    echo "已中止更新，现有安装未被覆盖。"
    rm -f "$TMP_UNINSTALL" "${SECURE_TMP}/pyproject.toml" "${SECURE_TMP}/uv.lock" "${SECURE_TMP}/.python-version"
    rm -rf "$TMP_PY"
    exit 1
fi

echo "⏳ 校验通过，正在停止旧进程..."
if is_systemd; then
    systemctl kill --signal=SIGKILL ip-sentinel-agent-daemon.service >/dev/null 2>&1 || true
    systemctl stop ip-sentinel-runner.timer ip-sentinel-updater.timer ip-sentinel-report.timer ip-sentinel-agent-daemon.service >/dev/null 2>&1 || true
fi
pkill -9 -f "webhook.py" >/dev/null 2>&1 || true
pkill -9 -f "agent_daemon.py" >/dev/null 2>&1 || true
pkill -9 -f "uv run.*ip_sentinel" >/dev/null 2>&1 || true
pkill -9 -f "${INSTALL_DIR}/py/" >/dev/null 2>&1 || true
pkill -9 -f "runner.sh" >/dev/null 2>&1 || true
pkill -9 -f "tg_report.sh" >/dev/null 2>&1 || true
pkill -9 -f "updater.sh" >/dev/null 2>&1 || true
pkill -9 -f "mod_google.sh" >/dev/null 2>&1 || true
pkill -9 -f "mod_trust.sh" >/dev/null 2>&1 || true
pkill -9 -f "sentinel_scheduler.sh" >/dev/null 2>&1 || true

mkdir -p "${INSTALL_DIR}/core"
mv "$TMP_UNINSTALL" "${INSTALL_DIR}/core/uninstall.sh"
chmod +x "${INSTALL_DIR}/core/uninstall.sh"
# 清理历史遗留的 Bash 逻辑脚本
for LEGACY_SH in runner.sh updater.sh tg_report.sh agent_daemon.sh mod_google.sh mod_trust.sh mod_quality.sh; do
    rm -f "${INSTALL_DIR}/core/${LEGACY_SH}" 2>/dev/null
done

rm -rf "${INSTALL_DIR}/py" 2>/dev/null
mv "$TMP_PY" "${INSTALL_DIR}/py"
chmod +x ${INSTALL_DIR}/py/*.py 2>/dev/null || true

mv "${SECURE_TMP}/pyproject.toml" "${INSTALL_DIR}/pyproject.toml"
mv "${SECURE_TMP}/uv.lock" "${INSTALL_DIR}/uv.lock"
mv "${SECURE_TMP}/.python-version" "${INSTALL_DIR}/.python-version"

ensure_uv
uv_sync_project "${INSTALL_DIR}" || exit 1

curl -fsSL --connect-timeout 10 --retry 3 "${REPO_RAW_URL}/data/user_agents.txt" -o "${INSTALL_DIR}/data/user_agents.txt"
if [ "$UPGRADE_MODE" == "false" ]; then
    curl -fsSL --connect-timeout 10 --retry 3 "${REPO_RAW_URL}/data/keywords/${KEYWORD_FILE}" -o "${INSTALL_DIR}/data/keywords/${KEYWORD_FILE}"
else
    curl -fsSL --connect-timeout 10 --retry 3 "${REPO_RAW_URL}/data/keywords/kw_${REGION_CODE}.txt" -o "${INSTALL_DIR}/data/keywords/kw_${REGION_CODE}.txt" 2>/dev/null || true
fi

# ==========================================================
# 配置 systemd 或 cron 调度
# ==========================================================
echo -e "\n[7/7] 正在注入系统守护进程与调度器..."

DEPLOY_UTC_HOUR=$(date -u +%H)
DEPLOY_UTC_MIN=$(date -u +%M)

echo $(date -u +%s) > "${INSTALL_DIR}/core/.ua_last_update"

if is_systemd; then
    echo "💡 检测到 Systemd 环境，正在部署原生守护服务..."
    
    cat > /etc/systemd/system/ip-sentinel-runner.service << EOF
[Unit]
Description=IP-Sentinel Runner Service
After=network.target
[Service]
Environment="PATH=${UV_PATH}"
SyslogIdentifier=ip-sentinel
Type=oneshot
WorkingDirectory=${INSTALL_DIR}
ExecStart=${UV_BIN} run python py/runner.py
User=root
CPUSchedulingPolicy=idle
IOSchedulingClass=idle
EOF

    cat > /etc/systemd/system/ip-sentinel-runner.timer << EOF
[Unit]
Description=Timer for IP-Sentinel Runner Service
[Timer]
OnCalendar=*:0/20
RandomizedDelaySec=180
Persistent=true
Unit=ip-sentinel-runner.service
[Install]
WantedBy=timers.target
EOF

    cat > /etc/systemd/system/ip-sentinel-updater.service << EOF
[Unit]
Description=IP-Sentinel Updater Service
After=network.target
[Service]
Environment="PATH=${UV_PATH}"
SyslogIdentifier=ip-sentinel
Type=oneshot
WorkingDirectory=${INSTALL_DIR}
ExecStart=${UV_BIN} run python py/updater.py
User=root
CPUSchedulingPolicy=idle
IOSchedulingClass=idle
EOF

    cat > /etc/systemd/system/ip-sentinel-updater.timer << EOF
[Unit]
Description=Timer for IP-Sentinel Updater Service
[Timer]
OnCalendar=*-*-* ${DEPLOY_UTC_HOUR}:${DEPLOY_UTC_MIN}:00 UTC
Persistent=true
Unit=ip-sentinel-updater.service
[Install]
WantedBy=timers.target
EOF

    systemctl daemon-reload
    systemctl enable --now ip-sentinel-runner.timer ip-sentinel-updater.timer

    if [[ -n "$TG_TOKEN" ]] && [[ -n "$CHAT_ID" ]]; then
        cat > /etc/systemd/system/ip-sentinel-report.service << EOF
[Unit]
Description=IP-Sentinel Telegram Report Service
After=network.target
[Service]
Environment="PATH=${UV_PATH}"
SyslogIdentifier=ip-sentinel
Type=oneshot
WorkingDirectory=${INSTALL_DIR}
ExecStart=${UV_BIN} run python py/report.py
User=root
CPUSchedulingPolicy=idle
IOSchedulingClass=idle
EOF

        cat > /etc/systemd/system/ip-sentinel-report.timer << EOF
[Unit]
Description=Timer for IP-Sentinel Telegram Report Service
[Timer]
OnCalendar=*-*-* 16:00:00 UTC
Unit=ip-sentinel-report.service
[Install]
WantedBy=timers.target
EOF

        cat > /etc/systemd/system/ip-sentinel-agent-daemon.service << EOF
[Unit]
Description=IP-Sentinel Agent Daemon Service
After=network.target
[Service]
Environment="PATH=${UV_PATH}"
SyslogIdentifier=ip-sentinel
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${UV_BIN} run python py/agent_daemon.py
Restart=always
RestartSec=5
User=root
CPUSchedulingPolicy=idle
IOSchedulingClass=idle
[Install]
WantedBy=multi-user.target
EOF

        DAEMON_IP=$( (curl -s -m 5 api.ip.sb/ip || curl -s -m 5 ifconfig.me) 2>/dev/null | tr -d '[:space:]' )
        [ -n "$DAEMON_IP" ] && echo "$DAEMON_IP" > "${INSTALL_DIR}/core/.last_ip" || echo "$(echo "$SAFE_PUBLIC_IP" | tr -d '[]')" > "${INSTALL_DIR}/core/.last_ip"
        
        systemctl daemon-reload
        systemctl enable --now ip-sentinel-report.timer
        systemctl enable --now ip-sentinel-agent-daemon.service
    fi
    else
        echo "💡 未检测到 Systemd，正在配置备用调度器 (兼容 Alpine/OpenRC)..."
        
        IS_RESTRICTED_ALPINE="false"
        if [ -f /etc/alpine-release ]; then
            if [ -d /proc/vz ] || grep -qa container=lxc /proc/1/environ 2>/dev/null || [ -f /.dockerenv ]; then
                IS_RESTRICTED_ALPINE="true"
            fi
        fi

        if [ "$IS_RESTRICTED_ALPINE" == "true" ]; then
            echo -e "⚠️ 探测到受限的 LXC/OpenVZ Alpine 环境，系统自带 Cron 极易假死。"
            echo -e "🔧 启用内置循环调度脚本 (Alpine 受限环境)..."
            
            rc-update del crond default >/dev/null 2>&1 || true
            rc-service crond stop >/dev/null 2>&1 || true
            pkill -9 crond >/dev/null 2>&1 || true
            crontab -l 2>/dev/null | grep -v "ip_sentinel" > "${SECURE_TMP}/cron_clean" || true
            [ -f "${SECURE_TMP}/cron_clean" ] && crontab "${SECURE_TMP}/cron_clean" >/dev/null 2>&1
            rm -f "${SECURE_TMP}/cron_clean"

            cat > ${INSTALL_DIR}/core/sentinel_scheduler.sh << EOF
#!/bin/bash
while true; do
    MIN=\$(date -u +%M)
    HOUR=\$(date -u +%H)
    if [ "\$MIN" == "00" ] || [ "\$MIN" == "20" ] || [ "\$MIN" == "40" ]; then
        ${UV_BIN} run --directory ${INSTALL_DIR} python py/runner.py >/dev/null 2>&1
    fi
    if [ "\$HOUR" == "${DEPLOY_UTC_HOUR}" ] && [ "\$MIN" == "${DEPLOY_UTC_MIN}" ]; then
        ${UV_BIN} run --directory ${INSTALL_DIR} python py/updater.py >/dev/null 2>&1
    fi
    if [ "\$HOUR" == "16" ] && [ "\$MIN" == "00" ]; then
        ${UV_BIN} run --directory ${INSTALL_DIR} python py/report.py >/dev/null 2>&1
    fi
    if ! pgrep -f 'ip_sentinel/py/webhook.py' >/dev/null && ! pgrep -f 'webhook.py' >/dev/null; then
        ${UV_BIN} run --directory ${INSTALL_DIR} python py/agent_daemon.py >/dev/null 2>&1 &
    fi
    sleep 60
done
EOF
            chmod +x ${INSTALL_DIR}/core/sentinel_scheduler.sh

            if command -v rc-update >/dev/null 2>&1 && [ -d "/etc/local.d" ]; then
                echo "nohup bash ${INSTALL_DIR}/core/sentinel_scheduler.sh >/dev/null 2>&1 &" > /etc/local.d/ip_sentinel_scheduler.start
                chmod +x /etc/local.d/ip_sentinel_scheduler.start
                rc-update add local default >/dev/null 2>&1
            else
                grep -q "sentinel_scheduler" /etc/profile || echo "nohup bash ${INSTALL_DIR}/core/sentinel_scheduler.sh >/dev/null 2>&1 &" >> /etc/profile
            fi
            
            [ -n "$PUBLIC_IP" ] && echo "$PUBLIC_IP" > "${INSTALL_DIR}/core/.last_ip"
            nohup bash ${INSTALL_DIR}/core/sentinel_scheduler.sh >/dev/null 2>&1 &
            
        else
            crontab -l 2>/dev/null | grep -v "ip_sentinel" > "${SECURE_TMP}/cron_backup" || true
            echo "*/20 * * * * ${UV_BIN} run --directory ${INSTALL_DIR} python py/runner.py >/dev/null 2>&1" >> "${SECURE_TMP}/cron_backup"
            echo "${DEPLOY_UTC_MIN} ${DEPLOY_UTC_HOUR} * * * ${UV_BIN} run --directory ${INSTALL_DIR} python py/updater.py >/dev/null 2>&1" >> "${SECURE_TMP}/cron_backup"
            
            if [[ -n "$TG_TOKEN" ]] && [[ -n "$CHAT_ID" ]]; then
                echo "0 16 * * * ${UV_BIN} run --directory ${INSTALL_DIR} python py/report.py >/dev/null 2>&1" >> "${SECURE_TMP}/cron_backup"
                echo "$SAFE_PUBLIC_IP" > "${INSTALL_DIR}/core/.last_ip"
                DAEMON_IP=$( (curl -s -m 5 api.ip.sb/ip || curl -s -m 5 ifconfig.me) 2>/dev/null | tr -d '[:space:]' )
                [ -n "$DAEMON_IP" ] && echo "$DAEMON_IP" > "${INSTALL_DIR}/core/.last_ip" || echo "$(echo "$SAFE_PUBLIC_IP" | tr -d '[]')" > "${INSTALL_DIR}/core/.last_ip"
                
                if command -v rc-update >/dev/null 2>&1 && [ -d "/etc/local.d" ]; then
                    echo "nohup ${UV_BIN} run --directory ${INSTALL_DIR} python py/agent_daemon.py >/dev/null 2>&1 &" > /etc/local.d/ip_sentinel.start
                    chmod +x /etc/local.d/ip_sentinel.start
                    rc-update add local default >/dev/null 2>&1
                else
                    echo "@reboot nohup ${UV_BIN} run --directory ${INSTALL_DIR} python py/agent_daemon.py >/dev/null 2>&1 &" >> "${SECURE_TMP}/cron_backup"
                fi
                
                echo "* * * * * pgrep -f 'ip_sentinel/py/webhook.py' >/dev/null || pgrep -f 'webhook.py' >/dev/null || nohup ${UV_BIN} run --directory ${INSTALL_DIR} python py/agent_daemon.py >/dev/null 2>&1 &" >> "${SECURE_TMP}/cron_backup"
                
                nohup ${UV_BIN} run --directory "${INSTALL_DIR}" python py/agent_daemon.py >/dev/null 2>&1 &
            fi
            
            [ -f "${SECURE_TMP}/cron_backup" ] && crontab "${SECURE_TMP}/cron_backup" >/dev/null 2>&1
            
            if [ -d "/etc/crontabs" ] && [ -f "/var/spool/cron/crontabs/root" ]; then
                cp -f /var/spool/cron/crontabs/root /etc/crontabs/root 2>/dev/null || true
                chmod 600 /etc/crontabs/root 2>/dev/null || true
            fi
            
            if command -v rc-service >/dev/null 2>&1; then
                rc-service crond restart >/dev/null 2>&1 || crond -b >/dev/null 2>&1
            else
                pkill -9 crond 2>/dev/null || true
                crond -b >/dev/null 2>&1 || true
            fi
            
            rm -f "${SECURE_TMP}/cron_backup"
        fi
    fi

# ----------------------------------------------------------
# 部署完成后向 Telegram 发送注册/升级通知
# ----------------------------------------------------------
if [[ -n "$TG_TOKEN" ]] && [[ -n "$CHAT_ID" ]]; then
    
    REG_MSG="#REGISTER#|${REGION_CODE}|${NODE_NAME}|${SAFE_PUBLIC_IP}|${AGENT_PORT}|${NODE_ALIAS}|${ENABLE_OTA}"
    
    if [ "$UPGRADE_MODE" == "true" ]; then
        OLD_VERSION=$(grep "^AGENT_VERSION=" "$CONFIG_FILE" | cut -d'"' -f2)
        [ -z "$OLD_VERSION" ] && OLD_VERSION="3.3.1"
        
        if version_lt "$OLD_VERSION" "3.3.2"; then
            echo -e "\n📡 正在发送跨版本升级通知 (v${OLD_VERSION} -> v${TARGET_VERSION})..."
            TEXT_MSG="✨ *IP-Sentinel 升级完成*
📍 节点：\`${NODE_ALIAS}\`
🌐 IP：\`${SAFE_PUBLIC_IP}\`
🚀 版本：v${TARGET_VERSION}

⚠️ *架构已变更，请复制下方注册指令并回复机器人以更新 Master 记录：*
\`${REG_MSG}\`"
            
            JSON_PAYLOAD=$(jq -n --arg cid "$CHAT_ID" --arg txt "$TEXT_MSG" --arg cb "manage:${NODE_NAME}" '{chat_id: $cid, text: $txt, parse_mode: "Markdown", reply_markup: {inline_keyboard: [[{text: "⚙️ 调出该节点控制台", callback_data: $cb}]]}}')
            curl -s -X POST "${TG_API_URL}" -H "Content-Type: application/json" -d "$JSON_PAYLOAD" >/dev/null 2>&1
            
            echo -e "\033[32m✅ 升级通知已推送！请前往 TG 点击注册指令完成身份同步！\033[0m"
            
        else
            echo -e "\n📡 正在发送升级通知 (v${OLD_VERSION} -> v${TARGET_VERSION})..."
            TEXT_MSG="✨ *IP-Sentinel 升级完成*
📍 节点：\`${NODE_ALIAS}\`
🌐 IP：\`${SAFE_PUBLIC_IP}\`
🚀 版本：v${TARGET_VERSION}"

            JSON_PAYLOAD=$(jq -n --arg cid "$CHAT_ID" --arg txt "$TEXT_MSG" --arg cb "manage:${NODE_NAME}" '{chat_id: $cid, text: $txt, parse_mode: "Markdown", reply_markup: {inline_keyboard: [[{text: "⚙️ 调出该节点控制台", callback_data: $cb}]]}}')
            curl -s -X POST "${TG_API_URL}" -H "Content-Type: application/json" -d "$JSON_PAYLOAD" >/dev/null 2>&1

            echo -e "\033[32m✅ 升级成功通知已推送到您的 Telegram！\033[0m"
        fi
        
        sed -i '/^NAME_HASHED=/d' "$CONFIG_FILE" 2>/dev/null
        if grep -q "^AGENT_VERSION=" "$CONFIG_FILE"; then
            sed -i "s/^AGENT_VERSION=.*/AGENT_VERSION=\"$TARGET_VERSION\"/" "$CONFIG_FILE"
        else
            echo "AGENT_VERSION=\"$TARGET_VERSION\"" >> "$CONFIG_FILE"
        fi
        
    else
        echo -e "\n📡 正在向 Telegram 发送注册消息..."
        TEXT_MSG="✨ *IP-Sentinel 部署成功！*
📍 区域：${REGION_NAME}
🌐 IP：${SAFE_PUBLIC_IP}
🔌 端口：${AGENT_PORT}

🔑 *请点击下方指令复制并回复给机器人：*
\`${REG_MSG}\`"

        JSON_PAYLOAD=$(jq -n --arg cid "$CHAT_ID" --arg txt "$TEXT_MSG" --arg cb "manage:${NODE_NAME}" '{chat_id: $cid, text: $txt, parse_mode: "Markdown", reply_markup: {inline_keyboard: [[{text: "⚙️ 调出该节点控制台", callback_data: $cb}]]}}')
        PUSH_RESULT=$(curl -s -X POST "${TG_API_URL}" -H "Content-Type: application/json" -d "$JSON_PAYLOAD")

        if echo "$PUSH_RESULT" | grep -q '"ok":true'; then
            echo -e "\033[32m✅ 注册信息已推送到您的 Telegram，请按指令完成最终激活！\033[0m"
        else
            echo -e "\033[31m❌ 消息推送失败，请检查 Chat ID 是否正确或是否已关注机器人。\033[0m"
        fi
    fi
fi

echo "========================================================"
if [ "$UPGRADE_MODE" == "true" ]; then
    echo "🎉 Agent 升级完成。"
else
    echo "🎉 Agent 安装完成。"
fi
echo "📍 区域: $REGION_NAME"
echo "⚙️ 定时任务: 每 20 分钟执行一次维护。"
if [[ -n "$TG_TOKEN" ]]; then
    echo "📡 Webhook 已启动 (端口: $AGENT_PORT)，已向 Master 发送注册消息。"
    
    FW_MSG=""
    if command -v ufw >/dev/null 2>&1 && ufw status | grep -qw active; then
        FW_MSG="ufw allow $AGENT_PORT/tcp"
    elif command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active firewalld | grep -qw active; then
        FW_MSG="firewall-cmd --zone=public --add-port=$AGENT_PORT/tcp --permanent && firewall-cmd --reload"
    elif command -v iptables >/dev/null 2>&1; then
        if [[ "$SAFE_PUBLIC_IP" == *":"* ]]; then
            FW_MSG="ip6tables -I INPUT -p tcp --dport $AGENT_PORT -j ACCEPT"
        else
            FW_MSG="iptables -I INPUT -p tcp --dport $AGENT_PORT -j ACCEPT"
        fi
    fi
    
    echo -e "\n\033[31m⚠️ 重要：节点使用公网 IP: $SAFE_PUBLIC_IP\033[0m"
    echo -e "\033[33m请在云厂商安全组/防火墙中放行 TCP 端口 $AGENT_PORT，否则 Master 无法下发指令。\033[0m"
    echo -e "\033[33m请勿将内网 IP 写入配置冒充公网 IP，否则远程管理将无法工作。\033[0m\n"
    if [ -n "$FW_MSG" ]; then
        echo "💡 检测到本地系统防火墙开启，您可以尝试执行以下命令放行本机端口 (注意: 云端安全组仍需您手动放行)："
        echo -e "\033[36m   $FW_MSG\033[0m"
    fi
fi
echo "🗑️ 若未来需卸载，可重新运行本脚本选择[2]或执行: bash ${INSTALL_DIR}/core/uninstall.sh"
echo "========================================================"

if [ "$UPGRADE_MODE" == "false" ]; then
    echo -e "\n📡 正在向开源社区汇报装机量 (完全匿名，不收集IP)..."
    AGENT_COUNT=$(curl -s -m 3 "https://ip-sentinel-count.samanthaestime296.workers.dev/ping/agent" || echo "")

    if [ -n "$AGENT_COUNT" ] && [[ "$AGENT_COUNT" =~ ^[0-9]+$ ]]; then
        echo -e "\033[32m✅ 感谢您成为全球第 ${AGENT_COUNT} 名 IP-Sentinel 节点维护者！\033[0m"
    else
        echo -e "\033[32m✅ 感谢您部署 IP-Sentinel！\033[0m"
    fi
fi

echo -e "\n========================================================"
echo -e "⭐ \033[33m如果本项目对您有帮助，欢迎在 GitHub 点 Star。\033[0m"
echo -e "💡 \033[32m您的 Star 有助于我们持续维护与更新项目。\033[0m"
echo -e "👉 \033[36m\033[4m\033]8;;https://github.com/hotyue/IP-Sentinel\033\\点击此处直达 GitHub 仓库点亮 Star 🌟\033[0m\033]8;;\033\\"
echo -e "========================================================\n"