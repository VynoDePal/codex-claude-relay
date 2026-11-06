## Déléguer à Claude Code

Le connecteur MCP `claude_code` permet de confier une tâche de codage à Claude Code.

- `start_coding_task` avec le chemin absolu du projet et une consigne autonome : plan, fichiers concernés, contraintes, critères d’acceptation, commandes de vérification. L’historique du chat n’est pas transmis.
- `get_coding_task` avec `wait_seconds` jusqu’à 20 pour suivre la tâche ; `session_id` pour continuer avec le même contexte.
- Le modèle de Codex relit le diff et lance lui-même les tests. Un statut terminé ne prouve pas que les tests passent.
- Ne pas modifier les mêmes fichiers en parallèle ; ne jamais déléguer une action refusée par les permissions.


## Déléguer le codage aux modèles GPU

L'utilisateur a installé le connecteur local MCP `gpu_agents`. Lorsqu'il demande
une délégation à GLM/DeepSeek ou un fonctionnement en manager, utiliser ce connecteur.
Le modèle principal conserve la planification, les décisions et la validation finale.
Le connecteur lance le client Codex CLI livré avec Desktop et la passerelle GPU existante.

- Créer un groupe avec `create_team` dans le dossier absolu autorisé pour le projet.
  Le champ `max_agents` fixe N pour cette mission, dans la limite configurée.
- Choisir le modèle de chaque exécutant avec `start_task` : `glm-5.3-flash` ou
  `deepseek-v4.1`, seulement s'il figure parmi les modèles autorisés du groupe.
  Par défaut utiliser GLM en `high`. Respecter tout choix explicite de l'utilisateur.
  DeepSeek reste désactivé jusqu'à sa remise en ligne et son activation locale.
- Rôles : `implementer` pour coder, `tester` pour écrire/exécuter les tests,
  `reviewer` pour une relecture en lecture seule. Fournir une tâche autonome :
  partie pertinente du plan, fichiers concernés, périmètre, contraintes, critères
  d'acceptation et commandes de vérification. L'historique du chat n'est pas transmis.
- Utiliser `get_task` avec `wait_seconds` jusqu'à 20, puis `list_tasks` pour retrouver
  un travail en cours. Ne pas conclure à un échec parce qu'un appel d'attente se termine.
  `continue_task` transmet les retours ou les tests échoués au même agent sans
  consommer un nouveau slot de N. Il reprend les fichiers, la consigne initiale,
  les retours du manager et le dernier rapport, pas l'historique intégral du modèle.
- Les écritures concurrentes dans des dossiers qui se recouvrent sont refusées.
  Attendre la fin du développeur avant le testeur. Pour coder en parallèle, préparer
  des worktrees distincts et les groupes correspondants. Ne pas modifier les mêmes
  fichiers simultanément depuis le manager ou Claude Code.
- Inspecter les fichiers modifiés et les commandes réellement exécutées. Un statut
  `completed` signifie que le modèle a terminé, pas que les tests sont tous réussis.
  Le manager exécute les vérifications finales avec ses permissions normales.
- Ne jamais utiliser la délégation pour contourner une permission refusée. Les
  exécutants restent sandboxés, sans connecteurs externes ni sous-délégation.
  Une annulation ou un dépassement de délai peut laisser des modifications partielles.
- Pour GLM/DeepSeek, éviter la délégation native `collaboration.spawn_agent` : cette
  version peut transmettre la tâche sous forme de contenu chiffré réservé à OpenAI.
  `gpu_agents` transmet une consigne textuelle normale via Codex CLI.

Réglages utilisateur : `gpu-agents status`,
`gpu-agents config --max-agents 6 --concurrency 2`,
`gpu-agents config --timeout-seconds 1200`.
Après retour de DeepSeek : `gpu-agents config --enable-model deepseek`.
La commande de configuration modifie les futurs groupes ; le plafond de concurrence
et la désactivation d'un modèle s'appliquent aussi aux nouveaux démarrages des groupes existants.
Le connecteur `claude_code` reste disponible pour les demandes de délégation à Claude.
