#!/bin/bash
# IP-Sentinel Master：环境检查后安装 .deb
set -euo pipefail

REPO="lasitan/IP-Sentinel"
CONF_DIR="/etc/ip-sentinel-master"
CONF="${CONF_DIR}/master.conf"
DATA_DIR="/var/lib/ip-sentinel-master"
VERSION_URL="https://raw.githubusercontent.com/${REPO}/main/version.txt"
RELEASE_BASE="https://github.com/${REPO}/releases/download"
SERVICE="ip-sentinel-master"

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

VERSION_BODY="$(curl -fsSL "$VERSION_URL")" || {
  echo "错误: 无法读取 ${VERSION_URL}"
  exit 1
}
MASTER_VERSION="$(printf '%s\n' "$VERSION_BODY" | awk -F= '/^MASTER_VERSION=/{print $2; exit}' | tr -d '\r" '"'"'')"
if [ -z "$MASTER_VERSION" ] || ! [[ "$MASTER_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+ ]]; then
  echo "错误: version.txt 中 MASTER_VERSION 无效: '${MASTER_VERSION}'"
  exit 1
fi
TAG="v${MASTER_VERSION}"
DEB_NAME="ip-sentinel-master_${MASTER_VERSION}_${DEB_ARCH}.deb"
URL="${RELEASE_BASE}/${TAG}/${DEB_NAME}"
echo "version.txt => MASTER_VERSION=${MASTER_VERSION} (tag ${TAG})"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

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

mkdir -p "$CONF_DIR" "$DATA_DIR" "$DATA_DIR/core" "$DATA_DIR/logs"

UPGRADE=false
[ -f "$CONF" ] && UPGRADE=true
if [ "${SILENT_MASTER_OTA:-}" = "true" ] && [ -f "$CONF" ]; then
  UPGRADE=true
fi

if [ "$UPGRADE" = false ]; then
  if [ -z "${TG_TOKEN:-}" ]; then
    read -r -p "Telegram Bot Token: " TG_TOKEN
  fi
  [ -n "${TG_TOKEN:-}" ] || { echo "Token 不能为空"; exit 1; }
  cat > "$CONF" <<EOF
TG_TOKEN="${TG_TOKEN}"
MASTER_DIR="${DATA_DIR}"
DB_FILE="${DATA_DIR}/sentinel.db"
MASTER_VERSION="${MASTER_VERSION}"
LISTEN_ADDR="127.0.0.1:19530"
TG_MODE="polling"
IS_OFFICIAL_GATEWAY="false"
ENABLE_MASTER_OTA="false"
EOF
  echo "已写入 ${CONF}"
  echo "提示: 启动后在 TG 发送 /nginx 部署反代；节点使用 MASTER_BASE_URL。"
else
  if grep -q '^MASTER_VERSION=' "$CONF"; then
    sed -i "s|^MASTER_VERSION=.*|MASTER_VERSION=\"${MASTER_VERSION}\"|" "$CONF" || true
  fi
  echo "升级模式：保留现有 ${CONF}"
fi

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  systemctl daemon-reload
  systemctl enable --now "$SERVICE"
  echo "服务 ${SERVICE} 已启用并启动"
else
  echo "警告: 无 systemd，请手动运行: IP_SENTINEL_MASTER_CONFIG=${CONF} /usr/bin/ip-sentinel-master"
fi

cat > "${DATA_DIR}/uninstall.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
systemctl disable --now ip-sentinel-master 2>/dev/null || true
apt-get remove -y ip-sentinel-master 2>/dev/null || dpkg -r ip-sentinel-master || true
echo "控制端软件包已移除。配置/数据可能仍在 /etc/ip-sentinel-master 与 /var/lib/ip-sentinel-master。"
EOF
chmod +x "${DATA_DIR}/uninstall.sh"

echo "控制端安装完成。"
