# scheduler-mcp

Scheduler self-hosted. Control plane dans Notion (base « Programmation »), execution plane en conteneur Docker avec ledger local SQLite. Execute des taches programmees : notifications, scripts deterministes, et agents LLM avec acces outils MCP du fleet.

## Architecture

- Boucle de tick interne (60s) qui lit les jobs dus dans SQLite et dispatche vers un pool de workers borne. Pas de systemd.
- Sync periodique Notion vers SQLite (la base Programmation est la source declarative).
- Rattrapage des jobs en retard via le ledger. Idempotence par (job, scheduled_for).
- Trois modes d'execution : notification, script, agent (least privilege par toolset).

Plan de build detaille et decoupage en commits dans BUILD_BRIEF.md.

## Ledger SQLite

Le ledger local (module scheduler_mcp/ledger.py) est la source de verite a l'execution : la boucle de tick ne garde aucun etat en memoire. Mode WAL (lecteurs concurrents + un ecrivain), migrations versionnees via PRAGMA user_version. Tous les horodatages sont en ISO 8601 UTC (suffixe Z), longueur fixe, pour que la comparaison de chaines en SQL reste chronologique.

Table jobs (declaratif, alimente par la sync Notion) :

| colonne | role |
| --- | --- |
| id | cle primaire interne |
| notion_page_id | identifiant de la page Programmation (unique) |
| nom, type | titre et mode d'execution (notification, script, agent) |
| schedule | expression cron ou ISO one-shot |
| payload, toolset | JSON serialise (parametres et outils MCP autorises) |
| statut | actif, en pause, a valider, termine |
| next_run, last_run, last_result | etat d'ordonnancement et dernier resultat |
| classif_reason | raison de classification (compiler) |
| lock_owner, lock_expires | verrou par job (anti double-dispatch) |
| created_at, updated_at | horodatages |

Table runs (audit + idempotence) : id, job_id, scheduled_for, started_at, finished_at, result, detail, journal_page_id. La contrainte UNIQUE(job_id, scheduled_for) garantit l'idempotence : un creneau deja execute n'est pas rejoue.

Garanties offertes par la couche d'acces :

- Idempotence : start_run reclame un creneau (job_id, scheduled_for) ; il retourne None si le creneau existe deja.
- Verrou par job : acquire_lock fait un UPDATE conditionnel atomique, un seul worker l'emporte ; le verrou expire (lock_expires) est repris automatiquement.
- Rattrapage : due_jobs selectionne les jobs actifs dont next_run est echue, y compris en retard.

## Configuration

Copier .env.example vers .env et renseigner les valeurs. Aucun secret n'est committe.

## Lancer

    cp .env.example .env
    docker compose up -d --build
    docker compose logs -f

Au demarrage, le service ouvre le ledger (SQLITE_PATH, volume /data), applique les migrations et active WAL avant de lancer les boucles.

## Tests

    python -m tests.test_ledger

Suite autonome (stdlib + aiosqlite) couvrant WAL, migrations, idempotence et verrou par job.

## Etat

Scaffold + ledger SQLite (schema jobs/runs, WAL, migrations, idempotence, verrou par job). Suite des commits selon BUILD_BRIEF.md.
