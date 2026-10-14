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

conf_set() {
  local file="$1" key="$2" val="$3"
  local line="${key}=\"${val}\""
  mkdir -p "$(dirname "$file")"
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

truthy() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|y|on) return 0 ;;
    *) return 1 ;;
  esac
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

# migrate 前缓存 BASE，防止旧迁移脚本清空后丢失
STASH_BASE="${MASTER_BASE_URL:-}"
[ -z "$STASH_BASE" ] && STASH_BASE="$(conf_get "$CONF" MASTER_BASE_URL)"

# --- Python → Go 无感迁移（仅 py/.venv 存在时；保留已有 https BASE）---
if curl -fsSL "${REPO_RAW}/scripts/migrate_legacy_py.sh" -o "${TMP}/migrate_legacy_py.sh"; then
  # shellcheck disable=SC1091
  . "${TMP}/migrate_legacy_py.sh"
  migrate_legacy_agent "$LEGACY_DIR" "$CONF_DIR" "$DATA_DIR" "$LOG_DIR"
else
  echo "警告: 无法拉取迁移脚本，跳过旧版 Python 清理（若为全新安装可忽略）"
fi

# migrate 后若 BASE 被清空则回填
AFTER_BASE="$(conf_get "$CONF" MASTER_BASE_URL)"
if [ -z "$AFTER_BASE" ] && [ -n "$STASH_BASE" ]; then
  conf_set "$CONF" "MASTER_BASE_URL" "$STASH_BASE"
  echo "已回填 MASTER_BASE_URL=${STASH_BASE}"
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

# 读取已有 / 旧配置
TG_TOKEN="${TG_TOKEN:-$(conf_get "$CONF" TG_TOKEN)}"
[ -z "${TG_TOKEN}" ] && TG_TOKEN="$(conf_get "${LEGACY_DIR}/config.conf" TG_TOKEN)"
CHAT_ID="${CHAT_ID:-$(conf_get "$CONF" CHAT_ID)}"
[ -z "${CHAT_ID}" ] && CHAT_ID="$(conf_get "${LEGACY_DIR}/config.conf" CHAT_ID)"
REGION_CODE="${REGION_CODE:-$(conf_get "$CONF" REGION_CODE)}"
[ -z "${REGION_CODE}" ] && REGION_CODE="$(conf_get "${LEGACY_DIR}/config.conf" REGION_CODE)"
REGION_CODE="${REGION_CODE:-UNKNOWN}"
EXISTING_BASE="$(conf_get "$CONF" MASTER_BASE_URL)"
if [ -z "${MASTER_BASE_URL:-}" ] && [ -n "$EXISTING_BASE" ]; then
  MASTER_BASE_URL="$EXISTING_BASE"
fi

# 已有合法完整配置时默认 KEEP_CONFIG（可用 KEEP_CONFIG=false 强制重配）
KEEP_CONFIG="${KEEP_CONFIG:-}"
if [ -z "$KEEP_CONFIG" ]; then
  if [ -n "${TG_TOKEN}" ] && [ -n "${CHAT_ID}" ] && [ -n "${MASTER_BASE_URL:-}" ] \
    && require_https_domain "${MASTER_BASE_URL}" >/dev/null 2>&1; then
    KEEP_CONFIG=true
  else
    KEEP_CONFIG=false
  fi
fi

DO_REGISTER=false
if truthy "$KEEP_CONFIG"; then
  echo "升级模式：保留现有配置（KEEP_CONFIG=true）"
  if [ -z "${TG_TOKEN:-}" ] || [ -z "${CHAT_ID:-}" ] || [ -z "${MASTER_BASE_URL:-}" ]; then
    echo "错误: KEEP_CONFIG 需要已有 TG_TOKEN / CHAT_ID / MASTER_BASE_URL"
    exit 1
  fi
  require_https_domain "${MASTER_BASE_URL}" || exit 1
  conf_set "$CONF" "TG_TOKEN" "$TG_TOKEN"
  conf_set "$CONF" "CHAT_ID" "$CHAT_ID"
  conf_set "$CONF" "MASTER_BASE_URL" "$MASTER_BASE_URL"
  conf_set "$CONF" "INSTALL_DIR" "$DATA_DIR"
  conf_set "$CONF" "AGENT_VERSION" "$AGENT_VERSION"
  conf_set "$CONF" "REGION_CODE" "$REGION_CODE"
  conf_set "$CONF" "LOG_FILE" "${LOG_DIR}/sentinel.log"
  # 缺省开关仅在缺失时补齐，不覆盖用户已改值
  [ -n "$(conf_get "$CONF" ENABLE_GOOGLE)" ] || conf_set "$CONF" "ENABLE_GOOGLE" "true"
  [ -n "$(conf_get "$CONF" ENABLE_TRUST)" ] || conf_set "$CONF" "ENABLE_TRUST" "true"
  [ -n "$(conf_get "$CONF" ENABLE_MAPS_GEO)" ] || conf_set "$CONF" "ENABLE_MAPS_GEO" "true"
  [ -n "$(conf_get "$CONF" ENABLE_OTA)" ] || conf_set "$CONF" "ENABLE_OTA" "true"
  echo "已更新 ${CONF}（保留 NODE_NAME/ALIAS/ENABLE_*/THREAD 等字段）"
  if truthy "${FORCE_REGISTER:-}"; then
    DO_REGISTER=true
  else
    echo "跳过自动注册（升级默认不重复注册；需要时设 FORCE_REGISTER=1）"
  fi
