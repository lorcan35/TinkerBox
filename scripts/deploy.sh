#!/usr/bin/env bash
#
# Wave 14 W14-H16 — Dragon deploy with rollback + auth-probed smoke.
#
# Prior workflow:
#   scp -r dragon_voice/ radxa@192.168.1.91:/home/radxa/
#   ssh radxa 'sudo systemctl restart tinkerclaw-voice'
# Problem: restart=on-failure loops forever if a SyntaxError / ImportError
# lands post-scp.  No rollback.  No proof the service actually came back up.
#
# This script:
#   1. Snapshots current /home/radxa/dragon_voice/ + dashboard.py to
#      /home/radxa/.deploy_rollback/<stamp>/  (keeps the last 5)
#   2. rsyncs the working tree (--delete so removed files go away)
#   3. Clears __pycache__ so stale .pyc never hides the real code
#   4. Restarts tinkerclaw-voice + tinkerclaw-dashboard
#   5. Polls /health until 200 (max 20 s) — catches crashed boot
#   6. AUTH-PROBES /api/v1/sessions with the DRAGON_API_TOKEN from .env —
#      catches a broken middleware.  If this fails, auto-rolls back.
#
# Usage:
#   scripts/deploy.sh                        # defaults: HOST=radxa@192.168.1.91
#   HOST=radxa@192.168.1.99 scripts/deploy.sh  # override host
#   NO_ROLLBACK=1 scripts/deploy.sh          # skip snapshot (faster, riskier)
#
# Exit 0 on success, 1 on deploy failure (post-rollback), 2 on env issues.

set -euo pipefail

HOST="${HOST:-radxa@192.168.1.91}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
ROLLBACK_ROOT="/home/radxa/.deploy_rollback"
KEEP_SNAPSHOTS="${KEEP_SNAPSHOTS:-5}"

cd "$REPO_ROOT"

rollback_and_fail() {
  echo "deploy: rolling back to snapshot $STAMP" >&2
  ssh "$HOST" "
    set -e
    if [ -d '$ROLLBACK_ROOT/$STAMP/dragon_voice' ]; then
      rm -rf /home/radxa/dragon_voice
      cp -a '$ROLLBACK_ROOT/$STAMP/dragon_voice' /home/radxa/dragon_voice
    fi
    if [ -f '$ROLLBACK_ROOT/$STAMP/dashboard.py' ]; then
      cp '$ROLLBACK_ROOT/$STAMP/dashboard.py' /home/radxa/
    fi
    if [ -f '$ROLLBACK_ROOT/$STAMP/schema.sql' ]; then
      cp '$ROLLBACK_ROOT/$STAMP/schema.sql' /home/radxa/
    fi
    sudo systemctl restart tinkerclaw-voice tinkerclaw-dashboard
  "
  echo "deploy: rolled back to pre-$STAMP state" >&2
  exit 1
}

# Sanity: the things we'll push must actually exist locally.
for f in dragon_voice dashboard.py schema.sql; do
  if [ ! -e "$f" ]; then
    echo "deploy: missing required local file: $f" >&2
    exit 2
  fi
done

# Quick ssh reachability probe — fail fast if Dragon is offline.
if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "$HOST" true 2>/dev/null; then
  echo "deploy: cannot reach $HOST (ssh timeout / auth rejected)" >&2
  exit 2
fi

echo "deploy: target=$HOST stamp=$STAMP"

# ── 1. snapshot current deployment for rollback ────────────────────────
if [ "${NO_ROLLBACK:-0}" != "1" ]; then
  echo "deploy: snapshotting current tree to $ROLLBACK_ROOT/$STAMP"
  ssh "$HOST" "
    set -e
    mkdir -p '$ROLLBACK_ROOT/$STAMP'
    test -d /home/radxa/dragon_voice && cp -a /home/radxa/dragon_voice '$ROLLBACK_ROOT/$STAMP/' || true
    test -f /home/radxa/dashboard.py  && cp    /home/radxa/dashboard.py  '$ROLLBACK_ROOT/$STAMP/' || true
    test -f /home/radxa/schema.sql    && cp    /home/radxa/schema.sql    '$ROLLBACK_ROOT/$STAMP/' || true
    ls -1dt $ROLLBACK_ROOT/*/ 2>/dev/null | tail -n +$((KEEP_SNAPSHOTS + 1)) | xargs -r rm -rf
  "
fi

# ── 2. push new code ───────────────────────────────────────────────────
echo "deploy: rsync'ing dragon_voice/ + dashboard.py + schema.sql"
rsync -az --delete \
  --exclude '__pycache__' --exclude '*.pyc' --exclude '.pytest_cache' \
  dragon_voice/ "$HOST":/home/radxa/dragon_voice/
scp -q dashboard.py schema.sql "$HOST":/home/radxa/

# ── 3. clear stale pyc (defends against import-time stale cache bugs) ──
ssh "$HOST" "find /home/radxa/dragon_voice -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true"

# ── 4. restart services ────────────────────────────────────────────────
echo "deploy: restarting tinkerclaw-voice + tinkerclaw-dashboard"
ssh "$HOST" 'sudo systemctl restart tinkerclaw-voice tinkerclaw-dashboard'

# ── 5. poll /health ────────────────────────────────────────────────────
VOICE_URL="http://${HOST#*@}:3502"
echo "deploy: polling $VOICE_URL/health (up to 20 s)"
HEALTH_OK=0
for i in $(seq 1 10); do
  sleep 2
  if curl -fs -m 3 "$VOICE_URL/health" >/dev/null 2>&1; then
    echo "deploy: /health OK after ${i}x2s"
    HEALTH_OK=1
    break
  fi
done
if [ "$HEALTH_OK" != "1" ]; then
  echo "deploy: /health never came up — ABORTING" >&2
  if [ "${NO_ROLLBACK:-0}" != "1" ]; then
    rollback_and_fail
  fi
  exit 1
fi

# ── 6. auth probe — prove the middleware didn't break ──────────────────
# Read the token straight off Dragon so a broken .env on our workstation
# doesn't false-positive the probe.
TOKEN=$(ssh "$HOST" 'grep -E "^DRAGON_API_TOKEN=" /home/radxa/.env 2>/dev/null | sed -E "s/^DRAGON_API_TOKEN=//; s/^\"(.*)\"$/\1/; s/^\x27(.*)\x27$/\1/"')
if [ -z "$TOKEN" ]; then
  echo "deploy: WARNING — DRAGON_API_TOKEN not set on Dragon; skipping auth probe" >&2
else
  NO_AUTH=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "$VOICE_URL/api/v1/sessions" || echo 000)
  WITH_AUTH=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -H "Authorization: Bearer $TOKEN" "$VOICE_URL/api/v1/sessions" || echo 000)
  if [ "$NO_AUTH" != "401" ] || [ "$WITH_AUTH" != "200" ]; then
    echo "deploy: auth probe FAILED (no-auth=$NO_AUTH expected 401; with-auth=$WITH_AUTH expected 200)" >&2
    if [ "${NO_ROLLBACK:-0}" != "1" ]; then
      rollback_and_fail
    fi
    exit 1
  fi
  echo "deploy: auth probe OK (no-auth 401, with-auth 200)"
fi

echo "deploy: $STAMP OK."
echo "deploy: rollback available: ssh $HOST 'ls $ROLLBACK_ROOT/$STAMP/'"
