#!/bin/bash
# IP-Sentinel Master：环境检查 →（可选）Python 无感迁移 → 安装 .deb
# 兼容旧版 /opt/ip_sentinel_master/master.conf（KEY="VALUE" + # 注释）。
set -euo pipefail

REPO="lasitan/IP-Sentinel"
REPO_RAW="https://raw.githubusercontent.com/${REPO}/main"
CONF_DIR="/etc/ip-sentinel-master"
CONF="${CONF_DIR}/master.conf"
DATA_DIR="/var/lib/ip-sentinel-master"
LEGACY_DIR="/opt/ip_sentinel_master"
VERSION_URL="${REPO_RAW}/version.txt"
RELEASE_BASE="https://github.com/${REPO}/releases/download"
SERVICE="ip-sentinel-master"

# curl|bash 时 stdin 是管道，交互必须读 /dev/tty
prompt() {
  local var="$1" msg="$2" val=""
  if [ -r /dev/tty ]; then
    printf '%s' "$msg" > /dev/tty
    IFS= read -r val < /dev/tty || true
  else
    echo "错误: 无法交互输入（非 TTY）。请用环境变量: ${var}=... $0" >&2
    echo "或: bash -c \"\$(curl -fsSL ${REPO_RAW}/master/install_master.sh)\"" >&2
    return 1
  fi
  printf -v "$var" '%s' "$val"
}

# --- 内嵌迁移工具（与 scripts/migrate_legacy_py.sh 行为一致，避免二次拉取失败）---
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

# 兼容旧版：TG_TOKEN="xxx" / TG_TOKEN=xxx / 行尾 CR / 前导空白 / # 注释
migrate_conf_get() {
  local file="$1" key="$2" line
  [ -f "$file" ] || return 0
  line="$(grep -E "^[[:space:]]*${key}=" "$file" | head -n1 || true)"
  [ -n "$line" ] || return 0
  printf '%s\n' "$line" | sed -E "s/^[[:space:]]*${key}=//" | sed -E 's/\r$//; s/^[[:space:]]+//; s/[[:space:]]+$//; s/^"//; s/"$//; s/^'\''//; s/'\''$//'
}

conf_has_token() {
  local f="$1"
  [ -f "$f" ] || return 1
  # 旧版固化格式：TG_TOKEN="8733...:AAF..."
  grep -qE '^[[:space:]]*TG_TOKEN="[^"]+"' "$f" || grep -qE '^[[:space:]]*TG_TOKEN=[^[:space:]#]+' "$f"
}

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
  if [ -z "$(migrate_conf_get "$conf" "PUBLIC_BASE_URL")" ]; then
    local pip
    pip="$(migrate_conf_get "$conf" "PUBLIC_IP")"
    if [ -n "$pip" ]; then
      migrate_conf_key_set "$conf" "PUBLIC_BASE_URL" "https://${pip}"
    fi
  fi
}

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

  if command -v systemctl >/dev/null 2>&1; then
    systemctl stop "ip-sentinel-master" 2>/dev/null || true
    systemctl disable "ip-sentinel-master" 2>/dev/null || true
  fi
  pkill -f "run_master.py" 2>/dev/null || true
  if command -v crontab >/dev/null 2>&1; then
    local tmpc
    tmpc="$(mktemp)"
    crontab -l 2>/dev/null | grep -vF "ip-sentinel-master" | grep -vF "run_master.py" | grep -vF "/opt/ip_sentinel_master" > "$tmpc" || true
    crontab "$tmpc" 2>/dev/null || true
    rm -f "$tmpc"
  fi

  mkdir -p "$conf_dir" "$data_dir" "$data_dir/core" "$data_dir/logs"

  local legacy_token="" new_token=""
  legacy_token="$(migrate_conf_get "$legacy_conf" "TG_TOKEN")"
  new_token="$(migrate_conf_get "$new_conf" "TG_TOKEN")"

  if [ -f "$legacy_conf" ]; then
    if [ ! -f "$new_conf" ] || [ -z "$new_token" ]; then
      cp -a "$legacy_conf" "$new_conf"
      echo "已迁移配置 → ${new_conf}（保留旧版全部键：TG_TOKEN/FORUM_*/PUBLIC_IP 等）"
    else
      echo "新配置已有 TG_TOKEN，保留 ${new_conf}"
    fi
  fi

  if [ -f "$new_conf" ]; then
    normalize_master_conf "$new_conf" "$data_dir"
    if [ -z "$(migrate_conf_get "$new_conf" "TG_TOKEN")" ] && [ -n "$legacy_token" ]; then
      migrate_conf_key_set "$new_conf" "TG_TOKEN" "$legacy_token"
    fi
    echo "已读取旧版配置键并规范化路径"
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

  rm -rf "${legacy}/.venv" "${legacy}/py" "${legacy}/uv.lock" "${legacy}/pyproject.toml" \
    "${legacy}/.python-version" "${legacy}/__pycache__" \
    "${legacy}/ms-playwright" "${legacy}/.cache/ms-playwright" 2>/dev/null || true
  rm -f /etc/systemd/system/ip-sentinel-master.service 2>/dev/null || true
  if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload 2>/dev/null || true
  fi
  echo "Python 控制端运行时已清理；配置与数据已保留。"
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

