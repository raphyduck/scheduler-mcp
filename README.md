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

## Boucle de tick

Le module scheduler_mcp/tick.py declenche les jobs. Toutes les TICK_INTERVAL_SECONDS (defaut 60s), un tick (tick_once) :

1. Lit les jobs dus via due_jobs (statut actif, next_run echue, y compris en retard).
2. Pour chaque job, tente le verrou (acquire_lock, TTL LOCK_TTL_SECONDS, defaut 900s). Un job deja verrouille est saute : pas de double dispatch, meme si un tick se chevauche ou apres un redemarrage.
3. Lance un worker par job verrouille, borne par un semaphore (MAX_CONCURRENT_RUNS).

Chaque worker (_run_job) :

- reclame le creneau (start_run) ; si le creneau a deja un run, il n'est pas rejoue (idempotence) ;
- dispatche vers l'executor du type de job (Dispatcher) ; une exception de l'executor devient un run failure, jamais un crash ;
- cloture le run (finish_run, report last_run / last_result sur le job) ;
- avance next_run au prochain creneau futur (rattrapage en une fois, pas de replay de tous les creneaux manques) ;
- libere le verrou.

Les executors concrets (notification, script, agent) sont ajoutes aux commits 5 a 7 et s'enregistrent via Dispatcher.register. En attendant, un type sans executor produit un run skipped explicite. Les boucles de tick et de sync isolent leurs erreurs pour ne jamais tuer le service.

## Executors

### Notification (zero LLM)

Le module scheduler_mcp/executors/notification.py envoie un message sur un canal. Le payload du job decrit le message :

    {"canal": "email", "destinataire": "x@y.com", "sujet": "Rappel", "message": "..."}

Canaux supportes (scheduler_mcp/executors/channels.py), avec alias tolerants : email / mail / imap, whatsapp / wa, sms / twilio / texto. Champs du payload tolerants aux alias (destinataire/to/recipient/numero, sujet/subject/objet, corps/message/body/texte) ; une cle options est fusionnee dans les arguments de l'outil (par exemple account_id pour l'email).

Chaque canal (interface Channel) traduit le message en ToolCall (serveur MCP + outil + arguments) : email -> imap_send_email, whatsapp -> send_message, sms -> send_sms. L'execution reelle de l'appel est deleguee a un ToolInvoker. La couche outils MCP du fleet est branchee au commit 11 ; en attendant, l'invoker par defaut echoue proprement et le job ressort en run failure (jamais de crash).

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
    python -m tests.test_tick
    python -m tests.test_notification

Suites autonomes (stdlib + aiosqlite, faux client Notion, executors et invoker MCP simules, sans reseau) couvrant WAL, migrations, idempotence, verrou par job, calcul de next_run, mapping tolerant aux accents, write-back, dispatch, anti double-dispatch, borne de concurrence et envoi de notification par canal.

## Etat

Scaffold + ledger SQLite + sync Notion vers SQLite + boucle de tick (pool de workers borne, verrou anti double-dispatch, idempotence, rattrapage) + executor notification (interface de canal email / WhatsApp / SMS). Restent les executors script et agent, le compiler, le Journal et la couche outils MCP selon BUILD_BRIEF.md.
