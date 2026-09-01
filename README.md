# local-agent

Délégation des tâches volumineuses ou mécaniques à un modèle servi localement par `mlx-serve`, afin de
préserver le contexte et le quota du modèle orchestrateur (Claude Code, Cursor).

Le principe : l'orchestrateur ne transmet **jamais** de contenu de fichier. Il transmet un chemin et une
consigne. Le local-agent découvre les fichiers avec `ripgrep`, sélectionne, découpe, interroge le modèle
local, puis ne renvoie qu'un rapport structuré de quelques centaines de tokens.

## Prérequis

- Python 3.9+ (uniquement la bibliothèque standard, aucune dépendance à installer)
- `ripgrep` (`rg`)
- `git`
- `mlx-serve` en écoute, avec une API compatible OpenAI
- Docker en marche pour les contrôles projet (`phpstan`, `phpunit`, `eslint`…)

## Lancer mlx-serve

Le serveur est fourni par l'application **MLX Core**. Il est actuellement lancé ainsi :

```bash
"/Applications/MLX Core.app/Contents/MacOS/mlx-serve" \
  --model ornith-ai/Ornith-1.5-35B-A3B-MLX-4bit \
  --serve --port 11234 --host 127.0.0.1
```

Vérifier qu'il répond :

```bash
curl -s http://127.0.0.1:11234/v1/models | head -c 200
~/.local-agent/bin/local-agent ping
```

`MLX_MODEL=auto` sélectionne automatiquement le modèle déjà chargé côté serveur, il n'y a donc rien à
changer ici en cas de bascule de modèle.

## Emplacement et périmètre

Tout vit dans `~/.local-agent/`, hors de tout dépôt git : rien n'est versionné et rien n'est partagé avec
une équipe. La racine du dépôt sur lequel travailler est déduite du répertoire courant via
`git rev-parse --show-toplevel`, l'outil fonctionne donc dans n'importe quel projet sans réglage, et
`LOCAL_AGENT_REPO_ROOT` permet de la forcer.

## Utilisation depuis Claude Code ou Cursor

Le serveur MCP est déclaré dans deux fichiers de configuration personnels :

- **Claude Code** : `~/.claude.json`, sous `projects["<chemin du projet>"].mcpServers.local-agent`, avec
  `LOCAL_AGENT_REPO_ROOT` épinglé sur le projet. Pour l'activer sur un autre projet, dupliquer l'entrée
  sous la clé du projet concerné.
- **Cursor** : `~/.cursor/mcp.json`, portée globale. Cursor lance le serveur depuis le répertoire
  personnel et **ne substitue pas** `${workspaceFolder}`, la racine doit donc y être épinglée en dur sur
  un projet. Pour travailler sur un autre dépôt depuis Cursor, passer son chemin absolu dans le paramètre
  `repo` de l'outil appelé, qui prime sur la configuration.

Les règles indiquant *quand* déléguer sont dans `~/.claude/CLAUDE.md` (mémoire utilisateur Claude Code).
Pour Cursor, les recopier dans Settings puis Rules puis User Rules, ou s'appuyer simplement sur les
descriptions des outils MCP qui portent déjà cette guidance.

Redémarrer le client après toute modification de ces fichiers.

| Outil MCP            | Rôle                                                        | Paramètres                                            |
| -------------------- | ----------------------------------------------------------- | ----------------------------------------------------- |
| `local_search`       | Localiser du code à partir d'une question                    | `query`, `path`, `globs`                               |
| `local_analyze`      | Analyse libre, résumé de fichiers, détection de doublons      | `path`, `task`, `mode`, `globs`, `max_files`           |
| `local_review`       | Première passe de revue de code                              | `path`, `task`, `globs`                                |
| `local_fix`          | Modification mécanique écrite sur disque                     | `path`, `task`, `globs`, `dry_run`, `allow_dirty`      |
| `local_test_analysis`| Contrôle projet filtré et synthétisé                         | `kind`, `target`, `filter`                             |
| `local_log_analysis` | Analyse de logs volumineux                                   | `path`, `task`, `patterns`                             |
| `local_ping`         | Diagnostic de connexion, racine résolue et configuration      | aucun                                                  |

Tous les outils acceptent en plus un paramètre optionnel `repo`, chemin absolu du dépôt à analyser, qui
prime sur la racine configurée. `local_ping` affiche la racine effective et signale explicitement une
racine inutilisable.

## Utilisation en ligne de commande

