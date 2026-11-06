#!/usr/bin/env bash
# Installe la configuration Claude dans Codex Desktop pour l'utilisateur courant.
# Options : --no-tests, --no-services, --sync (ce depot suit ensuite le relais actif).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_TESTS=1; SERVICES=1; SYNC=0
for arg in "$@"; do
  case "$arg" in
    --no-tests) RUN_TESTS=0 ;;
    --no-services) SERVICES=0 ;;
    --sync) SYNC=1 ;;
    *) echo "Option inconnue : $arg" >&2; exit 2 ;;
  esac
done
STAMP="$(date +%Y%m%d-%H%M%S)"

command -v python3 >/dev/null || { echo "Prerequis manquant : python3" >&2; exit 1; }
[ -x "$HOME/.local/bin/claude" ] || { echo "Prerequis manquant : Claude Code dans $HOME/.local/bin/claude (puis claude /login)." >&2; exit 1; }
python3 -c 'import aiohttp, cryptography, PIL' || { echo "Installez aiohttp, cryptography et Pillow pour python3." >&2; exit 1; }

install_dir() {  # source destination
  local src="$1" dst="$2"
  if [ -d "$dst" ]; then cp -a "$dst" "$dst.backup-$STAMP"; echo "Sauvegarde : $dst.backup-$STAMP"; fi
  mkdir -p "$dst"
  cp -a "$src"/. "$dst"/
}

install_dir "$REPO/relay" "$HOME/.codex/gateway-compat"
install_dir "$REPO/claude-code-bridge" "$HOME/.local/share/codex-claude-bridge"
install_dir "$REPO/gpu-agents" "$HOME/.local/share/codex-gpu-agents"
install_dir "$REPO/skills/model-routing" "$HOME/.codex/skills/model-routing"
mkdir -p "$HOME/.local/bin"
for script in codex-relays-check codex-relay-sync; do
  install -m 700 "$REPO/relay-guard/$script" "$HOME/.local/bin/$script"
done
install -m 700 "$REPO/gpu-agents/bin/gpu-agents" "$HOME/.local/bin/gpu-agents"
mkdir -p -m 700 "$HOME/.codex"
if [ -n "${CODEX_GPU_GATEWAY_URL:-}" ] && [ ! -f "$HOME/.codex/gpu-gateway.url" ]; then
  printf '%s\n' "$CODEX_GPU_GATEWAY_URL" > "$HOME/.codex/gpu-gateway.url"
  chmod 600 "$HOME/.codex/gpu-gateway.url"
fi
mkdir -p -m 700 "$HOME/.codex/relay-guard"
sed "s#__HOME__#$HOME#g" "$REPO/relay-guard/config-required.toml.template" > "$HOME/.codex/relay-guard/config-required.toml"
chmod 600 "$HOME/.codex/relay-guard/config-required.toml"
[ -f "$HOME/.codex/config.toml" ] || install -m 600 /dev/null "$HOME/.codex/config.toml"
if [ "$SYNC" = 1 ]; then
  printf '%s\n' "$REPO" > "$HOME/.codex/relay-guard/sync-repo"
  chmod 600 "$HOME/.codex/relay-guard/sync-repo"
fi

if [ "$SERVICES" = 1 ]; then
  mkdir -p "$HOME/.config/systemd/user"
  cp "$REPO"/systemd/* "$HOME/.config/systemd/user/"
  systemctl --user daemon-reload
  systemctl --user enable codex-model-router.service codex-relays-check.path codex-relays-check.timer
  systemctl --user restart codex-model-router.service
  systemctl --user start codex-relays-check.path codex-relays-check.timer
  if [ -f "$HOME/.codex/gateway.key" ] && [ -f "$HOME/.codex/gpu-gateway.url" ]; then systemctl --user enable --now codex-gpu-compat.service; fi
  if [ "$SYNC" = 1 ]; then systemctl --user enable --now codex-relay-sync.path codex-relay-sync.timer; fi
  sleep 2
  curl -s http://127.0.0.1:4012/health && echo
fi

if [ "$RUN_TESTS" = 1 ]; then "$REPO/scripts/run-tests.sh"; fi
"$HOME/.local/bin/codex-relays-check" --no-tests || true
echo "Installation terminee. Redemarrez Codex Desktop pour voir Claude dans le selecteur."
