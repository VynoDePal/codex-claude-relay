# Installation

## Prérequis

- Linux avec systemd utilisateur (testé sur Ubuntu 26.04).
- Codex Desktop (paquet `chatgpt`), connecté à un compte ChatGPT.
- Python 3.14 (le module `compression.zstd` sert à lire les historiques compressés), avec `aiohttp`, `cryptography` et `Pillow`. Versions vérifiées : aiohttp 3.13.3, cryptography 46.0.5, Pillow 12.1.1.
- Claude Code installé dans `~/.local/bin/claude` et connecté (`claude` puis `/login`). Version vérifiée : 2.1.318.
- Facultatif : Antigravity (`~/.local/bin/agy`) pour Gemini ; pour GLM et DeepSeek, une passerelle GPU compatible OpenAI, sa clé dans `~/.codex/gateway.key` et son adresse dans `~/.codex/gpu-gateway.url` (exemple : [config/gpu-gateway.url.example](../config/gpu-gateway.url.example)).

## Installation automatique

```bash
./scripts/install.sh
```

Le script :

1. copie `relay/` vers `~/.codex/gateway-compat/`, `claude-code-bridge/` vers `~/.local/share/codex-claude-bridge/`, `gpu-agents/` vers `~/.local/share/codex-gpu-agents/`, les commandes `codex-relays-check`, `codex-relay-sync` et `gpu-agents` vers `~/.local/bin/`, et la compétence vers `~/.codex/skills/model-routing/` ;
2. sauvegarde chaque dossier existant sous `<dossier>.backup-<date>` avant de l’écraser ;
3. installe les services de `systemd/` et les active ;
4. écrit `~/.codex/relay-guard/config-required.toml` puis lance `codex-relays-check`, qui ajoute à `config.toml` les réglages manquants (sauvegarde dans `~/.codex/relay-guard/`) ;
5. lance la suite de tests.

Redémarrez ensuite Codex Desktop : « Claude » apparaît dans le sélecteur de modèles.

Options : `--no-tests`, `--no-services`, `--sync` (le dépôt suit ensuite le relais actif, voir [Synchronisation automatique](../README.md#synchronisation-automatique)).

## Installation manuelle

1. Copier les dossiers comme ci-dessus.
2. Ajouter à `~/.codex/config.toml` le contenu de [config/config.toml.snippet](../config/config.toml.snippet).
3. Ajouter à `~/.codex/AGENTS.md` le passage de [config/AGENTS.snippet.md](../config/AGENTS.snippet.md) pour que les modèles connaissent la délégation à Claude Code.
4. Copier les fichiers de `systemd/` dans `~/.config/systemd/user/`, puis :

```bash
systemctl --user daemon-reload
systemctl --user enable --now codex-model-router codex-relays-check.path codex-relays-check.timer
```

## Adapter à une autre machine

- **Passerelle GPU :** écrivez son adresse dans `~/.codex/gpu-gateway.url` (mode 0600), ou passez-la à l’installation : `CODEX_GPU_GATEWAY_URL=http://ma-passerelle:4000 ./scripts/install.sh`. Sans adresse, le routeur fonctionne pour OpenAI, Claude et Gemini et n’affiche pas les modèles GPU ; le connecteur `gpu_agents` reste installé mais n’a alors aucun modèle à lancer.
- **Connecteur gpu_agents :** ses réglages (nombre d’agents, concurrence, modèles autorisés) se changent avec `gpu-agents config` et restent dans `~/.local/share/codex-gpu-agents/config.json`, non versionné.

## Mettre à jour

```bash
git pull && ./scripts/install.sh
```

