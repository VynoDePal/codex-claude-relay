# Journal des modifications

Toutes les modifications notables de ce projet sont consignées ici.
Le format s’inspire de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) et les versions suivent [Semantic Versioning](https://semver.org/lang/fr/).

## [Non publié]

### Ajouté

- `CHANGELOG.md`.
- Modèles d’issue (signaler un bug, proposer une amélioration) et de pull request dans `.github/`.
- Code de conduite (`CODE_OF_CONDUCT.md`, Contributor Covenant 2.1) et guide de contribution (`CONTRIBUTING.md`).
- Politique de sécurité (`SECURITY.md`) et signalement privé des vulnérabilités ; modèle d’issue pour les questions.
- Démo animée dans le README (`docs/images/demo.gif`) et topics GitHub.
- Sous-agents avec Claude : un parent Claude peut lancer et piloter des enfants Claude ou OpenAI, et reçoit un mode d’emploi des outils de sous-agents ([détails](docs/fonctionnement-claude.md#sous-agents)).
- Variable `CODEX_CLAUDE_CONCURRENCY` (1 à 8) pour le nombre de requêtes Claude simultanées.

### Modifié

- 3 requêtes Claude simultanées par défaut, au lieu de 2.

### Corrigé

- Un sous-agent OpenAI lancé par un parent Claude échouait avec « The encrypted content … could not be verified ».
- Un message intermédiaire d’un sous-agent OpenAI faisait échouer tous les tours suivants d’un parent Claude ; il devient une note lisible.

## [1.0.0] - 2026-11-06

Première version publique : Claude (Opus 5.5, via Claude Code) comme modèle dans Codex Desktop, avec tous les outils de Codex, la délégation à Claude Code et à des agents GPU, et un contrôle automatique après les mises à jour.

### Ajouté

#### Claude dans le sélecteur de modèles

- Modèle « Claude » (`claude-code-opus`) servi par un routeur local sur `127.0.0.1:4012`, à côté des modèles OpenAI, GPU (GLM, DeepSeek) et Gemini.
- Efforts low à max ; fenêtre de 1 000 000 tokens.
- Accès à tous les outils de Codex : shell, édition de fichiers, connecteurs, navigateur intégré et Google Chrome. Claude Code tourne sans ses outils internes et Codex exécute les appels avec ses propres permissions.

#### Longues conversations

- Compaction par Claude, avec résumé chiffré localement et relu aux tours suivants ; résumé rendu lisible si la conversation repasse à un modèle OpenAI ou GPU.
- Comptage des tokens sur le dernier appel réel, ce qui supprime les compactions en boucle.
- Passage d’un modèle OpenAI à Claude en cours de conversation : l’historique local est relu en flux (jusqu’à 4 Go, pages `.jsonl.zst` comprises) et archivé sur disque.
- Types d’éléments Codex inconnus transmis en texte au lieu de faire échouer le tour.

#### Contexte allégé

- Catalogue d’outils abrégé (environ 780 000 → 215 000 caractères) ; détails complets disponibles via `ALL_TOOLS`.
- Blocs d’instructions répétés dédoublonnés ; cache de prompt préservé.

#### Navigateur

- Consignes dédiées pour le navigateur intégré et Chrome.
- Documentation répétée et arbres d’accessibilité dépassés raccourcis ; arbres plafonnés à 30 000 caractères avec indication des éléments retirés.
- Captures limitées (30 images, 1 568 px) avec facteur de conversion des coordonnées pour viser juste.
- Nouvel essai automatique si Claude tente un outil interne puis croit avoir perdu les outils Codex.

#### Délégation

- Connecteur MCP `claude_code` : conversation avec Claude et tâches de codage en arrière-plan dans un projet.
- Connecteur MCP `gpu_agents` : le modèle de Codex reste manager et confie implémentation, tests ou relecture à des exécutants GLM/DeepSeek sandboxés.
- Compétence `model-routing` pour choisir quand déléguer.

#### Robustesse et maintenance

- `codex-relays-check` : après chaque mise à jour de Codex Desktop, au démarrage et chaque jour, redémarre les services, restaure les réglages de `config.toml`, lance les tests et signale les nouveaux types d’éléments.
- `codex-relay-sync` : synchronisation automatique du dépôt avec l’installation active, avec contrôle des secrets et tests avant chaque commit.
- Adresse de la passerelle GPU hors du code (`~/.codex/gpu-gateway.url` ou `CODEX_GPU_GATEWAY_URL`).
- `CODEX_CLIENT_VERSION` pour faire tourner le routeur sans Codex Desktop (CI, serveurs).
- Script d’installation `scripts/install.sh`, contrôle des secrets `scripts/check-secrets.sh`, GitHub Action (214 tests du relais, 21 tests de `gpu-agents`), licence MIT.

### Limites connues

- Le premier contexte d’un chat Claude reste lourd (instructions et compétences de Codex).
- Les tokens de Claude sont pris sur l’abonnement Claude ; sa limite de session peut interrompre un tour.
- Testé sur Ubuntu uniquement.

[Non publié]: https://github.com/VynoDePal/codex-claude-relay/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/VynoDePal/codex-claude-relay/releases/tag/v1.0.0