```bash
~/.local-agent/bin/local-agent ping
~/.local-agent/bin/local-agent config

~/.local-agent/bin/local-agent search "où est implémentée l'authentification ?" --path src
~/.local-agent/bin/local-agent review src/Api/Service
~/.local-agent/bin/local-agent summarize src/Workflow --glob '*.php'
~/.local-agent/bin/local-agent duplicates src/Services/ContratTravail --glob '*.php'
~/.local-agent/bin/local-agent inspect src/Admin --task "liste les DataTable sans filtre de structure"

~/.local-agent/bin/local-agent logs var/log/symfony.log
~/.local-agent/bin/local-agent logs var/log --pattern 'CRITICAL' --pattern 'Uncaught'

~/.local-agent/bin/local-agent fix src/Model --task "ajoute les docblocks @return manquants" --dry-run
~/.local-agent/bin/local-agent fix src/Model --task "ajoute les docblocks @return manquants"

~/.local-agent/bin/local-agent check phpstan --target src/Api
~/.local-agent/bin/local-agent check phpunit --target tests/Unit/Services/ContratTravail
~/.local-agent/bin/local-agent check cs-fixer --target src
~/.local-agent/bin/local-agent check eslint
```

`--json` remplace le rendu markdown par le rapport JSON brut.

## Configuration

Par variables d'environnement, ou via un fichier `~/.local-agent/local-agent.env` (copier
`local-agent.env.example`, ignoré par git). L'environnement réel a toujours priorité sur le fichier.

| Variable                        | Défaut                       | Rôle                                                        |
| ------------------------------- | ---------------------------- | ----------------------------------------------------------- |
| `MLX_BASE_URL`                  | `http://127.0.0.1:11234/v1`  | Racine de l'API compatible OpenAI                            |
| `MLX_MODEL`                     | `auto`                       | `auto` prend le modèle déjà chargé                           |
| `MLX_API_KEY`                   | vide                         | Jeton Bearer, uniquement si le serveur en exige un           |
| `MLX_TEMPERATURE`               | `0.2`                        | Température des requêtes                                     |
| `MLX_TIMEOUT`                   | `300`                        | Timeout HTTP par requête, en secondes                        |
| `MLX_MAX_TOKENS`                | `1600`                       | Plafond de génération par requête                            |
| `LOCAL_AGENT_MAX_FILES`         | `40`                         | Fichiers analysés au maximum par appel                       |
| `LOCAL_AGENT_MAX_FILE_SIZE`     | `120000`                     | Octets lus au maximum par fichier                            |
| `LOCAL_AGENT_MAX_OUTPUT_TOKENS` | `900`                        | Plafond du rapport renvoyé à l'orchestrateur                 |
| `LOCAL_AGENT_CHUNK_CHARS`       | `12000`                      | Taille d'un lot envoyé au modèle local, premier levier de latence |
| `LOCAL_AGENT_MAX_CHUNKS`        | `8`                          | Lots au maximum par appel                                    |
| `LOCAL_AGENT_MAX_MATCHES`       | `200`                        | Correspondances ripgrep conservées                           |
| `LOCAL_AGENT_FIX_MAX_FILE_SIZE` | `40000`                      | Au-delà, un fichier n'est pas réécrit                        |
| `LOCAL_AGENT_COMMAND_TIMEOUT`   | `900`                        | Timeout des contrôles projet                                 |
| `LOCAL_AGENT_REPO_ROOT`         | racine du dépôt              | Surcharge de la racine, utile pour les tests                  |

## Garde-fous

- **Confinement** : tout chemin est résolu et refusé s'il sort de la racine du dépôt.
- **Exclusions** : `.git`, `node_modules`, `vendor`, `var`, `temp`, `_db`, builds, caches, fichiers
  binaires. Un répertoire normalement exclu redevient lisible quand le chemin le désigne explicitement,
  ce qui permet d'analyser `var/log` sans ouvrir le reste de `var`.
- **`.gitignore`** : respecté par défaut, contourné seulement pour une cible explicitement ignorée.
- **Secrets** : `.env*`, `*.pem`, `*.key`, clés privées, `*credential*`, `*secret*`, dumps et sauvegardes
  ne sont jamais lus.
- **Écritures** : un fichier modifié mais non committé, ou non suivi par git, n'est jamais réécrit sans
  `allow_dirty`. Aucune commande destructrice n'est accessible, aucun `git reset`, aucune suppression de
  branche, aucun `git add` ni `git commit`.
- **Validation d'écriture** : écriture atomique, puis `php -l` dans le conteneur. Toute réécriture vide,
  identique, amputée de plus de moitié, plus que doublée ou ayant perdu son `<?php` est annulée et le
  contenu d'origine restauré.
- **Commandes** : seule une liste blanche de contrôles en lecture est exposée (`phpstan`, `phpunit`,
  `cs-fixer` en dry-run, `twig`, `yaml`, `eslint`). Aucune cible écrivant en base ni construisant les
  assets n'est atteignable.
- **Sortie bornée** : la lecture de la sortie de `rg` est plafonnée, et le rapport final est tronqué à
  `LOCAL_AGENT_MAX_OUTPUT_TOKENS`.

## Fonctionnement interne

