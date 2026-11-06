# Dépannage

| Symptôme | Cause probable | Que faire |
|---|---|---|
| « Claude » absent du sélecteur | routeur arrêté ou `openai_base_url` perdu après une mise à jour | `codex-relays-check`, puis redémarrer Codex Desktop |
| `Unsupported image-conversation item: …` | nouveau type d’élément Codex | le relais le transmet désormais en texte ; signalez le type à corriger |
| Compaction toutes les quelques minutes | ancien comptage cumulé des tokens | vérifier que le relais est à jour |
| `history exceeds the recovery size limit` | historique local de plus de 4 Go | augmenter `MAX_READ_BYTES` dans `claude_history.py` ou repartir d’un résumé |
| `invalid_encrypted_content` en repassant à OpenAI | résumé chiffré par le relais envoyé à OpenAI | corrigé dans `combined_proxy.py` |
| Claude dit ne plus avoir d’outils | tentative d’outil interne de Claude Code | `journalctl --user -u codex-model-router | grep codex-relay` |
| Clics décalés dans Chrome | capture réduite | le relais indique le facteur à côté de chaque capture |
| Aucun modèle GPU dans le sélecteur | `~/.codex/gpu-gateway.url` ou `~/.codex/gateway.key` absent, ou passerelle injoignable | écrire l’adresse et la clé, puis `systemctl --user restart codex-model-router` |
| `503 Unable to determine the installed Codex version`, ou tests du catalogue en échec ou bloqués sur une machine sans Codex Desktop | le routeur ne trouve pas le binaire `codex` pour connaître la version du client | installer Codex Desktop, ou définir `CODEX_CLIENT_VERSION` (voir [Installation](installation.md#adapter-à-une-autre-machine)) |
| `You've hit your session limit` | limite d’abonnement Claude | attendre la réinitialisation indiquée |
| Réponses très lentes | effort xhigh/max ou contexte très long | passer en high, ou repartir d’un nouveau chat |

## Commandes utiles

```bash
systemctl --user status codex-model-router
journalctl --user -u codex-model-router -n 50
curl -s http://127.0.0.1:4012/health
codex-relays-check
cat ~/.codex/relay-guard/last-check.txt
CODEX_CLIENT_VERSION=0.167.0 ./scripts/run-tests.sh   # tests sur une machine sans Codex Desktop
```