# 只用内嵌迁移（避免远端 migrate 脚本覆盖解析逻辑）
migrate_legacy_master "$LEGACY_DIR" "$CONF_DIR" "$DATA_DIR"

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

# 强制从旧版 master.conf 继承（不依赖 awk；旧格式 KEY="VALUE"）
if [ -f "${LEGACY_DIR}/master.conf" ] && conf_has_token "${LEGACY_DIR}/master.conf"; then
  if ! conf_has_token "$CONF"; then
    cp -a "${LEGACY_DIR}/master.conf" "$CONF"
    echo "已从 ${LEGACY_DIR}/master.conf 补迁配置（含 TG_TOKEN）"
  fi
fi
if [ -f "$CONF" ]; then
  normalize_master_conf "$CONF" "$DATA_DIR"
fi

UPGRADE=false
conf_has_token "$CONF" && UPGRADE=true
conf_has_token "${LEGACY_DIR}/master.conf" && UPGRADE=true
[ -f "${LEGACY_DIR}/sentinel.db" ] && UPGRADE=true
if [ "${SILENT_MASTER_OTA:-}" = "true" ]; then
  UPGRADE=true
fi

if [ "$UPGRADE" = false ]; then
  if [ -z "${TG_TOKEN:-}" ]; then
    prompt TG_TOKEN "Telegram Bot Token: " || exit 1
  fi
  [ -n "${TG_TOKEN:-}" ] || {
    echo "Token 不能为空。非交互安装请: TG_TOKEN='xxx' bash -c \"\$(curl -fsSL ${REPO_RAW}/master/install_master.sh)\""
    exit 1
  }
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
  # 升级：有旧 Token 则绝不再交互索要
  if ! conf_has_token "$CONF" && conf_has_token "${LEGACY_DIR}/master.conf"; then
    cp -a "${LEGACY_DIR}/master.conf" "$CONF"
  fi
  if [ -f "$CONF" ]; then
    normalize_master_conf "$CONF" "$DATA_DIR"
    migrate_conf_key_set "$CONF" "MASTER_VERSION" "$MASTER_VERSION"
  fi
  if ! conf_has_token "$CONF"; then
    if [ -z "${TG_TOKEN:-}" ]; then
      prompt TG_TOKEN "现有配置缺少 TG_TOKEN，请输入: " || exit 1
    fi
    [ -n "${TG_TOKEN:-}" ] || {
      echo "Token 不能为空（未在 ${LEGACY_DIR}/master.conf 或 ${CONF} 中找到 TG_TOKEN）"
      exit 1
    }
    migrate_conf_key_set "$CONF" "TG_TOKEN" "$TG_TOKEN"
  fi
  echo "升级/迁移模式：保留配置 ${CONF}"
  echo "  已识别 TG_TOKEN=$(migrate_conf_get "$CONF" "TG_TOKEN" | sed -E 's/^(.{8}).*/\1***/')"
  echo "  FORUM_* / PUBLIC_IP / ENABLE_MASTER_OTA 等旧键均已保留"
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

echo "控制端安装完成（Go/deb）。"
