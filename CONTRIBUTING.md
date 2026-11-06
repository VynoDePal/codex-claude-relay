# Contribuer à codex-claude-relay

Merci de votre intérêt ! Les signalements de bug, corrections et améliorations de la documentation sont bienvenus. En participant, vous acceptez le [code de conduite](CODE_OF_CONDUCT.md).

## Signaler un problème ou proposer une amélioration

Ouvrez une issue avec l’un des formulaires : **Signaler un bug** ou **Proposer une amélioration**. Pour un bug, indiquez :

- la version de Codex Desktop (`cat /usr/lib/chatgpt/version`) et de son client (`/usr/lib/chatgpt/resources/codex --version`) ;
- la version de Claude Code (`claude --version`) ;
- les étapes pour reproduire et les journaux utiles (`journalctl --user -u codex-model-router -n 50`).

**N’incluez jamais de secret** : jeton, clé, contenu de `~/.codex/auth.json`, adresse de votre passerelle GPU, chemin personnel ou identifiant de chat Codex. Relisez les journaux avant de les coller.

## Préparer l’environnement

```bash
git clone https://github.com/<votre-compte>/codex-claude-relay.git
cd codex-claude-relay
python3 -m pip install aiohttp cryptography Pillow   # Python 3.14
./scripts/run-tests.sh
```

Sur une machine sans Codex Desktop, définissez la version du client pour les tests du routeur :

```bash
CODEX_CLIENT_VERSION=0.167.0 ./scripts/run-tests.sh
```

L’installation complète (`./scripts/install.sh`) est décrite dans [docs/installation.md](docs/installation.md). Pour l’essayer sans toucher à votre configuration, lancez-la avec un dossier personnel temporaire : `HOME=$(mktemp -d) ./scripts/install.sh --no-services`.

## Proposer un changement

1. Forkez le dépôt et créez une branche dédiée.
2. Gardez le changement ciblé : un problème par pull request.
3. Ajoutez un test pour tout changement de comportement (`relay/test_*.py` ou `gpu-agents/test_*.py`, avec `unittest`, sans accès réseau).
4. Lancez `./scripts/run-tests.sh` et `./scripts/check-secrets.sh`.
5. Mettez à jour `README.md` ou `docs/` si le comportement change, et ajoutez une entrée dans [CHANGELOG.md](CHANGELOG.md), section « Non publié ».
6. Ouvrez une pull request avec le modèle proposé : problème résolu, changements, tests lancés.

La [GitHub Action CI](.github/workflows/ci.yml) lance le contrôle des secrets et tous les tests à chaque push et pull request. Une pull request doit être au vert pour être fusionnée.

## Dossiers synchronisés

`relay/`, `claude-code-bridge/`, `gpu-agents/`, `skills/model-routing/`, `relay-guard/` et `systemd/` sont publiés automatiquement depuis l’installation du mainteneur par `codex-relay-sync`. Une contribution acceptée dans ces dossiers y est d’abord reportée, puis republiée ; le résultat final est le même. `README.md`, `CHANGELOG.md`, `docs/`, `config/`, `scripts/` et `.github/` se modifient directement.

## Style

- Python 3.14, bibliothèque standard plus `aiohttp`, `cryptography` et `Pillow` ; pas de nouvelle dépendance sans discussion préalable.
- Suivez le style du fichier modifié.
- Ne journalisez jamais le contenu des requêtes, des jetons ou des clés.
- Documentation et messages destinés aux utilisateurs en français ; consignes envoyées aux modèles en anglais.

## Licence

En contribuant, vous acceptez que votre contribution soit distribuée sous la [licence MIT](LICENSE) du projet.
