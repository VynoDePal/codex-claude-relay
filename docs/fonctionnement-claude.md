# Fonctionnement détaillé de Claude dans Codex

Chaque section décrit un problème réellement rencontré et la façon dont le relais le traite.

## Modèle et efforts

- Slug `claude-code-opus`, affiché « Claude », appelle `claude-opus-5-5`.
- Efforts proposés : low, medium, high, xhigh, max. `ultra` n’existe pas pour Claude.
- Fenêtre annoncée à Codex : 1 000 000 tokens.

## Comptage des tokens et compaction

- Claude Code renvoie l’usage cumulé de tous ses appels internes d’un tour. Le relais signale à Codex l’usage du **dernier** appel, qui correspond à la taille réelle du contexte. Sans ça, un tour à deux appels semblait deux fois plus gros et déclenchait une compaction toutes les quelques minutes.
- Quand Codex demande une compaction, Claude rédige un résumé. Le relais le chiffre avec une clé locale (préfixe `claude-code-compaction-v1:`) et le relit aux tours suivants.
- Si la conversation repasse ensuite à un modèle OpenAI ou GPU, le routeur remplace ce résumé chiffré par son texte lisible ; OpenAI ne pourrait pas le déchiffrer.

## Passer d’un modèle OpenAI à Claude

Une compaction OpenAI est chiffrée et illisible pour Claude. Le relais relit donc l’historique local du chat (`~/.codex/sessions`, y compris les pages `.jsonl.zst`) jusqu’à ce point, en excluant le raisonnement chiffré.

- L’historique est écrit en flux dans une archive privée ; la mémoire ne dépend pas de la taille du chat.
- Limite de lecture : 4 Go décompressés (un chat réel de 35 pages fait 1,68 Go et se récupère en 7,6 s, 150 Mo de mémoire).
- Claude reçoit les 360 000 derniers caractères visibles et le chemin de l’archive complète.
- Les lignes coupées par une écriture interrompue sont ignorées et signalées.

## Historique Codex

- `additional_tools` (outils ajoutés en cours de conversation) redevient des outils appelables.
- Un type d’élément inconnu, apparu avec une mise à jour de Codex, est transmis comme texte historique (8 000 caractères au plus) au lieu de faire échouer le tour. Il est signalé dans le journal du routeur et par `codex-relays-check`.
- Les sorties d’outils sans appel associé (messages d’autres chats) sont conservées avec leur source.

## Catalogue d’outils

En mode code, la description de l’outil `exec` contient la déclaration complète des 666 outils des connecteurs, soit environ 780 000 caractères. Le relais garde en entier les outils de base et les serveurs `codex_app`, `claude_code`, `gpu_agents`, `node_repl`. Les autres sont réduits à une ligne marquée `[abbreviated]`, et une note indique à Claude de lire la déclaration complète via `ALL_TOOLS` dans `exec` avant l’appel. Le résultat est déterministe, pour garder le cache.

Mesure : une requête du même chat est passée de 859 718 à 628 670 tokens. Un test sur 5 demandes (Gmail, Agenda, Drive, Supabase, Netlify) a choisi le bon outil à chaque fois.

## Blocs répétés

Un long bloc d’instructions (4 000 caractères ou plus) strictement identique à un bloc déjà envoyé est remplacé par un renvoi à la première copie.

## Navigateur intégré et Google Chrome

Quand l’outil `mcp__cua_repl.js` est disponible, Claude reçoit un mode d’emploi : ouverture d’un onglet `iab` ou `chrome`, reprise d’un onglet existant, actions groupées terminées par `getAXState()`, lecture avec les sélecteurs Playwright, défilement animé, plan B au clavier.

- La documentation du navigateur déjà envoyée est remplacée par un renvoi.
- Un arbre d’accessibilité dépassé par un arbre plus récent du même onglet est raccourci, par paquets de 5 pour préserver le cache.
- Un arbre ne dépasse jamais 30 000 caractères : lignes coupées à 300 caractères, début et fin gardés, note avec les numéros d’éléments retirés.
- Gemini, qui ne voit pas les images, reçoit une version sans captures d’écran.

