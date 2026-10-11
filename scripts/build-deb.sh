#!/usr/bin/env bash
# Build ip-sentinel-master / ip-sentinel-agent .deb packages from version.txt.
# Requires: Go, nfpm. Versions are ONLY taken from version.txt (no silent defaults).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION_FILE="${ROOT}/version.txt"
if [ ! -f "$VERSION_FILE" ]; then
  echo "错误: 缺少 version.txt（${VERSION_FILE}）" >&2
  exit 1
fi

MASTER_VERSION="$(awk -F= '/^MASTER_VERSION=/{print $2; exit}' "$VERSION_FILE" | tr -d '\r" '"'"'')"
AGENT_VERSION="$(awk -F= '/^AGENT_VERSION=/{print $2; exit}' "$VERSION_FILE" | tr -d '\r" '"'"'')"

semver_ok() {
  [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.+-]+)?$ ]]
}

if [ -z "$MASTER_VERSION" ] || [ -z "$AGENT_VERSION" ]; then
  echo "错误: version.txt 必须包含 MASTER_VERSION 与 AGENT_VERSION" >&2
  echo "当前: MASTER_VERSION='${MASTER_VERSION}' AGENT_VERSION='${AGENT_VERSION}'" >&2
  exit 1
fi
if ! semver_ok "$MASTER_VERSION"; then
  echo "错误: MASTER_VERSION 非法: ${MASTER_VERSION}" >&2
  exit 1
fi
if ! semver_ok "$AGENT_VERSION"; then
  echo "错误: AGENT_VERSION 非法: ${AGENT_VERSION}" >&2
  exit 1
fi

echo "version.txt => MASTER_VERSION=${MASTER_VERSION} AGENT_VERSION=${AGENT_VERSION}"

# Optional: when releasing from a git tag (CI), require tag matches version.txt.
# Prefer MASTER_VERSION; if AGENT differs, tag must still equal MASTER and we warn,
# or set REQUIRE_AGENT_TAG=1 to also require AGENT == MASTER.
if [ -n "${GITHUB_REF_NAME:-}" ] && [[ "${GITHUB_REF_NAME}" == v* ]]; then
  TAG_VER="${GITHUB_REF_NAME#v}"
  if [ "$TAG_VER" != "$MASTER_VERSION" ]; then
    echo "错误: git tag (${GITHUB_REF_NAME}) 与 version.txt MASTER_VERSION (v${MASTER_VERSION}) 不一致" >&2
    exit 1
  fi
  if [ "$AGENT_VERSION" != "$MASTER_VERSION" ]; then
    if [ "${REQUIRE_AGENT_TAG:-0}" = "1" ]; then
      echo "错误: AGENT_VERSION (${AGENT_VERSION}) 与 MASTER_VERSION (${MASTER_VERSION}) 不一致" >&2
      exit 1
    fi
    echo "警告: AGENT_VERSION (${AGENT_VERSION}) 与 tag/MASTER (${MASTER_VERSION}) 不同，将分别打入各自 deb 版本号"
  fi
  echo "tag 校验通过: ${GITHUB_REF_NAME} <-> MASTER_VERSION=${MASTER_VERSION}"
elif [ "${REQUIRE_TAG_MATCH:-0}" = "1" ]; then
  echo "错误: REQUIRE_TAG_MATCH=1 但未检测到 GITHUB_REF_NAME=v* 标签" >&2
  exit 1
fi

ARCHS="${ARCHS:-amd64 arm64}"
OUT_DIR="${OUT_DIR:-dist/deb}"
BIN_DIR="${BIN_DIR:-dist/bin}"
mkdir -p "$OUT_DIR" "$BIN_DIR"

if ! command -v nfpm >/dev/null 2>&1; then
  echo "Installing nfpm..."
  go install github.com/goreleaser/nfpm/v2/cmd/nfpm@latest
  export PATH="$(go env GOPATH)/bin:${PATH}"
fi

render_yaml() {
  local src="$1" dest="$2" version="$3" arch="$4"
  sed -e "s/\${VERSION}/${version}/g" -e "s/\${ARCH}/${arch}/g" "$src" > "$dest"
}

for ARCH in $ARCHS; do
  case "$ARCH" in
    amd64) GOARCH=amd64 ;;
    arm64) GOARCH=arm64 ;;
    *) echo "unsupported arch: $ARCH"; exit 1 ;;
  esac

  echo "==> build binaries linux/${GOARCH}"
  GOOS=linux GOARCH="$GOARCH" CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" \
    -o "${BIN_DIR}/ip-sentinel-master" ./cmd/master
  GOOS=linux GOARCH="$GOARCH" CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" \
    -o "${BIN_DIR}/ip-sentinel-agent" ./cmd/agent

  TMP="$(mktemp -d)"
  render_yaml packaging/nfpm-master.yaml "${TMP}/master.yaml" "$MASTER_VERSION" "$ARCH"
  render_yaml packaging/nfpm-agent.yaml "${TMP}/agent.yaml" "$AGENT_VERSION" "$ARCH"

  echo "==> package master ${MASTER_VERSION} ${ARCH}"
  nfpm package --config "${TMP}/master.yaml" --packager deb --target \
    "${OUT_DIR}/ip-sentinel-master_${MASTER_VERSION}_${ARCH}.deb"

  echo "==> package agent ${AGENT_VERSION} ${ARCH}"
  nfpm package --config "${TMP}/agent.yaml" --packager deb --target \
    "${OUT_DIR}/ip-sentinel-agent_${AGENT_VERSION}_${ARCH}.deb"

  rm -rf "$TMP"
done

echo "Done. Packages in ${OUT_DIR}/ (from version.txt)"
ls -la "$OUT_DIR"
