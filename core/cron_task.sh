#!/bin/bash
# 由 install.sh 写入 INSTALL_DIR / UV_BIN；供 cron 与 systemd 调用，避免静默失败。
# 用法: cron_task.sh py/runner.py

set -uo pipefail

INSTALL_DIR="__INSTALL_DIR__"
UV_BIN="__UV_BIN__"
UV_PATH="__UV_PATH__"

SCRIPT_REL="${1:-}"
if [ -z "$SCRIPT_REL" ]; then
    echo "usage: cron_task.sh py/<script>.py" >&2
    exit 2
fi

LOG_DIR="${INSTALL_DIR}/logs"
SCHED_LOG="${LOG_DIR}/scheduler.log"
mkdir -p "$LOG_DIR"

export IP_SENTINEL_INSTALL_DIR="${INSTALL_DIR}"
export IP_SENTINEL_CONFIG="${INSTALL_DIR}/config.conf"
export PATH="${UV_PATH:-/usr/local/bin:/usr/bin:/bin}"

_ts() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }

{
    echo "[$(_ts)] START ${SCRIPT_REL} pid=$$"
    if [ ! -f "${INSTALL_DIR}/config.conf" ]; then
        echo "[$(_ts)] ERROR missing config: ${INSTALL_DIR}/config.conf"
        exit 3
    fi
    if [ ! -x "$UV_BIN" ] && ! command -v "$UV_BIN" >/dev/null 2>&1; then
        echo "[$(_ts)] ERROR uv not found: $UV_BIN"
        exit 4
    fi
    cd "${INSTALL_DIR}" || exit 5
    "$UV_BIN" run --directory "${INSTALL_DIR}" python "${SCRIPT_REL}"
    ec=$?
    echo "[$(_ts)] EXIT ${SCRIPT_REL} code=${ec}"
    exit "$ec"
} >> "${SCHED_LOG}" 2>&1
