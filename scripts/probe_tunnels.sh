#!/usr/bin/env bash
# Audit K10: verify the 3 ngrok tunnels (dashboard / voice / gateway) are
# alive and reachable.  Run from any machine with internet; doesn't need
# to be on the Dragon's LAN.
#
# Exits 0 if all green, 1 if any tunnel is down.

set -u

TUNNELS=(
  "tinkerclaw-dashboard.ngrok.dev"
  "tinkerclaw-voice.ngrok.dev"
  "tinkerclaw-gateway.ngrok.dev"
)

fail=0
for t in "${TUNNELS[@]}"; do
  code=$(curl -s --max-time 6 -o /dev/null -w "%{http_code}" "https://${t}/" 2>/dev/null)
  if [[ "$code" =~ ^(200|404|405|401)$ ]]; then
    # 404/405/401 are fine — means the tunnel is forwarding but the root
    # path isn't served.  Only network failures (000) indicate a dead tunnel.
    echo "  OK   ${t}  (HTTP $code)"
  else
    echo "  DOWN ${t}  (HTTP $code)"
    fail=1
  fi
done

exit $fail