## Images

- 30 images au plus par requête ; les plus anciennes sont remplacées par paquets de 10.
- Les captures sont réduites à 1 568 px et 1,15 Mpx. Une capture réduite indique le facteur de conversion des coordonnées, par exemple « multiply x by 1.2245 », pour que les clics visés sur l’image tombent juste.

## Outils internes de Claude Code

Claude Code tourne sans outils internes. S’il tente quand même Bash ou Read, puis termine sans appel Codex en croyant avoir perdu ses outils, le relais le note dans le journal et relance une fois la demande avec une note corrective.

## Sous-agents

Codex peut lancer des sous-agents avec les outils `spawn_agent`, `send_message`, `followup_task`, `wait_agent`, `interrupt_agent` et `list_agents`. Codex Desktop remet chaque consigne au destinataire dans un message entre agents (`agent_message`), dans un champ prévu pour du contenu chiffré (`encrypted_content`). Ce que contient ce champ dépend du modèle qui a écrit la consigne :

- **un modèle OpenAI** rend ses arguments déjà chiffrés. Le champ contient un jeton OpenAI (`gAAAAA…`) que seuls les serveurs OpenAI savent lire. Aucune copie lisible n’existe sur la machine, pas même dans l’historique du parent ;
- **Claude** rend ses arguments en clair. Le champ contient alors le texte du message tel quel.

[relay/claude_subagents.py](../relay/claude_subagents.py) traite ces deux cas :

- **vers Claude**, un texte en clair devient du texte ordinaire. Un jeton OpenAI est refusé s’il porte une tâche (`NEW_TASK`), avec un message qui propose une solution. S’il porte un message intermédiaire (`MESSAGE`), il est remplacé par une note « message illisible », pour que les tours suivants de l’agent ne restent pas bloqués ;
- **vers OpenAI**, le routeur transforme aussi le texte en clair en texte ordinaire. Sans ça, OpenAI refuse la requête avec « The encrypted content … could not be verified ». Les vrais jetons sont transmis sans changement.

Claude voit chaque message entre agents comme venant d’un autre agent, qui ne parle pas au nom de l’utilisateur et ne peut accorder aucune autorisation. Quand les outils de sous-agents sont disponibles, Claude reçoit aussi un mode d’emploi : lancer des sous-agents seulement si c’est demandé, écrire des tâches autonomes (`fork_turns: "none"` avec le contexte utile), donner des fichiers distincts aux enfants qui travaillent en parallèle, attendre avec `wait_agent` et de longs délais.

### Combinaisons parent / enfant

| Parent | Enfant | Résultat |
|---|---|---|
| Claude | Claude | Fonctionne : tâche, `send_message`, `followup_task`, `interrupt_agent`, `list_agents` |
| Claude | OpenAI | Fonctionne |
| OpenAI | OpenAI | Fonctionne, sans intervention du relais |
| OpenAI | Claude | Impossible : l’enfant renvoie une erreur qui propose de le relancer avec un modèle OpenAI ou de passer par le connecteur `claude_code` |

Les réponses finales des enfants (`FINAL_ANSWER`) arrivent toujours en clair, quel que soit le modèle. Un message intermédiaire (`send_message`) d’un enfant OpenAI vers un parent Claude reste illisible : le parent voit une note et s’appuie sur la réponse finale.

### Requêtes Claude simultanées

