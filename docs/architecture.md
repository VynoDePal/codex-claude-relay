# Architecture

## Le routeur

Codex Desktop est configuré avec `openai_base_url = "http://127.0.0.1:4012/v1"`. Toutes ses requêtes passent donc par [relay/combined_proxy.py](../relay/combined_proxy.py), un serveur aiohttp lancé par le service `codex-model-router`.

- `GET /v1/models` : fusionne le catalogue OpenAI, les modèles de la passerelle GPU, Claude (`claude-code-opus`, affiché « Claude Opus 5.5 » ; `claude-code-fable`, affiché « Claude Fable » ; `claude-code-sonnet`, affiché « Claude Sonnet ») et Gemini (`gemini-4-argon`).
- `POST /v1/responses` : envoie la requête au bon fournisseur selon le modèle.
- L’authentification reprend le jeton Codex de `~/.codex/auth.json` ; une requête sans ce jeton est refusée.

## Le relais Claude

Pour chaque requête vers Claude, [relay/claude_relay.py](../relay/claude_relay.py) :

1. récupère l’historique local si la conversation vient d’un modèle OpenAI ([claude_history.py](../relay/claude_history.py)) ;
2. lit les résumés de compaction propres au relais ([claude_compaction.py](../relay/claude_compaction.py)) ;
3. rend lisibles les messages entre agents écrits par Claude et signale ceux qu’un modèle OpenAI a chiffrés ([claude_subagents.py](../relay/claude_subagents.py)) ;
4. convertit l’historique Codex en messages ([claude_tools.py](../relay/claude_tools.py), [gpu_vision.py](../relay/gpu_vision.py)) ;
5. allège le catalogue d’outils ([tool_catalog.py](../relay/tool_catalog.py)), dédoublonne les blocs d’instructions répétés et raccourcit les sorties du navigateur ;
6. lance `claude -p --model claude-opus-5-5 --tools "" --json-schema …` : Claude Code tourne sans ses outils internes et doit rendre `{text, calls}` ;
7. convertit la réponse en événements Responses (texte + appels d’outils) que Codex exécute.

Le bloc d’outils et l’historique sont envoyés en blocs séparés et stables, pour que le cache de prompt de Claude réutilise le début de la requête d’un tour à l’autre.

Le routeur traite au plus 3 requêtes Claude à la fois, sous-agents compris (`CODEX_CLAUDE_CONCURRENCY`, de 1 à 8). Vers OpenAI, il transforme aussi en texte ordinaire les consignes en clair qu’un parent Claude adresse à un sous-agent OpenAI.

Une tâche de fond du routeur ([claude_interrupts.py](../relay/claude_interrupts.py)) suit les historiques de Codex : quand un sous-agent Claude ou Gemini est interrompu, elle arrête les commandes shell que son tour avait lancées.

## Le relais Gemini

[relay/gemini_relay.py](../relay/gemini_relay.py) réutilise la préparation de Claude puis appelle Antigravity (`agy`). Gemini ne reçoit pas les images ; au-delà de 160 Ko de conversation, les anciens messages sont résumés par morceaux et mis en cache.

## Le pont Claude Code (délégation)

[claude-code-bridge/bridge.py](../claude-code-bridge/bridge.py) est un serveur MCP en stdio déclaré dans `config.toml` sous `[mcp_servers.claude_code]`. Il expose :

| Outil | Rôle |
|---|---|
| `ask_claude` | conversation avec Claude, sans accès aux fichiers |
| `start_coding_task` | tâche de codage en arrière-plan dans un projet précis ([coding.py](../claude-code-bridge/coding.py)) |
| `get_coding_task`, `cancel_coding_task` | suivi et annulation |
| `list_conversations` | reprise d’une conversation |

Le modèle de Codex garde la décision : il relit le diff et lance lui-même les tests.

## Le connecteur gpu_agents (manager et exécutants GPU)

[gpu-agents/bridge.py](../gpu-agents/bridge.py) est un serveur MCP déclaré sous `[mcp_servers.gpu_agents]`. Il permet au modèle de Codex de rester manager et de confier implémentation, tests ou relecture à des exécutants GLM ou DeepSeek. Chaque exécutant est un vrai Codex CLI (`/usr/lib/chatgpt/resources/codex exec`) qui passe par le routeur, sandboxé, sans connecteurs ni sous-délégation ([gpu_agents.py](../gpu-agents/gpu_agents.py)).

| Outil | Rôle |
|---|---|
| `create_team` | groupe d’exécutants pour un projet, avec un plafond N d’agents |
| `start_task` | lance un exécutant (rôle implementer, tester ou reviewer) |
| `get_task`, `list_tasks`, `continue_task`, `cancel_task` | suivi, reprise avec retours, annulation |

Les écritures concurrentes dans des dossiers qui se recouvrent sont refusées. La commande `gpu-agents` règle le nombre d’agents, la concurrence, le délai et les modèles autorisés.

## Le contrôle après mise à jour

[relay-guard/codex-relays-check](../relay-guard/codex-relays-check) est déclenché par `codex-relays-check.path` (changement de `/usr/lib/chatgpt/version`), au démarrage de session et chaque jour. Il redémarre les services arrêtés, restaure dans `config.toml` les réglages de [config-required.toml.template](../relay-guard/config-required.toml.template), lance les tests et signale tout nouveau type d’élément d’historique introduit par Codex.

## Fichiers locaux à l’exécution (non versionnés)

| Chemin | Contenu |
|---|---|
| `~/.codex/auth.json` | connexion Codex (géré par Codex) |
| `~/.codex/gateway.key` | clé de la passerelle GPU |
| `~/.codex/gpu-gateway.url` | adresse de la passerelle GPU |
| `~/.local/share/codex-gpu-agents/` (`config.json`, `teams/`, `tasks/`, `logs/`) | réglages et état des exécutants GPU |
| `~/.codex/claude-selector/compaction.key` | clé Fernet des résumés de compaction ; à sauvegarder, sinon les anciens résumés deviennent illisibles |
| `~/.codex/claude-selector/history/` | historiques récupérés lors d’un passage OpenAI → Claude (mode 0600) |
| `~/.codex/gemini-selector/summaries/` | résumés Gemini en cache (30 jours) |
| `~/.local/share/codex-claude-bridge/jobs/` | état des tâches de codage déléguées |

## Synchronisation du dépôt

[relay-guard/codex-relay-sync](../relay-guard/codex-relay-sync) est lancé par `codex-relay-sync.path` (surveillance des dossiers du relais, du pont, de la compétence, des scripts et des services) et par `codex-relay-sync.timer` (toutes les heures). Un verrou empêche deux synchronisations simultanées ; les fichiers supprimés côté actif sont supprimés du dépôt pour `relay/`, `claude-code-bridge/` et `skills/`.

