#!/bin/bash
# IP-Sentinel Agent：环境检查 →（可选）Python 无感迁移 → 安装 .deb
set -euo pipefail

REPO="lasitan/IP-Sentinel"
REPO_RAW="https://raw.githubusercontent.com/${REPO}/main"
CONF_DIR="/etc/ip-sentinel"
CONF="${CONF_DIR}/config.conf"
DATA_DIR="/var/lib/ip-sentinel"
LOG_DIR="/var/log/ip-sentinel"
LEGACY_DIR="/opt/ip_sentinel"
VERSION_URL="${REPO_RAW}/version.txt"
RELEASE_BASE="https://github.com/${REPO}/releases/download"
DATA_RAW="${REPO_RAW}"
SERVICE="ip-sentinel-agent"

# curl|bash 时 stdin 是管道，交互必须读 /dev/tty
prompt() {
  local var="$1" msg="$2" val=""
  if [ -r /dev/tty ]; then
    printf '%s' "$msg" > /dev/tty
    IFS= read -r val < /dev/tty || true
  else
    echo "错误: 无法交互输入（非 TTY）。请用环境变量传入 ${var}，或: bash -c \"\$(curl -fsSL ${REPO_RAW}/core/install.sh)\"" >&2
    return 1
  fi
  printf -v "$var" '%s' "$val"
}

# 仅允许 https://域名（拒绝 IP / http / 裸 IP）
require_https_domain() {
  local u="$1"
  case "$u" in
    https://*) ;;
    *) echo "错误: MASTER_BASE_URL 须为 https://域名，当前: ${u}"; return 1 ;;
  esac
  local host
  host="$(printf '%s' "$u" | sed -E 's|^https://||; s|/.*||; s|:.*||')"
  if [ -z "$host" ] || [[ "$host" =~ ^[0-9.]+$ ]] || [[ "$host" =~ : ]]; then
    echo "错误: 不信任 IP，请使用域名（例 https://master.example.com）"
    return 1
  fi
  return 0
}

conf_get() {
  local file="$1" key="$2" line
  [ -f "$file" ] || return 0
  line="$(grep -E "^[[:space:]]*${key}=" "$file" | head -n1 || true)"
  [ -n "$line" ] || return 0
  printf '%s\n' "$line" | sed -E "s/^[[:space:]]*${key}=//" | sed -E 's/\r$//; s/^"//; s/"$//'
}

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "错误: 需要 root 权限"
  exit 1
fi

if [ ! -f /etc/os-release ]; then
  echo "错误: 缺少 /etc/os-release"
  exit 1
fi
# shellcheck disable=SC1091
. /etc/os-release
ID_LIKE_LOWER="$(echo "${ID_LIKE:-} ${ID:-}" | tr '[:upper:]' '[:lower:]')"
if ! command -v dpkg >/dev/null 2>&1; then
  echo "错误: 未找到 dpkg，请在 Debian/Ubuntu 系系统上安装"
  exit 1
fi
case " ${ID_LIKE_LOWER} " in
  *"debian"*|*"ubuntu"*) ;;
  *)
    echo "警告: 当前系统 ${PRETTY_NAME:-unknown} 可能非 Debian/Ubuntu，将尝试 dpkg 安装"
    ;;
esac

for bin in curl; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "错误: 缺少依赖命令: $bin"
    exit 1
  fi
done

arch="$(uname -m)"
case "$arch" in
  x86_64|amd64) DEB_ARCH=amd64 ;;
  aarch64|arm64) DEB_ARCH=arm64 ;;
  *) echo "错误: 不支持的架构: $arch"; exit 1 ;;
esac

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- Python → Go 无感迁移（保留配置，删除旧依赖）---
if curl -fsSL "${REPO_RAW}/scripts/migrate_legacy_py.sh" -o "${TMP}/migrate_legacy_py.sh"; then
  # shellcheck disable=SC1091
  . "${TMP}/migrate_legacy_py.sh"
  migrate_legacy_agent "$LEGACY_DIR" "$CONF_DIR" "$DATA_DIR" "$LOG_DIR"
else
  echo "警告: 无法拉取迁移脚本，跳过旧版 Python 清理（若为全新安装可忽略）"
fi

