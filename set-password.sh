#!/usr/bin/env bash
# Change the website password. Signs out every existing login.
set -euo pipefail
cd "$(dirname "$0")"
read -rsp "New website password: " P1; echo
read -rsp "Again: " P2; echo
[[ "$P1" == "$P2" ]] || { echo "Passwords don't match."; exit 1; }
printf '%s\n' "$P1" | docker compose exec -T idea-web python app.py set-password
