#!/usr/bin/env bash
# Refuse un commit qui contiendrait un jeton, une clé privée ou un chemin personnel.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
pattern='sk-[A-Za-z0-9]{16}|gh[opsu]_[A-Za-z0-9]{20}|BEGIN [A-Z ]*PRIVATE KEY|experimental_bearer_token|AIza[0-9A-Za-z_-]{20}|/home/[a-z][a-z0-9_-]+/|[a-z0-9-]+\.tail[0-9a-f]+\.ts\.net|tail[0-9a-f]{6}|[A-Za-z0-9._%+-]+@(gmail|yahoo|hotmail|outlook)\.com|@asin\.bj|01a[0-9a-f]{5}-[0-9a-f]{4}-[0-9a-f]{4}'
if grep -rInE "$pattern" --exclude-dir=.git --exclude=check-secrets.sh .; then
  echo "Secret ou chemin personnel détecté : commit refusé." >&2
  exit 1
fi
echo "Aucun secret détecté."