VERSION_BODY="$(curl -fsSL "$VERSION_URL")" || {
  echo "错误: 无法读取 ${VERSION_URL}"
  exit 1
}
MASTER_VERSION="$(printf '%s\n' "$VERSION_BODY" | awk -F= '/^MASTER_VERSION=/{print $2; exit}' | tr -d '\r" '"'"'')"
AGENT_VERSION="$(printf '%s\n' "$VERSION_BODY" | awk -F= '/^AGENT_VERSION=/{print $2; exit}' | tr -d '\r" '"'"'')"
if [ -z "$AGENT_VERSION" ] || ! [[ "$AGENT_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+ ]]; then
  echo "错误: version.txt 中 AGENT_VERSION 无效: '${AGENT_VERSION}'"
  exit 1
fi
if [ -z "$MASTER_VERSION" ] || ! [[ "$MASTER_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+ ]]; then
  echo "错误: version.txt 中 MASTER_VERSION 无效: '${MASTER_VERSION}'"
  exit 1
fi
TAG="v${MASTER_VERSION}"
DEB_NAME="ip-sentinel-agent_${AGENT_VERSION}_${DEB_ARCH}.deb"
URL="${RELEASE_BASE}/${TAG}/${DEB_NAME}"
echo "version.txt => MASTER_VERSION=${MASTER_VERSION} AGENT_VERSION=${AGENT_VERSION} (tag ${TAG})"

echo "检查通过: ${PRETTY_NAME:-linux} / ${DEB_ARCH} / ${DEB_NAME}"
echo "下载 ${URL} ..."
if ! curl -fsSL "$URL" -o "${TMP}/${DEB_NAME}"; then
  if [ "${ALLOW_SOURCE_BUILD:-0}" = "1" ]; then
    echo "Release deb 不可用，ALLOW_SOURCE_BUILD=1，改为源码构建..."
    if ! command -v go >/dev/null 2>&1; then
      echo "错误: 需要 Go 才能源码构建"
      exit 1
    fi
    git clone --depth 1 "https://github.com/${REPO}.git" "$TMP/src"
    (cd "$TMP/src" && ARCHS="$DEB_ARCH" bash scripts/build-deb.sh)
    cp "$TMP/src/dist/deb/${DEB_NAME}" "${TMP}/${DEB_NAME}"
  else
    echo "错误: 无法下载 deb。设置 ALLOW_SOURCE_BUILD=1 可回退源码构建。"
    exit 1
  fi
fi

echo "安装 ${DEB_NAME} ..."
if ! dpkg -i "${TMP}/${DEB_NAME}"; then
  apt-get update -y || true
  apt-get install -f -y
  dpkg -i "${TMP}/${DEB_NAME}"
fi

mkdir -p "$CONF_DIR" "$DATA_DIR" "$DATA_DIR/core" "$DATA_DIR/data" "$LOG_DIR"

cp -f "$(readlink -f "$0" 2>/dev/null || echo "$0")" "${DATA_DIR}/core/install.sh" 2>/dev/null || true
chmod +x "${DATA_DIR}/core/install.sh" 2>/dev/null || true

if [ ! -f "${DATA_DIR}/data/map.json" ]; then
  curl -fsSL "${DATA_RAW}/data/map.json" -o "${DATA_DIR}/data/map.json" || true
fi

# 仅信任旧配置中的 TG_TOKEN / CHAT_ID / REGION_CODE
TG_TOKEN="${TG_TOKEN:-$(conf_get "$CONF" TG_TOKEN)}"
[ -z "${TG_TOKEN}" ] && TG_TOKEN="$(conf_get "${LEGACY_DIR}/config.conf" TG_TOKEN)"
CHAT_ID="${CHAT_ID:-$(conf_get "$CONF" CHAT_ID)}"
[ -z "${CHAT_ID}" ] && CHAT_ID="$(conf_get "${LEGACY_DIR}/config.conf" CHAT_ID)"
REGION_CODE="${REGION_CODE:-$(conf_get "$CONF" REGION_CODE)}"
[ -z "${REGION_CODE}" ] && REGION_CODE="$(conf_get "${LEGACY_DIR}/config.conf" REGION_CODE)"
REGION_CODE="${REGION_CODE:-UNKNOWN}"

if [ -z "${TG_TOKEN:-}" ]; then
  prompt TG_TOKEN "Telegram Bot Token: " || exit 1
fi
if [ -z "${CHAT_ID:-}" ]; then
  prompt CHAT_ID "Chat ID: " || exit 1
fi
if [ -z "${REGION_CODE:-}" ]; then
  if [ "${SILENT_AGENT_OTA:-}" != "true" ]; then
    prompt REGION_CODE "区域代码 REGION_CODE [UNKNOWN]: " || true
  fi
  REGION_CODE="${REGION_CODE:-UNKNOWN}"
fi

# MASTER_BASE_URL 一律重配（不信任旧 IP / 自签）
NEED_BASE=true
if [ -n "${MASTER_BASE_URL:-}" ] && require_https_domain "${MASTER_BASE_URL}"; then
  NEED_BASE=false
fi
if [ "$NEED_BASE" = true ]; then
  if [ "${SILENT_AGENT_OTA:-}" = "true" ]; then
    echo "错误: 静默升级须提供 MASTER_BASE_URL=https://域名（禁止 IP）"
    exit 1
  fi
  prompt MASTER_BASE_URL "控制端 BASE URL (必须 https://域名，禁止 IP): " || exit 1
  require_https_domain "${MASTER_BASE_URL}" || exit 1
fi

[ -n "${TG_TOKEN:-}" ] && [ -n "${CHAT_ID:-}" ] && [ -n "${MASTER_BASE_URL:-}" ] || {
  echo "必填项缺失。例: TG_TOKEN=... CHAT_ID=... MASTER_BASE_URL=https://master.example.com REGION_CODE=SG"
  exit 1
}

cat > "$CONF" <<EOF
TG_TOKEN="${TG_TOKEN}"
CHAT_ID="${CHAT_ID}"
MASTER_BASE_URL="${MASTER_BASE_URL}"
INSTALL_DIR="${DATA_DIR}"
AGENT_VERSION="${AGENT_VERSION}"
REGION_CODE="${REGION_CODE}"
ENABLE_GOOGLE="true"
ENABLE_TRUST="true"
ENABLE_MAPS_GEO="true"
ENABLE_OTA="true"
LOG_FILE="${LOG_DIR}/sentinel.log"
EOF
echo "已写入 ${CONF}（仅信任 Token/ChatID/地区；MASTER_BASE_URL=${MASTER_BASE_URL}）"
echo "注册消息示例（发给 Bot）: #REGISTER#|NODE|IP|${REGION_CODE}|ALIAS|true"

if ! command -v google-chrome >/dev/null 2>&1 && ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1; then
  echo "提示: 未检测到 Chromium；Google 地理定位维护将回退 HTTP"
fi

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  systemctl daemon-reload
  systemctl enable --now "$SERVICE"
  echo "服务 ${SERVICE} 已启用并启动"
else
  echo "警告: 无 systemd，请手动运行节点进程"
fi

echo "节点安装完成（Go/deb）。"
