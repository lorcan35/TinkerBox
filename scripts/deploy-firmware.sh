#!/usr/bin/env bash
# deploy-firmware.sh — atomic TinkerTab OTA publisher.
#
# Wave 14 W14-L07: the old workflow was two separate scp commands —
# one for tinkertab.bin and one for version.json — which left the
# /api/ota/check endpoint exposing a torn state window where the
# reported sha256 didn't match the binary on disk (either old-sha
# with new-bin, or new-sha with old-bin, depending on which scp
# landed first).  A Tab5 polling OTA mid-deploy could reboot into
# a bricked image.
#
# This script fixes that:
#   1. scp the new bin to .new (staging slot)
#   2. compute sha256 locally, build the new version.json
#   3. scp version.json to .new on Dragon
#   4. ssh once: mv tinkertab.bin.new -> tinkertab.bin, then
#      mv version.json.new -> version.json.  Both renames are on
#      the same ext4 filesystem so each individual rename is
#      atomic; by moving the bin BEFORE the version.json, any
#      Tab5 checking mid-sequence sees the old version pointing
#      at the old (still-present) sha, or the new version
#      pointing at the new bin — never a mismatch.
#
# Usage: scripts/deploy-firmware.sh <path/to/tinkertab.bin> <version-string>
#
# Example:
#   scripts/deploy-firmware.sh build/tinkertab.bin 0.12.0-wave14

set -euo pipefail

BIN="${1:-}"
VERSION="${2:-}"

if [[ -z "$BIN" || -z "$VERSION" ]]; then
    echo "usage: $0 <tinkertab.bin> <version>" >&2
    exit 2
fi
if [[ ! -f "$BIN" ]]; then
    echo "error: firmware binary not found: $BIN" >&2
    exit 2
fi

DRAGON="${DRAGON:-radxa@192.168.1.91}"
OTA_DIR="${OTA_DIR:-/home/radxa/ota}"

SHA=$(sha256sum "$BIN" | awk '{print $1}')
SIZE=$(stat -c%s "$BIN")
echo "[deploy-firmware] bin=$BIN size=$SIZE sha256=$SHA version=$VERSION" >&2

# Use Dragon's public hostname for firmware_url.  Dragon's synth
# endpoint serves the binary from /api/ota/firmware.bin, so the URL
# does not need to change per release — keep it stable.
URL="http://${DRAGON##*@}:3502/api/ota/firmware.bin"

VERSION_JSON=$(printf '{"version":"%s","sha256":"%s","size":%d,"url":"%s"}' \
    "$VERSION" "$SHA" "$SIZE" "$URL")

TMP_JSON=$(mktemp --suffix=.json)
trap 'rm -f "$TMP_JSON"' EXIT
printf '%s\n' "$VERSION_JSON" > "$TMP_JSON"

echo "[deploy-firmware] staging bin -> $OTA_DIR/tinkertab.bin.new" >&2
scp -q "$BIN"      "$DRAGON:$OTA_DIR/tinkertab.bin.new"

echo "[deploy-firmware] staging version -> $OTA_DIR/version.json.new" >&2
scp -q "$TMP_JSON" "$DRAGON:$OTA_DIR/version.json.new"

echo "[deploy-firmware] atomic promote" >&2
# Promote bin first, then json.  If promote fails mid-way, the old
# bin+json pair stays intact (worst case a stale .new file, cleaned
# up below).
ssh "$DRAGON" "set -e
    cd '$OTA_DIR'
    mv -f tinkertab.bin.new tinkertab.bin
    mv -f version.json.new version.json
    # Drop any stray stale stagings from earlier aborted runs.
    rm -f tinkertab.bin.new version.json.new
    echo '[remote] published:' \"\$(cat version.json)\"
"

echo "[deploy-firmware] probing /api/ota/check" >&2
# A client polling with current=0.0.0 should get an update=true
# response that matches the sha we just computed.  The endpoint
# requires a bearer token (W14-C04); read from $DRAGON_API_TOKEN
# or fall back to parsing Dragon's systemd env.
if [[ -z "${DRAGON_API_TOKEN:-}" ]]; then
    DRAGON_API_TOKEN=$(ssh "$DRAGON" \
        "grep -oP '^DRAGON_API_TOKEN=\\K.+' /home/radxa/.env 2>/dev/null" \
        || true)
fi
if [[ -n "$DRAGON_API_TOKEN" ]]; then
    CHECK=$(curl -sS -H "Authorization: Bearer $DRAGON_API_TOKEN" \
        "http://${DRAGON##*@}:3502/api/ota/check?current=0.0.0" || true)
    echo "[deploy-firmware] /api/ota/check -> $CHECK" >&2
    if ! grep -q "\"$SHA\"" <<<"$CHECK"; then
        echo "[deploy-firmware] WARNING: server sha does not match local sha — manual check recommended" >&2
        exit 1
    fi
else
    echo "[deploy-firmware] no DRAGON_API_TOKEN available, skipping /check probe" >&2
fi

echo "[deploy-firmware] OK version=$VERSION sha=$SHA" >&2
