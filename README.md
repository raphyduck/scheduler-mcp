# scheduler-mcp

Scheduler self-hosted. Control plane dans Notion (base « Programmation »), execution plane en conteneur Docker avec ledger local SQLite. Execute des taches programmees : notifications, scripts deterministes, et agents LLM avec acces outils MCP du fleet.

## Architecture

- Boucle de tick interne (60s) qui lit les jobs dus dans SQLite et dispatche vers un pool de workers borne. Pas de systemd.
- Sync periodique Notion vers SQLite (la base Programmation est la source declarative).
- Rattrapage des jobs en retard via le ledger. Idempotence par (job, scheduled_for).
- Trois modes d'execution : notification, script, agent (least privilege par toolset).

Plan de build detaille et decoupage en commits dans BUILD_BRIEF.md.

## Configuration

Copier .env.example vers .env et renseigner les valeurs. Aucun secret n'est committe.

## Lancer

    cp .env.example .env
    docker compose up -d --build
    docker compose logs -f

## Etat

Scaffold initial runnable. Implementation commit par commit selon BUILD_BRIEF.md.
