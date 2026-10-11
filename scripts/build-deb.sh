#!/usr/bin/env bash
# Build ip-sentinel-master / ip-sentinel-agent .deb packages (requires Go + nfpm).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

MASTER_VERSION="$(awk -F= '/^MASTER_VERSION=/{print $2; exit}' version.txt | tr -d '\r')"
AGENT_VERSION="$(awk -F= '/^AGENT_VERSION=/{print $2; exit}' version.txt | tr -d '\r')"
MASTER_VERSION="${MASTER_VERSION:-5.0.0}"
AGENT_VERSION="${AGENT_VERSION:-5.0.0}"

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

echo "Done. Packages in ${OUT_DIR}/"
ls -la "$OUT_DIR"
