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
- append le run au Journal Notion et memorise la page dans runs.journal_page_id (voir Journal) ;
- avance next_run au prochain creneau futur (rattrapage en une fois, pas de replay de tous les creneaux manques) ;
- libere le verrou.

Les executors concrets (notification, script, agent) sont ajoutes aux commits 5 a 7 et s'enregistrent via Dispatcher.register. En attendant, un type sans executor produit un run skipped explicite. Les boucles de tick et de sync isolent leurs erreurs pour ne jamais tuer le service.

## Executors

### Notification (zero LLM)

Le module scheduler_mcp/executors/notification.py envoie un message sur un canal. Le payload du job decrit le message :

    {"canal": "email", "destinataire": "x@y.com", "sujet": "Rappel", "message": "..."}

Canaux supportes (scheduler_mcp/executors/channels.py), avec alias tolerants : email / mail / imap, whatsapp / wa, sms / twilio / texto. Champs du payload tolerants aux alias (destinataire/to/recipient/numero, sujet/subject/objet, corps/message/body/texte) ; une cle options est fusionnee dans les arguments de l'outil (par exemple account_id pour l'email).

Chaque canal (interface Channel) traduit le message en ToolCall (serveur MCP + outil + arguments) : email -> imap_send_email, whatsapp -> send_message, sms -> send_sms. L'execution reelle de l'appel est deleguee a un ToolInvoker. La couche outils MCP du fleet est branchee au commit 11 ; en attendant, l'invoker par defaut echoue proprement et le job ressort en run failure (jamais de crash).

### Script (zero LLM)

Le module scheduler_mcp/executors/script.py execute un script deterministe dans un subprocess isole. Le payload decrit la commande :

    {"args": ["python3", "/data/scripts/backup.py"], "timeout": 120}
    {"command": "rsync -a /src /dst", "shell": true, "env": {"KEY": "val"}}

- args (argv en liste, recommande) ou command (chaine ; decoupee par shlex en mode non-shell, ou passee au shell si shell=true). Champs cwd, env, timeout optionnels.
- Isolation : environnement reduit (PATH + env du payload uniquement) pour que le script n'herite pas des secrets du parent ; nouveau groupe de process ; stdin ferme.
- Capture stdout, stderr et code retour dans le detail du run. Code retour 0 -> success, sinon failure. Depassement du delai (SCRIPT_TIMEOUT_SECONDS, surchargeable par le payload) -> le groupe de process est tue, run failure.
- Gate de sensibilite : classify_sensitivity detecte les operations a risque (suppression, acces credentials/Bitwarden, transfert reseau externe, arret machine...). Le compiler (commit 8) s'en servira pour creer en statut a_valider tout script sensible, qui n'atteint l'executor qu'une fois passe en actif. L'executor journalise la sensibilite (audit) et peut la bloquer durement (option block_sensitive).

### Agent (LLM avec outils MCP)

Le module scheduler_mcp/executors/agent.py appelle l'API Anthropic Messages avec le connecteur MCP. Le toolset du job est mappe vers des serveurs MCP du fleet, exposes au modele et executes cote serveur Anthropic. Le payload decrit la tache :

    {"instruction": "Trie ma boite mail et resume les urgents", "system": "Tu es concis", "max_tokens": 4096}

- Le modele utilise est LLM_MODEL (configurable), avec LLM_MAX_TOKENS par defaut (surchargeable par le payload).
- Le toolset du job (multi-select Programmation) est resolu via le registre MCP_SERVERS (JSON nom -> {url, authorization_token?}). Chaque serveur devient une entree mcp_servers + un mcp_toolset, avec le header beta du connecteur MCP. Un serveur absent du registre est ignore avec un avertissement trace.
- Boucle d'outils : la requete est relancee tant que le serveur renvoie stop_reason pause_turn (reprise de la sequence d'outils MCP), avec un plafond d'iterations.
- Trace complete dans runs.detail : texte, appels d'outils (serveur/outil + arguments), resultats, usage et stop_reason. Un refus (refusal) ressort en failure.
- Least privilege / securite : Bitwarden (bw) est exclu du toolset, jamais auto-attribue a un job agent. Sans ANTHROPIC_API_KEY, le client est absent et le job ressort en failure explicite. Aucun secret n'est logge.
- Auth machine MCP (voir ci-dessous) : les serveurs qui n'ont pas leur propre authorization_token recoivent le token machine.

## Auth machine MCP

Decision retenue : connecteur MCP natif de l'API Messages (deja en place dans l'executor agent). Le module scheduler_mcp/auth.py (MachineAuth) fournit le token machine a injecter dans les serveurs MCP du fleet, pour que l'executor s'authentifie sans interaction.

- Token long-lived seede : MCP_AUTH_TOKEN, injecte tel quel dans les serveurs sans token propre. Le secret n'est jamais committe (.env / Bitwarden) ni logge.
- Refresh optionnel via proxy : si MCP_OAUTH_PROXY_URL est defini, le token est recupere puis rafraichi en cache avant expiration (le refresh effectif, cadence ~180j via MCP_AUTH_REFRESH_DAYS, est gere cote mcp-oauth-proxy). Une erreur du proxy n'echoue pas le run.
- Un token defini par serveur dans MCP_SERVERS a la priorite sur le token machine.