```
local_agent/
├── config.py    variables d'environnement et bornes
├── mlx.py       client HTTP compatible OpenAI, résolution du modèle, nettoyage des blocs de raisonnement
├── files.py     découverte ripgrep, garde-fous, lecture bornée, découpage en lots
├── shell.py     exécution de commandes en liste blanche, état du working tree
├── prompts.py   consignes, contrats de sortie, extraction JSON tolérante aux troncatures
├── tasks.py     search, analyze, logs, check
├── edit.py      réécriture de fichiers sous contrôle git
├── report.py    rapport compact et clamp de sortie
├── cli.py       interface en ligne de commande
└── mcp.py       serveur MCP stdio (JSON-RPC 2.0, sans dépendance)
```

`search` procède en trois temps : le modèle local dérive des expressions ripgrep depuis la question,
`rg` exécute la recherche, puis le modèle ne raisonne que sur les correspondances et de courtes fenêtres
de code autour des fichiers les plus denses. `analyze` fait un map par lot suivi d'une réduction.
`logs` regroupe les lignes par signature normalisée et ne soumet que les signatures dominantes.

## Sonde de bon fonctionnement

```bash
python3 ~/.local-agent/tests/mcp_probe.py          # 7 appels réels, ~30 s
python3 ~/.local-agent/tests/mcp_probe.py --quick  # ping et recherche uniquement
```

La sonde lance le serveur en stdio, joue une série d'appels, vérifie que le confinement des chemins est
appliqué et qu'aucune réponse ne dépasse 4 000 caractères. Code de sortie non nul en cas d'échec.

## Bancs de mesure

```bash
python3 ~/.local-agent/tests/bench.py             # volume économisé par tâche, et latence
python3 ~/.local-agent/tests/bench_exactitude.py --tours 3   # justesse contre vérité terrain
```

Les cas vivent dans `tests/cases.json` et visent ce dépôt même, sans dépendance à un projet
particulier. Pour mesurer sur un dépôt de travail réel, copier ce fichier en `tests/cases.local.json`
(ignoré par git), l'adapter, puis :

```bash
LOCAL_AGENT_BENCH_REPO=/chemin/du/depot python3 tests/bench.py --cases tests/cases.local.json
```

Le banc d'exactitude note par mots clés, ce qui est un proxy : il attrape les échecs francs, pas les
nuances. Ses `interdits` doivent rester des formulations sans équivoque, une phrase trop générale
produisant de faux négatifs.

### Latence

```bash
python3 ~/.local-agent/tests/bench_latence.py --tours 3
```

Un appel qui atteint le modèle coûte 4 à 10 secondes sur un 35B quantifié ; un appel tranché par
l'arbitrage de frugalité coûte moins d'une seconde. La décomposition par phase montre où passe le temps :

| Phase | Part |
|---|---|
| Synthèse par le modèle | 87 % |
| Dérivation des motifs | 12 % |
| ripgrep et extraction des fenêtres | moins de 1 % |

Le travail sur disque est donc gratuit, et **le coût suit la taille de l'entrée, pas celle de la
sortie** : abréger le rapport ne gagne que 0,5 s, tandis que passer le budget d'extraits de 48 000 à
12 000 caractères fait tomber la synthèse de 8,3 s à 6,6 s. C'est le réglage à toucher en premier,
`LOCAL_AGENT_CHUNK_CHARS`, en gardant à l'esprit qu'un budget trop étroit finit par priver le modèle du
contexte qui porte la réponse.

Attention en mesurant : `mlx-serve` garde un cache de prompt, donc rejouer deux fois la même requête
donne un temps trompeusement bas. Ne comparer que des requêtes distinctes.

## Limites connues

- Une analyse de 8 lots prend quelques minutes sur un modèle 35B quantifié : à réserver aux tâches de
  fond, pas aux allers-retours interactifs.
- `search` dépend de la qualité des motifs déduits de la question. Quand le modèle devine des noms de
  symboles qui n'existent pas, un repli lexical prend le relais, mais avec moins de précision : préciser
  un `path` ou des `globs` améliore nettement le résultat.
- **Une question française sur un code aux identifiants anglais peut n'avoir aucun pont lexical.**
  « Quels répertoires les garde-fous refusent-ils » ne mène pas à `DENIED_DIRECTORIES` si le modèle ne
  devine pas le terme anglais. Mesuré : les questions portant un identifiant, un nom de classe ou un
  acronyme répondent juste, les questions purement descriptives échouent une fois sur deux. Nommer le
  symbole quand on le connaît, ou viser un `path` plus étroit.
- `local_fix` réécrit un fichier entier, ce qui le limite aux fichiers de moins de 40 000 caractères et
  aux consignes réellement mécaniques.
- Les contrôles projet passent par `docker compose exec`, donc Docker doit être démarré.
- `php -l` est le seul contrôle syntaxique automatique. Sans Docker, l'écriture est acceptée sans
  vérification de syntaxe.
