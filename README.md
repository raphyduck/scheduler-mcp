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

## Sync Notion vers SQLite

Le module scheduler_mcp/notion_sync.py rapatrie periodiquement (NOTION_SYNC_INTERVAL_SECONDS, defaut 300s) la base Programmation et la projette dans le ledger. La base Programmation fait foi pour les champs declaratifs ; le ledger fait foi pour l'etat d'execution, qui est repousse vers Notion pour rester visible.

Cycle (sync_once) :

1. Pull de toutes les pages de la data source Programmation (pagination geree).
2. Projection de chaque page (parse_programmation_page). Les noms de proprietes sont resolus de facon tolerante aux accents et a la casse (echeance/cron, derniere execution, raison de classif), et la cle reelle est memorisee pour le write-back.
3. Calcul de next_run (compute_next_run) :
   - cron (croniter) : prochain creneau strictement apres l'ancre (derniere execution si presente, sinon maintenant). Un creneau manque pendant un downtime reste dans le passe pour etre rattrape, sans rejeu grace a l'idempotence.
   - one-shot ISO : la date cible tant qu'elle n'a pas ete executee, sinon plus de next_run.
4. Upsert dans le ledger. Un one-shot deja execute passe en statut termine (lifecycle).
5. Write-back vers Notion (prochain run, derniere execution, statut), limite aux champs reellement modifies pour eviter le churn d'ecriture.

Mapping des proprietes Programmation vers le ledger : Nom -> nom, type -> type, echeance/cron -> schedule, payload -> payload, toolset -> toolset, statut -> statut, prochain run -> next_run, derniere execution -> last_run, raison de classif -> classif_reason.

L'API Notion est appelee en version NOTION_VERSION (defaut 2025-09-03, endpoints data sources). Sans NOTION_TOKEN, la sync est sautee et le service reste demarrable. Les entrees supprimees cote Notion ne sont pas encore purgees du ledger (a traiter ulterieurement).

## Configuration

Copier .env.example vers .env et renseigner les valeurs. Aucun secret n'est committe.

## Lancer

    cp .env.example .env
    docker compose up -d --build
    docker compose logs -f

Au demarrage, le service ouvre le ledger (SQLITE_PATH, volume /data), applique les migrations et active WAL avant de lancer les boucles.

## Tests

    python -m tests.test_ledger
    python -m tests.test_notion_sync

Suites autonomes (stdlib + aiosqlite, faux client Notion sans reseau) couvrant WAL, migrations, idempotence, verrou par job, calcul de next_run, mapping tolerant aux accents et write-back.

## Etat

Scaffold + ledger SQLite + sync Notion vers SQLite (pull Programmation, calcul next_run via croniter, write-back statut / derniere execution / prochain run). Suite des commits selon BUILD_BRIEF.md.