Le routeur limite le nombre de requêtes Claude traitées en même temps, sous-agents compris. La valeur par défaut est 3. La variable `CODEX_CLAUDE_CONCURRENCY` la règle de 1 à 8 ; une valeur qui n’est pas un nombre revient à 3, et une valeur hors limites est ramenée à 1 ou 8. Les requêtes en surplus attendent leur tour. Un parent qui attend avec `wait_agent` n’occupe pas de place, car sa requête est terminée. Pour la définir, voir [Installation](installation.md#adapter-à-une-autre-machine).

Codex a sa propre limite d’agents actifs. Au-delà, `spawn_agent` échoue avec `agent thread limit reached` : le parent doit attendre la fin d’un enfant avant d’en relancer un.

### Tests réels

Le 10/11/2026, avec le client livré avec Codex Desktop (0.167) et `multi_agent_v2` :

- 3 enfants Claude lancés ensemble ont démarré à 70 ms d’écart et rendu les bons codes.
- Sur 4 enfants Claude, Codex a refusé le 4ᵉ (`agent thread limit reached`). Le parent l’a relancé après la fin d’un autre. Il n’y a jamais eu plus de 3 processus Claude.
- Un parent Claude a lancé deux enfants Claude qui ont écrit chacun leur fichier, a appelé `list_agents`, puis a interrompu un troisième enfant et lui a confié une nouvelle tâche avec `followup_task`. Les fichiers étaient corrects et l’enfant interrompu a traité la nouvelle tâche.
- Un parent Claude avec un enfant `gpt-6-astra` ou `gpt-6.1-sol` : `spawn_agent` et `followup_task` ont rendu les bons codes. Le `send_message` de l’enfant est devenu une note.
- Un parent `gpt-6.1-sol` avec un enfant Claude : l’erreur attendue, avec les solutions.

### Commandes arrêtées après `interrupt_agent`

Codex arrête le tour d’un agent interrompu, mais laisse tourner les commandes shell que ce tour a lancées. Pour un sous-agent servi par le relais (Claude ou Gemini), le routeur les arrête lui-même ([relay/claude_interrupts.py](../relay/claude_interrupts.py)) :

- toutes les 2 secondes, il lit les nouvelles lignes des historiques de `~/.codex/sessions` et repère l’événement `turn_aborted` (raison `interrupted`) d’un sous-agent ;
- il cherche les processus qui portent la variable `CODEX_THREAD_ID` de ce sous-agent et qui ont démarré avant l’interruption, ainsi que leurs descendants ;
- il leur envoie SIGTERM, puis SIGKILL après 2 secondes s’ils tournent encore. Une commande lancée après l’interruption, par exemple pour un `followup_task`, n’est pas touchée. Les processus de Codex lui-même (`app-server`, `codex-code-mode-host`) sont exclus ;
- l’historique déjà présent au démarrage du routeur n’est pas relu.

Les commandes d’un sous-agent OpenAI continuent de tourner après `interrupt_agent` : le relais ne sert pas ces agents et n’y touche pas. `CODEX_INTERRUPT_STOPS_COMMANDS=0` désactive ce mécanisme.

Test réel du 10/11 : un enfant Claude lancé sur `sleep 77`, puis interrompu après 25 s. Sans le mécanisme, la commande continuait après l’interruption ; avec, elle a disparu juste après (5 processus arrêtés : l’enveloppe de la sandbox Codex, le shell et `sleep`). Dans le scénario complet, l’enfant interrompu a ensuite traité son `followup_task` et rendu `ALPHA-1`.

## Diagnostic

`touch ~/.codex/claude-selector/dump-next-request` enregistre la requête suivante dans `~/.codex/claude-selector/last-request.json` (mode 0600). Supprimez ce fichier après analyse : il contient toute la conversation.

## Limites

- Le premier contexte d’un chat Claude reste lourd (instructions et compétences de Codex) ; une estimation donne environ 115 000 tokens pour un chat neuf.
- Les copies de la liste des compétences que Codex renvoie diffèrent de quelques lignes : elles ne sont pas dédoublonnées.
- Claude Code facture les tokens sur l’abonnement Claude ; la limite de session Anthropic peut interrompre un tour.
- Les tâches longues en effort xhigh génèrent beaucoup de réflexion : préférez high pour le travail courant.

