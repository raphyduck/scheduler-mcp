# Brief de build : scheduler-mcp

Tache pour Claude Code. Projet sur hobbitton.at sous ~/docker_images/scheduler-mcp, repo GitHub raphyduck/scheduler-mcp.

## Objectif

Service self-hosted qui execute des taches programmees et des rappels (notifications, scripts, agents LLM). Control plane dans Notion (base Programmation), execution plane en conteneur Docker avec ledger local SQLite. Comble l'absence de scheduler natif cote Claude.ai.

## Ressources Notion (deja creees, ne pas recreer)

- Page parent AI brain : 381b975f-3b22-8070-924d-c105a4756e7a
- Base Programmation (control plane) : database 827a4d7a-d5ed-46ef-a0f6-cd2e843838b1, data source c95175e7-d8d8-4127-af64-37d05a1f7ff6
  Champs : Nom (titre), type (select notification|script|agent), echeance/cron (texte), payload (texte), toolset (multi-select imap|browser|voicecall|twilio|whatsapp|notion|ssh), statut (select actif|en pause|a valider|termine), prochain run (date), derniere execution (date), raison de classif (texte)
- Base Journal (log append-only) : 1781b732-9e14-42f7-9c61-ab63e3f8ff0d. Champs exacts (accents obligatoires) : Action (titre), Detail (texte), Source (texte), Agent (texte), Type (select). Agent vaut toujours "Claude (assistant)".

## Stack (decisions prises)

- Python 3.12, asyncio. Conteneur python:3.12-slim.
- anthropic (SDK officiel) pour l'executor agent.
- Acces aux outils via le connecteur MCP de l'API Messages (param mcp_servers + header beta). Fallback : client MCP local. Couche outils pluggable derriere une interface.
- httpx pour l'API Notion REST.
- aiosqlite, mode WAL.
- croniter pour calculer next_run depuis une expression cron.
- Logging structure JSON sur stdout (deja en place, sans dependance).
- Dockerfile + docker-compose, restart: unless-stopped, volume /data pour SQLite.

## Architecture

- Boucle de tick interne, pas de systemd. Toutes les 60s : lire les jobs dus (next_run <= now ET statut actif), dispatcher vers un pool de workers borne (semaphore asyncio = MAX_CONCURRENT_RUNS). La boucle ne garde aucun etat en memoire, la verite est dans SQLite.
- Sync Notion -> SQLite periodique (300s) : upsert des entrees Programmation dans le ledger, calcul de next_run. Write-back du statut et de la derniere execution vers Notion.
- Rattrapage : un job en retard (next_run <= now apres un downtime) est repris au tick suivant. La requete des jobs dus inclut explicitement les jobs en retard. Plus robuste qu'un systemd Persistent car pilote par la donnee.
- Idempotence : cle d'execution (job_id + scheduled_for) en base, un run deja fait n'est pas rejoue.
- Verrou par job (lock_owner + lock_expires) pour eviter le double dispatch.

## Ledger SQLite

Table jobs : id (pk), notion_page_id (unique), nom, type, schedule (cron ou ISO one-shot), payload (JSON), toolset (JSON), statut, next_run, last_run, last_result, classif_reason, lock_owner, lock_expires, created_at, updated_at.
Table runs (audit) : id, job_id, scheduled_for, started_at, finished_at, result, detail, journal_page_id.

## Decoupage en commits (un commit par feature, README a jour a chaque commit)

1. Scaffold (FAIT) : structure repo, requirements, Dockerfile, docker-compose, config via .env, logging JSON, README, skeleton runnable.
2. Ledger SQLite : schema + migrations + couche d'acces + idempotence.
3. Sync Notion -> SQLite : pull Programmation, upsert, calcul next_run (croniter), write-back statut/derniere execution.
4. Boucle de tick : requete jobs dus (incl. retard), pool de workers borne, verrou anti double-dispatch.
5. Executor notification : envoi message sur canal (email imap-mcp, WhatsApp, SMS twilio). Interface de canal.
6. Executor script : subprocess isole, capture stdout/stderr + code retour. Gate a_valider si le script touche du sensible (suppression, credentials, envoi externe).
7. Executor agent : appel Anthropic Messages avec mcp_servers = toolset du job, boucle d'outils, trace complete dans runs.detail.
8. Compiler / registration : depuis une entree en langage naturel, classer le type, compiler le payload, scoper le toolset (least privilege), ecrire classif_reason. Type modifiable a posteriori. Script sensible cree en statut a_valider.
9. Journal : apres chaque run, append dans la base Journal (Agent "Claude (assistant)", champs exacts avec accents).
10. (Optionnel) Interface serveur MCP : exposer add/list/update de taches pour que l'app Claude et l'agent vocal creent des rappels en langage naturel.
11. Auth machine MCP : seeding d'un token long-lived pour que l'executor s'authentifie au fleet sans interaction (refresh 180j cote mcp-oauth-proxy). Confirmer : connecteur MCP natif de l'API vs client MCP local.

## Securite / gardes

- Bitwarden MCP exclu du toolset par defaut, jamais auto-attribue a un job agent.
- Script touchant du sensible : statut a_valider avant d'etre actif.
- Secrets uniquement en .env / Bitwarden, jamais en base ni dans un payload Notion (references d'emplacement seulement).
- Least privilege : un job n'a que les outils MCP listes dans son toolset.

## Criteres d'acceptation

- docker compose up demarre le service, restart: unless-stopped verifie.
- Un job cron de test (type notification) se declenche a l'heure et ecrit une entree Journal.
- Couper et relancer le conteneur ne perd ni ne rejoue un job (idempotence + rattrapage verifies).
- Un job type agent appelle un outil MCP du fleet et trace l'appel dans runs.
- README documente le deploiement, la config, le schema de la base Programmation.

## Conventions

- Discovery-first. Un commit par feature, messages clairs. README a jour a chaque commit. Pas de em dash dans le code, la doc ou les commits.
