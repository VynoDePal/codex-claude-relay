# Politique de sécurité

## Versions prises en charge

| Version | Correctifs de sécurité |
|---|---|
| 1.x | oui |

## Signaler une vulnérabilité

**N’ouvrez pas d’issue publique** pour une vulnérabilité.

Utilisez le signalement privé de GitHub : onglet **Security**, puis **Report a vulnerability** ([lien direct](https://github.com/VynoDePal/codex-claude-relay/security/advisories/new)). Seul le mainteneur voit le rapport.

Indiquez si possible :

- le composant concerné (routeur, relais Claude ou Gemini, `claude_code`, `gpu_agents`, scripts) ;
- les étapes pour reproduire et l’impact possible ;
- les versions de Codex Desktop et de Claude Code.

N’incluez aucun secret réel : jeton, clé, contenu de `~/.codex/auth.json` ou adresse de passerelle.

Vous recevrez une première réponse sous 7 jours. Une fois le correctif publié, la vulnérabilité est décrite dans un avis de sécurité et dans [CHANGELOG.md](CHANGELOG.md).

## Périmètre

Sont notamment concernés : l’exposition du routeur au-delà de `127.0.0.1`, le contournement de l’authentification par le jeton Codex, la fuite de jetons ou de clés dans les journaux ou le dépôt, et l’exécution d’une action refusée par les permissions de Codex via la délégation.
