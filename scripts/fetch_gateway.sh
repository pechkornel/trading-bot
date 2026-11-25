#!/usr/bin/env bash
# Download the pinned IBKR gateway release asset into vendor/ibkr-gateway/ and
# verify it against docker/gateway/gateway.sha256. Run from anywhere.
# It never unpacks or runs the file. A hash mismatch deletes the download.
set -euo pipefail

VERSION="v1.0.0"
ASSET="ibkr-gateway-linux-amd64.zip"
URL="https://github.com/petdomaa100/ibkr-client-portal-api-gateway-native-exe/releases/download/${VERSION}/${ASSET}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HASH_FILE="$ROOT/docker/gateway/gateway.sha256"
DEST_DIR="$ROOT/vendor/ibkr-gateway"
DEST="$DEST_DIR/$ASSET"

expected="$(awk -v a="$ASSET" '$2 == a { print $1 }' "$HASH_FILE")"
if [[ -z "$expected" ]]; then
    echo "no hash for $ASSET in docker/gateway/gateway.sha256" >&2
    exit 1
fi

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{ print $1 }'
    else
        shasum -a 256 "$1" | awk '{ print $1 }'
    fi
}

mkdir -p "$DEST_DIR"
tmp="$DEST.part"
rm -f "$tmp"
curl -fsSL --proto '=https' --tlsv1.2 -o "$tmp" "$URL"

actual="$(sha256_of "$tmp")"
if [[ "$actual" != "$expected" ]]; then
    rm -f "$tmp"
    echo "SHA-256 mismatch for $ASSET: expected $expected, got $actual. File deleted." >&2
    exit 1
fi

mv "$tmp" "$DEST"
echo "OK: $DEST verified ($actual)"
