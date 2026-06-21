"""Interface serveur MCP (BUILD_BRIEF.md commit 10, optionnel).

Expose add / list / update de taches pour que l'app Claude et l'agent vocal
creent et pilotent des rappels en langage naturel. La logique vit dans
TaskService (testable contre le ledger) ; un mince wrapper FastMCP l'expose comme
serveur MCP (import paresseux du paquet mcp).

add_task compile l'entree en langage naturel (Compiler, commit 8) puis l'inscrit
dans le ledger : la tache devient immediatement ordonnancable par la boucle de
tick. Un script sensible est cree en statut a_valider et n'est execute qu'apres
passage en actif via update_task.
"""

import uuid
from typing import Any, Optional

from .compiler import Compiler
from .config import Config, load_config
from .executors.agent import build_default_client
from .ledger import Ledger, now_iso
from .logging_conf import get_logger, setup_logging
from .notion_sync import compute_next_run

log = get_logger("scheduler_mcp.mcp_server")

# Statuts acceptes par update_task (la forme interne 'a_valider' et la forme Notion).
_VALID_STATUTS = {"actif", "en pause", "a_valider", "a valider", "termine"}


def _derive_nom(text: str) -> str:
    first = text.strip().splitlines()[0] if text.strip() else "tache"
    return first[:80]


def _summary(job: dict) -> dict:
    """Vue compacte d'un job pour les reponses MCP (sans dumper le payload)."""
    return {
        "id": job["id"],
        "nom": job["nom"],
        "type": job["type"],
        "statut": job["statut"],
        "schedule": job.get("schedule"),
        "next_run": job.get("next_run"),
        "last_run": job.get("last_run"),
        "last_result": job.get("last_result"),
        "toolset": job.get("toolset") or [],
        "classif_reason": job.get("classif_reason"),
    }


class TaskService:
    """Coeur des operations add / list / update, adosse au ledger."""

    def __init__(
        self, cfg: Config, ledger: Optional[Ledger] = None, compiler: Optional[Compiler] = None
    ) -> None:
        self._cfg = cfg
        self._ledger = ledger
        self._compiler = compiler

    async def _ensure(self) -> None:
        # Connexion paresseuse (dans la boucle du serveur MCP) ; les tests injectent.
        if self._ledger is None:
            self._ledger = await Ledger(self._cfg.sqlite_path).connect()
        if self._compiler is None:
            self._compiler = Compiler(self._cfg, client=build_default_client(self._cfg))

    async def add_task(self, description: str, nom: Optional[str] = None) -> dict:
        await self._ensure()
        compiled = await self._compiler.compile(description)
        now = now_iso()
        # Sans echeance, la tache est un one-shot a executer au prochain tick.
        next_run = compute_next_run(compiled.schedule, None, now) if compiled.schedule else now
        page_id = "mcp:" + uuid.uuid4().hex
        job_id = await self._ledger.upsert_job(
            notion_page_id=page_id,
            nom=nom or _derive_nom(description),
            type=compiled.type,
            schedule=compiled.schedule,
            payload=compiled.payload,
            toolset=compiled.toolset,
            statut=compiled.statut,
            classif_reason=compiled.classif_reason,
            next_run=next_run,
        )
        log.info("mcp.add_task", job=job_id, type=compiled.type, statut=compiled.statut)
        result = _summary(await self._ledger.get_job(job_id))
        result["sensitive_reasons"] = compiled.sensitive_reasons
        return result

    async def list_tasks(self, statut: Optional[str] = None) -> list[dict]:
        await self._ensure()
        jobs = await self._ledger.list_jobs()
        if statut:
            jobs = [j for j in jobs if j["statut"] == statut]
        return [_summary(j) for j in jobs]

    async def update_task(
        self,
        task_id: int,
        statut: Optional[str] = None,
        type: Optional[str] = None,
        schedule: Optional[str] = None,
        description: Optional[str] = None,
    ) -> dict:
        await self._ensure()
        job = await self._ledger.get_job(task_id)
        if job is None:
            raise ValueError(f"tache introuvable : {task_id}")

        changes: dict[str, Any] = {}
        if statut is not None:
            if statut not in _VALID_STATUTS:
                raise ValueError(f"statut invalide : {statut!r}")
            changes["statut"] = statut
        if type is not None:
            changes["type"] = type
        if description is not None:
            changes["payload"] = {"command": description}
        if schedule is not None:
            changes["schedule"] = schedule or None
            # Recalcule l'echeance a partir du nouveau schedule (ancre sur last_run).
            changes["next_run"] = (
                compute_next_run(schedule, job.get("last_run"), now_iso()) if schedule else None
            )

        await self._ledger.update_job(task_id, **changes)
        log.info("mcp.update_task", job=task_id, champs=sorted(changes))
        return _summary(await self._ledger.get_job(task_id))


def create_mcp_app(service: TaskService):
    """Construit l'application FastMCP exposant les trois outils. Import paresseux."""
    from mcp.server.fastmcp import FastMCP

    app = FastMCP("scheduler-mcp")

    @app.tool()
    async def add_task(description: str, nom: Optional[str] = None) -> dict:
        """Cree une tache programmee depuis une description en langage naturel.

        Le type (notification, script ou agent) et le payload sont compiles
        automatiquement ; une echeance (cron ou date) est inferee si presente.
        """
        return await service.add_task(description, nom=nom)

    @app.tool()
    async def list_tasks(statut: Optional[str] = None) -> list:
        """Liste les taches programmees, filtre optionnel par statut."""
        return await service.list_tasks(statut=statut)

    @app.tool()
    async def update_task(
        task_id: int,
        statut: Optional[str] = None,
        type: Optional[str] = None,
        schedule: Optional[str] = None,
        description: Optional[str] = None,
    ) -> dict:
        """Met a jour une tache : statut (en pause / actif / a_valider / termine), type, echeance (cron/date) ou description (reecrit les instructions de la tache)."""
        return await service.update_task(task_id, statut=statut, type=type, schedule=schedule, description=description)

    return app


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level)
    log.info("mcp_server.start", sqlite_path=cfg.sqlite_path)
    app = create_mcp_app(TaskService(cfg))
    app.run()  # transport stdio par defaut


if __name__ == "__main__":
    main()
