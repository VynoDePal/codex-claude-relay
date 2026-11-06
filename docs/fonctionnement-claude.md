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

## Diagnostic

`touch ~/.codex/claude-selector/dump-next-request` enregistre la requête suivante dans `~/.codex/claude-selector/last-request.json` (mode 0600). Supprimez ce fichier après analyse : il contient toute la conversation.

## Limites

- Le premier contexte d’un chat Claude reste lourd (instructions et compétences de Codex) ; une estimation donne environ 115 000 tokens pour un chat neuf.
- Les copies de la liste des compétences que Codex renvoie diffèrent de quelques lignes : elles ne sont pas dédoublonnées.
- Claude Code facture les tokens sur l’abonnement Claude ; la limite de session Anthropic peut interrompre un tour.
- Les tâches longues en effort xhigh génèrent beaucoup de réflexion : préférez high pour le travail courant.

