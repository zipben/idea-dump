#!/usr/bin/env bash
# Register the bot's own Signal number with signal-cli-rest-api.
# Usage:
#   ./register.sh request          # sends verification SMS (needs captcha, see below)
#   ./register.sh request voice    # voice call instead (do SMS first, wait 60s, then this)
#   ./register.sh verify 123456    # enter the code you received
#   ./register.sh finish 1234      # set a registration-lock PIN + profile name
# Captcha: open https://signalcaptchas.org/registration/generate.html, solve it,
# right-click "Open Signal" and copy the link (starts with signalcaptcha://), paste when asked.
set -euo pipefail
cd "$(dirname "$0")"
source .env
API=http://127.0.0.1:8099
case "${1:-}" in
  request)
    read -rp "Paste signalcaptcha:// link: " CAP
    VOICE=false; [[ "${2:-}" == voice ]] && VOICE=true
    curl -sS -X POST "$API/v1/register/$BOT_NUMBER" -H 'Content-Type: application/json' \
      -d "{\"captcha\":\"$CAP\",\"use_voice\":$VOICE}"; echo ;;
  verify)
    curl -sS -X POST "$API/v1/register/$BOT_NUMBER/verify/${2:?code}"; echo ;;
  finish)
    curl -sS -X POST "$API/v1/accounts/$BOT_NUMBER/pin" -H 'Content-Type: application/json' \
      -d "{\"pin\":\"${2:?pin}\"}"; echo
    curl -sS -X PUT "$API/v1/profiles/$BOT_NUMBER" -H 'Content-Type: application/json' \
      -d '{"name":"Idea Dump"}'; echo
    echo "Done. Now: docker compose up -d idea-bot" ;;
  *) sed -n '2,11p' "$0" ;;
esac