else
  if [ -z "${TG_TOKEN:-}" ]; then
    prompt TG_TOKEN "Telegram Bot Token: " || exit 1
  fi
  if [ -z "${CHAT_ID:-}" ]; then
    prompt CHAT_ID "Chat ID: " || exit 1
  fi
  if [ -z "${REGION_CODE:-}" ] || [ "${REGION_CODE}" = "UNKNOWN" ]; then
    if [ "${SILENT_AGENT_OTA:-}" != "true" ] && [ -z "$(conf_get "$CONF" REGION_CODE)" ]; then
      prompt REGION_CODE "区域代码 REGION_CODE [UNKNOWN]: " || true
    fi
    REGION_CODE="${REGION_CODE:-UNKNOWN}"
  fi

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

  # 全新/重配：写基础配置，保留已有 NODE_* 若存在
  PREV_NODE="$(conf_get "$CONF" NODE_NAME)"
  PREV_ALIAS="$(conf_get "$CONF" NODE_ALIAS)"
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
  [ -n "$PREV_NODE" ] && conf_set "$CONF" "NODE_NAME" "$PREV_NODE"
  [ -n "$PREV_ALIAS" ] && conf_set "$CONF" "NODE_ALIAS" "$PREV_ALIAS"
  echo "已写入 ${CONF}（MASTER_BASE_URL=${MASTER_BASE_URL}）"
  if [ "${SILENT_AGENT_OTA:-}" != "true" ]; then
    DO_REGISTER=true
  fi
fi

# 清理旧 Python Playwright 浏览器，并确保系统 Chromium 可用
if declare -F purge_playwright_browsers >/dev/null 2>&1; then
  purge_playwright_browsers
else
  rm -rf /root/.cache/ms-playwright /opt/ip_sentinel/ms-playwright /opt/ip_sentinel/.cache/ms-playwright 2>/dev/null || true
fi

ensure_chromium() {
  if command -v google-chrome >/dev/null 2>&1 || command -v chromium >/dev/null 2>&1 || command -v chromium-browser >/dev/null 2>&1; then
    echo "已检测到 Chromium/Chrome"
    return 0
  fi
  echo "未检测到 Chromium，正在安装…"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y || true
  if apt-get install -y chromium 2>/dev/null; then
    echo "已安装 chromium"
  elif apt-get install -y chromium-browser 2>/dev/null; then
    echo "已安装 chromium-browser"
  else
    echo "警告: 无法通过 apt 安装 Chromium；Google 地理定位将回退 HTTP"
    return 1
  fi
}
ensure_chromium || true

detect_public_ip() {
  local ip=""
  ip="$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
  [ -n "$ip" ] || ip="$(curl -fsS --max-time 8 https://ifconfig.me 2>/dev/null || true)"
  [ -n "$ip" ] || ip="$(curl -fsS --max-time 8 https://icanhazip.com 2>/dev/null || true)"
  printf '%s' "$(echo "$ip" | tr -d '[:space:]')"
}

# 向主控 POST /v1/register，由主控发确认按钮（勿用 Bot 自发 #REGISTER#，主控收不到自己的消息）
if [ "$DO_REGISTER" = true ]; then
  PUBLIC_IP="$(detect_public_ip)"
  HOST_RAW="$(hostname 2>/dev/null || echo node)"
  HOST_SAFE="$(printf '%s' "$HOST_RAW" | tr -cd 'A-Za-z0-9' | cut -c1-10)"
  [ -n "$HOST_SAFE" ] || HOST_SAFE="node"
  IP_HASH="$(printf '%s' "${PUBLIC_IP:-127.0.0.1}" | md5sum 2>/dev/null | awk '{print substr($1,1,4)}')"
  [ -n "$IP_HASH" ] || IP_HASH="0000"
  NODE_NAME="$(conf_get "$CONF" NODE_NAME)"
  [ -n "$NODE_NAME" ] || NODE_NAME="${HOST_SAFE}-${IP_HASH}"
  NODE_ALIAS="$(conf_get "$CONF" NODE_ALIAS)"
  [ -n "$NODE_ALIAS" ] || NODE_ALIAS="$(printf '%s' "$HOST_RAW" | cut -c1-30)"
  conf_set "$CONF" "NODE_NAME" "$NODE_NAME"
  conf_set "$CONF" "NODE_ALIAS" "$NODE_ALIAS"
  REG_MSG="#REGISTER#|${NODE_NAME}|${PUBLIC_IP}|${REGION_CODE}|${NODE_ALIAS}|true"
  BASE_TRIM="$(printf '%s' "${MASTER_BASE_URL}" | sed 's|/*$||')"
  REG_JSON="$(printf '{"chat_id":"%s","node":"%s","ip":"%s","region":"%s","alias":"%s","ota":"true"}' \
    "$CHAT_ID" "$NODE_NAME" "$PUBLIC_IP" "$REGION_CODE" "$NODE_ALIAS")"
  if curl -fsS --max-time 25 -X POST "${BASE_TRIM}/v1/register" \
    -H "Authorization: Bearer ${TG_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$REG_JSON" >/dev/null; then
    echo "已向控制端提交注册请求，请在 Telegram Bot 中点击「确认注册」。"
  else
    echo "警告: POST /v1/register 失败（主控需 >=5.0.11 且公网可达）。可私聊 Bot 发送："
    echo "  ${REG_MSG}"
  fi
fi

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  systemctl daemon-reload
  systemctl enable --now "$SERVICE"
  echo "服务 ${SERVICE} 已启用并启动"
else
  echo "警告: 无 systemd，请手动运行节点进程"
fi

echo "节点安装完成（Go/deb）。"