Le contrat HTTP du proxy (POST avec le token seede en Bearer, reponse JSON access_token + expiry) est minimal et a confirmer au branchement reel.

## Compiler / registration

Le module scheduler_mcp/compiler.py transforme une demande en langage naturel en job structure (Compiler.compile). C'est le cerveau de la registration : il sera appele par l'interface serveur MCP (commit 10) pour que l'app Claude et l'agent vocal creent des rappels en langage naturel. Le type reste modifiable a posteriori.

Sortie (CompiledJob) : type (notification | script | agent), payload compile, toolset scope, schedule infere (cron ou ISO), classif_reason, statut, et raisons de sensibilite.

- Classification : un appel LLM (Anthropic, modele LLM_MODEL) classe la demande et compile le payload. Un repli heuristique (mots-cles) prend le relais sans cle d'API ou si la reponse est inexploitable (JSON tolerant au texte autour) ; en repli, script et agent passent en a_valider (validation humaine).
- Least privilege : le toolset n'est garde que pour le type agent et restreint aux outils autorises (imap, browser, voicecall, twilio, whatsapp, notion, ssh) ; Bitwarden est toujours retire.
- Gate de sensibilite : un script touchant du sensible (classify_sensitivity, partage avec l'executor script) est cree en statut a_valider, avec les raisons reportees dans classif_reason.

## Journal

Le module scheduler_mcp/journal.py append une entree dans la base Journal Notion apres chaque run (log append-only), et memorise l'id de page dans runs.journal_page_id. La boucle de tick l'appelle juste apres la cloture du run.

- Champs ecrits, noms exacts (accents obligatoires) : Action (titre, « <nom> : <result> »), Détail (texte, detail du run tronque), Source (texte, « scheduler-mcp »), Agent (texte, toujours « Claude (assistant) »), Type (select, type du job).
- Parent de page : data_source_id si NOTION_JOURNAL_DS est renseigne (API 2025-09-03), sinon database_id (NOTION_JOURNAL_DB).
- Best effort : sans NOTION_TOKEN / base Journal le Journal est desactive ; une erreur Notion n'echoue jamais un run (deja cloture cote ledger).

## Interface serveur MCP

Le module scheduler_mcp/mcp_server.py expose trois outils MCP pour que l'app Claude et l'agent vocal creent et pilotent des rappels en langage naturel :

- add_task(description, nom?) : compile la description (Compiler) et inscrit la tache dans le ledger. La tache devient immediatement ordonnancable par la boucle de tick. Sans echeance, elle s'execute au prochain tick (one-shot) ; un script sensible est cree en statut a_valider.
- list_tasks(statut?) : liste compacte des taches (filtre optionnel par statut).
- update_task(task_id, statut?, type?, schedule?) : met a jour une tache (par exemple activer un script a_valider, ou changer l'echeance, ce qui recalcule next_run).

La logique vit dans TaskService (testable contre le ledger) ; un mince wrapper FastMCP l'expose en serveur MCP (import paresseux du paquet mcp). Les taches creees ici utilisent un identifiant interne mcp:<uuid> et coexistent avec les entrees synchronisees depuis Notion. Lancer le serveur :

    python -m scheduler_mcp.mcp_server

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
    python -m tests.test_script
    python -m tests.test_agent
    python -m tests.test_compiler
    python -m tests.test_journal
    python -m tests.test_mcp_server
    python -m tests.test_auth

Suites autonomes (stdlib + aiosqlite ; faux client Notion, faux client Anthropic, faux client HTTP et invoker MCP simules ; test_script lance de vrais subprocess locaux ; aucune dependance reseau). Couvrent WAL, migrations, idempotence, verrou par job, calcul de next_run, mapping tolerant aux accents, write-back, dispatch, anti double-dispatch, borne de concurrence, envoi de notification par canal, execution de script (capture, timeout, isolation env, sensibilite), executor agent (mapping toolset vers mcp_servers, boucle pause_turn, trace, exclusion Bitwarden, injection du token machine), compiler (classification, scope du toolset, gate a_valider, repli heuristique), Journal (champs exacts avec accents, Agent constant, parent de page, resilience), interface serveur MCP (add/list/update, echeance, gate a_valider), et auth machine (token seede, refresh proxy avec mise en cache et expiration).

## Etat

Decoupage du BUILD_BRIEF complet (commits 1 a 11) : scaffold, ledger SQLite, sync Notion vers SQLite, boucle de tick (pool de workers borne, verrou anti double-dispatch, idempotence, rattrapage), les trois executors notification / script / agent, compiler de registration (least privilege, gate a_valider), Journal Notion append-only, interface serveur MCP (add/list/update en langage naturel), et auth machine MCP (token seede + refresh proxy, connecteur natif). Reste a brancher au reel : la couche outils MCP des canaux de notification (commit 5, invoker concret) et la configuration du fleet (URL des serveurs MCP, secrets en .env / Bitwarden).
